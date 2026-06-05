"""
Re-file output tanpa analisis ulang.
=====================================
Ambil dokumen yang SUDAH difile (dari processing_log.json), jalankan rekonsiliasi
(penyatuan proyek & subfolder kembar), lalu PINDAHKAN file ke struktur folder yang
sudah disatukan. Tidak ada panggilan analisis per-file — hanya 2 panggilan rekonsiliasi.

Pakai ini setelah process_docs.py kalau rekonsiliasi sebelumnya gagal/terlewat.

    python refile_output.py
"""

import json
import shutil
from pathlib import Path

import process_docs as P


def build_inputs(entries: list):
    """Bangun input rekonsiliasi (projects, subfolders) dari entri log."""
    projects, subfolders = {}, {}
    for e in entries:
        comp = e.get("company") or P.UNIDENTIFIED
        proj = (e.get("project") or "").strip()
        if proj:
            projects[(comp, proj)] = projects.get((comp, proj), 0) + 1
        dept = (e.get("department") or "Lainnya").strip()
        sub  = (e.get("subfolder") or "").strip()
        if sub:
            subfolders.setdefault(dept, {}).setdefault(sub, 0)
            subfolders[dept][sub] += 1
    return projects, subfolders


def relpath_kind(relpath: str) -> str:
    """Tentukan jenis penempatan dari relpath lama."""
    head = relpath.split("/", 1)[0]
    if head == P.PROJECTS_FOLDER:
        return "project"
    if head == P.NO_PROJECT_FOLDER:
        return "noproject"
    return "company"


def build_relpath(kind: str, dept: str, sub: str, proj: str) -> str:
    dept = P.sanitize(dept or "Lainnya")
    sub  = P.sanitize(sub or "Umum")
    if kind == "project":
        return f"{P.PROJECTS_FOLDER}/{P.sanitize(proj)}/{dept}/{sub}"
    if kind == "noproject":
        return f"{P.NO_PROJECT_FOLDER}/{dept}/{sub}"
    return f"{dept}/{sub}"


def main():
    if not P.LOG_FILE.exists():
        print(f"\n✗ {P.LOG_FILE} tidak ada.\n")
        return
    log = json.loads(P.LOG_FILE.read_text(encoding="utf-8"))

    # Pilih entri run terakhir: file masih ada di disk + punya relpath + status ok.
    by_path = {}
    for i, e in enumerate(log):
        of = e.get("output_file")
        if of and e.get("status") == "ok" and e.get("relpath") and Path(of).exists():
            by_path[str(Path(of).resolve())] = i   # last wins
    idxs = sorted(set(by_path.values()))
    if not idxs:
        print("\n✗ Tidak ada dokumen ber-relpath yang filenya masih ada di disk.\n")
        return
    entries = [log[i] for i in idxs]

    print(f"\n{'='*65}")
    print(f"  Re-file Output — Waralalo Group")
    print(f"  Dokumen di disk : {len(entries)}")
    print(f"{'='*65}\n")

    projects, subfolders = build_inputs(entries)
    print(f"🔗 Rekonsiliasi {len(projects)} proyek + "
          f"{sum(len(v) for v in subfolders.values())} subfolder...")
    proj_map, sub_map, proj_ok = P.reconcile_with_claude(projects, subfolders)
    print(f"  ✓ {len(proj_map)} proyek + {len(sub_map)} subfolder disatukan")
    if not proj_ok:
        print("\n✗ Rekonsiliasi proyek gagal — dibatalkan (hindari setengah jalan).\n")
        return

    # Hitung rencana pemindahan.
    plan = []   # (idx, old_path, new_path, new_comp, new_proj, new_sub, rel)
    for i in idxs:
        e    = log[i]
        comp = e.get("company") or P.UNIDENTIFIED
        proj = P.normalize_project(e.get("project"))   # placeholder ("00_Unidentified") → None
        dept = e.get("department") or "Lainnya"
        sub  = e.get("subfolder") or "Umum"
        kind = relpath_kind(e["relpath"])

        new_comp, new_proj = comp, proj
        if proj and (comp, proj) in proj_map:
            new_comp, new_proj = proj_map[(comp, proj)]
        new_proj = P.normalize_project(new_proj)   # rekonsiliasi bisa balikin placeholder juga
        new_sub = sub_map.get((dept, sub), sub)

        # Proyek jadi placeholder/null tapi relpath lama "project" → tentukan ulang seperti
        # placement_relpath: Engineering/Operasional/HR → _Tanpa Proyek; sisanya → level perusahaan.
        if kind == "project" and not new_proj:
            kind = "noproject" if dept in P.PROJECT_ONLY_DEPTS else "company"

        rel      = build_relpath(kind, dept, new_sub, new_proj)
        old_path = Path(e["output_file"])
        new_path = P.OUTPUT_DIR / P.sanitize(new_comp) / Path(rel) / old_path.name
        plan.append((i, old_path, new_path, P.sanitize(new_comp),
                     P.sanitize(new_proj) if new_proj else None, P.sanitize(new_sub), rel))

    n_move = sum(1 for _, o, n, *_ in plan if o.resolve() != n.resolve())
    print(f"\n  📦 {n_move} dari {len(plan)} file akan dipindah ke folder baru.")
    print(f"{'─'*65}")
    if input("  Lanjutkan pindah file? (y/n): ").strip().lower() != "y":
        print("\n  ❌ Dibatalkan.\n")
        return

    # Eksekusi pemindahan + bangun ulang folder_index.
    index, moved = {}, 0
    for i, old_path, new_path, new_comp, new_proj, new_sub, rel in plan:
        e = log[i]
        if new_proj:
            P.register_project(index, new_comp, new_proj)
        P.register_subfolder(index, new_comp, rel)

        if old_path.resolve() != new_path.resolve():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            dest = P.unique_path(new_path)
            shutil.move(str(old_path), str(dest))
            new_path = dest
            moved += 1

        e["company"]   = new_comp
        e["project"]   = new_proj
        e["subfolder"] = new_sub
        e["relpath"]   = rel
        e["output_file"] = str(new_path)

    # Hapus folder kosong yang ditinggalkan.
    for d in sorted(P.OUTPUT_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    P.save_folder_index(index)
    P.LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    c = P.usage_cost()
    print(f"\n{'='*65}")
    print(f"  ✅ Selesai! {moved} file dipindah ke struktur baru.")
    print(f"  💰 Biaya rekonsiliasi: ${c['usd']:.4f} (≈ Rp {c['idr']:,.0f}), {c['calls']} panggilan")
    print(f"  📁 Output : {P.OUTPUT_DIR.resolve()}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n✗ ANTHROPIC_API_KEY tidak ditemukan.\n")
        exit(1)
    main()
