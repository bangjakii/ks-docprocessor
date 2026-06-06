"""
Kalibrasi router hybrid (GRATIS — tesseract + keputusan routing saja, TANPA vision).
Lihat tiap halaman scan diarahkan ke mana: TESSERACT (gratis) / VISION (bayar) / SKIP.
Pakai buat setel OCR_CONF_OK & OCR_MIN_INK sebelum full run.

  python calibrate_ocr.py            # sampel default dari D:
  python calibrate_ocr.py "nama.pdf" # paksa file tertentu (mis. yg kamu tau tulisan tangan)
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
# Sampel beragam jenis (cetak, tanda tangan, stempel, drawing, form)
HINTS = ("invoice", "faktur", "kwitansi", "pembayaran", "kontrak", "spk", "perjanjian",
         "penawaran", "sph", "berita acara", "serah terima", "absensi", "drawing",
         "general arrangement", "abs", "akta", "izin", "surat", "form", "naskah", "tenaga")
PER_HINT = 2
MAXN = 28


def route(img):
    """Replika logika read_scan_page hybrid TANPA panggil vision."""
    text, n, conf = IX._tesseract_scored(img)
    g = IX._looks_garbled(text)
    if n == 0:
        return "SKIP·empty", text, n, conf, g
    if conf >= IX.OCR_CONF_OK and not g:
        return "TESSERACT", text, n, conf, g
    if n < IX.OCR_MIN_INK:
        return "SKIP·drawing", text, n, conf, g
    return "VISION", text, n, conf, g


# kumpulkan sampel
args = [a for a in sys.argv[1:]]
files = []
if args:
    for dp, _, fs in os.walk(ROOT):
        if "Arsip_Rapih" in dp:
            continue
        for fn in fs:
            if any(a.lower() in fn.lower() for a in args) and fn.lower().endswith(".pdf"):
                files.append(Path(dp) / fn)
else:
    seen = {h: 0 for h in HINTS}
    for dp, _, fs in os.walk(ROOT):
        if "Arsip_Rapih" in dp:
            continue
        for fn in fs:
            if not fn.lower().endswith(".pdf"):
                continue
            low = fn.lower()
            for h in HINTS:
                if h in low and seen[h] < PER_HINT:
                    files.append(Path(dp) / fn); seen[h] += 1; break
            if len(files) >= MAXN:
                break
        if len(files) >= MAXN:
            break

print(f"Sampel: {len(files)} file | CONF_OK={IX.OCR_CONF_OK} MIN_INK={IX.OCR_MIN_INK}\n")
print(f"  {'DECISION':13s} {'n':>4} {'conf':>5} {'grbl':>4}  file / preview")
print("  " + "-" * 96)
tally = {}
for p in files:
    img = IX._render_page(p, 1, dpi=180)
    if img is None:
        print(f"  {'RENDER-GAGAL':13s}    -     -    -   {p.name[:40]}")
        continue
    dec, text, n, conf, g = route(img)
    tally[dec] = tally.get(dec, 0) + 1
    prev = " ".join(text.split())[:46]
    print(f"  {dec:13s} {n:>4} {conf:>5.0f} {str(g):>4}  {p.name[:34]:34s} | {prev}")

print("\n── DISTRIBUSI ROUTING ──")
for k in sorted(tally):
    print(f"  {k:13s}: {tally[k]}")
free = tally.get("TESSERACT", 0)
paid = tally.get("VISION", 0)
skip = tally.get("SKIP·empty", 0) + tally.get("SKIP·drawing", 0)
tot = max(1, free + paid + skip)
print(f"\n  Gratis (tesseract): {free}/{tot} = {100*free/tot:.0f}%"
      f" | Bayar (vision): {paid}/{tot} = {100*paid/tot:.0f}%"
      f" | Skip: {skip}/{tot}")
print("\nCek manual: baris TESSERACT preview-nya harus terbaca rapi. Kalau ada yg")
print("teksnya ngaco tapi ke-TESSERACT → CONF_OK kurang tinggi. Kalau cetak bersih")
print("malah ke-VISION → CONF_OK ketinggian. Setel via env OCR_CONF_OK lalu ulang.")
