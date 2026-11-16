#!/usr/bin/env python3
"""Hitung elevasi, lereng, tinggi kanopi, dan karakteristik hutan titik camera trap, lalu tulis ke Google Sheets.

Alur:
  1. Baca sheet koordinat dari Google Sheets yang dibuka untuk umum (tanpa kredensial).
  2. Dari DEM (Assets/DEM_*.tif): elevasi di titik, lereng di titik, dan rata-rata lereng dalam buffer.
  3. Dari raster tinggi kanopi (Assets/CHM_*.tif, opsional): tinggi kanopi di titik dan rata-rata dalam buffer.
  4. Dari raster hutan (Assets/Forest*.tif, opsional): apakah titik di dalam hutan, jarak ke tepi hutan,
     dan persen hutan dalam beberapa buffer. Piksel bernilai FOREST_VALUES = hutan; piksel lain
     (termasuk nodata) di dalam raster = non-hutan; di luar raster = tidak diketahui.
  5. Kirim hasilnya (tanpa koordinat) ke web app Apps Script (apps_script/KarakteristikLokasi.gs),
     yang menulisnya ke sheet "type". Tidak ada file hasil yang disimpan di repo.

Konfigurasi lewat environment variable (lihat README.md).
"""
import csv
import glob
import io
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.warp import transform
from rasterio.windows import Window

EKSPOR_CSV = "https://docs.google.com/spreadsheets/d/{id}/export"
OPEN_METEO = "https://api.open-meteo.com/v1/elevation"
JUDUL_CSV = ["Kunci", "CameraID", "Site", "Elevasi_m", "Slope_deg", "Slope_buffer_deg", "Sumber_Elevasi"]
JUDUL_KANOPI = ["Kanopi_m", "Kanopi_buffer_m", "Sumber_Kanopi"]


def judul_hutan(radius_list):
    return (["Hutan_titik", "Tepi_hutan_m"] + [f"Hutan_{r:g}m_persen" for r in radius_list]
            + ["Hutan_catatan", "Sumber_Hutan"])


# ---------------------------------------------------------------- utilitas
def env(nama, bawaan=None, wajib=False):
    nilai = os.environ.get(nama, "").strip()
    if not nilai:
        if wajib:
            sys.exit(f"::error::Environment variable {nama} belum diisi.")
        return bawaan
    return nilai


def ke_angka(v):
    """Ubah isi sel menjadi float; None jika kosong/bukan angka."""
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def cari_kolom(judul, nama):
    """Indeks kolom (mulai 0) dengan judul `nama` (tidak peka huruf besar/kecil), atau None."""
    target = nama.strip().lower()
    for i, j in enumerate(judul):
        if str(j).strip().lower() == target:
            return i
    return None


# ---------------------------------------------------------------- baca sheet
def unduh_sheet(sheet_id, gid):
    """Isi sheet sebagai list of list, lewat tautan ekspor CSV (sheet harus terbuka untuk umum)."""
    r = requests.get(EKSPOR_CSV.format(id=sheet_id), params={"format": "csv", "gid": gid}, timeout=60)
    teks = r.content.decode("utf-8-sig", errors="replace")
    if r.status_code != 200 or "text/csv" not in r.headers.get("content-type", "") \
            or teks.lstrip().lower().startswith(("<!doctype", "<html")):
        sys.exit(f"::error::Sheet tidak bisa dibaca (HTTP {r.status_code}). Pastikan spreadsheet dibagikan "
                 "sebagai 'Anyone with the link - Viewer', dan SHEET_ID serta SHEET_GID benar.")
    return list(csv.reader(io.StringIO(teks)))


def baca_titik(baris):
    """Satu titik per baris data yang koordinatnya valid, urut sesuai sheet."""
    if not baris:
        sys.exit("::error::Sheet kosong.")
    judul = baris[0]
    i_lat, i_lon = cari_kolom(judul, "Latitude"), cari_kolom(judul, "Longitude")
    if i_lat is None or i_lon is None:
        sys.exit("::error::Kolom 'Latitude' dan/atau 'Longitude' tidak ditemukan di baris judul.")
    i_cam, i_site = cari_kolom(judul, "CameraID"), cari_kolom(judul, "Site")
    if i_cam is None or i_site is None:
        sys.exit("::error::Kolom 'CameraID' dan/atau 'Site' tidak ditemukan di baris judul.")

    def sel(b, i):
        return b[i].strip() if len(b) > i else ""

    titik, dilewati = [], 0
    for b in baris[1:]:
        lat, lon = ke_angka(sel(b, i_lat)), ke_angka(sel(b, i_lon))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            dilewati += any(c.strip() for c in b)
            continue
        titik.append({"cam": sel(b, i_cam), "site": sel(b, i_site), "lat": lat, "lon": lon,
                      "elevasi": None, "slope": None, "slope_buffer": None, "sumber": "",
                      "kanopi": None, "kanopi_buffer": None, "sumber_kanopi": "",
                      "hutan": None, "tepi": None, "hutan_persen": {}, "hutan_catatan": "", "sumber_hutan": ""})
    return titik, dilewati


# ---------------------------------------------------------------- baca raster
def buka_raster(pola):
    """Buka raster dari pola file lokal (mis. Assets/DEM_*.tif, urut nama) atau daftar URL https
    dipisah koma. URL dibaca langsung tanpa diunduh utuh (Cloud Optimized GeoTIFF)."""
    if pola.startswith(("http://", "https://")):
        return [rasterio.open(u.strip()) for u in pola.split(",") if u.strip()]
    berkas = sorted(glob.glob(pola, recursive=True))
    return [rasterio.open(b) for b in berkas]


def baca_jendela(src, lat, lon, radius_m, kosong_nan=True):
    """Potongan raster di sekitar titik.

    Mengembalikan (z, jarak, n, dx, dy) atau None jika titik di luar raster:
      z      array float, NaN untuk piksel di luar raster (dan piksel kosong/nodata jika `kosong_nan`);
             titik berada di z[n, n]
      jarak  jarak pusat tiap piksel ke titik, dalam meter
      dx,dy  ukuran piksel dalam meter
    """
    x, y = lon, lat
    if src.crs and src.crs.to_epsg() != 4326:
        xs, ys = transform("EPSG:4326", src.crs, [lon], [lat])
        x, y = xs[0], ys[0]
    b = src.bounds
    if not (b.left <= x < b.right and b.bottom < y <= b.top):
        return None
    rx, ry = src.res
    if src.crs and src.crs.is_geographic:          # derajat -> meter
        dx = rx * 111320.0 * math.cos(math.radians(lat))
        dy = ry * 110574.0
    elif src.crs and src.crs.to_epsg() == 3857:    # Web Mercator: koreksi skala lintang
        dx, dy = rx * math.cos(math.radians(lat)), ry * math.cos(math.radians(lat))
    else:                                          # CRS terproyeksi lain: satuan sudah meter
        dx, dy = rx, ry
    n = int(math.ceil(radius_m / min(dx, dy))) + 2  # piksel ke tiap sisi (+ tepi untuk jendela 3x3)
    baris, kolom = src.index(x, y)
    sisi = 2 * n + 1
    z = np.full((sisi, sisi), np.nan)
    r0, c0 = baris - n, kolom - n
    r1, r2 = max(r0, 0), min(r0 + sisi, src.height)
    c1, c2 = max(c0, 0), min(c0 + sisi, src.width)
    if r1 < r2 and c1 < c2:
        jendela = Window(c1, r1, c2 - c1, r2 - r1)
        data = src.read(1, window=jendela).astype("float64")
        if kosong_nan:
            data[src.read_masks(1, window=jendela) == 0] = np.nan   # mask band (mis. awan pada CHMv2)
            if src.nodata is not None:
                data[data == src.nodata] = np.nan
            data[data < -1e4] = np.nan             # penanda kosong yang tidak tercatat di metadata
        z[r1 - r0:r2 - r0, c1 - c0:c2 - c0] = data
    px, py = src.xy(baris, kolom)                  # pusat piksel yang memuat titik
    timur = ((np.arange(sisi) - n) * rx + (px - x)) * (dx / rx)
    utara = ((n - np.arange(sisi)) * ry + (py - y)) * (dy / ry)
    jarak = np.hypot(timur[None, :], utara[:, None])
    return z, jarak, n, dx, dy


def lereng(z, dx, dy):
    """Kemiringan lereng (derajat) dengan metode Horn (jendela 3x3), seperti QGIS/GDAL.
    Piksel tepi dan piksel yang tetangganya kosong bernilai NaN."""
    s = np.full(z.shape, np.nan)
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy)
    s[1:-1, 1:-1] = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    return s


def rata_buffer(nilai, jarak, radius_m):
    """Rata-rata piksel yang pusatnya berada dalam radius; None jika semuanya kosong."""
    dalam = nilai[(jarak <= radius_m) & ~np.isnan(nilai)]
    return round(float(dalam.mean()), 1) if dalam.size else None


# ---------------------------------------------------------------- ambil nilai
def isi_dari_dem(titik, sumber, radius_m, nol_kosong):
    """Elevasi di titik, lereng di titik, dan rata-rata lereng dalam buffer.
    Jika `nol_kosong`, piksel bernilai tepat 0 dianggap kosong (isian di luar area proyek)."""
    for t in titik:
        for src in sumber:
            hasil = baca_jendela(src, t["lat"], t["lon"], radius_m)
            if hasil is None:
                continue
            z, jarak, n, dx, dy = hasil
            if nol_kosong:
                z[z == 0] = np.nan
            if np.isnan(z[n, n]):
                continue                          # piksel kosong: coba file lain
            s = lereng(z, dx, dy)
            t["elevasi"] = round(float(z[n, n]), 1)
            t["slope"] = None if np.isnan(s[n, n]) else round(float(s[n, n]), 1)
            t["slope_buffer"] = rata_buffer(s, jarak, radius_m)
            t["sumber"] = f"DEMNAS ({Path(src.name).name})"
            break


def isi_kanopi(titik, sumber, radius_m, maks):
    """Tinggi kanopi di titik dan rata-rata dalam buffer. Nilai di atas `maks` dianggap kode non-data."""
    for t in titik:
        for src in sumber:
            hasil = baca_jendela(src, t["lat"], t["lon"], radius_m)
            if hasil is None:
                continue
            z, jarak, n, _, _ = hasil
            z[z > maks] = np.nan
            if np.isnan(z[n, n]) and rata_buffer(z, jarak, radius_m) is None:
                continue
            t["kanopi"] = None if np.isnan(z[n, n]) else round(float(z[n, n]), 1)
            t["kanopi_buffer"] = rata_buffer(z, jarak, radius_m)
            t["sumber_kanopi"] = Path(src.name).name
            break


def isi_hutan(titik, sumber, nilai_hutan, radius_list, cari_m):
    """Status hutan di titik, jarak ke tepi hutan, dan persen hutan dalam tiap buffer.

    Piksel bernilai salah satu `nilai_hutan` = hutan; piksel lain di dalam raster (termasuk nodata)
    = non-hutan; di luar raster = tidak diketahui. Tepi hutan = piksel kelas berlawanan terdekat
    (di dalam hutan: non-hutan terdekat; di luar hutan: hutan terdekat), dicari sejauh `cari_m`.
    Jarak diukur ke pusat piksel itu, dikurangi setengah piksel (batas kelas ada di antara piksel).
    Hasil ditandai di `hutan_catatan` bila bagian yang tidak diketahui (di luar raster) berada dalam
    radius yang dipakai, karena tepi yang lebih dekat bisa saja ada di luar raster.
    """
    radius_baca = max([cari_m] + list(radius_list))
    for t in titik:
        for src in sumber:
            hasil = baca_jendela(src, t["lat"], t["lon"], radius_baca, kosong_nan=False)
            if hasil is None:
                continue
            z, jarak, n, dx, dy = hasil
            diketahui = ~np.isnan(z)
            hutan = diketahui & np.isin(z, nilai_hutan)
            bukan = diketahui & ~hutan
            di_hutan = bool(hutan[n, n])
            catatan = []

            lawan = (bukan if di_hutan else hutan) & (jarak <= cari_m)
            if lawan.any():
                d = float(jarak[lawan].min())
                t["tepi"] = round(max(d - 0.5 * (dx + dy) / 2, 0.0), 1)
                if (~diketahui & (jarak <= d)).any():
                    catatan.append("tepi_mungkin_lebih_dekat_di_luar_raster")
            else:
                catatan.append(f"tidak_ada_tepi_dalam_{cari_m:g}m")
                if (~diketahui & (jarak <= cari_m)).any():
                    catatan.append("area_pencarian_sebagian_di_luar_raster")

            for r in radius_list:
                cakram = jarak <= r
                tahu = cakram & diketahui
                if tahu.any():
                    t["hutan_persen"][r] = round(100.0 * float(hutan[tahu].sum()) / float(tahu.sum()), 1)
                if tahu.sum() < cakram.sum():
                    catatan.append(f"buffer_{r:g}m_terpotong")
            t["hutan"] = int(di_hutan)
            t["hutan_catatan"] = ";".join(catatan)
            t["sumber_hutan"] = Path(src.name).name
            break


def isi_dari_copernicus(titik):
    """Isi titik yang masih kosong dari Copernicus GLO-90 (API Open-Meteo, maks. 100 titik/permintaan)."""
    kosong = [t for t in titik if t["elevasi"] is None]
    for a in range(0, len(kosong), 100):
        grup = kosong[a:a + 100]
        try:
            r = requests.get(OPEN_METEO, params={
                "latitude": ",".join(str(t["lat"]) for t in grup),
                "longitude": ",".join(str(t["lon"]) for t in grup),
            }, timeout=60)
            r.raise_for_status()
            nilai = r.json()["elevation"]
        except Exception as e:                    # jaringan/API gagal: biarkan kosong
            print(f"::warning::Copernicus (Open-Meteo) gagal: {e}")
            return
        for t, v in zip(grup, nilai):
            if v is not None:
                t["elevasi"] = round(float(v), 1)
                t["sumber"] = "Copernicus GLO-90 (Open-Meteo)"


# ---------------------------------------------------------------- hasil
def buat_csv(titik, ada_kanopi, radius_hutan=None):
    """CSV hasil (teks) tanpa koordinat. Kolom Kunci = CameraID|Site, dipakai untuk mencocokkan baris di sheet."""
    def k(v):
        return "" if v is None else v

    f = io.StringIO()
    w = csv.writer(f, lineterminator="\n")
    w.writerow(JUDUL_CSV + (JUDUL_KANOPI if ada_kanopi else [])
               + (judul_hutan(radius_hutan) if radius_hutan else []))
    for t in titik:
        baris = [f"{t['cam']}|{t['site']}", t["cam"], t["site"],
                 k(t["elevasi"]), k(t["slope"]), k(t["slope_buffer"]), t["sumber"]]
        if ada_kanopi:
            baris += [k(t["kanopi"]), k(t["kanopi_buffer"]), t["sumber_kanopi"]]
        if radius_hutan:
            baris += ([k(t["hutan"]), k(t["tepi"])] + [k(t["hutan_persen"].get(r)) for r in radius_hutan]
                      + [t["hutan_catatan"], t["sumber_hutan"]])
        w.writerow(baris)
    return f.getvalue()


def kirim_ke_sheet(url, token, teks_csv):
    """Kirim CSV ke web app Apps Script, yang menulisnya ke sheet. Keluar dengan error jika gagal."""
    isi = json.dumps({"token": token, "csv": teks_csv})
    try:
        r = requests.post(url, data=isi.encode("utf-8"), timeout=180,
                          headers={"Content-Type": "text/plain; charset=utf-8"})
    except requests.RequestException as e:
        sys.exit(f"::error::Web app Apps Script tidak bisa dihubungi: {e}")
    try:
        hasil = r.json()
    except ValueError:
        sys.exit(f"::error::Web app tidak mengembalikan JSON (HTTP {r.status_code}). Periksa URL /exec dan "
                 f"pengaturan 'Who has access: Anyone'. Awal respons: {r.text[:150]!r}")
    if not hasil.get("ok"):
        sys.exit(f"::error::Gagal memperbarui sheet: {hasil.get('error', hasil)}")
    print(f"Sheet diperbarui: {hasil.get('baris')} baris."
          + (f" Kolom dilewati (belum ada di CSV): {', '.join(hasil['dilewati'])}." if hasil.get("dilewati") else ""))


def laporan(titik, dilewati, ada_kanopi, ada_hutan):
    """Ringkasan singkat di log (bukan di halaman ringkasan run)."""
    n = len(titik)
    terisi = sum(t["elevasi"] is not None for t in titik)
    print(f"Elevasi dan lereng: {terisi}/{n} titik.")
    if ada_kanopi:
        print(f"Tinggi kanopi: {sum(t['kanopi_buffer'] is not None for t in titik)}/{n} titik.")
    if ada_hutan:
        print(f"Hutan: {sum(t['hutan'] is not None for t in titik)}/{n} titik "
              f"({sum(t['hutan'] == 1 for t in titik)} di dalam hutan).")
        for t in titik:
            if t["hutan_catatan"]:
                print(f"Catatan hutan {t['cam']} {t['site']}: {t['hutan_catatan']}")
    if dilewati:
        print(f"::warning::{dilewati} baris dilewati karena koordinatnya kosong atau tidak valid.")
    if terisi < n:
        print(f"::warning::{n - terisi} titik tidak mendapat elevasi "
              "(di luar area berisi data pada file DEM). Periksa koordinatnya di sheet.")


# ---------------------------------------------------------------- utama
def main():
    sheet_id = env("SHEET_ID", "1Zkm82R3ct1HY1ov_HL35M9qEJudKPCkf0wGSNGxwdQ4")
    gid = env("SHEET_GID", "0")
    pola_dem = env("DEM_FILES", "Assets/DEM_*.tif")
    pola_kanopi = env("KANOPI_FILES", "Assets/CHM_*.tif")
    pola_hutan = env("FOREST_FILES", "Assets/Forest*.tif")
    nilai_hutan = [float(v) for v in env("FOREST_VALUES", "1").split(",") if v.strip()]
    radius_hutan = [float(v) for v in env("FOREST_BUFFERS_M", "50,250,500").split(",") if v.strip()]
    cari_hutan = float(env("FOREST_SEARCH_M", "3000"))
    radius_m = float(env("RADIUS_BUFFER_M", "50"))
    kanopi_maks = float(env("KANOPI_MAKS_M", "100"))
    aktif = ("1", "true", "ya", "yes")
    nol_kosong = env("DEM_NOL_KOSONG", "true").lower() in aktif
    tujuan = env("OUTPUT_CSV")                 # opsional: simpan juga ke file (mis. untuk pengujian)
    webapp = env("WEBAPP_URL")
    token = env("SHEET_TOKEN")
    pakai_cadangan = env("FALLBACK_COPERNICUS", "false").lower() in aktif

    titik, dilewati = baca_titik(unduh_sheet(sheet_id, gid))
    print(f"{len(titik)} titik berkoordinat valid.")
    if not titik:
        sys.exit("::error::Tidak ada titik berkoordinat valid; file hasil tidak diubah.")

    dem = buka_raster(pola_dem)
    if not dem:
        sys.exit(f"::error::Tidak ada file DEM yang cocok dengan '{pola_dem}'.")
    print("File DEM:", ", ".join(Path(d.name).name for d in dem))
    isi_dari_dem(titik, dem, radius_m, nol_kosong)
    if pakai_cadangan:
        isi_dari_copernicus(titik)

    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="3",
                      GDAL_HTTP_RETRY_DELAY="2", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff"):
        try:
            kanopi = buka_raster(pola_kanopi)
        except rasterio.errors.RasterioIOError as e:
            print(f"::warning::File kanopi tidak bisa dibuka ({e}); kolom kanopi dilewati.")
            kanopi = []
        if kanopi:
            print("File kanopi:", ", ".join(Path(d.name).name for d in kanopi))
            try:
                isi_kanopi(titik, kanopi, radius_m, kanopi_maks)
            except rasterio.errors.RasterioIOError as e:
                print(f"::warning::Gagal membaca data kanopi ({e}); kolom kanopi dikosongkan.")
                for t in titik:
                    t["kanopi"] = t["kanopi_buffer"] = None
                    t["sumber_kanopi"] = ""
    if not kanopi:
        print(f"Tidak ada file kanopi untuk '{pola_kanopi}'; kolom kanopi dilewati.")

    hutan = buka_raster(pola_hutan)
    if hutan:
        print("File hutan:", ", ".join(Path(d.name).name for d in hutan))
        isi_hutan(titik, hutan, nilai_hutan, radius_hutan, cari_hutan)
    else:
        print(f"Tidak ada file hutan untuk '{pola_hutan}'; kolom hutan dilewati.")

    for src in dem + kanopi + hutan:
        src.close()
    laporan(titik, dilewati, bool(kanopi), bool(hutan))
    teks_csv = buat_csv(titik, bool(kanopi), radius_hutan if hutan else None)
    if tujuan:
        Path(tujuan).parent.mkdir(parents=True, exist_ok=True)
        Path(tujuan).write_text(teks_csv, encoding="utf-8", newline="")
        print(f"CSV disimpan ke {tujuan}.")
    if webapp:
        if not token:
            sys.exit("::error::SHEET_TOKEN belum diisi (secret GitHub), padahal WEBAPP_URL terisi.")
        kirim_ke_sheet(webapp, token, teks_csv)
    elif not tujuan:
        sys.exit("::error::WEBAPP_URL maupun OUTPUT_CSV tidak diisi; hasil tidak ke mana-mana.")


if __name__ == "__main__":
    main()
