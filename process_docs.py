"""
KS Document Processor — Waralalo Group
=======================================
Pipeline: PDF dari inbox → identifikasi perusahaan + dept → split kalau perlu → filing

Struktur output:
    output/
    └── [Nama Perusahaan]/
        └── [Departemen]/
            └── [Subfolder AI]/
                └── file.pdf
"""

import os
import json
import re
import shutil
import difflib
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import anthropic
import pdfplumber
from pypdf import PdfReader, PdfWriter
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

# ── Konfigurasi ──────────────────────────────────────────────────────────────
INPUT_DIR    = Path("./inbox")
OUTPUT_DIR   = Path("./output")
LOG_FILE     = Path("./processing_log.json")
FOLDER_INDEX = Path("./folder_index.json")

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_WORKERS  = 5   # file diproses paralel — override lewat --workers

# Harga claude-sonnet-4-6 per 1 juta token (USD) — sama dgn 4-5.
PRICE_INPUT_PER_MTOK  = 3.0
PRICE_OUTPUT_PER_MTOK = 15.0
USD_TO_IDR            = 16000   # perkiraan kasar untuk tampilan rupiah

# Akumulator pemakaian token (thread-safe) untuk hitung biaya per run.
USAGE      = {"input": 0, "output": 0, "calls": 0}
USAGE_LOCK = Lock()

def record_usage(response):
    try:
        with USAGE_LOCK:
            USAGE["input"]  += response.usage.input_tokens
            USAGE["output"] += response.usage.output_tokens
            USAGE["calls"]  += 1
    except Exception:
        pass

def usage_cost() -> dict:
    inp, out = USAGE["input"], USAGE["output"]
    usd = inp / 1_000_000 * PRICE_INPUT_PER_MTOK + out / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    return {"input": inp, "output": out, "calls": USAGE["calls"], "usd": usd, "idr": usd * USD_TO_IDR}

UNIDENTIFIED = "00_Unidentified"

# Whitelist — the ONLY valid top-level company folders.
COMPANIES = [
    "PT Krakatau Shipyard",
    "PT Industri Kapal Nusantara",
    "PT Krakatau Sarana Dockyard",
    "PT Halmahera Shipping",
    "PT Indonesia Register",
    "PT Lautan Biru Nusantara",
    "PT Lautan Karya Gemilang",
    "KSO DKB-KS",
]

# Detected name (any spelling/abbrev) → canonical company.
# Note: DKB / Dok Kodja Bahari is NOT a standalone group member — it is the JV partner in KSO DKB-KS.
COMPANY_ALIASES = {
    "ks":                                "PT Krakatau Shipyard",
    "krakatau shipyard":                 "PT Krakatau Shipyard",
    "ikn":                               "PT Industri Kapal Nusantara",
    "industri kapal nusantara":          "PT Industri Kapal Nusantara",
    "ksd":                               "PT Krakatau Sarana Dockyard",
    "krakatau sarana dockyard":          "PT Krakatau Sarana Dockyard",
    "hs":                                "PT Halmahera Shipping",
    "halmahera shipping":                "PT Halmahera Shipping",
    "indonesia register":                "PT Indonesia Register",
    "lbn":                               "PT Lautan Biru Nusantara",
    "lautan biru nusantara":             "PT Lautan Biru Nusantara",
    "lkg":                               "PT Lautan Karya Gemilang",
    "lautan karya gemilang":             "PT Lautan Karya Gemilang",
    "dkb":                               "KSO DKB-KS",
    "kso dkb ks":                        "KSO DKB-KS",
    "dok kodja bahari":                  "KSO DKB-KS",
    "dok dan perkapalan kodja bahari":   "KSO DKB-KS",
    "dok perkapalan kodja bahari":       "KSO DKB-KS",
    "kodja bahari":                      "KSO DKB-KS",
}

DEPARTMENTS = ["Legal", "Marketing", "Finance", "HR", "Sales", "Operasional", "Engineering", "IT"]

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Kanonikalisasi nama perusahaan ────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lowercase, drop 'PT'/'(Persero)', turn '&'→'dan', strip punctuation."""
    s = name.lower().strip()
    s = s.replace("&", " dan ")
    s = re.sub(r"\(persero\)", " ", s)
    s = re.sub(r"\bpt\.?\b", " ", s)        # drop leading PT / PT.
    s = re.sub(r"[^a-z0-9 ]", " ", s)        # strip remaining punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Pre-normalized lookup tables (built once at import).
_CANON_NORM = {_normalize_name(c): c for c in COMPANIES}
_ALIAS_NORM = {_normalize_name(k): v for k, v in COMPANY_ALIASES.items()}
_ALL_KEYS   = {**_CANON_NORM, **_ALIAS_NORM}

def canonicalize_company(name: str, unresolved: list = None) -> str:
    """
    Map any detected company name to a canonical whitelist entry.
    Order: exact → alias → standalone acronym → multi-word substring → fuzzy.
    Anything else → 00_Unidentified (and appended to `unresolved` for visibility).
    """
    if not name or not name.strip():
        return UNIDENTIFIED
    raw  = name.strip()
    norm = _normalize_name(raw)
    if not norm:
        return UNIDENTIFIED

    # 1. exact canonical / alias
    if norm in _CANON_NORM:
        return _CANON_NORM[norm]
    if norm in _ALIAS_NORM:
        return _ALIAS_NORM[norm]

    tokens = set(norm.split())

    # 2. short acronym (ks, ikn, ksd, dkb) appearing as a standalone word
    for key, canon in _ALL_KEYS.items():
        if " " not in key and len(key) <= 4 and key in tokens:
            return canon

    # 3. multi-word key contained in the detected name (or vice versa)
    for key, canon in _ALL_KEYS.items():
        if " " in key and (key in norm or norm in key):
            return canon

    # 4. fuzzy match on the full normalized string
    best = difflib.get_close_matches(norm, list(_ALL_KEYS.keys()), n=1, cutoff=0.84)
    if best:
        return _ALL_KEYS[best[0]]

    # 5. give up — record so a missed alias can be spotted
    if unresolved is not None and raw not in unresolved:
        unresolved.append(raw)
    return UNIDENTIFIED


# ── Penempatan: cabang KORPORAT vs PROYEK ─────────────────────────────────────
# Struktur: [Perusahaan]/Korporat/[Dept]  atau  [Perusahaan]/Proyek/[Proyek]/[Dept]
# Korporat (level perusahaan): Legal, Marketing, Finance, HR.
# Proyek  (per proyek)       : Sales, Operasional, Engineering, HR.
# HR ada di dua-duanya: kalau ada proyek → Proyek/HR, kalau tidak → Korporat/HR.
# Finance SELALU Korporat (keuangan proyek pun ke sini; proyek disimpan sbg metadata).
KORPORAT_FOLDER   = "Korporat"
PROYEK_FOLDER     = "Proyek"
NO_PROJECT_FOLDER = "_Tanpa Proyek"
KORPORAT_DEPTS    = {"Legal", "Marketing", "Finance"}      # selalu Korporat
PROYEK_DEPTS      = {"Sales", "Operasional", "Engineering"} # selalu Proyek (butuh proyek)
# (kompat lama — sebagian kode/test masih merujuk nama ini)
PROJECTS_FOLDER     = PROYEK_FOLDER

# Nilai "proyek" yang sebenarnya BUKAN proyek (AI kadang isi string ini, bukan null).
# Dianggap = tak ada proyek → masuk _Tanpa Proyek, bukan jadi folder proyek sendiri.
# Catatan: "Lain-lain" SENGAJA tidak di sini — itu proyek catch-all yang valid dari rekonsiliasi.
NON_PROJECT_VALUES = {
    "00_unidentified", "unidentified", "tidak diketahui", "tidak teridentifikasi",
    "belum diketahui", "tidak ada", "n/a", "na", "-", "--", "null", "none", "umum",
}

def normalize_project(name) -> str | None:
    """Nama proyek bersih, atau None kalau kosong/placeholder (bukan proyek nyata)."""
    if not name:
        return None
    s = str(name).strip()
    if not s or s.lower() in NON_PROJECT_VALUES:
        return None
    return s

def placement_relpath(department: str, scope: str, project: str):
    """
    Tentukan path relatif (di bawah folder perusahaan) untuk sebuah dokumen.
    Return (base_relpath, kind, project_final). kind ∈ {korporat, proyek}.
    project_final = nama proyek HANYA kalau dokumen ditaruh di folder proyek (untuk daftar
    folder proyek). Metadata proyek dokumen tetap disimpan terpisah oleh resolve_destination.
    """
    dept = department or "Lainnya"
    project = normalize_project(project)   # placeholder ("00_Unidentified" dll) → None
    if dept in KORPORAT_DEPTS:
        return f"{KORPORAT_FOLDER}/{dept}", "korporat", None
    if dept in PROYEK_DEPTS or dept == "HR":
        if project:
            return f"{PROYEK_FOLDER}/{project}/{dept}", "proyek", project
        if dept == "HR":
            return f"{KORPORAT_FOLDER}/HR", "korporat", None   # HR tanpa proyek → Korporat
        return f"{PROYEK_FOLDER}/{NO_PROJECT_FOLDER}/{dept}", "proyek", None
    # IT atau departemen tak dikenal → Korporat
    return f"{KORPORAT_FOLDER}/{dept}", "korporat", None


# ── Folder index (memory antar run) ──────────────────────────────────────────
# Struktur: { company: { "projects": [..], "subfolders": ["Legal/Akta", ...] } }

def load_folder_index() -> dict:
    if FOLDER_INDEX.exists():
        try:
            return json.loads(FOLDER_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_folder_index(index: dict):
    FOLDER_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

def _company_entry(index: dict, company: str) -> dict:
    e = index.setdefault(company, {})
    e.setdefault("projects", [])
    e.setdefault("subfolders", [])
    return e

def _run_locked(fn, lock: Lock = None):
    if lock:
        with lock:
            fn()
    else:
        fn()

def get_existing_projects(index: dict, company: str) -> list:
    return list(index.get(company, {}).get("projects", []) or [])

def register_project(index: dict, company: str, project: str, lock: Lock = None):
    def _reg():
        e = _company_entry(index, company)
        if project and project not in e["projects"]:
            e["projects"].append(project)
    _run_locked(_reg, lock)

def register_subfolder(index: dict, company: str, relpath: str, lock: Lock = None):
    def _reg():
        e = _company_entry(index, company)
        if relpath and relpath not in e["subfolders"]:
            e["subfolders"].append(relpath)
    _run_locked(_reg, lock)

def describe_index(index: dict) -> str:
    lines = []
    for comp, e in index.items():
        if not isinstance(e, dict):
            continue
        projs = e.get("projects", []) or []
        subs  = e.get("subfolders", []) or []
        if projs:
            lines.append(f"{comp} — PROYEK: {', '.join(projs)}")
        if subs:
            lines.append(f"{comp} — SUBFOLDER: {', '.join(subs)}")
    return "\n".join(lines) if lines else "Belum ada (run pertama)"


# ── Kanonikalisasi nama proyek (per perusahaan) ───────────────────────────────

def _norm_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canonicalize_project(name: str, existing_projects: list) -> str:
    """
    Cocokkan nama proyek terdeteksi ke proyek yang sudah ada (kalau mirip),
    supaya tidak ada proyek kembar dengan ejaan berbeda. Kalau tak ada yang
    cocok → kembalikan nama baru apa adanya (proyek baru).
    """
    if not name or not name.strip():
        return None
    raw  = name.strip()
    norm = _norm_text(raw)
    if not norm:
        return None
    norm_map = {_norm_text(p): p for p in existing_projects if p}
    if norm in norm_map:
        return norm_map[norm]
    for k, orig in norm_map.items():
        if k and (k in norm or norm in k):
            return orig
    best = difflib.get_close_matches(norm, list(norm_map.keys()), n=1, cutoff=0.82)
    if best:
        return norm_map[best[0]]
    return raw


# ── Rekonsiliasi global (satu pass setelah analisis) ──────────────────────────
# Saat FASE 1 jalan paralel dengan index kosong, tiap file tidak melihat proyek/
# subfolder yang dibuat file lain → muncul banyak nama kembar. Pass ini menyatukan
# semuanya sekaligus lewat satu panggilan Claude, lalu diterapkan ke rencana filing.

def _iter_units(plan: dict):
    """Yield (analysis, unit) untuk tiap dokumen — non-split: unit=analysis; split: tiap doc."""
    for analysis in plan.values():
        if not analysis:
            continue
        if analysis.get("should_split"):
            for d in analysis.get("documents", []):
                yield analysis, d
        else:
            yield analysis, analysis

def collect_proposals(plan: dict):
    """Kumpulkan (company, project) dan subfolder per departemen dari seluruh rencana."""
    projects   = {}   # (company, project) -> count
    subfolders = {}   # department -> {subfolder -> count}
    for analysis, unit in _iter_units(plan):
        company = analysis.get("company") or UNIDENTIFIED
        proj    = (unit.get("project") or "").strip()
        if proj:
            projects[(company, proj)] = projects.get((company, proj), 0) + 1
        dept = (unit.get("department") or "Lainnya").strip()
        sub  = (unit.get("subfolder") or "").strip()
        if sub:
            subfolders.setdefault(dept, {})
            subfolders[dept][sub] = subfolders[dept].get(sub, 0) + 1
    return projects, subfolders

def _reconcile_call(prompt: str, label: str):
    """Satu panggilan reconcile (streaming, output besar). Return data JSON, atau None kalau gagal."""
    try:
        with client.messages.stream(
            model=CLAUDE_MODEL, max_tokens=32000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        record_usage(response)
        if response.stop_reason == "max_tokens":
            print(f"  ⚠ Rekonsiliasi {label}: output kepotong (max_tokens) — dilewati.")
            return None
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  ⚠ Rekonsiliasi {label} gagal ({e}) — dilewati.")
        return None

def _consolidate_project_owner(proj_map: dict, projects: dict):
    """
    Pastikan satu PROYEK hanya dimiliki satu perusahaan (hindari folder proyek kembar
    di banyak perusahaan). Untuk tiap proyek, pilih pemilik: perusahaan grup (bukan
    00_Unidentified) dengan dokumen terbanyak; kalau tak ada grup → 00_Unidentified.
    """
    weight = {}  # project -> {company -> jumlah dokumen}
    for (fc, fp), (tc, project) in proj_map.items():
        n = projects.get((fc, fp), 1)
        weight.setdefault(project, {}).setdefault(tc, 0)
        weight[project][tc] += n

    owner = {}
    for project, comps in weight.items():
        grp = {c: n for c, n in comps.items() if c != UNIDENTIFIED}
        pool = grp if grp else comps
        owner[project] = max(pool.items(), key=lambda kv: kv[1])[0]

    for key in list(proj_map.keys()):
        tc, project = proj_map[key]
        proj_map[key] = (owner.get(project, tc), project)
    return owner

def reconcile_with_claude(projects: dict, subfolders: dict):
    """
    Minta Claude menyatukan proyek & subfolder kembar — DUA panggilan terpisah
    supaya tiap respons punya ruang token cukup.
    Return (proj_map, sub_map, proj_ok):
      proj_map[(from_company, from_project)] = (to_company, to_project)
      sub_map[(department, from_sub)]        = to_sub
      proj_ok = True kalau reconcile proyek berhasil (untuk menentukan fallback)
    """
    proj_map, sub_map, proj_ok = {}, {}, False
    companies_block = chr(10).join(f"- {c}" for c in COMPANIES)

    # ── Panggilan 1: PROYEK (global, supaya bisa pindah antar-perusahaan) ──────
    if projects:
        proj_lines = "\n".join(f"- {comp} :: {proj}  (x{n})"
                               for (comp, proj), n in sorted(projects.items()))
        prompt = f"""Kamu merapikan penamaan PROYEK yang berantakan dari hasil filing dokumen galangan kapal.

PERUSAHAAN GRUP VALID (selain ini → "00_Unidentified"):
{companies_block}

DAFTAR PROYEK YANG DIUSULKAN (format: company :: project (xN dokumen)):
{proj_lines}

TUGAS: untuk SETIAP entri, tentukan "to_project" dan "to_company".
- "to_project" = nama proyek kanonik yang singkat & konsisten. Proyek-proyek serupa DIGABUNG jadi SATU nama.
  Contoh: "Kapal 50 Penumpang Kab Alor 2015", "Kapal 50 Penumpang Maluku APBNP 2015", "Pengadaan Kapal 50 Penumpang APBN 2015"
  → SEMUA jadi "Kapal 50 Penumpang 2015" (lokasi/paket TIDAK dipisah).
- "to_company" = SATU perusahaan pemilik proyek. Proyek yang sama HARUS punya to_company yang sama.
  Kalau jelas milik perusahaan grup, pakai itu; kalau tidak jelas → "00_Unidentified".
- Proyek yang benar-benar BEDA (mis. "Floating Dock" vs "Kapal Penumpang" vs "Ponton" vs "Dermaga") tetap terpisah.
- Nama yang jelas BUKAN proyek (nama perusahaan/vendor, tanggal saja) → "to_project" = "Lain-lain".

Balas HANYA JSON, tanpa penjelasan, tanpa backtick. SEMUA entri (termasuk yang tidak berubah):
{{"project_map":[{{"from_company":"00_Unidentified","from_project":"Kapal 50 Penumpang Kab Alor 2015","to_company":"PT Krakatau Shipyard","to_project":"Kapal 50 Penumpang 2015"}}]}}"""
        data = _reconcile_call(prompt, "proyek")
        if data is not None:
            proj_ok = True
            for m in data.get("project_map", []):
                fc, fp = m.get("from_company"), m.get("from_project")
                tc, tp = m.get("to_company"), (m.get("to_project") or m.get("from_project") or "").strip()
                if fc and fp and tp:
                    proj_map[(fc, fp)] = (canonicalize_company(tc) if tc else fc, tp)
            _consolidate_project_owner(proj_map, projects)

    # ── Panggilan 2: SUBFOLDER (per departemen, satu panggilan) ───────────────
    if subfolders:
        sub_lines = []
        for dept in sorted(subfolders):
            sub_lines.append(f"[{dept}]")
            for sub, n in sorted(subfolders[dept].items()):
                sub_lines.append(f"- {sub}  (x{n})")
        prompt = f"""Kamu menyatukan penamaan SUBFOLDER yang tidak konsisten dari hasil filing dokumen galangan kapal.

DAFTAR SUBFOLDER PER DEPARTEMEN (format: nama subfolder (xN dokumen)):
{chr(10).join(sub_lines)}

TUGAS: untuk tiap departemen, gabungkan subfolder yang artinya SAMA jadi SATU nama kanonik singkat (Title Case).
Contoh: "Dokumen Tender", "Surat Penawaran", "Proposal & Penawaran" → "Penawaran & Tender".
Jangan gabung subfolder yang artinya beda (mis. "Gambar Teknik" vs "Network Planning" vs "Spesifikasi Teknis").

Balas HANYA JSON, tanpa penjelasan, tanpa backtick. Cantumkan HANYA entri yang BERUBAH:
{{"subfolder_map":[{{"department":"Sales & Marketing","from":"Surat Penawaran","to":"Penawaran & Tender"}}]}}"""
        data = _reconcile_call(prompt, "subfolder")
        if data is not None:
            for m in data.get("subfolder_map", []):
                dept, frm, to = m.get("department"), m.get("from"), m.get("to")
                if dept and frm and to:
                    sub_map[(dept, frm)] = to

    return proj_map, sub_map, proj_ok

def apply_reconciliation(plan: dict, proj_map: dict, sub_map: dict) -> int:
    """Terapkan hasil rekonsiliasi ke rencana. Return jumlah perubahan."""
    changed = 0
    for analysis in plan.values():
        if not analysis:
            continue
        company = analysis.get("company") or UNIDENTIFIED
        units   = analysis.get("documents", []) if analysis.get("should_split") else [analysis]
        is_split = analysis.get("should_split")

        for unit in units:
            proj = (unit.get("project") or "").strip()
            if proj and (company, proj) in proj_map:
                to_company, to_project = proj_map[(company, proj)]
                if to_project and to_project != proj:
                    unit["project"] = to_project
                    changed += 1
                # Pindah perusahaan hanya untuk file utuh (split = satu file fisik, satu company).
                if not is_split and to_company and to_company != company:
                    analysis["company"] = to_company
                    company = to_company
                    changed += 1

            dept = (unit.get("department") or "Lainnya").strip()
            sub  = (unit.get("subfolder") or "").strip()
            if sub and (dept, sub) in sub_map and sub_map[(dept, sub)] != sub:
                unit["subfolder"] = sub_map[(dept, sub)]
                changed += 1
    return changed


# ── OCR ───────────────────────────────────────────────────────────────────────

def ocr_pdf(pdf_path: Path, max_pages: int = 8) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract

        tesseract_path = os.getenv("TESSERACT_PATH")
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path

        poppler_path = os.getenv("POPPLER_PATH") or None
        print(f"  📷 PDF scan — menjalankan OCR...")
        images = convert_from_path(str(pdf_path), dpi=180, last_page=max_pages, poppler_path=poppler_path)

        parts = []
        for i, img in enumerate(images):
            text = pytesseract.image_to_string(img, lang="ind+eng")
            if text.strip():
                parts.append(f"[Halaman {i+1}]\n{text}")

        result = "\n\n".join(parts)
        if result.strip():
            print(f"  ✓ OCR selesai ({len(images)} halaman)")
        return result
    except Exception as e:
        print(f"  ⚠ OCR gagal: {e}")
        return ""


def extract_text(pdf_path: Path, max_chars: int = 10000) -> str:
    parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(f"[Halaman {i+1}]\n{t}")
    except Exception as e:
        print(f"  ⚠ Gagal ekstrak: {e}")

    # Digital only — scan (tanpa layer teks) dibaca via Claude vision di analyze_pdf,
    # bukan Tesseract (OCR Tesseract di scan ID sering rusak → klasifikasi salah).
    return "\n\n".join(parts)[:max_chars]


# ── Utilitas ──────────────────────────────────────────────────────────────────

def get_page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0

def sanitize(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', " ", name.strip())
    return name[:80]

def ensure_pdf_name(raw: str, fallback: str) -> str:
    """Bersihkan nama file & pastikan berakhir tepat satu '.pdf' (hindari .pdf.pdf)."""
    base = (raw or fallback or "").strip()
    base = re.sub(r'\.pdf$', "", base, flags=re.IGNORECASE)
    return sanitize(base) + ".pdf"

def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        new_path = path.parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1

def save_pdf_pages(pdf_path: Path, page_start: int, page_end: int, out_path: Path) -> bool:
    try:
        reader = PdfReader(str(pdf_path))
        writer = PdfWriter()
        for i in range(page_start, min(page_end + 1, len(reader.pages))):
            writer.add_page(reader.pages[i])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            writer.write(f)
        return True
    except Exception as e:
        print(f"  ✗ Gagal simpan: {e}")
        return False


# ── Analisis Claude ───────────────────────────────────────────────────────────

VISION_PAGE_CAP = 4   # halaman awal scan yang dikirim ke Claude untuk klasifikasi


def render_pages_b64(pdf_path: Path, max_pages: int = VISION_PAGE_CAP) -> list:
    """Render halaman awal PDF → list base64 JPEG, untuk klasifikasi scan via vision."""
    try:
        import io, base64
        from pdf2image import convert_from_path
        poppler_path = os.getenv("POPPLER_PATH") or None
        images = convert_from_path(str(pdf_path), dpi=150, last_page=max_pages,
                                   poppler_path=poppler_path)
        out = []
        for im in images:
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            out.append(base64.b64encode(buf.getvalue()).decode())
        return out
    except Exception as e:
        print(f"  ⚠ render gambar gagal: {e}")
        return []


def analyze_pdf(pdf_path: Path, folder_index: dict, path_hint: str = None) -> dict:
    total_pages = get_page_count(pdf_path)
    if total_pages == 0:
        return {}

    text     = extract_text(pdf_path)                  # digital (pdfplumber) saja
    scanned  = len(text.strip()) < 80                  # tak ada layer teks → scan
    page_imgs = render_pages_b64(pdf_path) if scanned else []
    filename = pdf_path.name
    existing = describe_index(folder_index)
    hint_block = (f"\nLOKASI ARSIP ASAL (petunjuk kuat dari struktur folder manusia — "
                  f"prioritaskan untuk perusahaan/proyek bila isi dokumen ambigu):\n{path_hint}\n"
                  if path_hint else "")

    prompt = f"""Kamu adalah asisten filing dokumen untuk grup perusahaan galangan kapal Indonesia.

FILE: "{filename}"
TOTAL HALAMAN: {total_pages}{hint_block}

PERUSAHAAN GRUP (HANYA INI yang boleh jadi nilai "company" — WAJIB persis salah satu):
{chr(10).join(f"- {c}" for c in COMPANIES)}

SINGKATAN / NAMA LAIN (petakan ke nama resmi di atas):
- "KS" / "PT KS" → PT Krakatau Shipyard
- "IKN" → PT Industri Kapal Nusantara
- "KSD" → PT Krakatau Sarana Dockyard
- "DKB" / "Dok Kodja Bahari" / "PT Dok dan Perkapalan Kodja Bahari" (segala ejaan, &, (Persero)) → KSO DKB-KS
  (DKB BUKAN perusahaan grup tersendiri — dia partner JV di KSO DKB-KS)

STRUKTUR FILING — dua cabang: KORPORAT (level perusahaan) & PROYEK (per proyek):
- KORPORAT (scope="korporat"):
  • Legal — akta pendirian, NIB, izin/perizinan, SIUP/TDP/SITU, domisili, sertifikat perusahaan, kontrak/perjanjian
  • Marketing — company profile, brosur, presentasi perusahaan
  • Finance — laporan keuangan, INVOICE, PO (purchase order), bank garansi, aset, pajak, faktur, tagihan, kwitansi.
    (Keuangan proyek TETAP Finance/Korporat — TAPI tetap isi "project" sebagai metadata kalau dokumennya milik proyek.)
- PROYEK (scope="proyek", WAJIB isi "project"):
  • Sales — penawaran harga, dokumen tender, prakualifikasi, sampul penawaran, lelang
  • Operasional — vendor, BAST, jadwal/kalender pengerjaan, pengadaan/material, logistik, SPB
  • Engineering — gambar teknik, GA, drawing, spesifikasi, calculation, network planning
- HR (dua-duanya): CV, tenaga ahli, sertifikasi keamanan/keahlian, data pegawai.
  → kalau jelas untuk proyek tertentu (tenaga ahli proyek X) isi "project"; kalau pegawai umum, project=null.
Kalau departemen PROYEK tapi proyek tak teridentifikasi → biarkan project=null.

PROYEK & SUBFOLDER YANG SUDAH ADA (pakai ulang kalau cocok — JANGAN bikin ejaan baru untuk hal yang sama):
{existing}

TEKS ISI DOKUMEN:
{text if text.strip() else "[Dokumen hasil SCAN — baca isinya LANGSUNG dari GAMBAR halaman yang dilampirkan di pesan ini.]"}

TUGASMU: Analisis dokumen dan tentukan cara filing yang tepat.

ATURAN:
1. "company" WAJIB salah satu nama resmi grup di atas. Pakai daftar singkatan untuk mengenali. JANGAN buat nama perusahaan baru.
2. Pihak luar (klien/vendor/mitra yang BUKAN perusahaan grup) JANGAN dijadikan "company". Catat namanya di "counterparty" saja.
   - Dokumen antara perusahaan grup ↔ pihak luar → "company" = perusahaan grup, "counterparty" = pihak luar.
   - Dokumen antara DUA perusahaan grup (mis. PT Krakatau Shipyard ↔ DKB) → "company" = "KSO DKB-KS".
   - Tidak ada perusahaan grup yang jelas → "company" = "00_Unidentified".
3. Tentukan "department", "scope" ("korporat"/"proyek"), dan "project" sesuai STRUKTUR FILING di atas.
4. "project": nama proyek singkat & konsisten. Kalau ada di daftar proyek yang sudah ada, PAKAI ULANG persis. Kalau bukan proyek → null.
5. JANGAN potong: company profile, proposal, laporan tahunan, presentasi, manual, SOP, brosur, atau dokumen yang judulnya jelas satu topik.
6. BOLEH dipotong: file yang jelas berisi campuran dokumen berbeda (invoice+faktur+bank garansi, kontrak+BAST+referensi berbeda proyek, dll).
   Saat dipotong, tiap dokumen punya department/scope/project/subfolder sendiri.
   PENTING soal halaman (0-indexed): range antar-dokumen TIDAK BOLEH overlap — tiap halaman
   tepat milik SATU dokumen. Urut & sambung (page_start dok berikut = page_end dok sebelumnya + 1),
   cakup semua halaman 0..TOTAL-1. Contoh 8 halaman: 0-1, 2-2, 3-4, 5-7 (BUKAN 0-1,1-2,2-3...).
7. "subfolder": pakai yang sudah ada kalau cocok, buat baru kalau perlu. Nama singkat dan deskriptif.

Balas HANYA JSON, tanpa penjelasan, tanpa backtick.

Format kalau TIDAK dipotong (contoh dokumen proyek):
{{"company":"PT Krakatau Shipyard","counterparty":"PT Pertamina","department":"Engineering","scope":"proyek","project":"Pembangunan 2 Tug Boat 2024","subfolder":"Gambar Teknik","should_split":false,"filename_out":"General Arrangement TB-01.pdf","expire_date":null,"doc_number":null,"reason":"Gambar teknik proyek tug boat"}}

Format kalau TIDAK dipotong (contoh dokumen korporat):
{{"company":"PT Krakatau Shipyard","counterparty":null,"department":"Legal","scope":"korporat","project":null,"subfolder":"Akta","should_split":false,"filename_out":"Akta Pendirian PT KS No.12 2010.pdf","expire_date":null,"doc_number":null,"reason":"Akta notaris — dokumen korporat"}}

Format kalau DIPOTONG (tiap dokumen punya dept sendiri; invoice→Finance, penawaran→Sales):
{{"company":"PT Krakatau Shipyard","counterparty":"PT Pertamina","should_split":true,"reason":"Campuran invoice + penawaran harga vendor","documents":[{{"doc_name":"Invoice INV-2023-001","department":"Finance","scope":"korporat","project":"Pembangunan 2 Tug Boat 2024","subfolder":"Invoice","page_start":0,"page_end":2,"expire_date":null,"doc_number":"INV-2023-001"}},{{"doc_name":"Penawaran Harga Pompa","department":"Sales","scope":"proyek","project":"Pembangunan 2 Tug Boat 2024","subfolder":"Penawaran Harga","page_start":3,"page_end":4,"expire_date":null,"doc_number":null}}]}}"""

    if page_imgs:                                       # scan → kirim gambar + prompt (multimodal)
        content = [{"type": "image", "source": {"type": "base64",
                    "media_type": "image/jpeg", "data": b}} for b in page_imgs]
        content.append({"type": "text", "text": prompt})
    else:                                               # digital → teks saja
        content = prompt

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": content}]
        )
        record_usage(response)
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)

        # Safety net: force "company" onto the whitelist regardless of what the model returned.
        detected = result.get("company", "")
        result["company_detected"] = detected
        result["company"] = canonicalize_company(detected)

        if result.get("should_split") and result.get("documents"):
            # Urutkan per page_start lalu paksa TAK overlap: tiap halaman tepat milik satu
            # dokumen (page_start dok berikut > page_end dok sebelumnya). Range yang runtuh
            # setelah di-clamp dibuang. Mencegah halaman terduplikat antar potongan.
            valid, prev_end = [], -1
            for d in sorted(result["documents"], key=lambda x: int(x.get("page_start", 0))):
                ps = max(0, int(d.get("page_start", 0)), prev_end + 1)
                pe = min(total_pages - 1, int(d.get("page_end", total_pages - 1)))
                if ps <= pe:
                    d["page_start"], d["page_end"] = ps, pe
                    prev_end = pe
                    valid.append(d)
            result["documents"] = valid
            if not valid:                       # tak ada range valid → perlakukan utuh
                result["should_split"] = False

        return result

    except json.JSONDecodeError as e:
        print(f"  ✗ JSON tidak valid: {e}")
        return {}
    except Exception as e:
        print(f"  ✗ Error Claude: {e}")
        return {}


# ── Eksekusi filing ───────────────────────────────────────────────────────────

# Pemetaan subfolder freeform (mis. hasil split Claude: "Tagihan"/"Kwitansi"/"Faktur Pajak"/
# "Laporan Progress") → nama subfolder KANONIK per departemen — biar konsisten dgn path-first.
_CANON_SUB = {
    "Finance": [
        (("faktur", "pajak", "ppn", "pph"),                       "Faktur & Pajak"),
        (("purchase order", "po ", "p.o", "pesanan pembelian"),   "Purchase Order"),
        (("invoice",),                                            "Invoice"),
        (("kwitansi", "tagihan", "pembayaran", "bukti bayar",
          "rincian biaya", "permohonan pembayaran"),              "Pembayaran"),
        (("bank garansi", "jaminan", "garansi bank"),             "Bank Garansi"),
        (("laporan keuangan", "neraca", "laba rugi",
          "aset", "aktiva", "audited"),                           "Laporan Keuangan & Aset"),
    ],
    "Operasional": [
        (("bast", "berita acara", "serah terima"),                "BAST"),
        (("perintah kerja", "spk", "work order"),                 "SPK"),
        (("pengajuan barang", "permintaan barang",
          "permintaan material"),                                 "Pengajuan Barang"),
        (("surat jalan", "pengiriman", "delivery",
          "tanda terima barang"),                                 "Pengiriman"),
        (("laporan progres", "laporan progress", "realisasi",
          "kemajuan", "progress report", "monitoring"),           "Laporan Progres"),
        (("jadwal", "kalender", "schedule", "kurva s"),           "Jadwal"),
        (("vendor", "supplier", "rekanan"),                       "Vendor"),
        (("pengadaan", "logistik", "logistic"),                   "Pengadaan"),
    ],
    "Sales": [
        (("penawaran", "quotation", "sph", "harga"),              "Penawaran Harga"),
        (("tender", "lelang", "prakualifikasi", "sampul"),        "Tender"),
    ],
    "Legal": [
        (("akta", "akte", "notaris"),                             "Akta"),
        (("kontrak", "perjanjian", "mou"),                        "Kontrak"),
        (("npwp",),                                               "NPWP"),
        (("sertifikat tanah", "tanah"),                           "Tanah"),
        (("izin", "legalitas", "nib", "siup", "tdp",
          "domisili", "perizinan"),                               "Legalitas & Izin"),
    ],
    "Engineering": [
        (("gambar", "drawing", "general arrangement"),            "Gambar"),
        (("spesifikasi", "calculation", "perhitungan"),           "Spesifikasi & Perhitungan"),
        (("sertifikat material", "mill cert", "material cert"),   "Sertifikat Material"),
        (("brosur", "datasheet", "spec sheet"),                   "Brosur & Spesifikasi Alat"),
    ],
    "HR": [
        (("tenaga ahli", "curriculum", "resume"),                 "Tenaga Ahli"),
        (("sertifikasi personil", "sertifikat keahlian",
          "sertifikat personil"),                                 "Sertifikasi Personil"),
        (("pegawai", "kepegawaian", "karyawan",
          "absensi", "gaji", "kontrak kerja"),                    "Kepegawaian"),
    ],
    "Marketing": [
        (("company profile", "profil", "brosur", "compro"),       "Company Profile"),
    ],
}


def canon_subfolder(department: str, subfolder: str) -> str:
    """Map subfolder freeform → kanonik per dept (kalau cocok keyword). Subfolder
    multi-segmen (mis. 'Vendor/Item') TIDAK diutak-atik. Tak cocok → apa adanya."""
    if not subfolder or "/" in subfolder:
        return subfolder
    n = f" {subfolder.lower()} "
    for kws, canon in _CANON_SUB.get(department, []):
        if any(k in n for k in kws):
            return canon
    return subfolder


def resolve_destination(company: str, unit: dict, folder_index: dict, index_lock: Lock,
                        canon_project: bool = True) -> dict:
    """
    Tentukan folder tujuan satu dokumen berdasarkan department/scope/project.
    Mengembalikan dict: out_dir, relpath, department, project, subfolder.
    Sekaligus daftarkan proyek + subfolder ke folder_index.
    canon_project=False kalau nama proyek sudah dikanonikalisasi oleh pass rekonsiliasi.
    """
    department = sanitize(unit.get("department") or "Lainnya")
    # Kanonikalisasi subfolder freeform (hasil split/analisis Claude) → nama kanonik per dept.
    raw_sub = canon_subfolder(unit.get("department") or "Lainnya", str(unit.get("subfolder") or "Umum"))
    # subfolder boleh multi-level (mis. "Vendor/Item") — sanitize tiap segmen, pertahankan "/".
    subfolder  = "/".join(s for s in (sanitize(x) for x in raw_sub.split("/")) if s) or "Umum"
    scope      = (unit.get("scope") or "").strip().lower()

    # Kanonikalisasi nama proyek terhadap proyek yang sudah ada di perusahaan ini.
    # Buang placeholder ("00_Unidentified" dll) → None supaya masuk _Tanpa Proyek.
    raw_project = normalize_project(unit.get("project"))
    project = None
    if raw_project:
        if canon_project:
            existing = get_existing_projects(folder_index, company)
            project  = sanitize(canonicalize_project(str(raw_project), existing))
        else:
            project = sanitize(str(raw_project))

    base_rel, kind, project_final = placement_relpath(department, scope, project)
    relpath = f"{base_rel}/{subfolder}"
    out_dir = OUTPUT_DIR / company / Path(relpath)
    out_dir.mkdir(parents=True, exist_ok=True)

    if project_final:
        register_project(folder_index, company, project_final, index_lock)
    register_subfolder(folder_index, company, relpath, index_lock)

    return {
        "out_dir": out_dir, "relpath": relpath, "kind": kind,
        "department": department,
        # Folder pakai project_final; tapi METADATA proyek tetap disimpan (mis. invoice proyek
        # yang difile di Korporat/Finance tetap bertag proyeknya) untuk vector DB.
        "project": project_final or project, "subfolder": subfolder,
    }


def file_document(pdf_path: Path, analysis: dict, folder_index: dict, index_lock: Lock,
                  canon_project: bool = True) -> list[dict]:
    company      = sanitize(analysis.get("company") or UNIDENTIFIED)
    counterparty = analysis.get("counterparty") or None
    logs         = []

    if not analysis.get("should_split", False):
        dest         = resolve_destination(company, analysis, folder_index, index_lock, canon_project)
        filename_out = ensure_pdf_name(analysis.get("filename_out"), pdf_path.stem)
        out_path     = unique_path(dest["out_dir"] / filename_out)

        shutil.copy2(pdf_path, out_path)

        print(f"  📄 {company} / {dest['relpath']} / {filename_out}")
        logs.append({
            "source_file": pdf_path.name, "doc_name": analysis.get("filename_out", pdf_path.stem),
            "company": company, "counterparty": counterparty,
            "department": dest["department"], "project": dest["project"], "subfolder": dest["subfolder"],
            "relpath": dest["relpath"],
            "output_file": str(out_path), "split": False,
            "expire_date": analysis.get("expire_date"), "doc_number": analysis.get("doc_number"),
            "status": "ok", "processed_at": datetime.now().isoformat(),
        })
    else:
        docs = analysis.get("documents", [])
        print(f"  ✂  {company} — {len(docs)} dokumen:")
        for doc in docs:
            dest     = resolve_destination(company, doc, folder_index, index_lock, canon_project)
            doc_name = sanitize(doc.get("doc_name") or pdf_path.stem)
            out_path = unique_path(dest["out_dir"] / ensure_pdf_name(doc_name, pdf_path.stem))

            ok = save_pdf_pages(pdf_path, doc["page_start"], doc["page_end"], out_path)

            expire_str = f" | expire: {doc.get('expire_date')}" if doc.get("expire_date") else ""
            print(f"    {'✓' if ok else '✗'} {doc_name} → {dest['relpath']}{expire_str}")

            logs.append({
                "source_file": pdf_path.name, "doc_name": doc_name,
                "company": company, "counterparty": counterparty,
                "department": dest["department"], "project": dest["project"], "subfolder": dest["subfolder"],
                "relpath": dest["relpath"],
                "output_file": str(out_path) if ok else None,
                "pages": f"{doc['page_start']+1}-{doc['page_end']+1}", "split": True,
                "expire_date": doc.get("expire_date"), "doc_number": doc.get("doc_number"),
                "status": "ok" if ok else "error", "processed_at": datetime.now().isoformat(),
            })

    return logs


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_inbox(limit: int = None, reprocess: bool = False, clean: bool = False,
                  reconcile: bool = True, workers: int = MAX_WORKERS):
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    processed_dir = INPUT_DIR / "_processed"

    if reprocess:
        # Baca ulang file yang sudah pernah diproses — TANPA memindahkannya lagi.
        source_dir = processed_dir
        pdf_files  = list(source_dir.rglob("*.pdf")) + list(source_dir.rglob("*.PDF"))
        if not pdf_files:
            print(f"\n📂 Tidak ada PDF di '{source_dir}' untuk diproses ulang.\n")
            return
    else:
        source_dir = INPUT_DIR
        pdf_files  = list(INPUT_DIR.rglob("*.pdf")) + list(INPUT_DIR.rglob("*.PDF"))
        pdf_files  = [f for f in pdf_files if "_processed" not in f.parts]
        if not pdf_files:
            print(f"\n📂 Tidak ada PDF di folder '{INPUT_DIR}'.")
            print(f"   Taruh file PDF di folder 'inbox' lalu jalankan lagi.\n")
            return

    pdf_files   = sorted(set(pdf_files))   # dedup: di Windows *.pdf & *.PDF cocokkan file yang sama
    total_found = len(pdf_files)
    if limit is not None and limit > 0:
        pdf_files = pdf_files[:limit]   # sudah terurut → batch tes deterministik

    # --clean: mulai dari index kosong supaya FASE 1 tidak melihat subfolder lama.
    folder_index = {} if clean else load_folder_index()
    print_lock   = Lock()
    index_lock   = Lock()

    print(f"\n{'='*65}")
    print(f"  KS Document Processor — Waralalo Group")
    print(f"  Model   : {CLAUDE_MODEL}")
    print(f"  Sumber  : {source_dir}{'  (REPROCESS — file tidak dipindah)' if reprocess else ''}")
    if limit is not None and limit > 0:
        print(f"  File    : {len(pdf_files)} dari {total_found} PDF (MODE TES — limit {limit})")
    else:
        print(f"  File    : {total_found} PDF ditemukan")
    print(f"  Paralel : {workers} file sekaligus")
    print(f"  Output  : {OUTPUT_DIR.resolve()}")
    print(f"{'='*65}\n")

    # ── FASE 1: Analisis paralel ──────────────────────────────────────────────
    print(f"📋 FASE 1 — Menganalisis semua dokumen...\n")
    plan = {}

    def analyze_one(pdf_path: Path) -> tuple:
        result = analyze_pdf(pdf_path, folder_index)
        with print_lock:
            print(f"\n📄 {pdf_path.name}")
            if not result:
                print(f"   ⚠ Tidak bisa dianalisis → 00_Unidentified")
            elif result.get("should_split"):
                docs = result.get("documents", [])
                print(f"   🏢 {result.get('company')}")
                print(f"   ✂  {len(docs)} dokumen — {result.get('reason','')}")
                for d in docs:
                    proj = f" · proyek: {d.get('project')}" if d.get("project") else ""
                    print(f"      → [{d.get('department')}/{d.get('subfolder')}{proj}] {d.get('doc_name')} (hal {d['page_start']+1}–{d['page_end']+1})")
            else:
                proj = f" · proyek: {result.get('project')}" if result.get("project") else ""
                print(f"   🏢 {result.get('company')} / {result.get('department')} / {result.get('subfolder')}{proj}")
                print(f"   📄 {result.get('filename_out', pdf_path.name)}")
                if result.get("reason"):
                    print(f"   💬 {result.get('reason')}")
        return pdf_path, result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(analyze_one, p): p for p in pdf_files}
        for future in tqdm(as_completed(futures), total=len(pdf_files), desc="Menganalisis", unit="file"):
            pdf_path, result = future.result()
            plan[pdf_path] = result

    # Fail-fast: kalau SEMUA file gagal dianalisis, kemungkinan masalah API/koneksi/key —
    # jangan teruskan dan memfile semuanya ke 00_Unidentified.
    if len(pdf_files) >= 3 and all(not v for v in plan.values()):
        print(f"\n✗ Semua {len(pdf_files)} file gagal dianalisis — kemungkinan masalah API key / koneksi / rate limit.")
        print(f"  Dibatalkan tanpa filing. Cek ANTHROPIC_API_KEY & koneksi lalu jalankan lagi.\n")
        return

    # ── FASE 1.5: Rekonsiliasi global (satukan proyek & subfolder kembar) ─────
    reconcile_ok = False
    if reconcile:
        print(f"\n🔗 FASE 1.5 — Menyatukan proyek & subfolder yang kembar...")
        projects, subfolders         = collect_proposals(plan)
        proj_map, sub_map, proj_ok   = reconcile_with_claude(projects, subfolders)
        n_changed                    = apply_reconciliation(plan, proj_map, sub_map)
        reconcile_ok                 = proj_ok   # kalau gagal, biarkan FASE-3 fuzzy match jadi cadangan
        print(f"  ✓ {len(proj_map)} proyek + {len(sub_map)} subfolder disatukan ({n_changed} dokumen disesuaikan)")
        if not proj_ok:
            print(f"  ↩ Rekonsiliasi proyek gagal — pakai pencocokan fuzzy di FASE 2 sebagai cadangan.")

    # ── FASE 2: Ringkasan & konfirmasi ───────────────────────────────────────
    total_out   = sum(len(v.get("documents",[])) if v and v.get("should_split") else 1 for v in plan.values())
    split_count = sum(1 for v in plan.values() if v and v.get("should_split"))
    utuh_count  = sum(1 for v in plan.values() if v and not v.get("should_split"))
    gagal_count = sum(1 for v in plan.values() if not v)

    print(f"\n{'='*65}")
    print(f"  📊 RINGKASAN")
    print(f"{'─'*65}")
    print(f"  File masuk       : {len(pdf_files)}")
    print(f"  Disimpan utuh    : {utuh_count} file")
    print(f"  Dipotong         : {split_count} file → {total_out} dokumen")
    if gagal_count:
        print(f"  Tidak teridentif : {gagal_count} file → 00_Unidentified")

    # Nama perusahaan yang terdeteksi tapi tidak masuk whitelist — kemungkinan alias yang terlewat.
    unresolved = {}
    for p, v in plan.items():
        if v and v.get("company") == UNIDENTIFIED:
            detected = (v.get("company_detected") or "").strip()
            if detected and _normalize_name(detected):
                unresolved.setdefault(detected, []).append(p.name)
    if unresolved:
        print(f"{'─'*65}")
        print(f"  ⚠ Nama terdeteksi tapi tak masuk whitelist → 00_Unidentified:")
        print(f"    (kalau ini sebenarnya perusahaan grup, tambahkan ke COMPANY_ALIASES)")
        for name, files in sorted(unresolved.items()):
            print(f"     • \"{name}\"  ({len(files)} file)")

    if clean:
        print(f"{'─'*65}")
        print(f"  ⚠ --clean: SELURUH isi '{OUTPUT_DIR}' + '{FOLDER_INDEX.name}' akan DIHAPUS sebelum filing.")
    print(f"{'─'*65}")
    konfirmasi = input("  Lanjutkan filing? (y/n): ").strip().lower()

    if konfirmasi != "y":
        print("\n  ❌ Dibatalkan.\n")
        return

    # ── FASE 3: Eksekusi filing ───────────────────────────────────────────────
    if clean:
        # Bersihkan output + folder_index supaya hasil run mencerminkan logic terbaru (untuk tes).
        # Catatan: processing_log.json TIDAK ikut dihapus (riwayat tetap bertambah).
        if OUTPUT_DIR.exists():
            for child in OUTPUT_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        if FOLDER_INDEX.exists():
            FOLDER_INDEX.unlink()
        print(f"  🧹 Output + {FOLDER_INDEX.name} dibersihkan\n")

    print(f"\n📁 FASE 2 — Filing dokumen...\n")

    all_logs      = []
    processed_dir.mkdir(exist_ok=True)

    for pdf_path, analysis in plan.items():
        print(f"\n▶ {pdf_path.name}")
        if not analysis:
            out_dir  = OUTPUT_DIR / "00_Unidentified"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = unique_path(out_dir / pdf_path.name)
            shutil.copy2(pdf_path, out_path)
            print(f"  ⚠ → 00_Unidentified/")
            all_logs.append({"source_file": pdf_path.name, "company": "00_Unidentified",
                             "status": "unidentified", "processed_at": datetime.now().isoformat()})
        else:
            # Hanya skip fuzzy-match kalau rekonsiliasi proyek BERHASIL (nama sudah kanonik).
            # Kalau gagal, fuzzy-match FASE-3 jadi cadangan supaya proyek tetap tergabung.
            logs = file_document(pdf_path, analysis, folder_index, index_lock,
                                 canon_project=not reconcile_ok)
            all_logs.extend(logs)

        # Mode reprocess: file sudah ada di _processed, jangan dipindah.
        if not reprocess:
            dest = processed_dir / pdf_path.name
            if dest.exists():
                dest = unique_path(dest)
            shutil.move(str(pdf_path), dest)

    # Simpan index & log
    save_folder_index(folder_index)

    existing_log = []
    if LOG_FILE.exists():
        try:
            existing_log = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing_log.extend(all_logs)
    LOG_FILE.write_text(json.dumps(existing_log, ensure_ascii=False, indent=2), encoding="utf-8")

    success  = sum(1 for r in all_logs if r.get("status") == "ok")
    expiring = [r for r in all_logs if r.get("expire_date")]

    print(f"\n{'='*65}")
    print(f"  ✅ Selesai!")
    print(f"  {success}/{len(all_logs)} dokumen berhasil di-filing")
    print(f"  📁 Output : {OUTPUT_DIR.resolve()}")
    if expiring:
        print(f"\n  ⚠ Dokumen dengan expire date:")
        for r in sorted(expiring, key=lambda x: x.get("expire_date") or ""):
            print(f"     • {r.get('doc_name')} → {r['expire_date']}")

    c = usage_cost()
    print(f"{'─'*65}")
    print(f"  💰 Biaya run ({CLAUDE_MODEL}, {c['calls']} panggilan API):")
    print(f"     Token : {c['input']:,} input + {c['output']:,} output")
    print(f"     Biaya : ${c['usd']:.4f}  (≈ Rp {c['idr']:,.0f})")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="KS Document Processor — split & filing PDF dari inbox")
    parser.add_argument("-n", "--limit", type=int, default=None,
                        help="Jumlah maksimum file yang diproses (mode tes). Default: semua file.")
    parser.add_argument("-r", "--reprocess", action="store_true",
                        help="Proses ulang file di inbox/_processed/ (tanpa memindahkannya). Untuk tes ulang.")
    parser.add_argument("-c", "--clean", action="store_true",
                        help="Hapus seluruh isi output/ sebelum filing (mulai dari nol). Untuk tes ulang.")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Lewati pass rekonsiliasi (penyatuan proyek & subfolder kembar).")
    parser.add_argument("-w", "--workers", type=int, default=MAX_WORKERS,
                        help=f"Jumlah file diproses paralel. Default: {MAX_WORKERS}.")
    cli_args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n✗ ANTHROPIC_API_KEY tidak ditemukan.\n")
        exit(1)
    process_inbox(limit=cli_args.limit, reprocess=cli_args.reprocess, clean=cli_args.clean,
                  reconcile=not cli_args.no_reconcile, workers=cli_args.workers)
