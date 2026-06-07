# Deploy ke Hostinger VPS → arsip.krakataushipyard.com

Target: app diakses tim dari internet di **https://arsip.krakataushipyard.com**, login
dibatasi **Google Workspace** domain `krakataushipyard.com`.

```
Internet ──HTTPS──> Caddy(:443)  ──> oauth2-proxy(:4180, login Google Workspace)
                     (auto cert)        └──> uvicorn(:8000) ── FastAPI + frontend/dist
                                                              └── /opt/arsip-data (Arsip_Rapih)
                                                              └── Pinecone (cloud) + Claude (cloud)
```

---

## 0. Beli VPS + arahkan subdomain
- **VPS Hostinger (KVM):** minimal **4 vCPU / 16 GB / 200 GB**, OS **Ubuntu 24.04**.
  (8 vCPU bikin indexing 23j → ~12j. Disk ≥100GB: 43GB arsip + OS + ruang kerja.)
- **DNS** (hPanel → krakataushipyard.com → DNS): tambah **A record**
  `arsip` → `<IP_VPS>`. Tunggu propagasi (cek `ping arsip.krakataushipyard.com`).
- **Firewall VPS:** buka **80, 443, 22** saja. Port 8000/4180 tetap localhost.

## 1. Setup server (SSH sebagai root)
```bash
apt update && apt install -y git python3-pip python3-venv tmux \
    poppler-utils tesseract-ocr tesseract-ocr-ind
curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && apt install -y nodejs

git clone https://github.com/bangjakii/ks-docprocessor.git /opt/arsip-app
cd /opt/arsip-app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r webui/backend/requirements.txt
```
> Di Linux poppler & tesseract ada di PATH → **JANGAN** set `POPPLER_PATH`/`TESSERACT_PATH`.

**Buat `.env`** di `/opt/arsip-app/.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
PINECONE_API_KEY=...
ARCHIVE_ROOT=/opt/arsip-data
```

**Build frontend** (disajikan langsung oleh FastAPI, satu port):
```bash
cd webui/frontend && npm install && npm run build && cd ../..
```

## 2. Upload Arsip_Rapih (43 GB, sekali)
Dari laptop (Git Bash / WSL, `rsync` bisa resume kalau putus):
```bash
rsync -avP "/d/Arsip_Rapih/" root@<IP_VPS>:/opt/arsip-data/
```
(atau `scp -r "D:\Arsip_Rapih" root@<IP_VPS>:/opt/arsip-data`). ~5–10 jam.

## 3. Indexing di VPS (sekali, ~12 jam, resumable)
```bash
cd /opt/arsip-app && . .venv/bin/activate
tmux new -s index
python index_to_pinecone.py --dest /opt/arsip-data
#   detach: Ctrl+b lalu d   |   balik: tmux attach -t index
```
Pinecone & Claude lancar dari VPS. Resumable (checkpoint `pinecone_indexed.json`).

## 4. Backend jadi service (auto-restart)
`/etc/systemd/system/arsip.service`:
```ini
[Unit]
Description=Arsip KS backend
After=network.target

[Service]
WorkingDirectory=/opt/arsip-app
Environment=ARCHIVE_ROOT=/opt/arsip-data
ExecStart=/opt/arsip-app/.venv/bin/uvicorn webui.backend.main:app --host 127.0.0.1 --port 8000
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```
```bash
systemctl enable --now arsip && systemctl status arsip
```

## 5. Login Google Workspace (oauth2-proxy) + HTTPS (Caddy)

**5a. Google OAuth client** (Google Cloud Console, project di org Workspace lo):
- APIs & Services → **OAuth consent screen** → **Internal** (cuma user org).
- **Credentials** → Create **OAuth client ID** → *Web application*.
- Authorized redirect URI: `https://arsip.krakataushipyard.com/oauth2/callback`
- Simpan **Client ID** + **Client Secret**.

**5b. oauth2-proxy:**
```bash
# unduh rilis oauth2-proxy ke /usr/local/bin/oauth2-proxy (lihat github.com/oauth2-proxy)
COOKIE=$(openssl rand -base64 32)   # simpan
```
`/etc/oauth2-proxy.cfg`:
```
provider        = "google"
email_domains   = ["krakataushipyard.com"]   # HANYA email @krakataushipyard.com
client_id       = "<CLIENT_ID>"
client_secret   = "<CLIENT_SECRET>"
cookie_secret   = "<COOKIE>"
cookie_secure   = true
http_address    = "127.0.0.1:4180"
upstreams       = ["http://127.0.0.1:8000"]
redirect_url    = "https://arsip.krakataushipyard.com/oauth2/callback"
```
Jadikan service systemd juga (mirip langkah 4, ExecStart `oauth2-proxy --config /etc/oauth2-proxy.cfg`).

**5c. Caddy (auto-HTTPS Let's Encrypt):**
```bash
apt install -y caddy
```
`/etc/caddy/Caddyfile`:
```
arsip.krakataushipyard.com {
    reverse_proxy 127.0.0.1:4180
}
```
```bash
systemctl restart caddy
```
Caddy otomatis ambil sertifikat HTTPS. Buka **https://arsip.krakataushipyard.com** →
diarahkan login Google → hanya akun `@krakataushipyard.com` yang masuk. ✅

---

## Operasional
- **Update kode:** `cd /opt/arsip-app && git pull && . .venv/bin/activate && pip install -r requirements.txt && (cd webui/frontend && npm install && npm run build) && systemctl restart arsip`
- **Upload dari UI** otomatis ter-file ke `/opt/arsip-data` + ter-index (folder_index di-generate dari arsip existing → reuse proyek/subfolder).
- **Backup:** sync `/opt/arsip-data` ke Google Drive/storage lain berkala (jangan andalkan 1 disk).
- **Re-index ulang dari nol:** `python index_to_pinecone.py --dest /opt/arsip-data --reset-checkpoint`.
