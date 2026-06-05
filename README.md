# KS Document Processor
### Waralalo Group — Pipeline Digitalisasi Dokumen

Pipeline otomatis: **PDF campuran** → **identifikasi perusahaan + departemen** → **split per dokumen (kalau perlu)** → **filing rapi** → **index ke Pinecone**

---

## Struktur folder

```
ks-doc-processor/
├── process_docs.py        ← Script 1: identifikasi, split & filing PDF
├── index_to_pinecone.py   ← Script 2: embed & index ke vector DB
├── .env                   ← API keys + path Tesseract/Poppler
├── inbox/                 ← TARUH PDF CAMPURAN DI SINI
│   └── _processed/        ← File yang sudah diproses dipindah ke sini otomatis
├── output/                ← Hasil filing, tersortir per perusahaan
│   └── [Nama Perusahaan]/
│       ├── Legal/[Subfolder]/              ← level perusahaan
│       ├── Sales & Marketing/[Subfolder]/  ← level perusahaan
│       ├── Finance/[Subfolder]/            ← keuangan umum perusahaan
│       ├── Projects/
│       │   └── [Nama Proyek]/
│       │       ├── Engineering/[Subfolder]/
│       │       ├── Operasional/[Subfolder]/
│       │       ├── HR/[Subfolder]/
│       │       └── Finance/[Subfolder]/    ← keuangan proyek (invoice, termin)
│       └── 00_Unidentified/[Departemen]/[Subfolder]/  ← dept jelas tapi proyek tak terdeteksi
├── folder_index.json      ← Memory proyek + subfolder antar run (biar konsisten)
└── processing_log.json    ← Log semua dokumen yang sudah diproses
```

Hasil filing memakai hierarki **Perusahaan → (level perusahaan / proyek) → Departemen → Subfolder**.

**Perusahaan yang dikenal (whitelist — satu-satunya folder perusahaan yang valid):** PT Krakatau Shipyard, PT Industri Kapal Nusantara, PT Krakatau Sarana Dockyard, PT Halmahera Shipping, KSO DKB-KS, PT Indonesia Register, PT Lautan Biru Nusantara, PT Lautan Karya Gemilang. Nama lain/singkatan (KS, IKN, KSD, DKB→KSO DKB-KS) dipetakan otomatis; selain itu → `00_Unidentified`. Pihak luar (klien/vendor) tidak jadi folder — namanya disimpan sebagai `counterparty` di log.

**Aturan penempatan departemen:**
- **Legal, Sales & Marketing** → selalu level perusahaan.
- **Engineering, Operasional, HR** → selalu di dalam sebuah proyek (`Projects/[Nama Proyek]/`). Kalau proyek tak teridentifikasi → `00_Unidentified/[Departemen]/`.
- **Finance, IT** → bisa level perusahaan ATAU proyek, tergantung isi dokumen.

Nama proyek dan subfolder diingat di `folder_index.json` dan diumpankan balik ke Claude tiap run, supaya proyek/subfolder yang sama tidak terpecah jadi banyak ejaan berbeda.

---

## Setup (sekali saja)

### 1. Install dependencies
```bash
pip install pypdf pdfplumber anthropic pinecone openai python-dotenv tqdm pdf2image pytesseract
```

Untuk OCR (PDF hasil scan tanpa teks) butuh dua tool eksternal:
- **Tesseract OCR** (dengan language pack `ind` + `eng`) → https://github.com/UB-Mannheim/tesseract/wiki
- **Poppler** (sudah disertakan di folder `poppler-26.02.0/`)

### 2. Isi `.env`
```
ANTHROPIC_API_KEY=sk-ant-...
PINECONE_API_KEY=pcsk_...
OPENAI_API_KEY=sk-...
TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
POPPLER_PATH=...\poppler-26.02.0\Library\bin
```

Kamu butuh:
- **Anthropic API key** → https://console.anthropic.com (analisis & filing dokumen)
- **Pinecone API key** → https://app.pinecone.io (gratis untuk skala kecil)
- **OpenAI API key** → https://platform.openai.com (embedding, ~$0.0001/1K token)

`TESSERACT_PATH` dan `POPPLER_PATH` hanya dipakai kalau ada PDF scan yang perlu OCR.

---

## Cara pakai

### Step 1 — Scan dokumen fisik
Pakai **Microsoft Lens** atau **Adobe Scan** di HP. Export sebagai PDF.
Tidak perlu rename, tidak perlu sortir — langsung taruh di folder `inbox/`.

### Step 2 — Proses & filing
```bash
python process_docs.py
```

Opsi untuk testing:
```bash
python process_docs.py -n 10     # proses 10 file pertama saja (mode tes)
python process_docs.py -r        # proses ULANG file di inbox/_processed/ (tidak dipindah)
python process_docs.py -c        # hapus isi output/ + folder_index.json dulu (mulai bersih)
python process_docs.py -r -n 10 -c   # kombinasi: tes ulang 10 file, output fresh
```

Yang terjadi:
1. **Fase 1 — Analisis (paralel, 5 file sekaligus):** setiap PDF di `inbox/` dibaca. Teks diekstrak dengan `pdfplumber`; kalau PDF berupa gambar/scan, otomatis fallback ke **OCR** (Tesseract + Poppler).
2. Claude (`claude-sonnet-4-5`) menentukan: perusahaan, departemen, subfolder, dan apakah file perlu **dipotong** jadi beberapa dokumen.
3. **Ringkasan** ditampilkan (berapa utuh, berapa dipotong, berapa tak teridentifikasi) lalu minta konfirmasi `y/n`.
4. **Fase 2 — Filing:** dokumen disalin ke `output/` sesuai aturan penempatan (level perusahaan atau `Projects/[Nama Proyek]/[Departemen]/[Subfolder]/`) dengan nama otomatis.
5. File asli dipindah ke `inbox/_processed/`.
6. `folder_index.json` & `processing_log.json` diperbarui. Dokumen dengan `expire_date` ditampilkan di akhir sebagai pengingat.

> Aturan split: dokumen yang jelas satu topik (company profile, proposal, laporan tahunan, presentasi, manual, SOP, brosur) **tidak** dipotong. Yang dipotong hanya file berisi campuran dokumen berbeda (mis. invoice + faktur + bank garansi).

### Step 3 — Review manual (sekali di awal)
Buka folder `output/`, cek apakah dokumen sudah masuk perusahaan/departemen/subfolder yang benar. Kalau ada yang salah, pindah manual saja.

### Step 4 — Upload ke Google Drive (opsional)
Upload isi folder `output/` ke Google Drive dengan struktur yang sama.

### Step 5 — Index ke Pinecone
```bash
python index_to_pinecone.py
```

Yang terjadi:
1. Semua PDF di `output/` diekstrak teksnya dan dipecah jadi chunk (800 karakter, overlap 100).
2. Tiap chunk di-embed dengan OpenAI `text-embedding-3-small` (1536 dimensi).
3. Di-upsert ke Pinecone index `ks-documents` (dibuat otomatis kalau belum ada) lengkap dengan metadata dari `processing_log.json`.
4. File yang sudah pernah di-index akan di-skip otomatis.

Setelah ini, agent bisa query natural language:
> *"Apakah kita punya sertifikat BKI yang masih berlaku?"*
> *"Cari kontrak proyek kapal yang sudah selesai untuk referensi tender ini"*

Fungsi `search()` di akhir script bisa dipakai untuk testing query langsung.

---

## Dokumen yang perlu di-scan duluan (prioritas)

| Prioritas | Jenis | Contoh dokumen |
|-----------|-------|----------------|
| 🔴 1 | Legalitas | Akta, NIB, SIUP, NPWP, SK Kemenkumham |
| 🔴 1 | Sertifikasi | BKI, Kemenhub, K3, ISO |
| 🔴 1 | Pengalaman Proyek | Kontrak selesai, BAST, referensi |
| 🟡 2 | Keuangan | Laporan keuangan 3 tahun, SKHP bank |
| 🟡 2 | Tenaga Ahli | CV, SKA/SKT, ijazah personil kunci |
| 🟢 3 | Peralatan | Daftar aset, bukti kepemilikan |

---

## Catatan penting

- **PDF scan tanpa teks (image-only):** otomatis di-OCR (`ind+eng`, 180 DPI, maks 8 halaman pertama) selama `TESSERACT_PATH` & `POPPLER_PATH` benar. Kalau OCR gagal, dokumen tetap difile tapi tanpa teks untuk analisis.
- **Dokumen sangat panjang:** Claude hanya membaca ~10.000 karakter pertama untuk analisis struktur — cukup untuk mendeteksi perusahaan, departemen, dan batas antar dokumen.
- **Paralel:** default 5 file diproses bersamaan (`MAX_WORKERS` di `process_docs.py`). Naikkan kalau mau lebih cepat, turunkan kalau kena rate limit.
- **Re-index:** kalau dokumen diupdate, hapus vector lama dari Pinecone dulu (`index.delete(ids=[...])`) lalu jalankan ulang indexer.

---

## Troubleshooting

**"Tidak ada teks"** → PDF berupa gambar dan OCR gagal. Cek `TESSERACT_PATH`/`POPPLER_PATH`, atau scan ulang pakai Microsoft Lens
**"JSON tidak valid"** → jarang; script otomatis fallback ke `00_Unidentified`
**Filing salah** → pindah manual ke folder yang benar, sudah cukup
**Rate limit Anthropic** → turunkan `MAX_WORKERS`, atau jalankan ulang (file yang sudah diproses sudah pindah ke `_processed/`)
