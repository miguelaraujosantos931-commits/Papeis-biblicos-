"""
Job store em memória + worker em background thread. Pensado pra rodar num
único processo uvicorn (--workers 1) — o estado dos jobs vive na memória do
processo, então não escala horizontalmente sem trocar isso por um banco/fila
de verdade (ver README).
"""
from __future__ import annotations

import logging
import shutil
import threading
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import pipeline
from .pipeline import PipelineError, StyleConfig

logger = logging.getLogger("video_editor.jobs")

DATA_DIR = Path(__file__).resolve().parent.parent / "jobs_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()
_JOBS: Dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    status: str = "created"
    message: str = "Vídeos recebidos."
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    bruto_meta: Optional[dict] = None
    referencia_meta: Optional[dict] = None
    duration_warning: Optional[str] = None
    frames: List[str] = field(default_factory=list)
    style: Optional[dict] = None
    output_info: Optional[dict] = None

    def dir(self) -> Path:
        return DATA_DIR / self.id

    def to_public_dict(self) -> dict:
        return asdict(self)


def _set(job: Job, **kwargs) -> None:
    with _LOCK:
        for k, v in kwargs.items():
            setattr(job, k, v)


def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def create_job(bruto_bytes: bytes, bruto_name: str, ref_bytes: bytes, ref_name: str) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id)
    with _LOCK:
        _JOBS[job_id] = job

    job_dir = job.dir()
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    bruto_path = input_dir / f"bruto{Path(bruto_name).suffix or '.mp4'}"
    ref_path = input_dir / f"referencia{Path(ref_name).suffix or '.mp4'}"
    bruto_path.write_bytes(bruto_bytes)
    ref_path.write_bytes(ref_bytes)

    try:
        bruto_meta = pipeline.probe(bruto_path)
        ref_meta = pipeline.probe(ref_path)
    except PipelineError as exc:
        _set(job, status="error", error=str(exc), message="Falha ao analisar os vídeos enviados.")
        return job

    frames_dir = job_dir / "frames"
    try:
        frame_paths = pipeline.extract_frames(ref_path, frames_dir, count=8)
    except PipelineError as exc:
        _set(job, status="error", error=str(exc), message="Falha ao extrair frames da referência.")
        return job

    warning = None
    b_dur, r_dur = bruto_meta["duration"], ref_meta["duration"]
    if r_dur > 0 and (b_dur > r_dur * 4 or b_dur < r_dur * 0.25):
        warning = (
            f"O vídeo bruto tem {b_dur:.0f}s e a referência tem {r_dur:.0f}s — "
            "durações bem diferentes. Copiar o mesmo ritmo de corte da referência "
            "pode não fazer sentido aqui; o estilo (legenda/headline) ainda será aplicado normalmente."
        )

    _set(
        job,
        status="awaiting_confirmation",
        message="Estilo da referência pronto pra revisão.",
        bruto_meta=bruto_meta,
        referencia_meta=ref_meta,
        duration_warning=warning,
        frames=[p.name for p in frame_paths],
    )
    return job


def confirm_and_process(job: Job, style_dict: dict, model_size: str, language: str) -> None:
    if job.status not in ("awaiting_confirmation", "error"):
        raise PipelineError(
            f"Job está em status '{job.status}' — só dá pra (re)confirmar em "
            "'awaiting_confirmation' ou depois de um erro."
        )
    _set(job, error=None)
    _set(job, status="queued", message="Configuração recebida, iniciando processamento.", style=style_dict)
    t = threading.Thread(target=_process_job, args=(job, style_dict, model_size, language), daemon=True)
    t.start()


def _process_job(job: Job, style_dict: dict, model_size: str, language: str) -> None:
    job_dir = job.dir()
    input_dir = job_dir / "input"
    bruto_path = next(input_dir.glob("bruto.*"))
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        style = StyleConfig.from_dict(style_dict)

        _set(job, status="transcribing", message="Transcrevendo o áudio do vídeo bruto...")
        words = pipeline.transcribe(bruto_path, model_size=model_size, language=language)
        if len(words) < 2:
            raise PipelineError(
                "A transcrição saiu vazia ou muito curta — o áudio pode estar mudo, "
                "sem fala, ou muito baixo. Não faz sentido gerar legenda nesse caso."
            )
        (job_dir / "transcript.json").write_text(
            __import__("json").dumps([w.__dict__ for w in words], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _set(job, status="rendering_test", message="Testando o estilo num recorte de 10s...")
        filter_script = job_dir / "filter.txt"
        target_w, target_h = pipeline.build_filter_script(words, style, filter_script)

        test_clip = job_dir / "teste_10s.mp4"
        pipeline.render(bruto_path, filter_script, test_clip, start=0, duration=10, timeout=120)
        pipeline.validate_output(test_clip)

        _set(job, status="rendering_full", message="Teste de 10s passou. Renderizando o vídeo inteiro...")
        final_path = output_dir / "video-final.mp4"
        pipeline.render(bruto_path, filter_script, final_path, timeout=3600)

        _set(job, status="validating", message="Validando o arquivo final...")
        info = pipeline.validate_output(final_path)

        # limpeza de temporários — mantém só o que o usuário pode reaproveitar
        test_clip.unlink(missing_ok=True)
        for f in (job_dir / "frames").glob("*.jpg"):
            pass  # mantém os frames, são pequenos e úteis pra conferência

        _set(job, status="done", message="Vídeo pronto.", output_info=info)

    except PipelineError as exc:
        logger.warning("Job %s falhou: %s", job.id, exc)
        _set(job, status="error", error=str(exc), message="Falha no processamento.")
    except Exception as exc:  # nunca travar em silêncio — sempre reportar
        logger.error("Job %s falhou com erro inesperado:\n%s", job.id, traceback.format_exc())
        _set(job, status="error", error=f"Erro inesperado: {exc}", message="Falha no processamento.")


def output_path(job: Job) -> Path:
    return job.dir() / "output" / "video-final.mp4"


def frame_path(job: Job, filename: str) -> Path:
    safe_name = Path(filename).name  # evita path traversal
    return job.dir() / "frames" / safe_name


def cleanup_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job.dir(), ignore_errors=True)
