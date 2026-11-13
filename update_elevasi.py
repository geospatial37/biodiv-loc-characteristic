#!/usr/bin/env python3
"""Perbarui kolom elevasi titik camera trap di Google Sheets.

Alur:
  1. Baca Latitude/Longitude dari sheet.
  2. Ambil nilai piksel DEMNAS di setiap titik, dari file .tif di folder Assets/ repo ini.
  3. Titik di luar cakupan DEM dibiarkan kosong (atau diisi Copernicus GLO-90 jika diaktifkan).
  4. Tulis elevasi dan sumbernya kembali ke sheet.

Konfigurasi lewat environment variable (lihat README.md).
"""
import json
import os
import sys
from pathlib import Path

import gspread
import rasterio
import requests
from google.oauth2.service_account import Credentials
from rasterio.warp import transform

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
OPEN_METEO = "https://api.open-meteo.com/v1/elevation"


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
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def cari_kolom(judul, nama):
    """Indeks kolom (mulai 0) dengan judul `nama` (tidak peka huruf besar/kecil), atau None."""
    target = nama.strip().lower()
    for i, j in enumerate(judul):
        if str(j).strip().lower() == target:
            return i
    return None


# ---------------------------------------------------------------- baca sheet
def baca_titik(baris):
    """Kembalikan (judul, daftar titik). Satu titik per baris data, urut sesuai sheet."""
    if not baris:
        sys.exit("::error::Sheet kosong.")
    judul = list(baris[0])
    i_lat, i_lon = cari_kolom(judul, "Latitude"), cari_kolom(judul, "Longitude")
    if i_lat is None or i_lon is None:
        sys.exit("::error::Kolom 'Latitude' dan/atau 'Longitude' tidak ditemukan di baris judul.")
    titik = []
    for b in baris[1:]:
        lat = ke_angka(b[i_lat]) if len(b) > i_lat else None
        lon = ke_angka(b[i_lon]) if len(b) > i_lon else None
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
        titik.append({
            "label": str(b[0]) if b else "",
            "lat": lat, "lon": lon, "elevasi": None, "sumber": "",
        })
    return judul, titik


# ---------------------------------------------------------------- ambil elevasi
def isi_dari_dem(titik, dem_dir):
    """Isi elevasi dari file DEM lokal. Titik di luar cakupan dibiarkan kosong."""
    berkas = sorted(p for p in dem_dir.rglob("*") if p.suffix.lower() in (".tif", ".tiff"))
    if not berkas:
        sys.exit(f"::error::Tidak ada file .tif di folder '{dem_dir}'.")
    print("File DEM:", ", ".join(p.name for p in berkas))
    sumber = [rasterio.open(b) for b in berkas]
    try:
        for t in titik:
            if t["lat"] is None:
                continue
            for src in sumber:
                x, y = t["lon"], t["lat"]
                if src.crs and src.crs.to_epsg() != 4326:
                    xs, ys = transform("EPSG:4326", src.crs, [x], [y])
                    x, y = xs[0], ys[0]
                b = src.bounds
                if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                    continue
                v = float(next(src.sample([(x, y)]))[0])
                if v != v or (src.nodata is not None and v == src.nodata):
                    continue                      # piksel kosong: coba tile lain
                t["elevasi"] = round(v, 1)
                t["sumber"] = f"DEMNAS ({Path(src.name).name})"
                break
    finally:
        for src in sumber:
            src.close()


def isi_dari_copernicus(titik):
    """Isi titik yang masih kosong dari Copernicus GLO-90 (API Open-Meteo, maks. 100 titik/permintaan)."""
    kosong = [t for t in titik if t["lat"] is not None and t["elevasi"] is None]
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


# ---------------------------------------------------------------- tulis sheet
def tulis_hasil(ws, judul, titik, nama_elevasi, nama_sumber):
    """Tulis dua kolom hasil. Kolom dicari dari judulnya; jika belum ada, dibuat di kanan."""
    judul = list(judul)
    while judul and str(judul[-1]).strip() == "":
        judul.pop()
    kolom = {}
    for nama in (nama_elevasi, nama_sumber):
        i = cari_kolom(judul, nama)
        if i is None:
            judul.append(nama)
            i = len(judul) - 1
        kolom[nama] = i + 1                        # nomor kolom mulai 1
    kurang = max(kolom.values()) - ws.col_count
    if kurang > 0:
        ws.add_cols(kurang)
    data = {
        nama_elevasi: [[nama_elevasi]] + [["" if t["elevasi"] is None else t["elevasi"]] for t in titik],
        nama_sumber: [[nama_sumber]] + [[t["sumber"]] for t in titik],
    }
    for nama, nilai in data.items():
        ws.update(values=nilai, range_name=gspread.utils.rowcol_to_a1(1, kolom[nama]))
    return kolom


def ringkasan(titik, rinci):
    """Ringkasan untuk log dan halaman run GitHub Actions. Koordinat tidak pernah ditulis;
    nama titik dan elevasi hanya ditulis jika `rinci` aktif (log repo publik bisa dibaca siapa saja)."""
    valid = [t for t in titik if t["lat"] is not None]
    terisi = [t for t in valid if t["elevasi"] is not None]
    baris = ["## Elevasi camera trap", "", f"{len(terisi)} dari {len(valid)} titik terisi."]
    if rinci:
        baris += ["", "| Titik | Elevasi (m) | Sumber |", "|---|---|---|"]
        for t in valid:
            baris.append(f"| {t['label']} | {'' if t['elevasi'] is None else t['elevasi']} | {t['sumber']} |")
    teks = "\n".join(baris)
    print(teks)
    berkas = os.environ.get("GITHUB_STEP_SUMMARY")
    if berkas:
        with open(berkas, "a", encoding="utf-8") as f:
            f.write(teks + "\n")
    if len(terisi) < len(valid):
        print(f"::warning::{len(valid) - len(terisi)} titik tidak mendapat elevasi "
              "(di luar cakupan file DEM). Periksa koordinatnya di sheet.")


# ---------------------------------------------------------------- utama
def main(ws=None):
    sheet_id = env("SHEET_ID", wajib=ws is None)
    nama_sheet = env("SHEET_NAME", "type")
    dem_dir = Path(env("DEM_DIR", "Assets"))
    nama_elevasi = env("KOLOM_ELEVASI", "Elevasi_m")
    nama_sumber = env("KOLOM_SUMBER", "Sumber_Elevasi")
    aktif = ("1", "true", "ya", "yes")
    pakai_cadangan = env("FALLBACK_COPERNICUS", "false").lower() in aktif
    rinci = env("RINGKASAN_RINCI", "false").lower() in aktif

    if ws is None:
        try:
            info = json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON", wajib=True))
        except json.JSONDecodeError:
            sys.exit("::error::GOOGLE_SERVICE_ACCOUNT_JSON bukan JSON yang valid.")
        kredensial = Credentials.from_service_account_info(info, scopes=SCOPES)
        ws = gspread.authorize(kredensial).open_by_key(sheet_id).worksheet(nama_sheet)

    judul, titik = baca_titik(ws.get(value_render_option="UNFORMATTED_VALUE"))
    print(f"{sum(t['lat'] is not None for t in titik)} titik berkoordinat valid dari {len(titik)} baris.")

    isi_dari_dem(titik, dem_dir)
    if pakai_cadangan:
        isi_dari_copernicus(titik)

    kolom = tulis_hasil(ws, judul, titik, nama_elevasi, nama_sumber)
    print("Hasil ditulis ke kolom nomor:", kolom)
    ringkasan(titik, rinci)


if __name__ == "__main__":
    main()
