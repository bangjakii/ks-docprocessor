# KS Document Processor
### Waralalo Group — Pipeline Digitalisasi Dokumen

Pipeline otomatis: **PDF campuran** → **identifikasi perusahaan** → **split per dokumen (kalau perlu)** → **filing rapi per proyek** → **index ke Pinecone**

---

## Struktur folder

```
ks-doc-processor/
├── process_docs.py        ← Script 1: identifikasi, split & filing PDF
├── refile_output.py       ← Utility: rapikan ulang output tanpa analisis ulang
├── index_to_pinecone.py   ← Script 2: embed & index ke vector DB
├── .env                   ← API keys + path Tesseract/Poppler (salin dari .env.example)
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
│       └── _Tanpa Proyek/[Departemen]/[Subfolder]/  ← dept proyek tapi proyek tak terdeteksi
├── folder_index.json      ← Memory proyek + subfolder antar run (biar konsisten)
└── processing_log.json    ← Log semua dokumen yang sudah diproses
```

Hierarki filing: **Perusahaan → (level perusahaan / proyek) → Departemen → Subfolder**.

**Perusahaan yang dikenal (whitelist — satu-satunya folder perusahaan yang valid):** PT Krakatau Shipyard, PT Industri Kapal Nusantara, PT Krakatau Sarana Dockyard, PT Halmahera Shipping, KSO DKB-KS, PT Indonesia Register, PT Lautan Biru Nusantara, PT Lautan Karya Gemilang. Nama lain/singkatan (KS, IKN, KSD, DKB→KSO DKB-KS) dipetakan otomatis; selain itu → `00_Unidentified`. Pihak luar (klien/vendor) tidak jadi folder — namanya disimpan sebagai `counterparty` di log.

**Aturan penempatan departemen:**
- **Legal, Sales & Marketing** → selalu level perusahaan.
- **Engineering, Operasional, HR** → selalu di dalam sebuah proyek (`Projects/[Nama Proyek]/`). Kalau proyek tak teridentifikasi → `_Tanpa Proyek/[Departemen]/`.
- **Finance, IT** → bisa level perusahaan ATAU proyek, tergantung isi dokumen.

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
Salin `.env.example` jadi `.env`, lalu isi:
```
ANTHROPIC_API_KEY=sk-ant-...   # analisis & filing dokumen  → console.anthropic.com
PINECONE_API_KEY=pcsk_...      # vector DB                  → app.pinecone.io
OPENAI_API_KEY=sk-...          # embedding (~$0.0001/1K tok) → platform.openai.com
TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
POPPLER_PATH=...\poppler-26.02.0\Library\bin
```
`TESSERACT_PATH` & `POPPLER_PATH` hanya dipakai kalau ada PDF scan yang perlu OCR.

---

## Cara pakai

### Step 1 — Scan dokumen fisik
Pakai **Microsoft Lens** atau **Adobe Scan** di HP, export sebagai PDF. Tidak perlu rename/sortir — langsung taruh di folder `inbox/`.

### Step 2 — Proses & filing
```bash
python process_docs.py
```

Opsi (bisa digabung):
```bash
-n 10            # proses 10 file pertama saja (mode tes)
-w 10            # jumlah file diproses paralel (default 5)
-r               # proses ULANG file di inbox/_processed/ (tidak dipindah)
-c               # hapus isi output/ + folder_index.json dulu (mulai bersih)
--no-reconcile   # lewati pass penyatuan proyek/subfolder kembar
```
Contoh tes ulang dari nol: `python process_docs.py -r -c -w 10`

Yang terjadi:
1. **Fase 1 — Analisis (paralel):** tiap PDF dibaca. Teks diekstrak dengan `pdfplumber`; kalau PDF berupa scan/gambar, otomatis fallback ke **OCR** (Tesseract + Poppler). Claude (`claude-sonnet-4-5`) menentukan perusahaan, departemen, proyek, subfolder, dan apakah file perlu **dipotong**.
2. **Fase 1.5 — Rekonsiliasi:** satu panggilan Claude menyatukan nama proyek & subfolder yang kembar (ejaan beda → satu nama kanonik), dan memastikan tiap proyek hanya milik satu perusahaan.
3. **Ringkasan** ditampilkan (berapa utuh, dipotong, tak teridentifikasi) lalu minta konfirmasi `y/n`.
4. **Fase 2 — Filing:** dokumen disalin ke `output/` sesuai aturan penempatan. File asli dipindah ke `inbox/_processed/`. `folder_index.json` & `processing_log.json` diperbarui.
5. Di akhir ditampilkan dokumen ber-`expire_date` (pengingat) dan **biaya run** (token + estimasi USD/IDR).

> Aturan split: dokumen yang jelas satu topik (company profile, proposal, laporan tahunan, presentasi, manual, SOP, brosur) **tidak** dipotong. Yang dipotong hanya file berisi campuran dokumen berbeda (mis. invoice + faktur + bank garansi).

### Step 3 — Review manual
Buka `output/`, cek apakah dokumen sudah masuk perusahaan/proyek/subfolder yang benar. Kalau ada yang salah, pindah manual saja.

### Step 4 — (opsional) Rapikan ulang tanpa analisis ulang
Kalau hasil filing perlu disatukan lagi (proyek/subfolder masih terpecah) **tanpa** bayar analisis per-file:
```bash
python refile_output.py
```
Membaca `processing_log.json`, menjalankan rekonsiliasi (2 panggilan Claude), lalu **memindahkan** file yang sudah ada ke struktur yang sudah disatukan. Ada konfirmasi `y/n` sebelum file dipindah.

### Step 5 — Index ke Pinecone
```bash
python index_to_pinecone.py
```
1. Semua PDF di `output/` diekstrak teksnya, dipecah jadi chunk (800 karakter, overlap 100).
2. Tiap chunk di-embed dengan OpenAI `text-embedding-3-small` (1536 dimensi).
3. Di-upsert ke Pinecone index `ks-documents` (dibuat otomatis) lengkap dengan metadata dari `processing_log.json`. File yang sudah di-index akan di-skip.

Setelah ini bisa query natural language (lihat fungsi `search()` di akhir script):
> *"Apakah kita punya sertifikat BKI yang masih berlaku?"*
> *"Cari kontrak proyek kapal yang sudah selesai untuk referensi tender ini"*

---

## Catatan penting

- **PDF scan tanpa teks (image-only):** otomatis di-OCR (`ind+eng`, 180 DPI, maks 8 halaman pertama) selama path OCR benar. Kalau OCR gagal, dokumen tetap difile tapi tanpa teks untuk analisis.
- **Dokumen sangat panjang:** Claude hanya membaca ~10.000 karakter pertama — cukup untuk mendeteksi perusahaan, proyek, dan batas antar dokumen.
- **Paralel:** atur lewat `-w` (default 5). Naikkan untuk lebih cepat, turunkan kalau kena rate limit.
- **Konsistensi antar run:** nama proyek & subfolder diingat di `folder_index.json` dan diumpankan balik ke Claude, supaya tidak terpecah jadi banyak ejaan. `-c` menghapusnya untuk mulai bersih.
- **Re-index:** kalau dokumen diupdate, hapus vector lama dari Pinecone (`index.delete(ids=[...])`) lalu jalankan ulang indexer.

---

## Troubleshooting

**"Tidak ada teks"** → PDF berupa gambar dan OCR gagal. Cek `TESSERACT_PATH`/`POPPLER_PATH`, atau scan ulang pakai Microsoft Lens
**"JSON tidak valid"** → jarang; script otomatis fallback ke `00_Unidentified`
**Semua file → 00_Unidentified** → kemungkinan masalah API key/koneksi; script berhenti otomatis kalau SEMUA file gagal dianalisis
**Filing salah** → pindah manual ke folder yang benar, atau jalankan `refile_output.py`
**Rate limit Anthropic** → turunkan `-w`, atau jalankan ulang (file yang sudah diproses sudah pindah ke `_processed/`)
