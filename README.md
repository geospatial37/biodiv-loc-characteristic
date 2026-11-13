# biodiv-loc-characteristic

Mengisi elevasi (meter di atas permukaan laut) setiap titik camera trap di Google Sheets, dari DEMNAS yang tersimpan di repo ini. Dijalankan lewat GitHub Actions.

Setiap kali dijalankan, skrip:

1. membaca kolom `Latitude` dan `Longitude` dari sheet `type`;
2. mengambil nilai DEMNAS di setiap titik, dari file `.tif` di folder `Assets/`;
3. menulis hasilnya ke kolom `Elevasi_m` dan `Sumber_Elevasi` di sheet yang sama.

Semua baris dihitung ulang pada setiap run, jadi titik baru atau koordinat yang diubah ikut diperbarui.

## Isi repo

```
Assets/DEM_DEMNASProjectArea_20261113_v1.tif
update_elevasi.py
requirements.txt
README.md
.github/workflows/update-elevasi.yml
```

Di situs GitHub, file workflow dibuat lewat **Add file → Create new file**, lalu ketik nama lengkapnya: `.github/workflows/update-elevasi.yml`.

## Penyiapan (sekali saja)

### 1. Buat service account Google

1. Buka [console.cloud.google.com](https://console.cloud.google.com) dan buat project (atau pakai yang ada).
2. Di **APIs & Services → Library**, aktifkan **Google Sheets API**.
3. Di **IAM & Admin → Service Accounts**, buat service account baru (tanpa role apa pun).
4. Buka service account itu, tab **Keys → Add key → Create new key → JSON**. File JSON akan terunduh.
5. Catat email service account (berakhiran `iam.gserviceaccount.com`).

### 2. Beri akses ke spreadsheet

Di spreadsheet, klik **Share** dan tambahkan email service account sebagai **Editor**.

### 3. Isi secrets di GitHub

Di repo: **Settings → Secrets and variables → Actions → New repository secret**. Buat dua secret:

| Nama | Isi |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Seluruh isi file JSON dari langkah 1 |
| `SHEET_ID` | Bagian URL spreadsheet di antara `/d/` dan `/edit` |

Jangan pernah menyimpan file JSON itu di dalam repo.

## Menjalankan

Buka tab **Actions → Update elevasi camera trap → Run workflow**.

Untuk jadwal otomatis, hapus tanda `#` pada baris `schedule` dan `cron` di file workflow.

## Pengaturan

Diatur di bagian `env` pada file workflow:

| Variabel | Bawaan | Arti |
|---|---|---|
| `SHEET_NAME` | `type` | Nama sheet berisi koordinat |
| `DEM_DIR` | `Assets` | Folder di repo yang berisi file DEM (`.tif`) |
| `KOLOM_ELEVASI` | `Elevasi_m` | Judul kolom hasil elevasi |
| `KOLOM_SUMBER` | `Sumber_Elevasi` | Judul kolom sumber data |
| `FALLBACK_COPERNICUS` | `false` | `true` = titik di luar DEM diisi Copernicus GLO-90 (90 m) |
| `RINGKASAN_RINCI` | `false` | `true` = nama titik dan elevasi tampil di log run |

Kolom hasil dicari berdasarkan judulnya. Jika belum ada, kolom dibuat di sebelah kanan kolom terakhir.

## Catatan

- **Kolom hasil ditimpa** pada setiap run. Jangan mengisi kolom `Elevasi_m` dan `Sumber_Elevasi` secara manual.
- **Titik di luar DEM** dibiarkan kosong dan run memberi peringatan. Biasanya itu berarti koordinatnya salah ketik, atau area DEM perlu diperluas.
- **Mengganti DEM:** unggah file `.tif` baru ke `Assets/` dan hapus yang lama. Jika ada beberapa file, skrip memakai file pertama (urut nama) yang mencakup titik.
- **Repo publik:** log run bisa dibaca siapa saja. Koordinat tidak pernah ditulis ke log, dan nama titik hanya ditulis jika `RINGKASAN_RINCI` diaktifkan.
