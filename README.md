# biodiv-loc-characteristic

Menghitung karakteristik lokasi setiap titik camera trap: elevasi, kemiringan lereng, dan tinggi kanopi. Dijalankan lewat GitHub Actions, tanpa kredensial Google.

Setiap kali dijalankan:

1. skrip membaca kolom `CameraID`, `Site`, `Latitude`, dan `Longitude` dari sheet `type` (spreadsheet harus dibuka untuk umum);
2. skrip mengambil nilai dari raster di folder `Assets/`;
3. hasilnya disimpan ke `output/elevasi.csv` dan di-commit ke repo;
4. Google Sheets membaca file itu kembali dengan `IMPORTDATA`.

Semua titik dihitung ulang pada setiap run, jadi titik baru atau koordinat yang diubah ikut diperbarui.

## Kolom hasil

| Kolom | Arti |
|---|---|
| `Kunci` | Gabungan CameraID dan Site, dipakai untuk mencocokkan baris di Google Sheets |
| `Elevasi_m` | Elevasi DEMNAS di piksel titik (m dpl) |
| `Slope_deg` | Kemiringan lereng di piksel titik (derajat, metode Horn 3×3) |
| `Slope_buffer_deg` | Rata-rata kemiringan lereng dalam radius 50 m dari titik (derajat) |
| `Sumber_Elevasi` | File DEM yang dipakai |
| `Kanopi_m` | Tinggi kanopi di piksel titik (m) |
| `Kanopi_buffer_m` | Rata-rata tinggi kanopi dalam radius 50 m (m) |
| `Sumber_Kanopi` | Tile/file kanopi yang dipakai |

Untuk analisis, pakai nilai buffer. Nilai di satu piksel peka terhadap galat GPS dan noise raster.

## Isi repo

```
Assets/DEM_DEMNASProjectArea_20261113_v1.tif
update_elevasi.py
requirements.txt
README.md
.github/workflows/update-elevasi.yml
output/elevasi.csv          (dibuat otomatis oleh workflow)
```

File `DEM_*.tif` di folder `Assets/` dibaca sebagai DEM.

## Penyiapan (sekali saja)

1. **Buka spreadsheet:** klik **Share → General access → Anyone with the link → Viewer**.
2. **Simpan ID spreadsheet sebagai secret:** di repo, **Settings → Secrets and variables → Actions → New repository secret**, nama `SHEET_ID`, isi bagian URL spreadsheet di antara `/d/` dan `/edit`.
3. **Izinkan workflow menulis ke repo:** **Settings → Actions → General → Workflow permissions → Read and write permissions**.
4. **Jalankan sekali:** tab **Actions → Update elevasi camera trap → Run workflow**. File `output/elevasi.csv` akan muncul di repo.

## Menampilkan hasil di Google Sheets

Buat sheet baru bernama `elevasi`, lalu isi sel A1:

```
=IMPORTDATA("https://raw.githubusercontent.com/geospatial37/biodiv-loc-characteristic/main/output/elevasi.csv")
```

Di sheet `type`, ketik judul `Elevasi_m`, `Slope_deg`, `Slope_buffer_deg` di E1:G1. Kosongkan E2:G, lalu isi E2:

```
=ARRAYFORMULA(IF(A2:A="",,IFNA(VLOOKUP(A2:A&"|"&B2:B,elevasi!A:F,{4,5,6},FALSE))))
```

Untuk tinggi kanopi, ketik `Kanopi_m` dan `Kanopi_buffer_m` di H1:I1, lalu isi H2:

```
=ARRAYFORMULA(IF(A2:A="",,IFNA(VLOOKUP(A2:A&"|"&B2:B,elevasi!A:I,{8,9},FALSE))))
```

Rumus itu mencocokkan `CameraID|Site`, jadi tetap benar walau urutan baris berubah. Jika rumus error, ganti koma dengan titik koma.

Setelah sebuah run selesai, Google Sheets memperbarui `IMPORTDATA` dengan sendirinya; biasanya perlu waktu hingga sekitar satu jam.

## Tinggi kanopi

Bawaannya, skrip membaca **Version 2 High Resolution Canopy Height Maps by WRI and Meta (CHMv2)** langsung dari bucket AWS publik. Datanya berupa Cloud Optimized GeoTIFF, jadi skrip hanya mengambil potongan kecil di sekitar setiap titik; tidak ada yang perlu diunduh atau diunggah ke repo.

- Resolusi sekitar 1,2 m, proyeksi Web Mercator (EPSG:3857), nilai piksel = tinggi puncak kanopi dalam meter.
- Data dibagi per tile Web Mercator zoom 10, dengan nama file berupa quadkey. Area proyek ini tercakup dua tile: `1322333203` dan `1322333221`.
- Piksel yang ditandai berawan pada mask band dianggap kosong.
- Lisensi CC BY 4.0. Sitasi: Brandt dkk. (2026), *CHMv2: Improvements in Global Canopy Height Mapping using DINOv3*, Scientific Data, https://doi.org/10.1038/s41597-026-07882-0.

Jika area proyek bertambah ke luar kedua tile itu, tambahkan URL tile baru ke `KANOPI_FILES` di file workflow (dipisah koma).

Untuk memakai raster kanopi lain, simpan sebagai `Assets/CHM_<nama>.tif` dan ubah `KANOPI_FILES` menjadi `Assets/CHM_*.tif`.

Catatan: tinggi kanopi dari produk global adalah hasil pemodelan dari citra satelit, bukan pengukuran langsung. Galatnya bisa beberapa meter per piksel.

## Pengaturan

Diatur di bagian `env` pada file workflow:

| Variabel | Bawaan | Arti |
|---|---|---|
| `SHEET_GID` | `2116481874` | Angka setelah `gid=` pada URL sheet berisi koordinat |
| `DEM_FILES` | `Assets/DEM_*.tif` | Pola nama file DEM |
| `KANOPI_FILES` | dua URL tile CHMv2 | URL (dipisah koma) atau pola file lokal raster tinggi kanopi |
| `RADIUS_BUFFER_M` | `50` | Radius buffer (meter) untuk rata-rata lereng dan kanopi |
| `DEM_NOL_KOSONG` | `true` | Piksel DEM bernilai 0 dianggap kosong |
| `KANOPI_MAKS_M` | `100` | Nilai kanopi di atas ini dianggap kode non-data |
| `OUTPUT_CSV` | `output/elevasi.csv` | Lokasi file hasil |
| `FALLBACK_COPERNICUS` | `false` | `true` = elevasi titik di luar DEM diisi Copernicus GLO-90 (90 m) |

Untuk jadwal otomatis, hapus tanda `#` pada baris `schedule` dan `cron` di file workflow.

## Catatan

- **Area kosong pada DEM:** file DEM hanya berisi data di dalam area proyek; di luarnya piksel bernilai 0 atau penanda kosong. Titik yang jatuh di sana dibiarkan kosong dan run memberi peringatan.
- **Lereng dihitung dalam meter:** DEM berkoordinat derajat, jadi ukuran piksel dikonversi ke meter sebelum lereng dihitung.
- **Yang terbuka untuk umum:** `output/elevasi.csv` berisi CameraID, nama site, dan nilai-nilai di atas, tanpa koordinat. Namun siapa pun yang memegang tautan spreadsheet bisa membaca seluruh isinya, termasuk koordinat. `SHEET_ID` disimpan sebagai secret agar tautan itu tidak tertulis di repo.
- **Mengganti raster:** unggah file `.tif` baru ke `Assets/` dan hapus yang lama. Jika ada beberapa file sejenis, skrip memakai file pertama (urut nama) yang berisi data di titik itu.
