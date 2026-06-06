"""
Bukti GRATIS (tesseract saja, TANPA vision/API): faktur pajak sering NYEMPIL di
dalam bundel yang namanya BUKAN faktur. Scan halaman-per-halaman bundel kandidat,
deteksi tanda khas faktur pajak. Output: file + halaman yang ngandung faktur.

  python detect_buried_faktur.py
"""
import os, sys
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
from pathlib import Path
import index_to_pinecone as IX

if IX.TESSERACT_PATH:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = IX.TESSERACT_PATH

ROOT = Path("D:/")
PAGE_CAP = 12          # scan s/d 12 hal per bundel (gratis)
SIG = ("kode dan nomor seri faktur pajak", "pengusaha kena pajak",
       "dasar pengenaan pajak", "faktur pajak")
# Bundel kandidat: nama mengandung ini TAPI bukan 'faktur'/'pajak'/'ppn'
CAND_HINT = ("invoice", "realisasi", "tagihan", "pembayaran", "surat keluar")
EXCLUDE = ("faktur", "pajak", "ppn")

cands = []
for dp, _, fs in os.walk(ROOT):
    if "Arsip_Rapih" in dp:
        continue
    for f in fs:
        low = f.lower()
        if not low.endswith(".pdf"):
            continue
        if any(h in low for h in CAND_HINT) and not any(x in low for x in EXCLUDE):
            cands.append(Path(dp) / f)
    if len(cands) >= 25:
        break

print(f"Bundel kandidat (nama BUKAN faktur): {len(cands)}\n")
hits = []
for p in cands:
    npages = IX._poppler_pagecount(p) or 1
    found_pages = []
    for pn in range(1, min(npages, PAGE_CAP) + 1):
        img = IX._render_page(p, pn, dpi=180)
        if img is None:
            continue
        text, n, conf = IX._tesseract_scored(img)
        low = text.lower()
        if any(s in low for s in SIG):
            which = [s for s in SIG if s in low]
            found_pages.append((pn, which, " ".join(text.split())[:70]))
    flag = "★ ADA FAKTUR" if found_pages else "—"
    print(f"  {flag:13s} | {p.name[:44]:44s} | hal={npages}")
    for pn, which, prev in found_pages:
        print(f"        └ hal {pn}: {which}  | {prev}")
    if found_pages:
        hits.append((p, found_pages))

print(f"\n── HASIL ── bundel berisi faktur tersembunyi: {len(hits)}/{len(cands)}")
for p, fp in hits:
    pages = ", ".join(str(pn) for pn, _, _ in fp)
    print(f"  • {p.name}  (faktur di hal {pages})")
print("\nKalau ada ★ → faktur pajak EMANG nyempil di bundel non-faktur. Itu yang")
print("harus dicari lewat Pinecone (per-halaman), bukan lewat nama file.")
