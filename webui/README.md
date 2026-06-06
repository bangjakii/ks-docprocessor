# Arsip KS — Web UI

Aplikasi web (React + FastAPI) di atas pipeline yang sudah ada: **cari** dokumen,
**buka file asli**, **statistik arsip**, dan **upload + auto-klasifikasi → filing**.

```
webui/
├── backend/   FastAPI  → reuse search / analyze_pdf / _file_quietly / index
└── frontend/  React+Vite
```

## Prasyarat
- **Python deps:** `pip install -r webui/backend/requirements.txt`
- **Node.js LTS** (untuk frontend): https://nodejs.org
- `.env` (di root repo) sudah berisi `ANTHROPIC_API_KEY` + `PINECONE_API_KEY`.
- **Drive arsip (D:) harus terkonek** — backend baca `archive_log.json` + file asli dari sana.
  Override path: `set ARCHIVE_ROOT=D:\Arsip_Rapih`.

## Jalankan (development)
Dua terminal, dari **root repo**:

```powershell
# Terminal 1 — backend
uvicorn webui.backend.main:app --reload --port 8000

# Terminal 2 — frontend
cd webui\frontend
npm install        # sekali saja
npm run dev        # buka http://localhost:5173
```

## Jalankan (produksi / 1 server untuk tim)
```powershell
cd webui\frontend && npm install && npm run build   # hasil → frontend/dist
cd ..\..
uvicorn webui.backend.main:app --host 0.0.0.0 --port 8000
# buka http://<ip-server>:8000  (frontend disajikan langsung oleh FastAPI)
```

## Catatan
- **Cari & upload-index butuh Pinecone** (jaringan). Statistik & filter dari `archive_log.json` (lokal).
- **Upload:** Claude klasifikasi → kamu **review/koreksi** → konfirmasi → file ke `Arsip_Rapih` + index.
  Tiap file ≈ $0.01–0.05 (Claude). Bundel campuran v1 difile utuh.
- **Akses tim:** mesin yang menjalankan backend harus tetap nyala + drive arsip terkonek.
  Untuk multi-user jangka panjang, pindahkan arsip ke server/NAS dan set `ARCHIVE_ROOT`.
- Belum ada login — tambahkan reverse-proxy + auth bila diekspos ke jaringan luas.
