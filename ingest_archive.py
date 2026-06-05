"""
Ingest arsip terstruktur — DRY-RUN ANALYZER (read-only).
=========================================================
Menyusuri pohon folder arsip (mis. D:\\) dan, untuk SETIAP PDF, menebak
penempatan (perusahaan / departemen / proyek / subfolder) HANYA dari nama
folder + nama file — TANPA membaca isi PDF, TANPA memanggil Claude, TANPA
memindahkan file apa pun.

Tujuan: melihat seberapa jauh "path-first" bisa menempatkan dokumen sebelum
disambungkan ke pipeline. Output:
  - ringkasan di konsol (per perusahaan + distribusi keyakinan)
  - daftar token folder yang TIDAK dikenali (buat memperkaya kosakata)
  - file rincian  archive_plan.jsonl  (satu baris per PDF) untuk diperiksa

Pakai:
    python ingest_archive.py --root "D:\\" [--limit N] [--only "PT. KRAKATAU SHIPYARD"]

Keyakinan:
  HIGH  = perusahaan + departemen terbaca dari path  → kandidat SKIP-Claude
  MED   = salah satu terbaca (perusahaan ATAU departemen)
  LOW   = tidak ada yang terbaca                      → perlu analisis isi
"""

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import process_docs as P

# ── Top-level yang dilewati (sistem / bukan dokumen) ──────────────────────────
SKIP_TOP = {
    "$RECYCLE.BIN", "System Volume Information", "found.000", "autorun.inf",
}

# ── Kosakata departemen: substring (sudah dinormalisasi) → departemen kanonik ──
# Dicek terurut; yang pertama cocok menang. Spesifik dulu, umum belakangan.
DEPT_KEYWORDS = [
    # HR sebelum Legal: "sertifikat keahlian / tenaga ahli" jangan ketabrak "sertifikat"→Legal.
    ("HR",               ["sdm", "personil", "personalia", "tenaga kerja", "tenaga ahli",
                           "karyawan", "idcard", "id card", "absensi", "gaji",
                           "kepegawaian", "curriculum", " cv ", "ijazah", "keahlian",
                           "daftar tenaga", "biodata", "ktp direksi", "skck"]),
    ("Legal",            ["legalitas", "legal", "perizinan", "perijinan", "izin usaha",
                           "ijin", "akte", "akta", "npwp", "siup", "situ", " tdp",
                           "domisili", "hukum", "sertifikat", "certificate", " cert",
                           "perjanjian", "kbli", "nib", "pengesahan", "notaris",
                           "tanah", "imb", " ho ", "amdal", "license", "lisensi", "pelepasan"]),
    ("Finance",          ["keuangan", "finance", "pajak", "tagihan", "invoice", "faktur",
                           "termin", " bank", "anggaran", "pembayaran", "kwitansi",
                           "rekening", "cash", "biaya", "budget", "rab", "harga"]),
    ("Sales & Marketing",["marketing", "maketing", "tender", "lelang", "penawaran",
                           "penaawaran", "pemasaran", "proposal", "prakualifikasi",
                           "kualifikasi", "lpse", "kontrak", "company profile", "profile",
                           "profil", "brosur", "presentasi", "presentation", "sampul",
                           "quotation", " bid ", "spk", "spal"]),
    ("Engineering",      ["engineering", "teknik", "teknis", "desain", "design", "gambar",
                           "drawing", "rancangan", "rancang", "spesifikasi", "spec",
                           "network planning", "general arrangement", "key plan",
                           "ships particular", "principal dimension", "piping",
                           "architecture", "cable", "laying", "fmea", "machinery",
                           "hull", "outfitting", "welding", "blasting", "painting",
                           "ballast", "bilge", "propulsion", "hydraulic", "electric",
                           "calculation", "stability", "manual", "lines plan", "midship"]),
    ("Operasional",      ["operasional", "operasi", "logistic", "logistik", "gudang",
                           "produksi", " spb", "pengadaan", "purchasing", "material",
                           "bongkaran", "laporan plate", "kawat las", "sewa", "rekanan",
                           "po ", "berita acara", "delivery", "pengiriman", "stock"]),
    ("IT",               ["website", " web ", "lpse akun", "akun lpse"]),
]

# ── Pola proyek: kalau salah satu muncul di segmen, segmen itu kandidat proyek ─
PROJECT_HINTS = [
    "kapal", "ponton", "tongkang", "dock", " dok ", "floating dock", " fd ", "fd-",
    "kontainer", "container", "teus", "perintis", "patroli", "tugboat", "tug boat",
    "barge", "fpv", "dwt", " gt", "penumpang", "pertamina", "kpdt", "perhubungan",
    "replating", "docking", "fighting craft", "buoyant", "fiberglass", "pelra",
    "puloampel", "dermaga", "graving", "graADL",
]

# ── Pola junk (folder aset/software, bukan dokumen) ───────────────────────────
JUNK_HINTS = [
    "template", "font", "autocad", "coreldraw", "corel", "adobe", "photoshop",
    "joomla", "flash", "graphics.suite", "nope", "installer", "setup",
]

# ── Propagasi perusahaan per-tender ───────────────────────────────────────────
# Top-level yang isinya tender lintas-perusahaan: file unid mewarisi perusahaan
# dari file lain dalam tender yang sama (>1 perusahaan grup → KSO, aturan JV).
TENDER_TOPS = {"MAKETING TENDER"}
# Default manual untuk PROGRAM tender yang nol sinyal perusahaan di path.
# (KPDT 2016 terbukti KS; folder KS juga punya "Tender KPDT" → KPDT = tender KS.)
PROGRAM_DEFAULTS = {
    ("MAKETING TENDER", "TENDER KPDT"): "PT Krakatau Shipyard",
}


def tender_group_key(top: str, segments: list):
    """Kelompok tender = top + s/d 2 segmen pertama (program + varian)."""
    return tuple([top] + segments[:2])


def propagate_tender_company(plan: list) -> dict:
    """
    Pass kedua: tularkan perusahaan dalam satu grup tender ke file yang unid.
    Mutasi plan in-place; set row['src'] = 'propagasi'/'default'/'path'.
    Return statistik.
    """
    stats = Counter()
    groups = defaultdict(list)
    for r in plan:
        if r["top"] in TENDER_TOPS:
            groups[tender_group_key(r["top"], r["segments"])].append(r)

    for key, rows in groups.items():
        signals = Counter(r["company"] for r in rows if r["company"] != P.UNIDENTIFIED)
        if len(signals) > 1:
            owner = "KSO DKB-KS"                     # >1 perusahaan grup → JV
        elif len(signals) == 1:
            owner = next(iter(signals))
        else:
            # nol sinyal → coba default per-program
            owner = PROGRAM_DEFAULTS.get((key[0], key[1] if len(key) > 1 else None))
        if not owner:
            continue
        is_default = len(signals) == 0
        for r in rows:
            if r["company"] != owner:
                # JV: minoritas (mis. KS partner) ditarik ke KSO juga
                if r["company"] == P.UNIDENTIFIED or len(signals) > 1:
                    r["company"] = owner
                    r["src"] = "default" if is_default else "propagasi"
                    stats["default" if is_default else "propagasi"] += 1
    return stats


def norm(s: str) -> str:
    return P._normalize_name(s)


def company_from_path(segments: list) -> tuple:
    """
    Tebak perusahaan dari segmen path (dangkal→dalam). Strict: exact/alias/
    akronim/substring — TANPA fuzzy. Return (canon_or_UNIDENTIFIED, segmen_pemicu_or_None).
    """
    for seg in segments:
        n = norm(seg)
        if not n:
            continue
        toks = set(n.split())
        # KSO diprioritaskan: 'dkb'/'kso' kuat menandakan KSO DKB-KS (jangan ketabrak 'ks').
        if "dkb" in toks or "kso" in toks or "kodja" in n:
            return "KSO DKB-KS", seg
        if n in P._CANON_NORM:
            return P._CANON_NORM[n], seg
        if n in P._ALIAS_NORM:
            return P._ALIAS_NORM[n], seg
        # akronim panjang dulu (ikn, ksd) baru pendek (ks)
        for key in ("ikn", "ksd"):
            if key in toks:
                return P._ALIAS_NORM[key], seg
        # multiword alias/canon sebagai substring
        for key, canon in P._ALL_KEYS.items():
            if " " in key and (key in n or n in key):
                return canon, seg
        if "ks" in toks:
            return "PT Krakatau Shipyard", seg
    return P.UNIDENTIFIED, None


def dept_from_path(segments: list) -> tuple:
    """Departemen kanonik pertama yang cocok (dalam→dangkal). Return (dept_or_None, seg)."""
    for seg in reversed(segments):
        n = f" {norm(seg)} "
        for dept, kws in DEPT_KEYWORDS:
            if any(k in n for k in kws):
                return dept, seg
    return None, None


def project_from_path(segments: list, company_seg, dept_seg) -> tuple:
    """Segmen kandidat proyek paling spesifik (dalam→dangkal). Return (proj_or_None, seg)."""
    for seg in reversed(segments):
        if seg in (company_seg, dept_seg):
            continue
        n = norm(seg)
        if any(h in f" {n} " for h in PROJECT_HINTS):
            # bersihkan nomor urut depan ("4. patroli ..." → "patroli ...")
            clean = re.sub(r"^\s*[ivxlc]+\.?\s+|^\s*\d+\.?\s+", "", seg, flags=re.I).strip()
            return clean, seg
    return None, None


def is_junk(segments: list) -> bool:
    for seg in segments:
        n = norm(seg)
        if any(h in n for h in JUNK_HINTS):
            return True
    return False


def score(row) -> str:
    if row["junk"]:
        return "JUNK"
    cf = row["company"] != P.UNIDENTIFIED
    df = row["department"] is not None
    return "HIGH" if cf and df else "MED" if (cf or df) else "LOW"


def analyze(root: Path, only: str = None, limit: int = None):
    plan = []
    unknown_tokens = Counter()
    n_seen = 0

    tops = [only] if only else [d for d in sorted(os.listdir(root))
                                if d not in SKIP_TOP and (root / d).is_dir()]
    # ── Pass 1: parse tiap PDF dari path ──────────────────────────────────────
    for top in tops:
        for dp, dirs, files in os.walk(root / top):
            dirs.sort()
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                if limit and n_seen >= limit:
                    break
                n_seen += 1
                full = Path(dp) / f
                rel_parts = full.relative_to(root).parts          # (top, ..., file.pdf)
                segments = list(rel_parts[:-1])                    # folder saja
                stem = Path(f).stem

                company, cseg = company_from_path(segments)
                dept, dseg = dept_from_path(segments + [stem])
                project, _ = project_from_path(segments + [stem], cseg, dseg)

                plan.append({
                    "path": str(full), "top": top, "segments": segments[1:],
                    "company": company, "department": dept, "project": project,
                    "subfolder": segments[-1] if segments else "Umum",
                    "junk": is_junk(segments), "src": "path", "cseg": cseg, "dseg": dseg,
                })
            if limit and n_seen >= limit:
                break

    # ── Pass 2: propagasi perusahaan per-tender ───────────────────────────────
    prop_stats = propagate_tender_company(plan)

    # ── Pass 3: skor + agregasi ───────────────────────────────────────────────
    per_company, per_conf = Counter(), Counter()
    for r in plan:
        r["confidence"] = score(r)
        per_company[r["company"]] += 1
        per_conf[r["confidence"]] += 1
        if r["confidence"] in ("LOW", "MED"):
            for seg in r["segments"]:
                if seg not in (r["cseg"], r["dseg"]):
                    for tok in norm(seg).split():
                        if len(tok) >= 4:
                            unknown_tokens[tok] += 1
        for k in ("segments", "junk", "cseg", "dseg", "top"):
            r.pop(k, None)

    return plan, per_company, per_conf, unknown_tokens, n_seen, prop_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="D:\\")
    ap.add_argument("--only", default=None, help="batasi ke satu folder top-level")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="archive_plan.jsonl")
    args = ap.parse_args()

    root = Path(args.root)
    print(f"\n{'='*70}\n  DRY-RUN Ingest Arsip (read-only)  —  root: {root}")
    if args.only:
        print(f"  Hanya: {args.only}")
    print(f"{'='*70}\n  Menyusuri folder... (tanpa baca isi PDF, tanpa Claude)\n")

    plan, per_company, per_conf, unknown, n, prop = analyze(root, args.only, args.limit)

    print(f"  Total PDF dipindai : {n}")
    print(f"  Propagasi tender   : {prop.get('propagasi',0)} via sinyal grup, "
          f"{prop.get('default',0)} via default program\n")
    print(f"  ── Keyakinan penempatan ──")
    order = ["HIGH", "MED", "LOW", "JUNK"]
    for k in order:
        v = per_conf.get(k, 0)
        pct = (100 * v / n) if n else 0
        bar = "█" * int(pct / 2)
        print(f"    {k:5} {v:6d}  {pct:5.1f}%  {bar}")
    skip = per_conf.get("HIGH", 0)
    print(f"\n  → Kandidat SKIP-Claude (HIGH): {skip} "
          f"({100*skip/n:.1f}%)  | perlu analisis isi: {n-skip-per_conf.get('JUNK',0)}\n")

    print(f"  ── PDF per perusahaan (dari path) ──")
    for comp, v in per_company.most_common():
        print(f"    {v:6d}  {comp}")

    print(f"\n  ── 25 token folder TAK dikenali tersering (buat perkaya kosakata) ──")
    for tok, v in unknown.most_common(25):
        print(f"    {v:5d}  {tok}")

    Path(args.out).write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in plan), encoding="utf-8")
    print(f"\n  📝 Rincian per file → {args.out}  ({len(plan)} baris)")
    print(f"{'='*70}\n  (read-only — tidak ada file yang dipindah)\n")


if __name__ == "__main__":
    main()
