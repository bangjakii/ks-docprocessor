"""
Bukti SEBENARNYA: faktur pajak nyempil di dalam bundel yg namanya BUKAN faktur,
dan Pinecone (chunking per-halaman) bisa nemu halaman fakturnya — INI alasan
kita simpan bundel utuh, bukan cari lewat nama file.

detect_buried_faktur.py udah nunjukin (gratis) bundel mana yg berisi faktur.
Di sini kita index bundel UTUH + pengecoh, lalu query 'faktur pajak PPN' —
harusnya yg balik = HALAMAN faktur di dalam bundel (mis. Invoice...909 hal 4).

  python prove_faktur.py     (mayoritas tesseract gratis, ~$0.02)
"""
import os, sys, time
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
from pathlib import Path
import index_to_pinecone as IX

IX.VISION_MODEL = "claude-haiku-4-5"
IX.OCR_PAGE_CAP = 6          # cukup buat nyampe hal faktur (biasanya hal 2-4)
TEST = "ks-valfaktur"
NS = "__default__"
ROOT = Path("D:/")

# Bundel berisi faktur tersembunyi (dari detect_buried_faktur.py) — nama BUKAN faktur
BURIED = ("Invoice Tongkang Soekawati 909.pdf", "Invoice Tongkang Terang 3002.pdf",
          "Surat Permohonan Pembayaran Pekerjaan (BG. Titan 36).pdf",
          "Invoice Odyssey (Pembelian Kapal MT. Birdie).pdf",
          "Scan kwitansi pembayaran tb tenang 1602.pdf")
# Pengecoh: bundel/dok TANPA faktur
DISTRACT = ("SPK CV Enisya - undocking.pdf", "penawaran harga tongkang dan tug boat",
            "Berita Acara BG. Titan 36.pdf", "Laporan realisasi pekerjaan-Soekawati 909",
            "Kwitansi Pembayaran (Termin ke-2).pdf")


def find(names):
    out = {}
    for dp, _, fs in os.walk(ROOT):
        if "Arsip_Rapih" in dp:
            continue
        for f in fs:
            for n in names:
                if n.lower() in f.lower() and f.lower().endswith(".pdf") and n not in out:
                    out[n] = Path(dp) / f
        if len(out) == len(names):
            break
    return list(out.values())


buried = find(BURIED)
distract = find(DISTRACT)
buried_names = {p.stem for p in buried}
print("BUNDEL berisi faktur tersembunyi (nama BUKAN faktur):")
for p in buried: print("  ★", p.name)
print("PENGECOH (tanpa faktur):")
for p in distract: print("  ·", p.name)

pc = IX.get_pinecone()
index = IX.retry(lambda: IX.ensure_index(pc, TEST, IX.EMBED_MODEL), what="ensure")

print("\n── Indexing UTUH (hybrid, per-halaman) ──")
for p in buried + distract:
    meta = {"doc_name": p.stem, "subfolder": "", "company": "", "department": ""}
    recs = IX.records_for(p, IX.doc_id(p.name), meta)
    star = "★" if p.stem in buried_names else "·"
    if not recs:
        print(f"  {star} skip {p.name[:46]}"); continue
    IX.retry(lambda: index.upsert_records(namespace=NS, records=recs), what="upsert")
    print(f"  {star} {p.name[:48]} [{len(recs)} chunk, {recs[-1]['page_number']} hal]")
print("settle..."); time.sleep(15)

print("\n" + "=" * 60)
print("TARGET: query harus balikin HALAMAN faktur di dalam bundel ★")
for q in ("faktur pajak PPN", "kode dan nomor seri faktur pajak",
          "pengusaha kena pajak dasar pengenaan pajak"):
    print(f"\n🔍 '{q}'")
    try:
        IX.search(q, top_k=4, index_name=TEST)
    except Exception as e:
        print("  ✗", type(e).__name__, e)

print("\n── CLEANUP ──")
try:
    IX.retry(lambda: pc.delete_index(TEST), what="del")
    print("  🗑 dihapus.")
except Exception as e:
    print("  ⚠ hapus manual:", e)
