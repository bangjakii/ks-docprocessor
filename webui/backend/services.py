"""
Service layer UI: bungkus kode pipeline yg sudah ada (process_docs, ingest_archive,
index_to_pinecone, extract_fields) jadi fungsi siap-dipanggil FastAPI.

Reuse penuh — TIDAK menduplikasi logika klasifikasi/filing/index:
  - cari            → index_to_pinecone.search (Pinecone)
  - statistik/filter → archive_log.json (lokal, cepat)
  - klasifikasi      → process_docs.analyze_pdf (PDF) / classify_text (non-PDF)
  - filing           → ingest_archive._file_quietly (identik pipeline batch)
  - index dok baru   → index_to_pinecone.records_for + upsert
"""
import os, sys, json, threading, tempfile, uuid, re
from pathlib import Path
from datetime import datetime

# ── bikin modul root repo bisa di-import + relative-file (folder_index.json dll) resolve ──
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import process_docs as P
import ingest_archive as A
import index_to_pinecone as IX
import extract_fields as EF

ARCHIVE_ROOT = Path(os.getenv("ARCHIVE_ROOT", str(IX.DEST_DIR))).resolve()
LOG_PATH = ARCHIVE_ROOT / "archive_log.json"
STAGING = ROOT / "webui" / "_staging"
STAGING.mkdir(parents=True, exist_ok=True)

IX._enable_cost_meter()
_lock = threading.Lock()                 # untuk folder_index + tulis log
_folder_index = None
_pc_index = None
_log_cache = None                        # list[dict] entri archive_log.json
_TEMP = {}                               # temp_id → {"path":..., "proposal":...}


# ── util ──────────────────────────────────────────────────────────────────────
def _build_folder_index_from_log():
    """Sintesis folder_index {company:{projects,subfolders}} dari archive_log.json
    supaya classifier upload TAU struktur yg sudah ada → reuse proyek/subfolder
    (bukan bikin varian ejaan). Penting utk 'arsip ke path yang sudah ada'."""
    idx = {}
    for r in load_log():
        if r.get("status") != "ok" or not r.get("company"):
            continue
        e = idx.setdefault(r["company"], {"projects": [], "subfolders": []})
        proj, rel = r.get("project"), r.get("relpath")
        if proj and proj not in e["projects"]:
            e["projects"].append(proj)
        if rel and rel not in e["subfolders"]:
            e["subfolders"].append(rel)
    return idx


def _folder_idx():
    global _folder_index
    if _folder_index is None:
        try:
            _folder_index = P.load_folder_index()
        except Exception:
            _folder_index = {}
        if not _folder_index:                      # belum ada → bangun dari arsip existing
            _folder_index = _build_folder_index_from_log()
            try:
                P.save_folder_index(_folder_index)
            except Exception:
                pass
    return _folder_index


def _pinecone():
    """Handle index Pinecone (lazy). Bisa gagal kalau jaringan/credential bermasalah."""
    global _pc_index
    if _pc_index is None:
        pc = IX.get_pinecone()
        _pc_index = IX.ensure_index(pc, IX.INDEX_NAME, IX.EMBED_MODEL)
    return _pc_index


def load_log(force=False):
    global _log_cache
    if _log_cache is None or force:
        if LOG_PATH.exists():
            _log_cache = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        else:
            _log_cache = []
    return _log_cache


def _append_log(rows):
    with _lock:
        log = load_log()
        log.extend(rows)
        LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


def cost():
    return round(IX._USAGE.get("cost", 0.0), 4)


# ── STATISTIK + FILTER (dari archive_log.json, tanpa Pinecone) ─────────────────
def get_stats():
    log = [r for r in load_log() if r.get("status") == "ok"]
    def tally(key, top=None):
        c = {}
        for r in log:
            v = r.get(key) or "—"
            c[v] = c.get(v, 0) + 1
        items = sorted(c.items(), key=lambda x: -x[1])
        return items[:top] if top else items
    return {
        "total": len(log),
        "by_company":    [{"name": k, "count": v} for k, v in tally("company")],
        "by_department": [{"name": k, "count": v} for k, v in tally("department")],
        "by_project":    [{"name": k, "count": v} for k, v in tally("project", top=20)],
        "with_counterparty": sum(1 for r in log if r.get("counterparty")),
        "with_expire":       sum(1 for r in log if r.get("expire_date")),
        "with_doc_number":   sum(1 for r in log if r.get("doc_number")),
    }


def get_filters():
    log = [r for r in load_log() if r.get("status") == "ok"]
    def distinct(key):
        return sorted({(r.get(key) or "").strip() for r in log if (r.get(key) or "").strip()})
    return {"companies": distinct("company"), "departments": distinct("department"),
            "projects": distinct("project")}


# ── CARI (Pinecone) ────────────────────────────────────────────────────────────
def _g(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def run_search(q, company=None, department=None, project=None, top_k=15):
    index = _pinecone()
    flt = {k: {"$eq": v} for k, v in
           {"company": company, "department": department, "project": project}.items() if v}
    query = {"inputs": {"text": q}, "top_k": int(top_k)}
    if flt:
        query["filter"] = flt
    res = IX.retry(lambda: index.search(
        namespace=IX.NAMESPACE, query=query,
        fields=["doc_name", "company", "department", "project", "subfolder",
                "filename", "relpath", "expire_date", "doc_number", "counterparty",
                "page_number", "chunk_text"]), what="search")
    hits = _g(_g(res, "result"), "hits") or []
    out = []
    for h in hits:
        f = _g(h, "fields") or {}
        comp, rel, fn = f.get("company", ""), f.get("relpath", ""), f.get("filename", "")
        path = "/".join(x.strip("/") for x in (comp, rel, fn) if x)   # utk /api/file
        out.append({
            "score": round(float(_g(h, "score", 0.0) or 0.0), 4),
            "doc_name": f.get("doc_name", ""), "company": comp,
            "department": f.get("department", ""), "project": f.get("project", ""),
            "subfolder": f.get("subfolder", ""), "filename": fn,
            "relpath": rel, "page": f.get("page_number", ""), "path": path,
            "expire_date": f.get("expire_date", ""), "doc_number": f.get("doc_number", ""),
            "counterparty": f.get("counterparty", ""),
            "snippet": (f.get("chunk_text", "") or "")[:500],
        })
    return {"query": q, "filter": flt, "hits": out, "count": len(out)}


# ── BUKA FILE ASLI ──────────────────────────────────────────────────────────────
def resolve_file(path_str):
    """Validasi path ada DI DALAM ARCHIVE_ROOT (cegah path traversal). Terima abs/rel."""
    p = Path(path_str)
    if not p.is_absolute():
        p = ARCHIVE_ROOT / path_str
    p = p.resolve()
    if ARCHIVE_ROOT not in p.parents and p != ARCHIVE_ROOT:
        raise ValueError("path di luar arsip")
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p


def extract_page(path_str, page, context=0):
    """Potong HALAMAN dari bundel → PDF 1-lembar (atau ±context). Untuk fitur:
    hasil search nyangkut di hal-N bundel → ambil halaman itu saja.
    Return (path, was_cut). Non-PDF / file 1-halaman / mencakup semua → file utuh."""
    src = resolve_file(path_str)
    page = int(page or 0)                          # page_number metadata = 1-indexed
    context = max(0, int(context or 0))
    if src.suffix.lower() != ".pdf" or page < 1:
        return src, False
    n = P.get_page_count(src)
    if n <= 1 or page > n:                          # bukan bundel / di luar range → file utuh
        return src, False
    lo0, hi0 = max(0, page - 1 - context), min(n - 1, page - 1 + context)   # → 0-indexed
    if lo0 == 0 and hi0 == n - 1:                   # mencakup semua → file utuh
        return src, False
    safe = re.sub(r"[^\w.\- ]", "_", src.stem)[:50]
    out = STAGING / f"cut_{safe}_hal{page}.pdf"
    if not P.save_pdf_pages(src, lo0, hi0, out):    # REUSE splitter pipeline (process_docs)
        return src, False
    return out, True


# ── UPLOAD: klasifikasi (review dulu) ───────────────────────────────────────────
_TEXT_PROMPT = """Kamu asisten filing dokumen grup galangan kapal Indonesia.
FILE: "{fn}"

PERUSAHAAN GRUP (company WAJIB persis salah satu, atau "00_Unidentified"):
{companies}
Singkatan: KS→PT Krakatau Shipyard, IKN→PT Industri Kapal Nusantara, KSD→PT Krakatau Sarana Dockyard, DKB/Dok Kodja Bahari→KSO DKB-KS.

DEPARTEMEN: Legal, Marketing, Finance (invoice/PO/pajak/faktur/kwitansi/bank garansi),
Sales (penawaran/tender/lelang), Operasional (vendor/BAST/pengadaan/logistik),
Engineering (gambar teknik/GA/drawing/spesifikasi), HR (CV/pegawai/sertifikasi).
scope = "korporat" atau "proyek" (kalau proyek, isi "project").

PROYEK/SUBFOLDER yg sudah ada (pakai ulang kalau cocok):
{existing}

TEKS DOKUMEN:
{text}

Balas HANYA JSON: {{"company":..,"counterparty":null|..,"department":..,"scope":"korporat"|"proyek","project":null|..,"subfolder":..,"reason":..}}"""


def _classify_text(text, filename):
    existing = P.describe_index(_folder_idx())
    prompt = _TEXT_PROMPT.format(fn=filename, companies="\n".join(f"- {c}" for c in P.COMPANIES),
                                 existing=existing, text=(text or "")[:8000])
    try:
        r = P.client.messages.create(model=P.CLAUDE_MODEL, max_tokens=600,
                                     messages=[{"role": "user", "content": prompt}])
        P.record_usage(r)
        raw = re.sub(r"^```json|^```|```$", "", r.content[0].text.strip(), flags=re.M).strip()
        d = json.loads(raw)
        d["company_detected"] = d.get("company", "")
        d["company"] = P.canonicalize_company(d.get("company", ""))
        return d
    except Exception as e:
        return {"company": P.UNIDENTIFIED, "department": "Lainnya", "scope": "korporat",
                "project": None, "subfolder": "Umum", "counterparty": None,
                "reason": f"auto-classify gagal: {e}"}


def classify_upload(filename, data: bytes):
    """Simpan ke staging, klasifikasi + ekstrak field. TIDAK difile (review dulu)."""
    temp_id = uuid.uuid4().hex[:12]
    safe = re.sub(r"[^\w.\- ]", "_", filename)
    path = STAGING / f"{temp_id}__{safe}"
    path.write_bytes(data)

    is_pdf = path.suffix.lower() == ".pdf"
    if is_pdf:
        a = P.analyze_pdf(path, _folder_idx()) or {}
    else:
        try:
            txt = " ".join(t for _, t in IX.extract_pages_any(path, cap=4))
        except Exception:
            txt = ""
        a = _classify_text(txt, filename)

    # field terstruktur (regex + LLM) dari teks
    try:
        txt_all = " ".join(t for _, t in IX.extract_pages_any(path, cap=IX.cap_for(a)))
        EF.enrich(a, txt_all, use_llm=True)
    except Exception:
        pass

    proposal = {
        "temp_id": temp_id, "filename": filename,
        "company": a.get("company") or P.UNIDENTIFIED,
        "company_detected": a.get("company_detected", a.get("company", "")),
        "counterparty": a.get("counterparty"),
        "department": a.get("department") or "Lainnya",
        "scope": a.get("scope") or ("proyek" if a.get("project") else "korporat"),
        "project": a.get("project"),
        "subfolder": a.get("subfolder") or "Umum",
        "doc_number": a.get("doc_number"), "expire_date": a.get("expire_date"),
        "reason": a.get("reason", ""),
        "should_split": bool(a.get("should_split")),   # info: ini bundel campuran
    }
    _TEMP[temp_id] = {"path": str(path), "proposal": proposal}
    return proposal


# ── UPLOAD: konfirmasi → file + index ───────────────────────────────────────────
def confirm_upload(temp_id, edited: dict):
    rec = _TEMP.get(temp_id)
    if not rec:
        raise KeyError("temp_id tidak ditemukan / sudah kadaluarsa")
    path = Path(rec["path"])
    if not path.is_file():
        raise FileNotFoundError("file staging hilang")

    # analysis final = proposal di-override input user
    p = dict(rec["proposal"]); p.update({k: v for k, v in (edited or {}).items() if v is not None})
    proj = P.normalize_project(p.get("project")) if p.get("project") else None
    analysis = {
        "company": p["company"], "counterparty": p.get("counterparty"),
        "department": p.get("department") or "Lainnya",
        "scope": "proyek" if proj else (p.get("scope") or "korporat"),
        "project": proj, "subfolder": p.get("subfolder") or "Umum",
        "should_split": False,                          # upload selalu utuh (v1)
        "filename_out": p["filename"], "expire_date": p.get("expire_date"),
        "doc_number": p.get("doc_number"),
    }
    # 1) FILE ke Arsip_Rapih (identik pipeline batch)
    rows = A._file_quietly(path, analysis, _folder_idx(), _lock)
    rows = [r for r in rows if r.get("status") == "ok"]
    if not rows:
        raise RuntimeError("gagal memfile dokumen")
    try:
        P.save_folder_index(_folder_idx())
    except Exception:
        pass

    # 2) INDEX ke Pinecone (best-effort; kalau jaringan gagal, file tetap terfile)
    indexed, vec = False, 0
    try:
        index = _pinecone()
        for r in rows:
            out = Path(r["output_file"])
            meta = dict(r)
            base = IX.doc_id(str(out.resolve()))
            pages = IX.extract_pages_any(out, cap=IX.cap_for(meta))
            recs = IX.records_for(out, base, meta, pages=pages)
            if recs:
                IX.retry(lambda c=recs: index.upsert_records(namespace=IX.NAMESPACE, records=c),
                         what="upsert")
                vec += len(recs)
        indexed = True
    except Exception as e:
        rows[0]["_index_error"] = str(e)

    # 3) catat ke log + bersihkan staging
    _append_log(rows)
    _log_cache_reload()
    try:
        path.unlink()
    except Exception:
        pass
    _TEMP.pop(temp_id, None)

    r0 = rows[0]
    return {"filed": True, "indexed": indexed, "vectors": vec,
            "output_file": r0.get("output_file"), "relpath": r0.get("relpath"),
            "company": r0.get("company"), "department": r0.get("department"),
            "project": r0.get("project"), "subfolder": r0.get("subfolder"),
            "index_error": r0.get("_index_error")}


def _log_cache_reload():
    load_log(force=True)
