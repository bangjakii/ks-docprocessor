"""
Validasi retrieval di KORPUS NYATA.
Index ~60 PDF asli (beragam jenis) pakai Claude vision (haiku, hemat) ke index
sementara 'ks-valtest', lalu query hal spesifik yang HARUS ada jawabannya.
Di akhir index sementara dihapus.

Jalankan:  python validate_retrieval.py
"""
import os, time
from pathlib import Path
import index_to_pinecone as IX

IX.VISION_MODEL  = "claude-haiku-4-5"   # hemat untuk transkripsi
IX.OCR_PAGE_CAP  = 8                     # cukup buat konten dalam bundel (mis. faktur hal 4)
TEST_INDEX = "ks-valtest"
NS = "__default__"
ROOT = Path("D:/")

# Korpus variatif — pastikan ada doc yang bisa di-query jawabannya.
BUCKETS = {
    "invoice/faktur": ("invoice", "faktur", "tagihan", "kwitansi", "pembayaran"),
    "kontrak/spk":    ("kontrak", "perjanjian", "spk", "perintah kerja"),
    "penawaran":      ("penawaran", "sph", "quotation"),
    "bast/serah":     ("bast", "berita acara", "serah terima"),
    "engineering":    ("drawing", "gambar", "ga ", "general arrangement", "abs"),
    "legal/pajak":    ("akta", "npwp", "pph", "spt", "izin", "nib"),
}
PER = 10
found = {k: [] for k in BUCKETS}
for dp, dirs, files in os.walk(ROOT):
    if "Arsip_Rapih" in dp:
        continue
    for f in files:
        if not f.lower().endswith(".pdf"):
            continue
        low = f.lower()
        for k, hints in BUCKETS.items():
            if len(found[k]) < PER and any(h in low for h in hints):
                found[k].append(Path(dp) / f)
                break
    if all(len(v) >= PER for v in found.values()):
        break

corpus = [p for ps in found.values() for p in ps]
print("Korpus:", {k: len(v) for k, v in found.items()}, f"= {len(corpus)} file")

pc = IX.get_pinecone()
index = IX.retry(lambda: IX.ensure_index(pc, TEST_INDEX, IX.EMBED_MODEL), what="ensure")

print("\n── Indexing (vision haiku) ──")
n = 0
for p in corpus:
    meta = {"doc_name": p.stem, "subfolder": "", "company": "", "department": ""}
    recs = IX.records_for(p, IX.doc_id(p.name), meta)
    if not recs:
        print(f"  · skip {p.name[:46]}")
        continue
    IX.retry(lambda: index.upsert_records(namespace=NS, records=recs), what="upsert")
    n += 1
    print(f"  ✓ {p.name[:52]} [{len(recs)} chunk]")
print(f"\n{n} dokumen ter-index. Tunggu settle..."); time.sleep(15)

# Query: campuran tematik + ENTITAS SPESIFIK (yang harusnya nemu doc tepat)
QUERIES = [
    "faktur pajak PPN",
    "kwitansi pembayaran progress",
    "surat perintah kerja undocking kapal",
    "penawaran harga tongkang tug boat",
    "berita acara serah terima pekerjaan",
    "gambar teknik approval ABS",
]
print("\n" + "=" * 64 + "\n  HASIL QUERY\n" + "=" * 64)
for q in QUERIES:
    try:
        IX.search(q, top_k=4, index_name=TEST_INDEX)
    except Exception as e:
        print(f"  ✗ '{q}': {type(e).__name__}")

print("\n── CLEANUP ──")
try:
    IX.retry(lambda: pc.delete_index(TEST_INDEX), what="delete")
    print(f"  🗑  '{TEST_INDEX}' dihapus.")
except Exception as e:
    print(f"  ⚠ hapus manual ks-valtest nanti: {e}")
