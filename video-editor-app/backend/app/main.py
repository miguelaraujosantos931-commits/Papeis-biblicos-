"""
API FastAPI do agente editor de vídeo. Fluxo:

1. POST /api/jobs           — upload dos 2 vídeos, roda ffprobe + extrai frames
                               da referência, devolve o job aguardando confirmação
                               do estilo (o "gate" antes de processar tudo).
2. POST /api/jobs/{id}/confirm — recebe a config de estilo escolhida pelo usuário
                               e dispara transcrição + teste de 10s + render final
                               em background.
3. GET  /api/jobs/{id}       — poll de status.
4. GET  /api/jobs/{id}/download — baixa o vídeo final quando status == "done".
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import fonts, jobs
from .pipeline import PipelineError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("video_editor.main")

app = FastAPI(title="Video Editor Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB por arquivo

_STATE = {"deps": {}, "fonts": {}}


@app.on_event("startup")
def on_startup() -> None:
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except ImportError:
        whisper_ok = False

    _STATE["deps"] = {"ffmpeg": ffmpeg_ok, "ffprobe": ffprobe_ok, "faster_whisper": whisper_ok}
    _STATE["fonts"] = fonts.ensure_fonts()

    if not (ffmpeg_ok and ffprobe_ok):
        logger.error(
            "ffmpeg/ffprobe não encontrados no servidor — instale-os antes de usar a API "
            "(veja o README do backend)."
        )
    if not _STATE["fonts"]["ok"]:
        logger.warning("Fontes com falha no download: %s", _STATE["fonts"]["failed"])


@app.get("/api/health")
def health() -> dict:
    return {"dependencies": _STATE["deps"], "fonts": _STATE["fonts"]}


@app.get("/api/fonts")
def list_fonts() -> dict:
    return {"caption_fonts": list(fonts.FONT_CHOICES.keys()), "headline_fonts": list(fonts.FONT_CHOICES.keys())}


@app.post("/api/jobs")
async def create_job(
    bruto: UploadFile = File(...), referencia: UploadFile = File(...)
) -> dict:
    if not (_STATE["deps"].get("ffmpeg") and _STATE["deps"].get("ffprobe")):
        raise HTTPException(503, "ffmpeg/ffprobe não estão disponíveis neste servidor.")

    bruto_bytes = await bruto.read()
    ref_bytes = await referencia.read()

    if not bruto_bytes or not ref_bytes:
        raise HTTPException(400, "Um dos arquivos enviados está vazio.")
    if len(bruto_bytes) > MAX_UPLOAD_BYTES or len(ref_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Arquivo excede o limite de 2GB por vídeo.")

    job = jobs.create_job(bruto_bytes, bruto.filename or "bruto.mp4", ref_bytes, referencia.filename or "referencia.mp4")
    if job.status == "error":
        raise HTTPException(422, job.error)
    return job.to_public_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado.")
    return job.to_public_dict()


@app.get("/api/jobs/{job_id}/frames/{filename}")
def get_frame(job_id: str, filename: str) -> FileResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado.")
    path = jobs.frame_path(job, filename)
    if not path.exists():
        raise HTTPException(404, "Frame não encontrado.")
    return FileResponse(path, media_type="image/jpeg")


class ConfirmBody(BaseModel):
    style: dict
    model_size: str = "small"
    language: str = "pt"


@app.post("/api/jobs/{job_id}/confirm")
def confirm_job(job_id: str, body: ConfirmBody) -> dict:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado.")
    try:
        jobs.confirm_and_process(job, body.style, body.model_size, body.language)
    except PipelineError as exc:
        raise HTTPException(409, str(exc))
    return job.to_public_dict()


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado.")
    if job.status != "done":
        raise HTTPException(409, f"Job ainda não terminou (status atual: {job.status}).")
    path = jobs.output_path(job)
    if not path.exists():
        raise HTTPException(404, "Arquivo de saída não encontrado.")
    return FileResponse(path, media_type="video/mp4", filename="video-final.mp4")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    jobs.cleanup_job(job_id)
    return {"ok": True}


# Serve o frontend estático a partir do mesmo processo, se a pasta existir
# (facilita rodar tudo com um único comando em dev/deploy simples).
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
