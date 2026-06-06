"""
Diagnosa kenapa banyak file '· skip' (recs kosong) di validate_retrieval.

TAHAP 1 (GRATIS): untuk korpus yang sama, laporkan per file —
  - pdfplumber kebuka? berapa halaman?
  - panjang teks pdfplumber tiap halaman (1..cap) + flag garbled
  - render halaman-1 berhasil? (pdf2image/poppler)
Tanpa panggil vision. Ini cukup buat misahin:
  (a) pdfplumber error  (b) render gagal (poppler)  (c) image-only → butuh vision
TAHAP 2 (BAYAR, opsional): vision-probe hal-1 cuma untuk kandidat skip,
  dinyalakan dengan argumen 'probe' → python diagnose_skips.py probe
"""
import os, sys
from pathlib import Path
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
import pdfplumber
import index_to_pinecone as IX

IX.OCR_PAGE_CAP = 3
IX.VISION_MODEL = "claude-haiku-4-5"
CAP = IX.OCR_PAGE_CAP
PROBE = len(sys.argv) > 1 and sys.argv[1] == "probe"
ROOT = Path("D:/")

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
print(f"Korpus: {len(corpus)} file | CAP={CAP} | probe_vision={PROBE}\n")

skips = []
for p in corpus:
    # pdfplumber
    try:
        with pdfplumber.open(p) as pdf:
            npages = len(pdf.pages)
            lens = []
            for i, page in enumerate(pdf.pages[:CAP]):
                t = (page.extract_text() or "").strip()
                lens.append((i + 1, len(t), IX._looks_garbled(t)))
        pp_ok = True
    except Exception as e:
        npages, lens, pp_ok = 0, [], False
        perr = f"{type(e).__name__}: {e}"

    # render hal-1 (gratis, poppler) + jumlah halaman poppler
    img = IX._render_page(p, 1)
    render = f"{img.size[0]}x{img.size[1]}" if img is not None else "GAGAL"
    popp = IX._poppler_pagecount(p)

    # apakah ada teks pdfplumber non-kosong & non-garbled dalam cap?
    has_clean = any(L > 0 and not g for _, L, g in lens)
    # halaman yang BUTUH vision (kosong / garbled) dalam cap
    need_vision = [pn for pn, L, g in lens if L == 0 or g]

    tag = ""
    if not pp_ok:
        tag = f"PDFPLUMBER-ERROR ({perr})"
        skips.append(p)
    elif npages == 0:
        tag = "0-HALAMAN"
        skips.append(p)
    elif has_clean:
        tag = "OK (ada teks digital bersih)"
    elif img is None:
        tag = "RENDER-GAGAL → vision mustahil → SKIP"
        skips.append(p)
    else:
        tag = f"IMAGE-ONLY → butuh vision hal {need_vision}"
        skips.append(p)

    print(f"  {p.name[:46]:46s} | plumber_hal={npages:>3} popp_hal={popp:>3} | "
          f"render={render:>9} | plumber_len={[L for _,L,_ in lens]} | {tag}")

print(f"\nKandidat skip (perlu vision / bermasalah): {len(skips)}/{len(corpus)}")

if PROBE and skips:
    print("\n── TAHAP 2: vision-probe hal-1 (haiku) untuk kandidat skip ──")
    empty = 0
    for p in skips[:12]:
        v = IX._vision_page(p, 1)
        status = f"{len(v)} char" if v else "KOSONG"
        if not v:
            empty += 1
        print(f"  {p.name[:50]:50s} | vision hal-1: {status}  {v[:60]!r}")
    print(f"\n  vision balik KOSONG: {empty}/{min(12,len(skips))} "
          f"(kalau tinggi → masalah di vision/render, bukan korpus)")
