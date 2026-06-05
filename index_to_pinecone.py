"""
KS Pinecone Indexer — PT Krakatau Shipyard
==========================================
Ambil dokumen yang sudah di-split dari ./output, embed, lalu index ke Pinecone.
Jalankan setelah process_docs.py selesai dan hasil review manual OK.

Cara pakai:
    pip install pinecone-client anthropic pdfplumber python-dotenv
    python index_to_pinecone.py
"""

import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, date

import pdfplumber
from anthropic import Anthropic
from pinecone import Pinecone, ServerlessSpec
from dotenv import load_dotenv

load_dotenv()

# ── Konfigurasi ──────────────────────────────────────────────────────────────
OUTPUT_DIR    = Path("./output")
LOG_FILE      = Path("./processing_log.json")
INDEX_NAME    = "ks-documents"
EMBED_MODEL   = "text-embedding-3-small"   # OpenAI — atau ganti ke provider lain
CHUNK_SIZE    = 800    # karakter per chunk
CHUNK_OVERLAP = 100

# Pinecone
pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))

# Claude (untuk query nanti, bukan untuk embed)
claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Embedding — pakai OpenAI text-embedding ───────────────────────────────────
# Kalau mau pakai provider lain, ganti fungsi ini saja

try:
    from openai import OpenAI
    oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def embed(text: str) -> list[float]:
        resp = oai.embeddings.create(model=EMBED_MODEL, input=text[:8000])
        return resp.data[0].embedding

    EMBED_DIM = 1536  # dimensi text-embedding-3-small

except ImportError:
    print("⚠ openai tidak terinstall. Jalankan: pip install openai")
    print("  Atau ganti fungsi embed() di script ini dengan provider lain.")
    exit(1)


# ── Utilitas ─────────────────────────────────────────────────────────────────

def extract_text(pdf_path: Path) -> str:
    """Ekstrak teks dari PDF."""
    parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
    except Exception as e:
        print(f"  ⚠ Gagal ekstrak {pdf_path.name}: {e}")
    return "\n".join(parts)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Pecah teks jadi chunks dengan overlap."""
    if not text.strip():
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += size - overlap
    return chunks


def doc_id(pdf_path: Path) -> str:
    """ID unik per dokumen berdasarkan path."""
    return hashlib.md5(str(pdf_path.resolve()).encode()).hexdigest()[:16]


def ensure_index():
    """Buat Pinecone index kalau belum ada."""
    existing = [i.name for i in pc.list_indexes()]
    if INDEX_NAME not in existing:
        print(f"  📦 Membuat Pinecone index '{INDEX_NAME}'...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
        print(f"  ✓ Index siap")
    return pc.Index(INDEX_NAME)


# ── Load log dari process_docs.py ────────────────────────────────────────────

def load_doc_metadata() -> dict:
    """
    Buat mapping: absolute output path → metadata dari processing_log.json.
    Key dibuat dari output_file yang di-resolve ke path absolut, supaya cocok
    dengan pdf_path.resolve() di indexer (output_file disimpan sebagai path relatif).
    """
    meta_map = {}
    if not LOG_FILE.exists():
        return meta_map
    try:
        entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        for e in entries:
            if e.get("output_file") and e.get("status") == "ok":
                key = str(Path(e["output_file"]).resolve())
                meta_map[key] = e
    except Exception:
        pass
    return meta_map


# ── Main indexer ──────────────────────────────────────────────────────────────

def index_all():
    index    = ensure_index()
    meta_map = load_doc_metadata()

    # Kumpulkan semua PDF di output
    pdf_files = list(OUTPUT_DIR.rglob("*.pdf"))
    if not pdf_files:
        print(f"\n📂 Tidak ada PDF di {OUTPUT_DIR}. Jalankan process_docs.py dulu.\n")
        return

    print(f"\n{'='*60}")
    print(f"  KS Pinecone Indexer — PT Krakatau Shipyard")
    print(f"  {len(pdf_files)} file PDF akan di-index")
    print(f"{'='*60}\n")

    indexed   = 0
    skipped   = 0
    total_vec = 0

    for pdf_path in pdf_files:
        print(f"📄 {pdf_path.name}")
        doc_meta = meta_map.get(str(pdf_path.resolve()), {})
        base_id  = doc_id(pdf_path)

        # Cek apakah sudah pernah di-index (ada vector dengan prefix ini)
        try:
            check = index.fetch(ids=[f"{base_id}_0"])
            if check.vectors:
                print(f"  ↩ Sudah di-index, skip")
                skipped += 1
                continue
        except Exception:
            pass

        text = extract_text(pdf_path)
        if not text.strip():
            print(f"  ⚠ Tidak ada teks (mungkin scanned image) — skip")
            skipped += 1
            continue

        chunks = chunk_text(text)
        if not chunks:
            skipped += 1
            continue

        vectors = []
        for i, chunk in enumerate(chunks):
            try:
                vector = embed(chunk)
                metadata = {
                    "doc_name":     doc_meta.get("doc_name", pdf_path.stem),
                    "company":      doc_meta.get("company", ""),
                    "counterparty": doc_meta.get("counterparty") or "",
                    "department":   doc_meta.get("department", ""),
                    "project":      doc_meta.get("project") or "",
                    "subfolder":    doc_meta.get("subfolder", ""),
                    "relpath":      doc_meta.get("relpath", ""),
                    "source_file":  doc_meta.get("source_file", ""),
                    "filename":     pdf_path.name,
                    "filepath":     str(pdf_path.resolve()),
                    "expire_date":  doc_meta.get("expire_date") or "",
                    "doc_number":   doc_meta.get("doc_number") or "",
                    "chunk_index":  i,
                    "chunk_total":  len(chunks),
                    "text":         chunk,          # simpan teks untuk retrieval
                    "indexed_at":   datetime.now().isoformat(),
                }
                vectors.append({
                    "id":       f"{base_id}_{i}",
                    "values":   vector,
                    "metadata": metadata,
                })
            except Exception as e:
                print(f"  ✗ Gagal embed chunk {i}: {e}")

        if vectors:
            # Upsert ke Pinecone (batch 100)
            batch_size = 100
            for b in range(0, len(vectors), batch_size):
                index.upsert(vectors=vectors[b:b+batch_size])
            total_vec += len(vectors)
            indexed += 1
            print(f"  ✓ {len(chunks)} chunks di-index → {pdf_path.parent.name}/")

    print(f"\n{'='*60}")
    print(f"  ✅ Selesai: {indexed} file di-index, {skipped} skip")
    print(f"  🔢 Total vectors di Pinecone: {total_vec}")
    print(f"{'='*60}\n")


# ── Query helper — buat testing ───────────────────────────────────────────────

def search(query: str, top_k: int = 5, company: str = None,
           project: str = None, department: str = None):
    """
    Cari dokumen di Pinecone pakai kalimat natural, opsional filter metadata.
    Contoh:
        search("sertifikat BKI yang masih berlaku")
        search("invoice termin", company="PT Krakatau Shipyard", department="Finance")
        search("gambar teknik", project="Pembangunan 2 Tug Boat 2024")
    """
    index  = pc.Index(INDEX_NAME)
    vector = embed(query)

    filter_dict = {}
    if company:
        filter_dict["company"] = {"$eq": company}
    if project:
        filter_dict["project"] = {"$eq": project}
    if department:
        filter_dict["department"] = {"$eq": department}

    results = index.query(
        vector=vector,
        top_k=top_k,
        include_metadata=True,
        filter=filter_dict if filter_dict else None
    )

    print(f"\n🔍 Query: '{query}'" + (f"  (filter: {filter_dict})" if filter_dict else ""))
    print(f"{'─'*50}")
    for match in results.matches:
        m = match.metadata
        expire = f" | expire: {m['expire_date']}" if m.get("expire_date") else ""
        proj   = f" | proyek: {m['project']}" if m.get("project") else ""
        print(f"  [{match.score:.3f}] {m.get('doc_name','')}")
        print(f"          {m.get('company','')} / {m.get('department','')}{proj}")
        print(f"          File: {m.get('filename','')}{expire}")
        print(f"          Preview: {m.get('text','')[:120]}...")
        print()

    return results


if __name__ == "__main__":
    missing = []
    if not os.getenv("PINECONE_API_KEY"):
        missing.append("PINECONE_API_KEY")
    if not os.getenv("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")

    if missing:
        print(f"\n✗ Key berikut tidak ada di .env: {', '.join(missing)}\n")
        exit(1)

    index_all()

    # Test query setelah index
    print("\n── Test query ──────────────────────────────────────────")
    search("sertifikat BKI galangan kapal")
    search("laporan keuangan terbaru", department="Finance")
    search("kontrak pembangunan kapal", company="PT Krakatau Shipyard")
