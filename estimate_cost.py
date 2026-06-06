"""
Estimasi biaya index SEMUA file ke Pinecone.
Sampel ACAK lintas seluruh D: (proporsional komposisi arsip), jalanin pipeline
ASLI (hybrid OCR haiku/sonnet, cap per-doc-type, extraction) + meter biaya,
lalu ekstrapolasi per-grup (PDF / IMAGE / OFFICE / FALLBACK) ke jumlah penuh.

  python estimate_cost.py [N]      # default N=60
"""
import os, sys, random
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
from pathlib import Path
from collections import Counter, defaultdict
import index_to_pinecone as IX
import extract_fields as EF

random.seed(7)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
ROOT = Path("D:/")
IX._enable_cost_meter()

IMG = IX._IMG_EXT
OFFICE = IX._DOCX_EXT | IX._XLSX_EXT
def group(ext):
    if ext == ".pdf": return "PDF"
    if ext in IMG: return "IMAGE"
    if ext in OFFICE: return "OFFICE"
    return "FALLBACK"

print("Mengumpulkan daftar file di D: …")
allf = []
full = Counter()
for dp, _, fs in os.walk(ROOT):
    if "Arsip_Rapih" in dp or "Arsip_DryRun" in dp:
        continue
    for f in fs:
        e = Path(f).suffix.lower()
        if e in IX.SUPPORTED_EXT:
            full[e] += 1
            allf.append(Path(dp) / f)
print(f"Total file terdukung: {len(allf):,}")

sample = random.sample(allf, min(N, len(allf)))
gcost, gn = defaultdict(float), Counter()
print(f"\nSampel acak {len(sample)} file (pipeline asli + meter):")
print(f"  {'grup':9s} {'$/file':9s} file")
print("  " + "-" * 60)
for p in sample:
    g = group(p.suffix.lower())
    meta = {"doc_name": p.stem, "department": "", "subfolder": p.stem, "company": ""}
    before = IX._USAGE["cost"]
    try:
        pages = IX.extract_pages_any(p, cap=IX.cap_for(meta))
        EF.enrich(meta, " ".join(t for _, t in pages), use_llm=True)
    except Exception as ex:
        print(f"  ⚠ {p.name[:40]}: {type(ex).__name__}")
        continue
    c = IX._USAGE["cost"] - before
    gcost[g] += c; gn[g] += 1
    print(f"  {g:9s} ${c:<8.4f} {p.name[:44]}")

# ── Ekstrapolasi per grup ──
full_group = Counter()
for e, c in full.items():
    full_group[group(e)] += c

print("\n" + "=" * 64)
print(f"  BIAYA SAMPEL: ${IX._USAGE['cost']:.4f} ({len(sample)} file)")
print("=" * 64)
print(f"  {'grup':9s} {'sampel':7s} {'$/file':10s} {'×full':8s} {'= est':10s}")
est = 0.0
for g in ("PDF", "IMAGE", "OFFICE", "FALLBACK"):
    avg = gcost[g] / gn[g] if gn[g] else 0.0
    sub = avg * full_group[g]
    est += sub
    print(f"  {g:9s} n={gn[g]:<4d}  ${avg:<8.4f}  {full_group[g]:>6,}   ${sub:>8.2f}")
print("-" * 64)
print(f"  TOTAL {sum(full_group.values()):,} file  →  ESTIMASI ~${est:.0f}")
print(f"  Rentang (±40%): ${est*0.6:.0f} – ${est*1.4:.0f}")
print("\n  Catatan: tanpa archive_log, dept ditebak dari nama → cap/extraction sedikit")
print("  beda dari produksi. Embedding Pinecone sendiri GRATIS (integrated); biaya = Claude saja.")
