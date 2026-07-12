# Shopee Partner Dashboard Network Event Capture

Proyek ini dibuat untuk menangkap dan merekam seluruh lalu lintas jaringan API (HTTP Fetch, XMLHttpRequest, dan WebSocket) secara real-time pada **Shopee Partner Dashboard**. Tujuannya adalah membantu menganalisis pola traffic dan mendeteksi alasan mengapa Shopee sering kali menutup sementara outlet online mitra secara otomatis.

---

## 🛠️ Desain & Fitur Utama

1. **JS Interceptor Monkeypatch**:
   - Menyuntikkan script *monkeypatch* kustom ke dalam V8 Engine Chrome saat inisialisasi dokumen baru via **Chrome DevTools Protocol (CDP)** (`Page.addScriptToEvaluateOnNewDocument`).
   - Melakukan intersep otomatis pada `window.fetch`, `XMLHttpRequest`, dan koneksi `window.WebSocket` (baik pesan keluar `WS_SEND` maupun pesan masuk `WS_RECV`).
   - Menyaring asset statis (`.js`, `.css`, gambar, font) secara otomatis agar tidak membanjiri log.

2. **Penyimpanan Handal menggunakan Session Storage**:
   - Log hasil tangkapan di-buffer ke dalam `sessionStorage` agar data tidak hilang apabila terjadi reload halaman atau perpindahan rute secara penuh sebelum script Python sempat melakukan polling.

3. **Log CSV Bersih + Payload JSON Terperinci**:
   - Menyimpan ringkasan lalu lintas dalam format CSV (`captured_logs/{account_name}/traffic_log_*.csv`) yang rapi dan mudah dianalisis di Excel atau Google Sheets.
   - Respon/request body yang utuh disimpan dalam folder terpisah (`payloads/`) dalam bentuk format JSON cantik (`.json`), lalu dihubungkan langsung dari baris CSV terkait via kolom `Payload File`.

4. **Deteksi Transisi URL**:
   - Merekam setiap perubahan URL navigasi di browser (seperti pengalihan ke halaman logout, merchant selector, atau onboarding).

---

## 📁 Struktur Folder

```
event-log-capture/
├── core/
│   ├── __init__.py
│   ├── browser.py          # Modul manajemen driver Chrome & pemulihan sesi
│   ├── client.py
│   ├── logger.py
│   └── otp.py
├── data/                   # Sesi JSON & link ke profil Chrome (symlinks)
├── captured_logs/          # Hasil tangkapan (dibuat otomatis)
│   └── [account_name]/
│       ├── traffic_log_[timestamp].csv
│       └── payloads/       # File payload JSON individual
├── credentials.json
├── capture_events.py      # Script utama penangkap event
├── test_switch.py         # Script test untuk open & switch merchant
└── README.md
```

---

## 🧪 Skrip Pengujian Switching Merchant (`test_switch.py`)

Skrip `test_switch.py` disediakan untuk menguji pembukaan dashboard dan perpindahan ke merchant target (`SuperFood .`) secara spesifik. 

Skrip ini memisahkan proses inisialisasi sesi dengan perpindahan merchant agar dapat menyuntikkan interseptor jaringan *sebelum* perpindahan merchant dilakukan, sehingga seluruh traffic jaringan yang terjadi selama pergantian merchant dapat terekam di layar terminal.

### Cara Menjalankan Tes:
```bash
./.venv/bin/python test_switch.py [nama_akun]
```
Contoh menggunakan profil akun `superfoodapp`:
```bash
./.venv/bin/python test_switch.py superfoodapp
```

---

## 🚀 Cara Menjalankan Penangkap Traffic Utama

Gunakan virtual environment lokal yang telah terpasang modul `selenium`, `webdriver-manager`, dan `requests`:

```bash
# Masuk ke direktori project
cd /home/akbarhann/project/event-log-capture

# Jalankan script capture_events.py
./.venv/bin/python capture_events.py
```

### Opsi Tambahan:
Kamu bisa menentukan nama akun target secara langsung melalui argumen CLI:
```bash
./.venv/bin/python capture_events.py auto7307
```
Jika tidak ada argumen nama akun yang dilewatkan, script akan otomatis mendeteksi sesi yang tersedia di folder `data/` dan menampilkan menu interaktif untuk dipilih.

---

## 📊 Format Data CSV

CSV hasil perekaman memiliki kolom sebagai berikut:

| Nama Kolom | Keterangan |
| :--- | :--- |
| **Timestamp** | Waktu pencatatan kejadian (format ISO-8601). |
| **Type** | Tipe event (`fetch`, `xhr`, `websocket_open`, `websocket_send`, `websocket_receive`, `url_change`). |
| **Method** | HTTP Method (`GET`, `POST`, `NAVIGATION`, `WS_SEND`, `WS_RECV`). |
| **URL** | Alamat API target atau URL koneksi WebSocket. |
| **Status** | Kode status HTTP (misal `200`, `401`, atau `101` untuk WebSocket). |
| **Request Snippet** | Potongan singkat (150 karakter awal) payload request. |
| **Response Snippet** | Potongan singkat (150 karakter awal) isi response. |
| **Payload File** | Path ke file JSON jika data request/response lengkap ingin ditinjau. |
| **Page URL** | Lokasi URL browser tempat request tersebut dilakukan. |

---

## 🔍 Cara Menganalisis Pola Penutupan Outlet

Saat program berjalan, monitor terminal kamu secara real-time. Carilah event-event yang berkaitan dengan status outlet, misalnya:
- Panggilan API ke endpoint yang memuat kata kunci seperti `status`, `close`, `open`, `merchant`, atau `shop`.
- Cari kemunculan kode response API berupa pesan kegagalan, atau payload `POST` yang merubah status toko dari **Buka (Open)** menjadi **Tutup Sementara (Temporarily Closed)**.
- Telusuri file payload JSON di folder `payloads/` untuk melihat detail pesan kembalian server guna mengetahui apakah penutupan dipicu oleh sistem Shopee karena orderan terbengkalai, koneksi ping terputus, atau sebab administratif lainnya.
