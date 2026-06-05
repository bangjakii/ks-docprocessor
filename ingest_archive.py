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
import contextlib
import io
import json
import logging
import os
import re
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import process_docs as P
import refile_output as R

logging.getLogger("pypdf").setLevel(logging.ERROR)     # jangan banjiri warning PDF rusak

# Bundel campur (invoice+faktur+PO) hampir selalu di Finance/Operasional & multi-halaman.
# File HIGH yang cocok kriteria ini dicek-isi supaya bisa dipecah per-dokumen.
SPLIT_PRONE_DEPTS = {"Finance", "Operasional"}
SPLIT_MIN_PAGES   = 6

try:
    from tqdm import tqdm
except ImportError:                       # fallback tanpa progress bar
    def tqdm(x=None, **k): return x if x is not None else iter(())

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


# ── Mode APPLY: benar-benar menata file (SALIN) ke struktur bersih ────────────

def analysis_from_path(row: dict) -> dict:
    """Sintesis hasil analisis untuk file HIGH — langsung dari tebakan path, tanpa Claude."""
    proj = P.normalize_project(row.get("project"))
    return {
        "company": row["company"], "counterparty": None,
        "department": row["department"] or "Lainnya",
        "scope": "project" if proj else "company",
        "project": proj, "subfolder": row.get("subfolder") or "Umum",
        "should_split": False,
        "filename_out": Path(row["path"]).name,     # pertahankan nama asli arsip
    }


# Checkpoint di C: (BUKAN di D:) supaya catatan progres selamat kalau disk lepas.
STATE_FILE = Path("archive_ingest_state.jsonl")


def _file_quietly(pdf_path, analysis, index, lock):
    """file_document tapi tahan output ramai-nya; kembalikan logs (atau entri error)."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return P.file_document(Path(pdf_path), analysis, index, lock, canon_project=True)
    except Exception as e:
        return [{"source_file": Path(pdf_path).name, "status": "error",
                 "error": str(e), "company": analysis.get("company")}]


def _load_checkpoint(dest: Path):
    """Kembalikan (logs_sebelumnya, set_source_path_yang_sudah_ok) dari run sebelumnya."""
    logs, done = [], set()
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue                              # baris korup (crash di tengah) → abaikan
            if e.get("_dest") != str(dest):
                continue                              # checkpoint untuk dest lain
            logs.append(e)
            if e.get("status") == "ok" and e.get("source_path"):
                done.add(e["source_path"])
    return logs, done


def reconcile_dest(dest: Path, logs: list, index: dict):
    """Fase C: satukan nama proyek & subfolder kembar jadi kanonik, lalu pindahkan."""
    entries = [l for l in logs if l.get("status") == "ok" and l.get("output_file")
               and l.get("relpath") and Path(l["output_file"]).exists()]
    if not entries:
        return
    projects, subfolders = R.build_inputs(entries)
    print(f"\n  ▶ Fase C: rekonsiliasi {len(projects)} proyek + "
          f"{sum(len(v) for v in subfolders.values())} subfolder (Claude)...")
    proj_map, sub_map, proj_ok = P.reconcile_with_claude(projects, subfolders)
    if not proj_ok:
        print("    ⚠ Rekonsiliasi proyek gagal — struktur mentah dipertahankan.")
        return

    moved = 0
    for e in entries:
        comp = e.get("company") or P.UNIDENTIFIED
        proj = P.normalize_project(e.get("project"))
        dept = e.get("department") or "Lainnya"
        sub  = e.get("subfolder") or "Umum"
        kind = R.relpath_kind(e["relpath"])
        new_comp, new_proj = comp, proj
        if proj and (comp, proj) in proj_map:
            new_comp, new_proj = proj_map[(comp, proj)]
        new_proj = P.normalize_project(new_proj)
        new_sub  = sub_map.get((dept, sub), sub)
        if kind == "project" and not new_proj:
            kind = "noproject" if dept in P.PROJECT_ONLY_DEPTS else "company"
        rel = R.build_relpath(kind, dept, new_sub, new_proj)
        old_path = Path(e["output_file"])
        new_path = dest / P.sanitize(new_comp) / Path(rel) / old_path.name
        if new_proj:
            P.register_project(index, P.sanitize(new_comp), P.sanitize(new_proj))
        P.register_subfolder(index, P.sanitize(new_comp), rel)
        if old_path.resolve() != new_path.resolve():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            d = P.unique_path(new_path)
            shutil.move(str(old_path), str(d))
            new_path = d
            moved += 1
        e["company"]   = P.sanitize(new_comp)
        e["project"]   = P.sanitize(new_proj) if new_proj else None
        e["subfolder"] = P.sanitize(new_sub)
        e["relpath"]   = rel
        e["output_file"] = str(new_path)

    for d in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"    ✓ {moved} file dipindah ke struktur kanonik.")


def _page_count_quiet(path):
    try:
        with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn), contextlib.redirect_stdout(dn):
            return P.get_page_count(path)
    except Exception:
        return 0


def apply_plan(root: Path, dest: Path, plan: list, workers: int, analyze_uncertain: bool,
               reconcile: bool = True, split_check_enabled: bool = True):
    from threading import Event
    P.OUTPUT_DIR = dest                              # arahkan filing ke folder rapi baru
    fidx = dest / "folder_index.json"                # lanjutkan index lama biar konsisten
    index = json.loads(fidx.read_text(encoding="utf-8")) if fidx.exists() else {}
    lock, state_lock, abort = Lock(), Lock(), Event()

    prev_logs, done = _load_checkpoint(dest)
    logs = list(prev_logs)
    sf = STATE_FILE.open("a", encoding="utf-8")       # append — checkpoint inkremental

    def record(entries, src):
        """Tandai source + dest, tulis ke checkpoint (C:) seketika, kumpulkan ke logs."""
        with state_lock:
            for e in entries:
                e["source_path"], e["_dest"] = str(src), str(dest)
                sf.write(json.dumps(e, ensure_ascii=False) + "\n")
                logs.append(e)
            sf.flush()

    def alive():
        if root.exists() and dest.parent.exists():
            return True
        abort.set()
        return False

    high = [r for r in plan if r["confidence"] == "HIGH" and r["path"] not in done]
    unc  = [r for r in plan if r["confidence"] in ("MED", "LOW") and r["path"] not in done]
    junk = [r for r in plan if r["confidence"] == "JUNK"]
    skipped = len(done)
    if skipped:
        print(f"\n  ↻ Lanjut dari checkpoint: {skipped} file sudah beres → dilewati.")

    # ── Gerbang split: HIGH di Finance/Operasional & multi-halaman → cek-isi ───
    split_check = []
    if analyze_uncertain and split_check_enabled:
        prone = [r for r in high if r["department"] in SPLIT_PRONE_DEPTS]
        if prone:
            print(f"\n  ⊟ Cek halaman {len(prone)} file Finance/Operasional (deteksi bundel campur)...")
            keep = []
            for r in tqdm(prone, desc="    cek-hlm", unit="f"):
                (split_check if _page_count_quiet(r["path"]) >= SPLIT_MIN_PAGES else keep).append(r)
            prone_set = {id(r) for r in prone}
            high = [r for r in high if id(r) not in prone_set] + keep
            print(f"    → {len(split_check)} bundel kandidat dialihkan ke analisis-isi (bisa dipecah).")
    content_jobs = unc + split_check          # dua-duanya lewat analyze_pdf (split-aware)

    # ── Fase A: HIGH — salin langsung dari path (gratis) ──────────────────────
    print(f"\n  ▶ Fase A: {len(high)} file HIGH → salin langsung dari path (tanpa Claude)")
    for i, r in enumerate(tqdm(high, desc="    nyalin", unit="f")):
        if i % 200 == 0 and not alive():
            break
        record(_file_quietly(r["path"], analysis_from_path(r), index, lock), r["path"])

    # ── Fase B: file ragu + bundel kandidat — analisis isi (berbayar) ─────────
    if not abort.is_set() and analyze_uncertain and content_jobs:
        print(f"\n  ▶ Fase B: {len(content_jobs)} file → analisis isi "
              f"({len(unc)} ragu + {len(split_check)} cek-bundel, Claude paralel x{workers})")

        def work(r):
            if abort.is_set() or not root.exists():   # disk lepas → jangan bayar Claude
                return None
            rel = str(Path(r["path"]).relative_to(root))
            res = P.analyze_pdf(r["path"], index, path_hint=rel)
            if not res:                               # gagal baca → parkir pakai tebakan path
                res = analysis_from_path(r)
                res["department"] = r["department"] or "_Perlu Dicek"
            elif r["company"] != P.UNIDENTIFIED:      # path punya perusahaan yakin → menang
                res["company"] = r["company"]
            return _file_quietly(r["path"], res, index, lock)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, r): r for r in content_jobs}
            for n, fu in enumerate(tqdm(as_completed(futs), total=len(futs),
                                        desc="    analisis", unit="f")):
                out = fu.result()
                if out is not None:
                    record(out, futs[fu]["path"])
                if n % 100 == 0:
                    alive()
    elif not abort.is_set() and unc:                  # parkir tanpa Claude
        print(f"\n  ▶ Fase B: {len(unc)} file ragu → parkir ke _Perlu Dicek (tanpa Claude)")
        for i, r in enumerate(tqdm(unc, desc="    parkir", unit="f")):
            if i % 200 == 0 and not alive():
                break
            a = analysis_from_path(r)
            a["department"], a["scope"], a["project"] = "_Perlu Dicek", "company", None
            record(_file_quietly(r["path"], a, index, lock), r["path"])

    sf.close()

    # ── Fase C: rekonsiliasi nama proyek/subfolder (hanya kalau run tuntas) ────
    if reconcile and not abort.is_set():
        try:
            reconcile_dest(dest, logs, index)
        except Exception as e:
            print(f"    ⚠ Rekonsiliasi error: {e} — struktur mentah dipertahankan.")

    # ── Tulis index + log final ke dest (best-effort — dest bisa hilang) ───────
    try:
        (dest / "folder_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        (dest / "archive_log.json").write_text(
            json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    ok  = sum(1 for l in logs if l.get("status") == "ok")
    err = sum(1 for l in logs if l.get("status") == "error")
    c = P.usage_cost()
    print(f"\n{'='*70}")
    if abort.is_set():
        print(f"  ⚠  DISK TERPUTUS / tujuan hilang — dihentikan dengan aman.")
        print(f"  💾 Progres tersimpan di checkpoint: {ok} file beres sejauh ini.")
        print(f"  ↻  Sambungkan lagi disk D:, lalu JALANKAN ULANG command yang SAMA")
        print(f"     untuk melanjutkan dari titik terakhir (tidak mengulang/bayar ulang).")
    else:
        print(f"  ✅ Selesai menata arsip (SALIN — file asli di {root} tidak disentuh)")
        print(f"  📁 Hasil rapi : {dest.resolve()}")
        print(f"  ℹ  Checkpoint disimpan ({STATE_FILE}); run ulang akan melewati yang sudah"
              f" beres. Pakai --fresh kalau mau menata dari nol.")
    print(f"  📄 Terfile    : {ok} dokumen  ({err} gagal, {len(junk)} junk dilewati)")
    print(f"  💰 Biaya Claude: ${c['usd']:.2f} (≈ Rp {c['idr']:,.0f}), {c['calls']} panggilan")
    print(f"{'='*70}\n")


def dest_segments(row: dict):
    """Path tujuan (di bawah folder perusahaan) untuk sebuah row — seperti saat apply."""
    proj  = P.normalize_project(row.get("project"))
    dept  = row.get("department") or "Lainnya"
    scope = "project" if proj else "company"
    base_rel, _, _ = P.placement_relpath(dept, scope, proj)
    return row["company"], base_rel.split("/") + [row.get("subfolder") or "Umum"]


def print_dest_tree(plan: list, max_depth: int = 3, width: int = 12):
    """Cetak pratinjau pohon folder hasil (perusahaan → ... ) dengan jumlah file."""
    rootnode = {"n": 0, "ch": {}}
    for r in plan:
        if r["confidence"] == "JUNK":
            continue
        company, segs = dest_segments(r)
        cur = rootnode
        for name in [company] + segs:
            cur = cur["ch"].setdefault(name, {"n": 0, "ch": {}})
            cur["n"] += 1

    def show(node, name, indent, depth):
        print(f"    {indent}{name}  [{node['n']}]")
        if depth <= 1 or not node["ch"]:
            return
        kids = sorted(node["ch"].items(), key=lambda kv: -kv[1]["n"])
        for k, v in kids[:width]:
            show(v, k, indent + "  ", depth - 1)
        if len(kids) > width:
            print(f"    {indent}  … (+{len(kids)-width} folder lagi)")

    print(f"\n  ── Pratinjau pohon folder hasil (perusahaan → dept/proyek → subfolder) ──")
    for comp, node in sorted(rootnode["ch"].items(), key=lambda kv: -kv[1]["n"]):
        show(node, comp, "", max_depth)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="D:\\")
    ap.add_argument("--only", default=None, help="batasi ke satu folder top-level")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="archive_plan.jsonl")
    ap.add_argument("--tree-depth", type=int, default=3, help="kedalaman pratinjau pohon")
    ap.add_argument("--tree-width", type=int, default=12, help="maks anak per node di pohon")
    ap.add_argument("--apply", action="store_true",
                    help="benar-benar menata file (SALIN ke --dest). Tanpa ini = preview saja.")
    ap.add_argument("--dest", default="D:\\Arsip_Rapih",
                    help="folder tujuan hasil rapi (default D:\\Arsip_Rapih)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--no-analyze", action="store_true",
                    help="jangan analisis isi file ragu (parkir ke _Perlu Dicek, gratis)")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="lewati Fase C (penyatuan nama proyek/subfolder kembar)")
    ap.add_argument("--no-split-check", action="store_true",
                    help="jangan cek-pecah bundel campur di Finance/Operasional multi-halaman")
    ap.add_argument("--yes", action="store_true", help="lewati konfirmasi y/n")
    ap.add_argument("--fresh", action="store_true",
                    help="abaikan checkpoint & mulai menata dari nol")
    args = ap.parse_args()

    if args.fresh and STATE_FILE.exists():
        STATE_FILE.unlink()

    root = Path(args.root)
    mode = "APPLY (SALIN)" if args.apply else "DRY-RUN (read-only)"
    print(f"\n{'='*70}\n  Ingest Arsip — {mode}  —  root: {root}")
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

    print_dest_tree(plan, max_depth=args.tree_depth, width=args.tree_width)
    unc_n = per_conf.get("MED", 0) + per_conf.get("LOW", 0)
    print(f"\n  ⓘ ~{unc_n} file MED/LOW posisinya TENTATIF di pohon ini — dept/proyeknya"
          f" bisa berubah setelah analisis isi saat --apply.")

    print(f"\n  ── 25 token folder TAK dikenali tersering (buat perkaya kosakata) ──")
    for tok, v in unknown.most_common(25):
        print(f"    {v:5d}  {tok}")

    Path(args.out).write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in plan), encoding="utf-8")
    print(f"\n  📝 Rincian per file → {args.out}  ({len(plan)} baris)")

    if not args.apply:
        print(f"{'='*70}\n  (read-only — tidak ada file yang dipindah)")
        print(f"  Untuk benar-benar menata: tambah --apply  (akan SALIN ke {args.dest})\n")
        return

    # ── Mode APPLY: konfirmasi dulu, baru tata ────────────────────────────────
    dest = Path(args.dest)
    unc = per_conf.get("MED", 0) + per_conf.get("LOW", 0)
    est_usd = (0 if args.no_analyze else unc * 0.0124)
    print(f"\n{'='*70}\n  ⚠  MODE APPLY — akan MENYALIN file ke: {dest}")
    print(f"  • {per_conf.get('HIGH',0)} file HIGH  → salin langsung dari path (gratis)")
    print(f"  • {unc} file ragu       → " +
          ("PARKIR ke _Perlu Dicek (gratis)" if args.no_analyze
           else f"analisis isi via Claude (≈ ${est_usd:.2f})"))
    print(f"  • {per_conf.get('JUNK',0)} file junk  → dilewati")
    if not args.no_analyze and not args.no_split_check:
        print(f"  • Cek-bundel: file HIGH Finance/Operasional multi-halaman dipecah (≈ $4)")
    if not args.no_reconcile:
        print(f"  • Fase C: rekonsiliasi nama proyek/subfolder kembar (Claude, ≈ $0.10–0.30)")
    print(f"  • File asli di {root} TIDAK disentuh (ini operasi SALIN).")
    print(f"{'─'*70}")
    if not args.yes and input("  Lanjutkan menata (salin) file? (y/n): ").strip().lower() != "y":
        print("\n  ❌ Dibatalkan.\n")
        return
    apply_plan(root, dest, plan, args.workers, analyze_uncertain=not args.no_analyze,
               reconcile=not args.no_reconcile, split_check_enabled=not args.no_split_check)


if __name__ == "__main__":
    main()
