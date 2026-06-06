"""
KS Pinecone Indexer — Waralalo Group
====================================
Ambil dokumen yang sudah ditata oleh ingest_archive.py (--apply) dari folder
hasil (default D:\\Arsip_Rapih), ekstrak teks (pdfplumber + OCR untuk scan),
pecah PER-HALAMAN, lalu index ke Pinecone dengan INTEGRATED EMBEDDING
(Pinecone yang embed server-side — tidak perlu OpenAI).

Granularitas pencarian = chunk per-halaman (bukan pemotongan file fisik). Lihat
keputusan di memory: bundel campur disimpan utuh, granularitas via chunking.

Cara pakai:
    pip install "pinecone[asyncio]" pdfplumber pdf2image pytesseract python-dotenv
    python index_to_pinecone.py                     # index seluruh Arsip_Rapih
    python index_to_pinecone.py --limit 50           # uji 50 file dulu
    python index_to_pinecone.py --query "faktur pajak PT PAL"   # cari (tes)

Env (.env): PINECONE_API_KEY, (opsional) TESSERACT_PATH, POPPLER_PATH
"""

import os
import re
import json
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime

import pdfplumber
from dotenv import load_dotenv

load_dotenv()

# ── Konfigurasi default ───────────────────────────────────────────────────────
DEST_DIR      = Path(os.getenv("INGEST_DEST", r"D:\Arsip_Rapih"))
INDEX_NAME    = "ks-documents"
EMBED_MODEL   = "multilingual-e5-large"   # integrated, 1024-dim, multilingual (ID)
NAMESPACE     = "__default__"
CHUNK_SIZE    = 1500     # karakter per chunk dalam satu halaman
CHUNK_OVERLAP = 200
OCR_PAGE_CAP  = 15       # halaman maksimum yang di-OCR per file scan (batasi waktu)
BATCH         = 96       # upsert_records per batch
CHECKPOINT    = Path("pinecone_indexed.json")   # set base_id yang sudah selesai

# Metadata field yang dibawa per chunk (selain _id & chunk_text yang di-embed).
META_FIELDS = ("doc_name", "company", "counterparty", "department", "project",
               "subfolder", "relpath", "source_file", "expire_date", "doc_number")

# ── OCR (self-contained, tidak butuh Anthropic) ───────────────────────────────
TESSERACT_PATH = os.getenv("TESSERACT_PATH")
POPPLER_PATH   = os.getenv("POPPLER_PATH")
try:
    import pytesseract
    from pdf2image import convert_from_path
    if TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    _OCR_OK = True
except ImportError:
    _OCR_OK = False


def _ocr_page(pdf_path: Path, page_no: int) -> str:
    """OCR satu halaman (1-indexed). Bahasa ind+eng. Gagal → string kosong."""
    if not _OCR_OK:
        return ""
    try:
        imgs = convert_from_path(str(pdf_path), dpi=180, first_page=page_no,
                                 last_page=page_no, poppler_path=POPPLER_PATH or None)
        return pytesseract.image_to_string(imgs[0], lang="ind+eng") if imgs else ""
    except Exception:
        return ""


def page_texts(pdf_path: Path) -> list:
    """Kembalikan [(page_number, text)] per halaman; OCR untuk halaman tanpa teks
    (sampai OCR_PAGE_CAP). Halaman kosong setelah usaha dibuang."""
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                pages.append([i + 1, (page.extract_text() or "").strip()])
    except Exception as e:
        print(f"  ⚠ gagal buka {pdf_path.name}: {e}")
        return []
    # OCR halaman yang kosong (scan) — dibatasi cap supaya tak makan waktu.
    if any(not t for _, t in pages):
        for row in pages:
            pn, t = row
            if not t and pn <= OCR_PAGE_CAP:
                row[1] = _ocr_page(pdf_path, pn).strip()
    return [(pn, t) for pn, t in pages if t]


# ── Chunking per-halaman ──────────────────────────────────────────────────────

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += size - overlap
    return chunks


def doc_id(rel: str) -> str:
    """ID stabil per dokumen dari relpath di dalam dest (tahan pindah drive)."""
    return hashlib.md5(rel.encode("utf-8")).hexdigest()[:16]


def records_for(pdf_path: Path, base_id: str, meta: dict) -> list:
    """Bangun record per (halaman, sub-chunk) dengan metadata file yang utuh."""
    recs, k = [], 0
    for page_no, text in page_texts(pdf_path):
        for j, chunk in enumerate(chunk_text(text)):
            rec = {"_id": f"{base_id}_p{page_no}_{j}", "chunk_text": chunk,
                   "page_number": page_no, "chunk_index": k,
                   "filename": pdf_path.name}
            for f in META_FIELDS:
                v = meta.get(f)
                rec[f] = "" if v is None else str(v)
            recs.append(rec)
            k += 1
    return recs


# ── Metadata dari archive_log.json ────────────────────────────────────────────

def load_meta_map(dest: Path) -> dict:
    """output_file (absolut) → entri metadata, dari archive_log.json hasil ingest."""
    log = dest / "archive_log.json"
    mp = {}
    if not log.exists():
        print(f"  ⚠ {log} tidak ada — metadata kosong (jalankan ingest --apply dulu).")
        return mp
    try:
        for e in json.loads(log.read_text(encoding="utf-8")):
            if e.get("output_file") and e.get("status") == "ok":
                mp[str(Path(e["output_file"]).resolve())] = e
    except Exception as e:
        print(f"  ⚠ gagal baca log: {e}")
    return mp


# ── Pinecone ──────────────────────────────────────────────────────────────────

def get_pinecone():
    from pinecone import Pinecone
    key = os.getenv("PINECONE_API_KEY")
    if not key:
        print("\n✗ PINECONE_API_KEY tidak ada di .env\n"); raise SystemExit(1)
    return Pinecone(api_key=key)


def _index_ready(pc, name: str) -> bool:
    try:
        s = pc.describe_index(name).status
        return bool(s.get("ready") if isinstance(s, dict) else getattr(s, "ready", False))
    except Exception:
        return False


def ensure_index(pc, name: str, model: str):
    if not pc.has_index(name):
        print(f"  📦 Membuat index '{name}' (integrated: {model})...")
        pc.create_index_for_model(
            name=name, cloud="aws", region="us-east-1",
            embed={"model": model, "field_map": {"text": "chunk_text"}},
        )
        for _ in range(120):                 # tunggu maksimum ~2 menit
            if _index_ready(pc, name):
                break
            time.sleep(1)
        print("  ✓ Index siap")
    return pc.Index(name)


def _load_ckpt() -> set:
    if CHECKPOINT.exists():
        try:
            return set(json.loads(CHECKPOINT.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_ckpt(done: set):
    CHECKPOINT.write_text(json.dumps(sorted(done)), encoding="utf-8")


# ── Indexing utama ────────────────────────────────────────────────────────────

def index_all(dest: Path, index_name: str, model: str, namespace: str, limit: int):
    pc = get_pinecone()
    index = ensure_index(pc, index_name, model)
    meta_map = load_meta_map(dest)
    done = _load_ckpt()

    pdfs = [p for p in dest.rglob("*.pdf")]
    if limit:
        pdfs = pdfs[:limit]
    if not pdfs:
        print(f"\n📂 Tidak ada PDF di {dest}. Jalankan ingest_archive.py --apply dulu.\n")
        return

    print(f"\n{'='*64}\n  KS Pinecone Indexer — {len(pdfs)} PDF di {dest}")
    print(f"  index='{index_name}' model='{model}' ns='{namespace}'\n{'='*64}\n")

    n_doc, n_skip, n_vec, batch = 0, 0, 0, []

    def flush():
        nonlocal batch, n_vec
        if batch:
            index.upsert_records(namespace=namespace, records=batch)
            n_vec += len(batch)
            batch = []

    for p in pdfs:
        rel = str(p.relative_to(dest))
        base = doc_id(rel)
        if base in done:
            n_skip += 1
            continue
        meta = meta_map.get(str(p.resolve()), {})
        recs = records_for(p, base, meta)
        if not recs:
            print(f"  ⚠ {p.name}: tak ada teks (scan gagal OCR?) — skip")
            n_skip += 1
            done.add(base)          # jangan coba ulang terus
            continue
        for r in recs:
            batch.append(r)
            if len(batch) >= BATCH:
                flush()
        flush()                      # tuntaskan per dokumen → checkpoint konsisten
        done.add(base)
        n_doc += 1
        if n_doc % 25 == 0:
            _save_ckpt(done)
            print(f"  … {n_doc} dokumen, {n_vec} chunk")
        comp = (meta.get('company') or '?')[:22]
        print(f"  ✓ {p.name[:48]:48} [{len(recs)} chunk] {comp}")

    _save_ckpt(done)
    print(f"\n{'='*64}")
    print(f"  ✅ {n_doc} dokumen di-index ({n_vec} chunk), {n_skip} skip")
    print(f"  🔢 Namespace '{namespace}' di index '{index_name}'")
    print(f"{'='*64}\n")


# ── Query helper (tes) ────────────────────────────────────────────────────────

def search(query: str, top_k: int = 5, index_name: str = INDEX_NAME,
           namespace: str = NAMESPACE, **filters):
    """Cari natural-language + filter metadata opsional.
       search("faktur pajak", company="PT Krakatau Shipyard", department="Finance")"""
    pc = get_pinecone()
    index = pc.Index(index_name)
    flt = {k: {"$eq": v} for k, v in filters.items() if v}
    q = {"inputs": {"text": query}, "top_k": top_k}
    if flt:
        q["filter"] = flt
    res = index.search(namespace=namespace, query=q,
                       fields=["doc_name", "company", "department", "project",
                               "filename", "expire_date", "page_number", "chunk_text"])

    def g(obj, key, default=None):           # tahan response object ATAU dict
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    hits = g(g(res, "result"), "hits") or []
    print(f"\n🔍 '{query}'" + (f"  filter={flt}" if flt else "") + f"  → {len(hits)} hit")
    print("─" * 60)
    for h in hits:
        f = g(h, "fields") or {}
        score = g(h, "score", 0.0) or 0.0    # SDK Hit: property .score (wire _score)
        proj = f" | proyek: {f.get('project')}" if f.get("project") else ""
        exp  = f" | expire: {f.get('expire_date')}" if f.get("expire_date") else ""
        print(f"  [{score:.3f}] {f.get('doc_name','')}  (hal {f.get('page_number','?')})")
        print(f"          {f.get('company','')} / {f.get('department','')}{proj}")
        print(f"          {f.get('filename','')}{exp}")
        print(f"          “{(f.get('chunk_text','') or '')[:120]}…”\n")
    return res


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Index Arsip_Rapih → Pinecone (integrated embedding)")
    ap.add_argument("--dest", default=str(DEST_DIR), help="folder hasil ingest (default D:\\Arsip_Rapih)")
    ap.add_argument("--index", default=INDEX_NAME)
    ap.add_argument("--model", default=EMBED_MODEL)
    ap.add_argument("--namespace", default=NAMESPACE)
    ap.add_argument("--limit", type=int, default=None, help="batasi jumlah PDF (uji coba)")
    ap.add_argument("--reset-checkpoint", action="store_true", help="abaikan checkpoint, index ulang semua")
    ap.add_argument("--query", default=None, help="mode tes: cari kalimat ini lalu keluar")
    args = ap.parse_args()

    if args.query:
        search(args.query, index_name=args.index, namespace=args.namespace)
        return

    if args.reset_checkpoint and CHECKPOINT.exists():
        CHECKPOINT.unlink()
    index_all(Path(args.dest), args.index, args.model, args.namespace, args.limit)


if __name__ == "__main__":
    main()
