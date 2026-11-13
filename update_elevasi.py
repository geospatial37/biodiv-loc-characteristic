#!/usr/bin/env python3
"""Hitung elevasi, kemiringan lereng, dan tinggi kanopi titik camera trap, lalu simpan sebagai CSV di repo.

Alur:
  1. Baca sheet koordinat dari Google Sheets yang dibuka untuk umum (tanpa kredensial).
  2. Dari DEM (Assets/DEM_*.tif): elevasi di titik, lereng di titik, dan rata-rata lereng dalam buffer.
  3. Dari raster tinggi kanopi (Assets/CHM_*.tif, opsional): tinggi kanopi di titik dan rata-rata dalam buffer.
  4. Tulis hasilnya ke output/elevasi.csv (tanpa koordinat). Workflow lalu meng-commit file itu,
     dan Google Sheets membacanya kembali dengan IMPORTDATA.

Konfigurasi lewat environment variable (lihat README.md).
"""
import csv
import glob
import io
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
                      "kanopi": None, "kanopi_buffer": None, "sumber_kanopi": ""})
    return titik, dilewati


# ---------------------------------------------------------------- baca raster
def buka_raster(pola):
    """Buka raster dari pola file lokal (mis. Assets/DEM_*.tif, urut nama) atau daftar URL https
    dipisah koma. URL dibaca langsung tanpa diunduh utuh (Cloud Optimized GeoTIFF)."""
    if pola.startswith(("http://", "https://")):
        return [rasterio.open(u.strip()) for u in pola.split(",") if u.strip()]
    berkas = sorted(glob.glob(pola, recursive=True))
    return [rasterio.open(b) for b in berkas]


def baca_jendela(src, lat, lon, radius_m):
    """Potongan raster di sekitar titik.

    Mengembalikan (z, jarak, n, dx, dy) atau None jika titik di luar raster:
      z      array float, NaN untuk piksel kosong/di luar raster; titik berada di z[n, n]
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
        data[src.read_masks(1, window=jendela) == 0] = np.nan   # mask band (mis. awan pada CHMv2)
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        data[data < -1e4] = np.nan                 # penanda kosong yang tidak tercatat di metadata
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


# ---------------------------------------------------------------- tulis hasil
def tulis_csv(titik, tujuan, ada_kanopi):
    """Tulis hasil tanpa koordinat. Kolom Kunci = CameraID|Site, dipakai VLOOKUP di Google Sheets."""
    def k(v):
        return "" if v is None else v

    tujuan.parent.mkdir(parents=True, exist_ok=True)
    with open(tujuan, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(JUDUL_CSV + (JUDUL_KANOPI if ada_kanopi else []))
        for t in titik:
            baris = [f"{t['cam']}|{t['site']}", t["cam"], t["site"],
                     k(t["elevasi"]), k(t["slope"]), k(t["slope_buffer"]), t["sumber"]]
            if ada_kanopi:
                baris += [k(t["kanopi"]), k(t["kanopi_buffer"]), t["sumber_kanopi"]]
            w.writerow(baris)


def ringkasan(titik, dilewati, tujuan, ada_kanopi, radius_m):
    terisi = sum(t["elevasi"] is not None for t in titik)
    teks = (f"## Karakteristik lokasi camera trap\n\n"
            f"- Elevasi dan lereng: {terisi} dari {len(titik)} titik terisi (buffer {radius_m:g} m).\n")
    if ada_kanopi:
        kan = sum(t["kanopi_buffer"] is not None for t in titik)
        teks += f"- Tinggi kanopi: {kan} dari {len(titik)} titik terisi.\n"
    else:
        teks += "- Tinggi kanopi: dilewati (belum ada file raster kanopi).\n"
    teks += f"- Hasil: `{tujuan.as_posix()}`."
    print(teks)
    berkas = os.environ.get("GITHUB_STEP_SUMMARY")
    if berkas:
        with open(berkas, "a", encoding="utf-8") as f:
            f.write(teks + "\n")
    if dilewati:
        print(f"::warning::{dilewati} baris dilewati karena koordinatnya kosong atau tidak valid.")
    if terisi < len(titik):
        print(f"::warning::{len(titik) - terisi} titik tidak mendapat elevasi "
              "(di luar area berisi data pada file DEM). Periksa koordinatnya di sheet.")


# ---------------------------------------------------------------- utama
def main():
    sheet_id = env("SHEET_ID", "1Zkm82R3ct1HY1ov_HL35M9qEJudKPCkf0wGSNGxwdQ4")
    gid = env("SHEET_GID", "0")
    pola_dem = env("DEM_FILES", "Assets/DEM_*.tif")
    pola_kanopi = env("KANOPI_FILES", "Assets/CHM_*.tif")
    radius_m = float(env("RADIUS_BUFFER_M", "50"))
    kanopi_maks = float(env("KANOPI_MAKS_M", "100"))
    aktif = ("1", "true", "ya", "yes")
    nol_kosong = env("DEM_NOL_KOSONG", "true").lower() in aktif
    tujuan = Path(env("OUTPUT_CSV", "output/elevasi.csv"))
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

    for src in dem + kanopi:
        src.close()
    tulis_csv(titik, tujuan, bool(kanopi))
    ringkasan(titik, dilewati, tujuan, bool(kanopi), radius_m)


if __name__ == "__main__":
    main()
