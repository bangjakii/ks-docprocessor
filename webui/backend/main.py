"""
FastAPI backend UI arsip KS.
  cd <repo>; uvicorn webui.backend.main:app --host 0.0.0.0 --port 8000
Endpoint:
  GET  /api/health
  GET  /api/filters                 daftar company/department/project
  GET  /api/stats                   statistik arsip
  GET  /api/search?q=&company=&department=&project=&top_k=
  GET  /api/file?path=              buka file asli (PDF/gambar/dll)
  POST /api/classify  (multipart 'files')   upload+klasifikasi (review, belum difile)
  POST /api/confirm   (json)        konfirmasi → file + index
"""
import sys
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webui.backend import services as S

app = FastAPI(title="KS Arsip UI", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True, "archive_root": str(S.ARCHIVE_ROOT), "cost_usd": S.cost()}


@app.get("/api/filters")
def filters():
    return S.get_filters()


@app.get("/api/stats")
def stats():
    return S.get_stats()


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), company: str = "", department: str = "",
           project: str = "", top_k: int = 15):
    try:
        return S.run_search(q, company or None, department or None, project or None, top_k)
    except Exception as e:
        raise HTTPException(502, f"search gagal (Pinecone?): {e}")


@app.get("/api/file")
def get_file(path: str):
    try:
        p = S.resolve_file(path)
    except FileNotFoundError:
        raise HTTPException(404, "file tidak ditemukan")
    except ValueError as e:
        raise HTTPException(403, str(e))
    return FileResponse(str(p), filename=p.name)


@app.get("/api/extract")
def extract(path: str, page: int = 0, context: int = 0):
    """Potong halaman dari bundel → PDF 1-lembar (±context)."""
    try:
        p, cut = S.extract_page(path, page, context)
    except FileNotFoundError:
        raise HTTPException(404, "file tidak ditemukan")
    except ValueError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(500, f"gagal potong: {e}")
    return FileResponse(str(p), filename=p.name,
                        media_type="application/pdf" if cut else None)


@app.post("/api/classify")
async def classify(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        try:
            out.append(S.classify_upload(f.filename, data))
        except Exception as e:
            out.append({"filename": f.filename, "error": str(e)})
    return {"results": out, "cost_usd": S.cost()}


class ConfirmItem(BaseModel):
    temp_id: str
    company: str | None = None
    counterparty: str | None = None
    department: str | None = None
    scope: str | None = None
    project: str | None = None
    subfolder: str | None = None
    doc_number: str | None = None
    expire_date: str | None = None


@app.post("/api/confirm")
def confirm(items: list[ConfirmItem]):
    out = []
    for it in items:
        d = it.model_dump()
        tid = d.pop("temp_id")
        try:
            out.append({"temp_id": tid, **S.confirm_upload(tid, d)})
        except Exception as e:
            out.append({"temp_id": tid, "filed": False, "error": str(e)})
    return {"results": out, "cost_usd": S.cost()}


# ── sajikan frontend hasil build (kalau ada) ───────────────────────────────────
_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
