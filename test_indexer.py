"""
TEST RUN indexer (jalur kode asli) + ESTIMASI BIAYA.
Sampel campuran tipe (pdf/jpg/docx/xlsx) dari D:, pakai setting PRODUKSI
(hybrid: haiku utk vision normal, opus utk scan terjelek). Lacak token tiap
panggilan Claude → hitung biaya sampel → ekstrapolasi ke seluruh arsip per-tipe.

Fase 1 (selalu): bangun record + extraction + BIAYA.
Fase 2 (best-effort): upsert ke index sementara + query.

  python test_indexer.py
"""
import os, sys, time
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
from pathlib import Path
from collections import defaultdict, Counter
import index_to_pinecone as IX
import extract_fields as EF

# ── Setting PRODUKSI (default baru: haiku/sonnet, cap per doc-type) ──
IX.OCR_ENGINE = "hybrid"
IX.VISION_MODEL = "claude-haiku-4-5"
IX.VISION_MODEL_STRONG = "claude-sonnet-4-6"
EF.EXTRACT_MODEL = "claude-haiku-4-5"
TEST, NS, ROOT = "ks-testindex", "__default__", Path("D:/")

# Harga $/1M token (input, output)
PRICE = {"claude-opus-4-8": (5, 25), "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5": (1, 5)}
USAGE = []  # (model, in_tok, out_tok)


def _wrap(client):
    orig = client.messages.create
    def create(**kw):
        r = orig(**kw)
        u = getattr(r, "usage", None)
        if u:
            USAGE.append((kw.get("model"), getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0)))
        return r
    client.messages.create = create
    return client


from anthropic import Anthropic
def _mk(): return _wrap(Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")))
IX._claude_client = _mk()      # pre-isi cache client IX → kebungkus
EF._client = _mk()             # pre-isi cache client EF → kebungkus

def _cost(usage):
    tot = 0.0
    for m, i, o in usage:
        pin, pout = PRICE.get(m, (0, 0))
        tot += i / 1e6 * pin + o / 1e6 * pout
    return tot


# ── Sampel campuran tipe ──
TARGET = {".pdf": 8, ".jpg": 6, ".docx": 3, ".xlsx": 3}
got = defaultdict(list)
for dp, _, fs in os.walk(ROOT):
    if "Arsip_Rapih" in dp or "Arsip_DryRun" in dp:
        continue
    for f in fs:
        e = Path(f).suffix.lower()
        if e in TARGET and len(got[e]) < TARGET[e]:
            got[e].append(Path(dp) / f)
    if all(len(got[e]) >= n for e, n in TARGET.items()):
        break
sample = [p for ps in got.values() for p in ps]
print(f"Sampel: { {e: len(v) for e, v in got.items()} } = {len(sample)} file (setting produksi)\n")

print("=" * 104)
print("  FASE 1 — record + extraction + biaya")
print("=" * 104)
print(f"  {'tipe':5s} {'cap':3s} {'chunk':5s} {'$/file':8s} {'doc_number':20s} {'counterparty':20s} file")
print("  " + "-" * 112)

all_recs, cost_by_ext, n_by_ext = [], defaultdict(float), Counter()
for p in sample:
    ext = p.suffix.lower()
    dept = ("Finance" if ext in (".xlsx",) else "Legal")
    meta = {"doc_name": p.stem, "department": dept, "subfolder": p.stem, "company": "", "project": ""}
    cap = IX.cap_for(meta)                              # B: cap per doc-type
    mark = len(USAGE)
    pages = IX.extract_pages_any(p, cap=cap)
    EF.enrich(meta, " ".join(t for _, t in pages), use_llm=True)
    recs = IX.records_for(p, IX.doc_id(p.name), meta, pages=pages)
    fcost = _cost(USAGE[mark:])
    cost_by_ext[ext] += fcost; n_by_ext[ext] += 1
    all_recs.append((p, recs))
    print(f"  {ext:5s} {cap:<3d} {len(recs):<5d} ${fcost:<7.4f} {str(meta.get('doc_number'))[:20]:20s} "
          f"{str(meta.get('counterparty'))[:20]:20s} {p.name[:32]}")

# ── Biaya ──
print("\n" + "=" * 104)
print("  BIAYA SAMPEL (token Claude)")
print("=" * 104)
by_model_tok = defaultdict(lambda: [0, 0])
for m, i, o in USAGE:
    by_model_tok[m][0] += i; by_model_tok[m][1] += o
for m, (i, o) in by_model_tok.items():
    pin, pout = PRICE.get(m, (0, 0))
    print(f"  {m:22s} in={i:>8,} out={o:>7,}  ${i/1e6*pin + o/1e6*pout:.4f}")
sample_cost = _cost(USAGE)
print(f"  {'TOTAL sampel':22s} {len(sample)} file → ${sample_cost:.4f}")

print("\n  Rata-rata biaya per tipe:")
for e in TARGET:
    if n_by_ext[e]:
        print(f"    {e:6s} ${cost_by_ext[e]/n_by_ext[e]:.4f}/file  (n={n_by_ext[e]})")

# ── Ekstrapolasi ke seluruh arsip ──
print("\n" + "=" * 104)
print("  ESTIMASI BIAYA SELURUH ARSIP (ekstrapolasi per-tipe)")
print("=" * 104)
print("  Menghitung jumlah file per tipe di D: …")
full = Counter()
for dp, _, fs in os.walk(ROOT):
    if "Arsip_Rapih" in dp or "Arsip_DryRun" in dp:
        continue
    for f in fs:
        e = Path(f).suffix.lower()
        if e in IX.SUPPORTED_EXT:
            full[e] += 1
est = 0.0
for e, cnt in sorted(full.items(), key=lambda x: -x[1]):
    avg = (cost_by_ext[e] / n_by_ext[e]) if n_by_ext[e] else 0.0
    sub = avg * cnt
    est += sub
    tag = f"${avg:.4f}/file" if n_by_ext[e] else "(tipe lain, anggap ~gratis/fallback)"
    print(f"    {e:6s} {cnt:>6,} file × {tag:18s} = ${sub:.2f}")
print(f"\n  TOTAL FILE: {sum(full.values()):,}  →  ESTIMASI: ~${est:.0f}")
print(f"  Rentang wajar: ${est*0.6:.0f} – ${est*1.4:.0f}")
print("  ⚠ Sampel kecil & folder ini OCR-heavy → angka kasar. Biaya nyata tergantung")
print("     proporsi scan vs digital & berapa halaman/ file (dibatasi OCR_PAGE_CAP).")

# ── FASE 2: index + query (best-effort) ──
print("\n" + "=" * 104 + "\n  FASE 2 — index + query (best-effort)\n" + "=" * 104)
try:
    pc = IX.get_pinecone()
    index = IX.retry(lambda: IX.ensure_index(pc, TEST, IX.EMBED_MODEL), what="ensure")
    for p, recs in all_recs:
        IX.retry(lambda: index.upsert_records(namespace=NS, records=recs), what="upsert")
    print(f"  ✓ {sum(len(r) for _, r in all_recs)} chunk ter-upsert. settle…")
    time.sleep(12)
    for q in ("penawaran harga", "izin usaha", "daftar perusahaan pelayaran"):
        print(f"\n🔍 '{q}'")
        IX.search(q, top_k=3, index_name=TEST)
    IX.retry(lambda: pc.delete_index(TEST), what="del")
    print("\n  🗑 index test dihapus.")
except Exception as e:
    print(f"  ⚠ Fase 2 gagal (Pinecone?): {type(e).__name__} — {e}")
    print("  Fase 1 + biaya di atas tetap valid.")
