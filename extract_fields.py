"""
Ekstraksi field terstruktur dari TEKS yang sudah di-OCR (gratis, regex):
  - doc_number    : nomor dokumen (pola Indonesia + nomor seri faktur pajak)
  - expire_date   : tanggal kadaluarsa/berlaku s.d. (buat alerting)  → ISO yyyy-mm-dd
  - counterparty  : lawan transaksi (PT/CV lain, BUKAN company grup KS)

Dipakai per-HALAMAN di index_to_pinecone.records_for (bundel punya banyak no/pihak).
Counterparty pakai LLM-fallback opsional (lihat extract_counterparty_llm).

Validasi gratis:  python extract_fields.py
"""
import re

try:
    import process_docs as P
    _GROUP = set()
    for _k, _v in getattr(P, "_ALL_KEYS", {}).items():
        _GROUP.add(re.sub(r"[^a-z0-9 ]", " ", str(_k).lower()).strip())
        _GROUP.add(re.sub(r"[^a-z0-9 ]", " ", str(_v).lower()).strip())
except Exception:
    _GROUP = set()
_GROUP |= {"krakatau shipyard", "krakatau", "shipyard", "ikn", "ksd", "dkb", "kso",
           "industri kapal nusantara"}
_GROUP = {g for g in _GROUP if len(g) >= 2}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


# ── doc_number ────────────────────────────────────────────────────────────────
_DOCNO_FAKTUR = re.compile(r"\b(\d{3}\.\d{3}-\d{2}\.\d{8})\b")          # 010.005-18.44622951
_DOCNO_SLASH = re.compile(
    r"(?:no\.?|nomor)\s*[:.]?\s*"
    r"([0-9A-Za-z.\-]{0,22}(?:\s*/\s*[0-9A-Za-z.\-]{1,22}){1,5})", re.I)


def extract_doc_number(text: str):
    m = _DOCNO_FAKTUR.search(text)
    if m:
        return m.group(1)
    for m in _DOCNO_SLASH.finditer(text):
        cand = re.sub(r"\s*/\s*", "/", m.group(1)).strip(" /")
        if "/" in cand and re.search(r"\d", cand) and len(cand) >= 6:
            return cand
    return None


# ── expire_date ───────────────────────────────────────────────────────────────
_BULAN = ("januari februari maret april mei juni juli agustus september "
          "oktober november desember").split()
_MON = {b: i + 1 for i, b in enumerate(_BULAN)}
_DATE = (r"(\d{1,2}\s+(?:" + "|".join(_BULAN) + r")\s+\d{4}"
         r"|\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4})")
_EXPIRE = re.compile(
    r"(?:masa\s+berlaku|berlaku\s+(?:s(?:ampai|\.?\s*d\.?|/d)|hingga)"
    r"(?:\s+(?:dengan\s+)?tanggal)?|s\.?\s*/?\s*d\.?\s*tanggal|"
    r"sampai\s+dengan(?:\s+tanggal)?|berakhir\s+(?:pada\s+)?(?:tanggal\s+)?|"
    r"valid\s+until|expir\w*)[^0-9]{0,25}" + _DATE, re.I)


def _norm_date(s: str):
    s = s.strip()
    m = re.match(r"(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})", s)
    if m:
        mo = _MON.get(m.group(2).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = "20" + y if len(y) == 2 else y
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"
    return s


def extract_expire_date(text: str):
    m = _EXPIRE.search(text)
    return _norm_date(m.group(1)) if m else None


# ── counterparty ──────────────────────────────────────────────────────────────
_CO = r"(?:PT|CV|UD|PD|Koperasi|Firma)\.?\s+[A-Z][A-Za-z0-9 .,&'\-]{2,40}"
_KEPADA = re.compile(r"kepada[\s,]*(?:yth\.?)?\s*[:\-]?\s*(?:bpk\.?|ibu|sdr\.?)?\s*(" + _CO + ")", re.I)
_ANTARA = re.compile(r"antara\s+(" + _CO + r")\s+(?:dengan|dan)\s+(" + _CO + ")", re.I)
_PELAK = re.compile(r"(?:pelaksana|penyedia|vendor|kontraktor|pihak\s+kedua)\s*[:\-]?\s*(" + _CO + ")", re.I)


def _clean_co(s: str):
    s = re.split(r"[\n\r]|\s{2,}|\bUp\b|\bup\b|\bdi\b\s|\bperihal\b|\balamat\b", s, maxsplit=1)[0]
    return re.sub(r"\s+", " ", s).strip(" .,:-")


def _is_group(name: str) -> bool:
    n = _norm(name)
    return any(g in n for g in _GROUP)


def extract_counterparty(text: str):
    m = _ANTARA.search(text)
    if m:
        for c in (_clean_co(m.group(1)), _clean_co(m.group(2))):
            if c and not _is_group(c):
                return c
    for rx in (_KEPADA, _PELAK):
        m = rx.search(text)
        if m:
            c = _clean_co(m.group(1))
            if c and not _is_group(c):
                return c
    return None


def extract_all(text: str) -> dict:
    return {
        "doc_number": extract_doc_number(text),
        "expire_date": extract_expire_date(text),
        "counterparty": extract_counterparty(text),
    }


# ── LLM fallback (text-only, murah) ───────────────────────────────────────────
import os, json

EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-haiku-4-5")
_client = None


def _claude():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


_EXTRACT_PROMPT = (
    "Dari TEKS dokumen bisnis Indonesia ini, ekstrak metadata. Balas HANYA JSON "
    "valid tanpa penjelasan, kunci persis:\n"
    '- "doc_number": nomor dokumen utama (mis. "083/KS-SPH/VI/2012" atau nomor seri '
    'faktur pajak), null kalau tak ada.\n'
    '- "expire_date": tanggal masa berlaku / kadaluarsa / berlaku-sampai, format '
    'YYYY-MM-DD. null kalau dokumen TIDAK punya masa berlaku.\n'
    '- "counterparty": nama perusahaan LAWAN TRANSAKSI (PT/CV selain grup Krakatau '
    'Shipyard / IKN / KSO / Industri Kapal Nusantara), null kalau tak ada/tak jelas.\n'
    "Jangan mengarang. Ragu = null.\n\nTEKS:\n")


def extract_fields_llm(text: str, want=("doc_number", "expire_date", "counterparty")) -> dict:
    text = (text or "").strip()
    if len(text) < 30:
        return {}
    try:
        msg = _claude().messages.create(
            model=EXTRACT_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": _EXTRACT_PROMPT + text[:6000]}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
        d = json.loads(raw)
        out = {}
        for k in want:
            v = d.get(k)
            out[k] = v if (v and str(v).strip().lower() not in ("null", "none", "")) else None
        return out
    except Exception:
        return {}


# Doc-type korporat-diri/lisensi/sertifikat → TIDAK punya counterparty (lawan transaksi).
_NO_CP = ("akta", "npwp", "nib", "siup", "tdp", "sk menteri", "sk menkumham", "iso ",
          "ohsas", "sertifikat", "certificate", "izin", "ijin", "ktp", "pkp", "skdu",
          "company profile", "comp profile", "cp pt", "struktur organisasi",
          "laporan keuangan", "neraca", "daftar ", "brosur", "spesifikasi",
          "drawing", "general arrangement", "gambar", "tenaga ahli", "absensi")


def _wants(meta: dict) -> set:
    """Field mana yg LAYAK diekstrak (hemat LLM call).
    expire_date: WHITELIST (cuma dok ber-masa-berlaku).
    counterparty: BLACKLIST — relevan utk HAMPIR SEMUA dok (kontrak, pinjaman, borrower
    agreement, surat, dll) KECUALI dok korporat-diri/lisensi/sertifikat (_NO_CP)."""
    blob = _norm(" ".join(str(meta.get(x, "")) for x in ("department", "subfolder", "doc_name")))
    want = set()
    if any(w in blob for w in ("sertifikat", "certificate", "izin", "ijin", "garansi",
                               "jaminan", "siup", "tdp", "kontrak", "perjanjian",
                               "pinjaman", "kredit", "borrower", "sk ", "sertif")):
        want.add("expire_date")
    if not any(w in blob for w in _NO_CP):
        want.add("counterparty")
    return want


def enrich(meta: dict, text: str, use_llm: bool = True) -> dict:
    """Isi doc_number/expire_date/counterparty: regex dulu (gratis), LLM hanya utk
    field yg masih kosong DAN doc-type-nya relevan. Mutasi & kembalikan meta."""
    if not meta.get("doc_number"):
        dn = extract_doc_number(text)
        if dn:
            meta["doc_number"] = dn
    want = _wants(meta)
    if "expire_date" in want and not meta.get("expire_date"):
        e = extract_expire_date(text)
        if e:
            meta["expire_date"] = e
    if "counterparty" in want and not meta.get("counterparty"):
        c = extract_counterparty(text)
        if c:
            meta["counterparty"] = c
    need_llm = (("expire_date" in want and not meta.get("expire_date")) or
                ("counterparty" in want and not meta.get("counterparty")))
    if use_llm and need_llm:
        llm = extract_fields_llm(text, want=tuple(want | {"doc_number"}))
        for k in ("doc_number", "expire_date", "counterparty"):
            if not meta.get(k) and llm.get(k):
                meta[k] = llm[k]
    return meta


# ── validasi gratis ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os, sys
    for _s in (sys.stdout, sys.stderr):
        try: _s.reconfigure(encoding="utf-8")
        except Exception: pass
    from pathlib import Path
    import index_to_pinecone as IX
    IX.OCR_ENGINE = "tesseract"          # paksa gratis utk validasi
    if IX.TESSERACT_PATH:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = IX.TESSERACT_PATH

    ROOT = Path("D:/")
    HINTS = ("spk", "kontrak", "perjanjian", "penawaran", "sph", "kwitansi",
             "invoice", "faktur", "izin", "akta", "garansi", "jaminan", "berita acara")
    sample, seen = [], {h: 0 for h in HINTS}
    for dp, _, fs in os.walk(ROOT):
        if "Arsip_Rapih" in dp:
            continue
        for f in fs:
            low = f.lower()
            if not low.endswith(".pdf"):
                continue
            for h in HINTS:
                if h in low and seen[h] < 2:
                    sample.append(Path(dp) / f); seen[h] += 1; break
        if len(sample) >= 24:
            break

    print(f"Sampel: {len(sample)} file (OCR tesseract gratis)\n")
    print(f"  {'doc_number':24s} {'expire':11s} {'counterparty':24s} file")
    print("  " + "-" * 92)
    hit_no = hit_exp = hit_cp = 0
    for p in sample:
        txt = " ".join(t for _, t in IX.page_texts(p)[:4])
        r = extract_all(txt)
        hit_no += bool(r["doc_number"]); hit_exp += bool(r["expire_date"]); hit_cp += bool(r["counterparty"])
        print(f"  {str(r['doc_number'])[:24]:24s} {str(r['expire_date'])[:11]:11s} "
              f"{str(r['counterparty'])[:24]:24s} {p.name[:30]}")
    n = max(1, len(sample))
    print(f"\n  Ketemu: doc_number {hit_no}/{n} | expire {hit_exp}/{n} | counterparty {hit_cp}/{n}")
    print("  (expire wajar rendah — cuma dok ber-masa-berlaku yg punya)")
