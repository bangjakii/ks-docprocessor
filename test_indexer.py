"""
TEST RUN indexer (jalur kode asli: hybrid OCR -> enrich field -> fallback record
-> index -> query) di sampel mentah D:, sebelum Arsip_Rapih ada.
Fase 1 (selalu tampil): bangun record + tunjukin field hasil extraction.
Fase 2 (best-effort)  : upsert ke index sementara + query.

  python test_indexer.py
"""
import os, sys, time
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
from pathlib import Path
import index_to_pinecone as IX
import extract_fields as EF

IX.VISION_MODEL = "claude-haiku-4-5"
IX.OCR_PAGE_CAP = 4
TEST, NS, ROOT = "ks-testindex", "__default__", Path("D:/")

# sampel beragam + department (biar targeting extraction kebangun)
BUCKETS = [
    ("Finance",     ("invoice", "kwitansi", "faktur", "tagihan")),
    ("Legal",       ("kontrak", "perjanjian", "spk", "akta", "izin", "sertifikat", "garansi")),
    ("Sales",       ("penawaran", "sph")),
    ("Operasional", ("berita acara", "serah terima")),
    ("Engineering", ("general arrangement", "drawing", "abs", "gambar")),
]
PER = 3
sample = []
seen = {d: 0 for d, _ in BUCKETS}
for dp, _, fs in os.walk(ROOT):
    if "Arsip_Rapih" in dp:
        continue
    for f in fs:
        low = f.lower()
        if not low.endswith(".pdf"):
            continue
        for dept, hints in BUCKETS:
            if seen[dept] < PER and any(h in low for h in hints):
                sample.append((Path(dp) / f, dept)); seen[dept] += 1; break
    if all(v >= PER for v in seen.values()):
        break

print(f"Sampel: {len(sample)} file\n")
print("=" * 100)
print("  FASE 1 — record + hasil extraction (hybrid OCR + regex/LLM)")
print("=" * 100)
print(f"  {'dept':11s} {'doc_number':22s} {'expire':11s} {'counterparty':22s} {'chunk':5s} file")
print("  " + "-" * 110)

all_recs, sample_rec = [], None
for p, dept in sample:
    meta = {"doc_name": p.stem, "department": dept, "subfolder": p.stem,
            "company": "", "project": ""}
    pages = IX.page_texts(p)
    EF.enrich(meta, " ".join(t for _, t in pages), use_llm=True)
    recs = IX.records_for(p, IX.doc_id(p.name), meta, pages=pages)
    all_recs.append((p, recs))
    if sample_rec is None and len(recs) and recs[0].get("doc_number"):
        sample_rec = recs[0]
    print(f"  {dept:11s} {str(meta.get('doc_number'))[:22]:22s} "
          f"{str(meta.get('expire_date'))[:11]:11s} {str(meta.get('counterparty'))[:22]:22s} "
          f"{len(recs):<5d} {p.name[:34]}")

print("\n  Contoh 1 record utuh (semua field metadata):")
if sample_rec:
    for k, v in sample_rec.items():
        if k == "chunk_text":
            v = (str(v)[:70] + "…")
        print(f"      {k:14s}: {v}")

# ── FASE 2: index + query (best-effort) ──
print("\n" + "=" * 100)
print("  FASE 2 — index ke Pinecone + query (best-effort)")
print("=" * 100)
try:
    pc = IX.get_pinecone()
    index = IX.retry(lambda: IX.ensure_index(pc, TEST, IX.EMBED_MODEL), what="ensure")
    for p, recs in all_recs:
        IX.retry(lambda: index.upsert_records(namespace=NS, records=recs), what="upsert")
    print(f"  ✓ {sum(len(r) for _, r in all_recs)} chunk ter-upsert. settle…")
    time.sleep(12)
    for q in ("penawaran harga kapal", "berita acara serah terima", "gambar general arrangement"):
        print(f"\n🔍 '{q}'")
        IX.search(q, top_k=3, index_name=TEST)
    IX.retry(lambda: pc.delete_index(TEST), what="del")
    print("\n  🗑 index test dihapus.")
except Exception as e:
    print(f"  ⚠ Fase 2 gagal (jaringan Pinecone?): {type(e).__name__} — {e}")
    print("  Fase 1 di atas tetap valid (itu output indexer-nya).")
