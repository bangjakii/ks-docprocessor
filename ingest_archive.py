"""
Ingest arsip terstruktur — DRY-RUN ANALYZER (read-only).
=========================================================
Menyusuri pohon folder arsip (mis. D:\\) dan, untuk SETIAP PDF, menebak
penempatan (perusahaan / departemen / proyek / subfolder) HANYA dari nama
folder + nama file — TANPA membaca isi PDF, TANPA memanggil Claude, TANPA
memindahkan file apa pun.

Tujuan: melihat seberapa jauh "path-first" bisa menempatkan dokumen sebelum
disambungkan ke pipeline. Output:
  - ringkasan di konsol (per perusahaan + distribusi keyakinan)
  - daftar token folder yang TIDAK dikenali (buat memperkaya kosakata)
  - file rincian  archive_plan.jsonl  (satu baris per PDF) untuk diperiksa

Pakai:
    python ingest_archive.py --root "D:\\" [--limit N] [--only "PT. KRAKATAU SHIPYARD"]

Keyakinan:
  HIGH  = perusahaan + departemen terbaca dari path  → kandidat SKIP-Claude
  MED   = salah satu terbaca (perusahaan ATAU departemen)
  LOW   = tidak ada yang terbaca                      → perlu analisis isi
"""

import argparse
import contextlib
import io
import json
import logging
import os
import re
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

import process_docs as P
import refile_output as R

logging.getLogger("pypdf").setLevel(logging.ERROR)     # jangan banjiri warning PDF rusak

# Bundel campur (invoice+faktur+PO) hampir selalu di Finance/Operasional & multi-halaman.
# File HIGH yang cocok kriteria ini dicek-isi supaya bisa dipecah per-dokumen.
SPLIT_PRONE_DEPTS = {"Finance", "Operasional"}
SPLIT_MIN_PAGES   = 6

# Ekstensi DOKUMEN yang ikut ditata (Office + CAD + gambar). Sisanya (software, font,
# arsip .zip/.rar, .psd/.cdr/.ai, media, sistem) DILEWATI sebagai sampah.
DOC_EXTS = {
    ".pdf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".csv",   # Office
    ".dwg", ".dxf",                                                       # CAD
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp",                     # gambar
}

try:
    from tqdm import tqdm
except ImportError:                       # fallback tanpa progress bar
    def tqdm(x=None, **k): return x if x is not None else iter(())

# ── Top-level yang dilewati (sistem / bukan dokumen / hasil tool sendiri) ─────
# PENTING: "Arsip_Rapih" = folder TUJUAN; jangan pernah men-scan output sendiri
# (kalau tidak, hasil run lama ke-scan ulang → angka & klasifikasi ngaco).
SKIP_TOP = {
    "$RECYCLE.BIN", "System Volume Information", "found.000", "autorun.inf",
    "Arsip_Rapih", "_Arsip_Rapih", "Arsip Rapih",
}

# ── JENIS DOKUMEN → (departemen, subfolder kanonik) ───────────────────────────
# Satu match menentukan DEPARTEMEN + nama SUBFOLDER bersih (jenis dokumen). Subfolder
# BUKAN nama item/alat (Bow Thruster) — item/vendor itu metadata. Dicek per-segmen
# (dalam→dangkal); URUTAN = prioritas (spesifik & tak-ambigu dulu).
DOCTYPE_RULES = [
    # (keywords, department, subfolder_kanonik)
    # — HR (spesifik dulu supaya "sertifikat keahlian/keamanan" tak ketabrak sertifikat lain) —
    (["tenaga ahli", "tenaga teknis", "tenaga kerja", "daftar tenaga", "curriculum vitae",
      "riwayat hidup", "biodata"],                          "HR", "Tenaga Ahli"),
    (["sertifikat keahlian", "sertifikat keamanan", "sertifikasi keamanan",
      "sertifikat keselamatan", "keselamatan kerja", " k3 ", "ijazah"], "HR", "Sertifikasi Personil"),
    (["sdm", "personil", "personalia", "kepegawaian", "karyawan", "pegawai", "ktp direksi",
      "skck", "gaji", "absensi"],                           "HR", "Kepegawaian"),
    # — Finance —
    (["invoice", "tagihan"],                                "Finance", "Invoice"),
    (["faktur", "pajak"],                                   "Finance", "Faktur & Pajak"),
    (["purchase order", " po ", "po-", "pesanan pembelian", "pesanan pembelan"], "Finance", "Purchase Order"),
    (["bank garansi", "garansi bank"],                      "Finance", "Bank Garansi"),
    (["memo pembayaran", "permohonan pembayaran", "pembayaran", "pelunasan", "kwitansi", "termin"],
                                                            "Finance", "Pembayaran"),
    (["laporan keuangan", "lapkeu", "neraca", "cash flow", "arus kas", "anggaran", " rab ",
      "rekening", " aset", "asset"],                        "Finance", "Laporan Keuangan & Aset"),
    # — Sales —
    (["penawaran", "penaawaran", "harga", "quotation", "spph"], "Sales", "Penawaran Harga"),
    (["tender", "lelang", "prakualifikasi", "kualifikasi", "sampul", "lpse", "rks", " bid "],
                                                            "Sales", "Tender"),
    # — Engineering (termasuk sertifikat material/alat) —
    (["gambar teknik", "gambar", "drawing", "general arrangement", " ga ", "key plan",
      "lines plan", "midship"],                             "Engineering", "Gambar"),
    (["spesifikasi", "spesifikasi teknis", " spek", "calculation", "perhitungan",
      "network planning", "ships particular", "principal dimension"], "Engineering", "Spesifikasi & Perhitungan"),
    (["mill certificate", "material certificate", "class certificate", "type approval",
      "sertifikat material", "sertifikat bahan", " bki ", "certificate", "sertifikat"],
                                                            "Engineering", "Sertifikat Material"),
    (["rancangan", "desain", "design", "piping", "machinery", "hull", "outfitting", "fmea",
      "teknis", "teknik"],                                  "Engineering", "Teknis"),
    # — Operasional —
    (["bast", "berita acara", "serah terima"],              "Operasional", "BAST"),
    (["surat perintah kerja", " spk", "work order"],        "Operasional", "SPK"),
    (["pengajuan barang", "permintaan barang", "permintaan material", "purchase requisition"],
                                                            "Operasional", "Pengajuan Barang"),
    (["surat jalan", "delivery order", "tanda terima barang", "bukti terima barang"],
                                                            "Operasional", "Pengiriman"),
    (["laporan realisasi", "laporan pekerjaan", "laporan progres", "laporan kemajuan",
      "progress report", "kemajuan pekerjaan", "realisasi pekerjaan", "monitoring"],
                                                            "Operasional", "Laporan Progres"),
    (["jadwal", "kalender", "time schedule", "kurva s"],    "Operasional", "Jadwal"),
    (["vendor", "supplier", "rekanan"],                     "Operasional", "Vendor"),
    (["pengadaan", "material", "logistik", "logistic", " spb", "pengiriman", "delivery",
      "gudang", "bongkaran", "mobilisasi", " stock", "persiapan pembangunan", "produksi"],
                                                            "Operasional", "Pengadaan"),
    # — Marketing — ("marketing"/"maketing" DIBUANG: cocok folder "MAKETING TENDER" → tender
    #   salah jadi Marketing. Pakai frasa spesifik dokumen marketing saja.)
    (["company profile", "profil perusahaan", "brosur", "presentasi perusahaan",
      "katalog perusahaan", "profil singkat perusahaan"], "Marketing", "Company Profile"),
    # — Legal (dokumen badan usaha) —
    (["akta", "akte", "notaris", "pengesahan"],             "Legal", "Akta"),
    (["nib", "siup", "situ", " tdp", "izin usaha", "perizinan", "perijinan", "ijin", "kbli",
      "domisili", "amdal", "imb", " ho ", "lisensi", "license"], "Legal", "Legalitas & Izin"),
    (["kontrak", "perjanjian", "mou"],                      "Legal", "Kontrak"),
    (["sertifikat tanah", "tanah", "pelepasan"],            "Legal", "Tanah"),
    (["npwp"],                                              "Legal", "NPWP"),
    (["legalitas", "legal", "hukum"],                       "Legal", "Legalitas & Izin"),
    # — IT —
    (["website", " web ", "aplikasi", "software", "sistem informasi"], "IT", "Sistem & Aplikasi"),
]

# ── Proyek KANONIK ────────────────────────────────────────────────────────────
# Daripada memungut teks folder mentah (yang menarik nama orang/jenis-dokumen/komponen
# jadi "proyek"), kita cocokkan path ke pola proyek NYATA. Tiap entri: (semua keyword
# harus ada di path) → nama proyek bersih. Urutan = spesifik dulu. Yang tak cocok pola
# mana pun → BUKAN proyek (None) → masuk _Tanpa Proyek / level perusahaan, bukan folder sampah.
# Keyword dengan spasi (mis. " fd ", " gt ") = harus token utuh.
PROJECT_CANON = [
    (["perintis", "2000"],          "Kapal Perintis 2000 GT"),
    (["kontainer", "100"],          "Kapal Kontainer 100 TEUS"),
    (["container", "100"],          "Kapal Kontainer 100 TEUS"),
    (["kontainer", "teus"],         "Kapal Kontainer 100 TEUS"),
    (["container", "teus"],         "Kapal Kontainer 100 TEUS"),
    (["kontainer paket n"],         "Kapal Kontainer 100 TEUS"),
    (["pertamina", "17500"],        "Tanker Pertamina 17500 DWT"),
    (["fighting craft"],            "Patroli Pertamina"),
    (["rigid buoyant"],             "Patroli Pertamina"),
    (["buoyant boat"],              "Patroli Pertamina"),
    (["fpv"],                       "Patroli FPV"),
    (["patroli"],                   "Patroli FPV"),
    (["floating dock"],             "Floating Dock"),
    ([" fd "],                      "Floating Dock"),
    (["fd-"],                       "Floating Dock"),
    (["rehabilitasi dock"],         "Rehabilitasi Dock Surabaya"),
    (["pelra"],                     "Kapal Pelra (Kayu)"),
    (["50 penumpang"],              "Kapal 50 Penumpang"),
    (["20 penumpang"],              "Kapal 20 Penumpang"),
    (["kpdt"],                      "Tender KPDT"),
    (["bus air"],                   "Bus Air Danau Toba"),
    (["100 penumpang"],             "Bus Air Danau Toba"),
    (["rivercat"],                  "RiverCAT RoRo 150 PAX"),
    ([" roro "],                    "Kapal RoRo"),
    (["ro ro"],                     "Kapal RoRo"),
    (["suction dredger"],           "Suction Dredger Boat"),
    (["dredger"],                   "Suction Dredger Boat"),
    (["isap lumpur"],               "Kapal Isap Lumpur"),
    (["replating"],                 "Replating MT Pelita"),
    (["mt pelita"],                 "Replating MT Pelita"),
    (["ponton"],                    "Ponton"),
    (["tongkang"],                  "Tongkang"),
    (["deck barge"],                "Tongkang"),
    (["tugboat"],                   "Tugboat"),
    (["tug boat"],                  "Tugboat"),
    (["docking"],                   "Docking & Reparasi Kapal"),
]

# ── Pola junk (folder aset/software, bukan dokumen) ───────────────────────────
JUNK_HINTS = [
    "template", "font", "autocad", "coreldraw", "corel", "adobe", "photoshop",
    "joomla", "flash", "graphics.suite", "nope", "installer", "setup",
]

# ── Propagasi perusahaan per-tender ───────────────────────────────────────────
# Top-level yang isinya tender lintas-perusahaan: file unid mewarisi perusahaan
# dari file lain dalam tender yang sama (>1 perusahaan grup → KSO, aturan JV).
TENDER_TOPS = {"MAKETING TENDER"}
# Default manual untuk PROGRAM tender yang nol sinyal perusahaan di path.
# (KPDT 2016 terbukti KS; folder KS juga punya "Tender KPDT" → KPDT = tender KS.)
PROGRAM_DEFAULTS = {
    ("MAKETING TENDER", "TENDER KPDT"): "PT Krakatau Shipyard",
}


def tender_group_key(top: str, segments: list):
    """Kelompok tender = top + s/d 2 segmen pertama (program + varian)."""
    return tuple([top] + segments[:2])


def propagate_tender_company(plan: list) -> dict:
    """
    Pass kedua: tularkan perusahaan dalam satu grup tender ke file yang unid.
    Mutasi plan in-place; set row['src'] = 'propagasi'/'default'/'path'.
    Return statistik.
    """
    stats = Counter()
    groups = defaultdict(list)
    for r in plan:
        if r["top"] in TENDER_TOPS:
            groups[tender_group_key(r["top"], r["segments"])].append(r)

    for key, rows in groups.items():
        signals = Counter(r["company"] for r in rows if r["company"] != P.UNIDENTIFIED)
        if len(signals) > 1:
            owner = "KSO DKB-KS"                     # >1 perusahaan grup → JV
        elif len(signals) == 1:
            owner = next(iter(signals))
        else:
            # nol sinyal → coba default per-program
            owner = PROGRAM_DEFAULTS.get((key[0], key[1] if len(key) > 1 else None))
        if not owner:
            continue
        is_default = len(signals) == 0
        for r in rows:
            if r["company"] != owner:
                # JV: minoritas (mis. KS partner) ditarik ke KSO juga
                if r["company"] == P.UNIDENTIFIED or len(signals) > 1:
                    r["company"] = owner
                    r["src"] = "default" if is_default else "propagasi"
                    stats["default" if is_default else "propagasi"] += 1
    return stats


def norm(s: str) -> str:
    return P._normalize_name(s)


def company_from_path(segments: list) -> tuple:
    """
    Tebak perusahaan dari segmen path (dangkal→dalam). Strict: exact/alias/
    akronim/substring — TANPA fuzzy. Return (canon_or_UNIDENTIFIED, segmen_pemicu_or_None).
    """
    for seg in segments:
        n = norm(seg)
        if not n:
            continue
        toks = set(n.split())
        # KSO diprioritaskan: 'dkb'/'kso' kuat menandakan KSO DKB-KS (jangan ketabrak 'ks').
        if "dkb" in toks or "kso" in toks or "kodja" in n:
            return "KSO DKB-KS", seg
        if n in P._CANON_NORM:
            return P._CANON_NORM[n], seg
        if n in P._ALIAS_NORM:
            return P._ALIAS_NORM[n], seg
        # akronim panjang dulu (ikn, ksd) baru pendek (ks)
        for key in ("ikn", "ksd"):
            if key in toks:
                return P._ALIAS_NORM[key], seg
        # multiword alias/canon sebagai substring
        for key, canon in P._ALL_KEYS.items():
            if " " in key and (key in n or n in key):
                return canon, seg
        if "ks" in toks:
            return "PT Krakatau Shipyard", seg
    return P.UNIDENTIFIED, None


def classify_doctype(segments: list):
    """
    Tebak (departemen, subfolder-kanonik, segmen-pemicu) dari JENIS dokumen di path
    (dalam→dangkal, prioritas DOCTYPE_RULES). Tak cocok → (None, None, None).
    """
    for seg in reversed(segments):
        n = f" {norm(seg)} "
        for kws, dept, sub in DOCTYPE_RULES:
            if any(k in n for k in kws):
                return dept, sub, seg
    return None, None, None


# Penanda konteks PENGADAAN — dipakai untuk mengelompokkan subfolder per ITEM (Plat Baja,
# Pompa) supaya tiap departemen tetap terkelompok per item. (Dept TETAP per jenis dokumen.)
PROCUREMENT_MARKERS = ["pengadaan", "purchasing", " material ", "pesanan pembelian",
                       "pembelian", "vendor", "supplier"]

def _strip_num(s: str) -> str:
    """Buang penomoran depan: '1. ', 'II. ', 'A. ' → sisa nama."""
    return re.sub(r"^\s*([ivxlcdm]+|[a-z]|\d+)[\.\)]\s+", "", s, flags=re.I).strip()


_VENDOR_RE = re.compile(r"^(pt|cv|ud|pd|fa|toko|koperasi|kop)[\.\s]", re.I)

def detect_vendor(segments: list):
    """Nama VENDOR (pihak luar) dari path — segmen ber-prefix PT/CV/UD/dll yang BUKAN
    perusahaan grup. Dalam→dangkal (vendor biasanya lebih dalam). None kalau tak ada."""
    for seg in reversed(segments):
        s = _strip_num(seg)
        if _VENDOR_RE.match(s) and company_from_path([seg])[0] == P.UNIDENTIFIED:
            return s
    return None


def procurement_item(segments: list):
    """
    Kalau path konteks PENGADAAN, kembalikan nama ITEM-nya (folder setelah wrapper
    pengadaan/material/proyek) supaya semua dok item dikumpulkan di Operasional/<Item>.
    Return: nama item (str), atau "" kalau pengadaan tapi item tak jelas, atau None kalau
    bukan konteks pengadaan.
    """
    blob = " " + " ".join(norm(s) for s in segments) + " "
    if not any(m in blob for m in PROCUREMENT_MARKERS):
        return None
    start = 0
    for i, s in enumerate(segments):
        n = f" {norm(s)} "
        if (" pengadaan " in n or " material " in n or " purchasing " in n
                or "pengadaan" in norm(s) or project_from_path([s])[0]):
            start = i + 1
    if 0 < start < len(segments):
        return _strip_num(segments[start])     # ITEM = folder pertama setelah wrapper
    return ""


def project_from_path(segments: list, *_) -> tuple:
    """
    Cocokkan path ke proyek KANONIK. Cuma folder (BUKAN nama file) yang dipertimbangkan.
    Return (nama_proyek_kanonik_or_None, None). Tak cocok pola → None (bukan folder sampah).
    """
    blob = " " + " ".join(norm(s) for s in segments) + " "
    for keys, canon in PROJECT_CANON:
        if all(k in blob for k in keys):
            return canon, None
    return None, None


def is_junk(segments: list) -> bool:
    for seg in segments:
        n = norm(seg)
        if any(h in n for h in JUNK_HINTS):
            return True
    return False


def score(row) -> str:
    if row["junk"]:
        return "JUNK"
    cf = row["company"] != P.UNIDENTIFIED
    df = row["department"] is not None
    return "HIGH" if cf and df else "MED" if (cf or df) else "LOW"


def analyze(root: Path, only: str = None, limit: int = None):
    plan = []
    unknown_tokens = Counter()
    n_seen = 0

    tops = [only] if only else [d for d in sorted(os.listdir(root))
                                if d not in SKIP_TOP and (root / d).is_dir()]
    # ── Pass 1: parse tiap PDF dari path ──────────────────────────────────────
    for top in tops:
        for dp, dirs, files in os.walk(root / top):
            dirs.sort()
            for f in files:
                if Path(f).suffix.lower() not in DOC_EXTS:
                    continue
                if limit and n_seen >= limit:
                    break
                n_seen += 1
                full = Path(dp) / f
                rel_parts = full.relative_to(root).parts          # (top, ..., file.pdf)
                segments = list(rel_parts[:-1])                    # folder saja
                stem = Path(f).stem

                company, cseg = company_from_path(segments)
                project, _ = project_from_path(segments)      # folder saja, BUKAN nama file
                # Departemen + subfolder KANONIK (jenis dokumen) dari path/nama file.
                dept, doc_sub, dseg = classify_doctype(segments + [stem])
                subfolder = doc_sub or (segments[-1] if segments else "Umum")
                # OPERASIONAL diorganisir per VENDOR (item di bawah vendor) sesuai taxonomy;
                # kalau tak ada vendor → kelompokkan per ITEM; BAST/Jadwal tetap jenis dokumen.
                if dept == "Operasional":
                    vendor = detect_vendor(segments)
                    item   = procurement_item(segments)
                    if vendor:
                        subfolder = f"{vendor}/{item}" if item else vendor
                    elif doc_sub == "Pengadaan" and item:
                        subfolder = item

                plan.append({
                    "path": str(full), "top": top, "segments": segments[1:],
                    "company": company, "department": dept, "project": project,
                    "subfolder": subfolder,
                    "junk": is_junk(segments), "src": "path", "cseg": cseg, "dseg": dseg,
                })
            if limit and n_seen >= limit:
                break

    # ── Pass 2: propagasi perusahaan per-tender ───────────────────────────────
    prop_stats = propagate_tender_company(plan)

    # ── Pass 3: skor + agregasi ───────────────────────────────────────────────
    per_company, per_conf = Counter(), Counter()
    for r in plan:
        r["confidence"] = score(r)
        per_company[r["company"]] += 1
        per_conf[r["confidence"]] += 1
        if r["confidence"] in ("LOW", "MED"):
            for seg in r["segments"]:
                if seg not in (r["cseg"], r["dseg"]):
                    for tok in norm(seg).split():
                        if len(tok) >= 4:
                            unknown_tokens[tok] += 1
        for k in ("segments", "junk", "cseg", "dseg", "top"):
            r.pop(k, None)

    return plan, per_company, per_conf, unknown_tokens, n_seen, prop_stats


# ── Mode APPLY: benar-benar menata file (SALIN) ke struktur bersih ────────────

def analysis_from_path(row: dict) -> dict:
    """Sintesis hasil analisis untuk file HIGH — langsung dari tebakan path, tanpa Claude."""
    proj = P.normalize_project(row.get("project"))
    return {
        "company": row["company"], "counterparty": None,
        "department": row["department"] or "Lainnya",
        "scope": "project" if proj else "company",
        "project": proj, "subfolder": row.get("subfolder") or "Umum",
        "should_split": False,
        "filename_out": Path(row["path"]).name,     # pertahankan nama asli arsip
    }


# Checkpoint di C: (BUKAN di D:) supaya catatan progres selamat kalau disk lepas.
STATE_FILE = Path("archive_ingest_state.jsonl")


def _file_nonpdf(src: Path, analysis: dict, index, lock):
    """Salin file NON-PDF (Office/CAD/gambar) ke folder tujuan dari path — nama & ekstensi asli."""
    try:
        company = P.sanitize(analysis.get("company") or P.UNIDENTIFIED)
        with contextlib.redirect_stdout(io.StringIO()):
            dest = P.resolve_destination(company, analysis, index, lock, canon_project=True)
        out_path = P.unique_path(dest["out_dir"] / src.name)   # pertahankan nama+ekstensi asli
        shutil.copy2(src, out_path)
        return [{"source_file": src.name, "doc_name": src.stem,
                 "company": company, "counterparty": None,
                 "department": dest["department"], "project": dest["project"],
                 "subfolder": dest["subfolder"], "relpath": dest["relpath"],
                 "output_file": str(out_path), "split": False,
                 "expire_date": None, "doc_number": None,
                 "status": "ok", "processed_at": datetime.now().isoformat()}]
    except Exception as e:
        return [{"source_file": src.name, "status": "error",
                 "error": str(e), "company": analysis.get("company")}]


def _file_quietly(pdf_path, analysis, index, lock):
    """File satu dokumen tanpa output ramai. PDF → file_document (bisa split); non-PDF → salin utuh."""
    src = Path(pdf_path)
    if src.suffix.lower() != ".pdf":
        return _file_nonpdf(src, analysis, index, lock)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return P.file_document(src, analysis, index, lock, canon_project=True)
    except Exception as e:
        return [{"source_file": src.name, "status": "error",
                 "error": str(e), "company": analysis.get("company")}]


def _load_checkpoint(dest: Path):
    """Kembalikan (logs_sebelumnya, set_source_path_yang_sudah_ok) dari run sebelumnya."""
    logs, done = [], set()
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue                              # baris korup (crash di tengah) → abaikan
            if e.get("_dest") != str(dest):
                continue                              # checkpoint untuk dest lain
            logs.append(e)
            if e.get("status") == "ok" and e.get("source_path"):
                done.add(e["source_path"])
    return logs, done


def reconcile_dest(dest: Path, logs: list, index: dict):
    """Fase C: satukan nama PROYEK kembar, lalu pindahkan. Subfolder TIDAK direkonsiliasi —
    sudah kanonik dari classify_doctype, kalau di-rename Claude malah jadi salah."""
    entries = [l for l in logs if l.get("status") == "ok" and l.get("output_file")
               and l.get("relpath") and Path(l["output_file"]).exists()]
    if not entries:
        return
    projects, _ = R.build_inputs(entries)
    print(f"\n  ▶ Fase C: rekonsiliasi {len(projects)} nama proyek (Claude)...")
    proj_map, sub_map, proj_ok = P.reconcile_with_claude(projects, {})   # {} = skip subfolder
    if not proj_ok:
        print("    ⚠ Rekonsiliasi proyek gagal — struktur mentah dipertahankan.")
        return

    moved = 0
    for e in entries:
        comp = e.get("company") or P.UNIDENTIFIED
        proj = P.normalize_project(e.get("project"))
        dept = e.get("department") or "Lainnya"
        sub  = e.get("subfolder") or "Umum"
        new_comp, new_proj = comp, proj
        if proj and (comp, proj) in proj_map:
            new_comp, new_proj = proj_map[(comp, proj)]
        new_proj = P.normalize_project(new_proj)
        new_sub  = sub_map.get((dept, sub), sub)
        rel = R.build_relpath(dept, new_sub, new_proj)        # aturan Korporat/Proyek
        old_path = Path(e["output_file"])
        new_path = dest / P.sanitize(new_comp) / Path(rel) / old_path.name
        # daftar folder proyek hanya kalau dokumen benar2 ditaruh di cabang Proyek
        _, _kind, _pf = P.placement_relpath(dept, "proyek" if new_proj else "korporat", new_proj)
        if _pf:
            P.register_project(index, P.sanitize(new_comp), P.sanitize(_pf))
        P.register_subfolder(index, P.sanitize(new_comp), rel)
        if old_path.resolve() != new_path.resolve():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            d = P.unique_path(new_path)
            shutil.move(str(old_path), str(d))
            new_path = d
            moved += 1
        e["company"]   = P.sanitize(new_comp)
        e["project"]   = P.sanitize(new_proj) if new_proj else None
        e["subfolder"] = P.sanitize(new_sub)
        e["relpath"]   = rel
        e["output_file"] = str(new_path)

    for d in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"    ✓ {moved} file dipindah ke struktur kanonik.")


def _page_count_quiet(path):
    try:
        with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn), contextlib.redirect_stdout(dn):
            return P.get_page_count(path)
    except Exception:
        return 0


def apply_plan(root: Path, dest: Path, plan: list, workers: int, analyze_uncertain: bool,
               reconcile: bool = True, split_check_enabled: bool = True):
    from threading import Event
    P.OUTPUT_DIR = dest                              # arahkan filing ke folder rapi baru
    fidx = dest / "folder_index.json"                # lanjutkan index lama biar konsisten
    index = json.loads(fidx.read_text(encoding="utf-8")) if fidx.exists() else {}
    lock, state_lock, abort = Lock(), Lock(), Event()

    prev_logs, done = _load_checkpoint(dest)
    logs = list(prev_logs)
    sf = STATE_FILE.open("a", encoding="utf-8")       # append — checkpoint inkremental

    def record(entries, src):
        """Tandai source + dest, tulis ke checkpoint (C:) seketika, kumpulkan ke logs."""
        with state_lock:
            for e in entries:
                e["source_path"], e["_dest"] = str(src), str(dest)
                sf.write(json.dumps(e, ensure_ascii=False) + "\n")
                logs.append(e)
            sf.flush()

    def alive():
        if root.exists() and dest.parent.exists():
            return True
        abort.set()
        return False

    high = [r for r in plan if r["confidence"] == "HIGH" and r["path"] not in done]
    unc  = [r for r in plan if r["confidence"] in ("MED", "LOW") and r["path"] not in done]
    junk = [r for r in plan if r["confidence"] == "JUNK"]
    skipped = len(done)
    if skipped:
        print(f"\n  ↻ Lanjut dari checkpoint: {skipped} file sudah beres → dilewati.")

    is_pdf = lambda r: r["path"].lower().endswith(".pdf")

    # ── Gerbang split: HIGH PDF di Finance/Operasional & multi-halaman → cek-isi ─
    split_check = []
    if analyze_uncertain and split_check_enabled:
        prone = [r for r in high if r["department"] in SPLIT_PRONE_DEPTS and is_pdf(r)]
        if prone:
            print(f"\n  ⊟ Cek halaman {len(prone)} file Finance/Operasional (deteksi bundel campur)...")
            keep = []
            for r in tqdm(prone, desc="    cek-hlm", unit="f"):
                (split_check if _page_count_quiet(r["path"]) >= SPLIT_MIN_PAGES else keep).append(r)
            for r in split_check:
                r["_splitcheck"] = True       # HIGH → klasifikasi path tetap acuan kalau tak dipecah
            prone_set = {id(r) for r in prone}
            high = [r for r in high if id(r) not in prone_set] + keep
            print(f"    → {len(split_check)} bundel kandidat dialihkan ke analisis-isi (bisa dipecah).")

    # PDF ragu → analisis isi (Claude); non-PDF ragu → parkir (tak bisa dibaca isinya).
    if analyze_uncertain:
        content_jobs = [r for r in unc if is_pdf(r)] + split_check
        park_jobs    = [r for r in unc if not is_pdf(r)]
    else:
        content_jobs, park_jobs = [], unc

    # ── Fase A: HIGH — salin langsung dari path (gratis) ──────────────────────
    print(f"\n  ▶ Fase A: {len(high)} file HIGH → salin langsung dari path (tanpa Claude)")
    for i, r in enumerate(tqdm(high, desc="    nyalin", unit="f")):
        if i % 200 == 0 and not alive():
            break
        record(_file_quietly(r["path"], analysis_from_path(r), index, lock), r["path"])

    # ── Fase B: PDF ragu + bundel kandidat — analisis isi (berbayar) ──────────
    if not abort.is_set() and content_jobs:
        print(f"\n  ▶ Fase B: {len(content_jobs)} PDF → analisis isi "
              f"({len(content_jobs)-len(split_check)} ragu + {len(split_check)} cek-bundel, "
              f"Claude paralel x{workers})")

        def work(r):
            if abort.is_set() or not root.exists():   # disk lepas → jangan bayar Claude
                return None
            rel = str(Path(r["path"]).relative_to(root))
            res = P.analyze_pdf(Path(r["path"]), index, path_hint=rel)
            if not res:                               # gagal baca → parkir pakai tebakan path
                res = analysis_from_path(r)
                res["department"] = r["department"] or "_Perlu Dicek"
            elif r.get("_splitcheck") and not res.get("should_split"):
                res = analysis_from_path(r)           # HIGH & ternyata BUKAN bundel → percaya path
            elif r["company"] != P.UNIDENTIFIED:      # path punya perusahaan yakin → menang
                res["company"] = r["company"]
            return _file_quietly(r["path"], res, index, lock)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, r): r for r in content_jobs}
            for n, fu in enumerate(tqdm(as_completed(futs), total=len(futs),
                                        desc="    analisis", unit="f")):
                out = fu.result()
                if out is not None:
                    record(out, futs[fu]["path"])
                if n % 100 == 0:
                    alive()

    # ── Parkir: file ragu yang tak dianalisis (non-PDF, atau semua jika --no-analyze) ─
    if not abort.is_set() and park_jobs:
        label = "non-PDF ragu" if analyze_uncertain else "file ragu"
        print(f"\n  ▶ Parkir {len(park_jobs)} {label} → _Perlu Dicek (tanpa Claude)")
        for i, r in enumerate(tqdm(park_jobs, desc="    parkir", unit="f")):
            if i % 200 == 0 and not alive():
                break
            a = analysis_from_path(r)
            a["department"], a["scope"], a["project"] = "_Perlu Dicek", "company", None
            record(_file_quietly(r["path"], a, index, lock), r["path"])

    sf.close()

    # ── Fase C: rekonsiliasi nama proyek/subfolder (hanya kalau run tuntas) ────
    if reconcile and not abort.is_set():
        try:
            reconcile_dest(dest, logs, index)
        except Exception as e:
            print(f"    ⚠ Rekonsiliasi error: {e} — struktur mentah dipertahankan.")

    # ── Tulis index + log final ke dest (best-effort — dest bisa hilang) ───────
    try:
        (dest / "folder_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        (dest / "archive_log.json").write_text(
            json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    ok  = sum(1 for l in logs if l.get("status") == "ok")
    err = sum(1 for l in logs if l.get("status") == "error")
    c = P.usage_cost()
    print(f"\n{'='*70}")
    if abort.is_set():
        print(f"  ⚠  DISK TERPUTUS / tujuan hilang — dihentikan dengan aman.")
        print(f"  💾 Progres tersimpan di checkpoint: {ok} file beres sejauh ini.")
        print(f"  ↻  Sambungkan lagi disk D:, lalu JALANKAN ULANG command yang SAMA")
        print(f"     untuk melanjutkan dari titik terakhir (tidak mengulang/bayar ulang).")
    else:
        print(f"  ✅ Selesai menata arsip (SALIN — file asli di {root} tidak disentuh)")
        print(f"  📁 Hasil rapi : {dest.resolve()}")
        print(f"  ℹ  Checkpoint disimpan ({STATE_FILE}); run ulang akan melewati yang sudah"
              f" beres. Pakai --fresh kalau mau menata dari nol.")
    print(f"  📄 Terfile    : {ok} dokumen  ({err} gagal, {len(junk)} junk dilewati)")
    print(f"  💰 Biaya Claude: ${c['usd']:.2f} (≈ Rp {c['idr']:,.0f}), {c['calls']} panggilan")
    print(f"{'='*70}\n")


def dest_segments(row: dict):
    """Path tujuan (di bawah folder perusahaan) untuk sebuah row — seperti saat apply."""
    proj  = P.normalize_project(row.get("project"))
    dept  = row.get("department") or "Lainnya"
    scope = "project" if proj else "company"
    base_rel, _, _ = P.placement_relpath(dept, scope, proj)
    return row["company"], base_rel.split("/") + [row.get("subfolder") or "Umum"]


def print_dest_tree(plan: list, max_depth: int = 3, width: int = 12):
    """Cetak pratinjau pohon folder hasil (perusahaan → ... ) dengan jumlah file."""
    rootnode = {"n": 0, "ch": {}}
    for r in plan:
        if r["confidence"] == "JUNK":
            continue
        company, segs = dest_segments(r)
        cur = rootnode
        for name in [company] + segs:
            cur = cur["ch"].setdefault(name, {"n": 0, "ch": {}})
            cur["n"] += 1

    def show(node, name, indent, depth):
        print(f"    {indent}{name}  [{node['n']}]")
        if depth <= 1 or not node["ch"]:
            return
        kids = sorted(node["ch"].items(), key=lambda kv: -kv[1]["n"])
        for k, v in kids[:width]:
            show(v, k, indent + "  ", depth - 1)
        if len(kids) > width:
            print(f"    {indent}  … (+{len(kids)-width} folder lagi)")

    print(f"\n  ── Pratinjau pohon folder hasil (perusahaan → dept/proyek → subfolder) ──")
    for comp, node in sorted(rootnode["ch"].items(), key=lambda kv: -kv[1]["n"]):
        show(node, comp, "", max_depth)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="D:\\")
    ap.add_argument("--only", default=None, help="batasi ke satu folder top-level")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="archive_plan.jsonl")
    ap.add_argument("--tree-depth", type=int, default=3, help="kedalaman pratinjau pohon")
    ap.add_argument("--tree-width", type=int, default=12, help="maks anak per node di pohon")
    ap.add_argument("--apply", action="store_true",
                    help="benar-benar menata file (SALIN ke --dest). Tanpa ini = preview saja.")
    ap.add_argument("--dest", default="D:\\Arsip_Rapih",
                    help="folder tujuan hasil rapi (default D:\\Arsip_Rapih)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--no-analyze", action="store_true",
                    help="jangan analisis isi file ragu (parkir ke _Perlu Dicek, gratis)")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="lewati Fase C (penyatuan nama proyek/subfolder kembar)")
    ap.add_argument("--no-split-check", action="store_true",
                    help="jangan cek-pecah bundel campur di Finance/Operasional multi-halaman")
    ap.add_argument("--yes", action="store_true", help="lewati konfirmasi y/n")
    ap.add_argument("--fresh", action="store_true",
                    help="abaikan checkpoint & mulai menata dari nol")
    args = ap.parse_args()

    if args.fresh and STATE_FILE.exists():
        STATE_FILE.unlink()

    root = Path(args.root)
    # Jangan pernah scan folder TUJUAN (kalau ada di bawah root) — hindari scan output sendiri.
    try:
        dpath = Path(args.dest).resolve()
        if dpath.parent == root.resolve():
            SKIP_TOP.add(dpath.name)
    except Exception:
        pass
    mode = "APPLY (SALIN)" if args.apply else "DRY-RUN (read-only)"
    print(f"\n{'='*70}\n  Ingest Arsip — {mode}  —  root: {root}")
    if args.only:
        print(f"  Hanya: {args.only}")
    print(f"{'='*70}\n  Menyusuri folder... (tanpa baca isi PDF, tanpa Claude)\n")

    plan, per_company, per_conf, unknown, n, prop = analyze(root, args.only, args.limit)

    n_pdf = sum(1 for r in plan if r["path"].lower().endswith(".pdf"))
    print(f"  Total file dipindai: {n}  ({n_pdf} PDF + {n-n_pdf} non-PDF)")
    print(f"  Propagasi tender   : {prop.get('propagasi',0)} via sinyal grup, "
          f"{prop.get('default',0)} via default program\n")
    print(f"  ── Keyakinan penempatan ──")
    order = ["HIGH", "MED", "LOW", "JUNK"]
    for k in order:
        v = per_conf.get(k, 0)
        pct = (100 * v / n) if n else 0
        bar = "█" * int(pct / 2)
        print(f"    {k:5} {v:6d}  {pct:5.1f}%  {bar}")
    skip = per_conf.get("HIGH", 0)
    pdf_unc = sum(1 for r in plan if r["confidence"] in ("MED", "LOW")
                  and r["path"].lower().endswith(".pdf"))
    nonpdf_unc = (per_conf.get("MED", 0) + per_conf.get("LOW", 0)) - pdf_unc
    print(f"\n  → HIGH (path, gratis): {skip} ({100*skip/n:.1f}%)  |  "
          f"PDF ragu→Claude: {pdf_unc}  |  non-PDF ragu→parkir: {nonpdf_unc}\n")

    print(f"  ── PDF per perusahaan (dari path) ──")
    for comp, v in per_company.most_common():
        print(f"    {v:6d}  {comp}")

    print_dest_tree(plan, max_depth=args.tree_depth, width=args.tree_width)
    unc_n = per_conf.get("MED", 0) + per_conf.get("LOW", 0)
    print(f"\n  ⓘ ~{unc_n} file MED/LOW posisinya TENTATIF di pohon ini — dept/proyeknya"
          f" bisa berubah setelah analisis isi saat --apply.")

    print(f"\n  ── 25 token folder TAK dikenali tersering (buat perkaya kosakata) ──")
    for tok, v in unknown.most_common(25):
        print(f"    {v:5d}  {tok}")

    Path(args.out).write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in plan), encoding="utf-8")
    print(f"\n  📝 Rincian per file → {args.out}  ({len(plan)} baris)")

    if not args.apply:
        print(f"{'='*70}\n  (read-only — tidak ada file yang dipindah)")
        print(f"  Untuk benar-benar menata: tambah --apply  (akan SALIN ke {args.dest})\n")
        return

    # ── Mode APPLY: konfirmasi dulu, baru tata ────────────────────────────────
    dest = Path(args.dest)
    unc = per_conf.get("MED", 0) + per_conf.get("LOW", 0)
    is_pdf = lambda r: r["path"].lower().endswith(".pdf")
    unc_pdf = sum(1 for r in plan if r["confidence"] in ("MED", "LOW") and is_pdf(r))
    est_usd = (0 if args.no_analyze else unc_pdf * 0.0124)
    print(f"\n{'='*70}\n  ⚠  MODE APPLY — akan MENYALIN file ke: {dest}")
    print(f"  • {per_conf.get('HIGH',0)} file HIGH  → salin langsung dari path (gratis)")
    if args.no_analyze:
        print(f"  • {unc} file ragu       → PARKIR ke _Perlu Dicek (gratis)")
    else:
        print(f"  • {unc_pdf} PDF ragu      → analisis isi via Claude (≈ ${est_usd:.2f})")
        print(f"  • {unc-unc_pdf} non-PDF ragu  → PARKIR ke _Perlu Dicek (gratis)")
    print(f"  • {per_conf.get('JUNK',0)} file junk  → dilewati")
    if not args.no_analyze and not args.no_split_check:
        print(f"  • Cek-bundel: file HIGH Finance/Operasional multi-halaman dipecah (≈ $4)")
    if not args.no_reconcile:
        print(f"  • Fase C: rekonsiliasi nama proyek/subfolder kembar (Claude, ≈ $0.10–0.30)")
    print(f"  • File asli di {root} TIDAK disentuh (ini operasi SALIN).")
    print(f"{'─'*70}")
    if not args.yes and input("  Lanjutkan menata (salin) file? (y/n): ").strip().lower() != "y":
        print("\n  ❌ Dibatalkan.\n")
        return
    apply_plan(root, dest, plan, args.workers, analyze_uncertain=not args.no_analyze,
               reconcile=not args.no_reconcile, split_check_enabled=not args.no_split_check)


if __name__ == "__main__":
    main()
