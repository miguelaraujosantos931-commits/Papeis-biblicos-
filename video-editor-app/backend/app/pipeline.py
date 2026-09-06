"""
Pipeline de edição de vídeo: probe, extração de frames, transcrição
(faster-whisper, word-level), geração de legenda estilo CapCut (palavra
destacada) e headline via drawtext, montagem do filtergraph, render (teste de
10s + final) e validação do arquivo de saída.

Tudo com ffmpeg/ffprobe puro + fontfile= apontando pra .ttf local (nunca por
nome), texto sempre escapado antes de entrar em qualquer string de filtro, e
caminhos sempre absolutos.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import ImageFont

from .fonts import font_path
from .text_escape import escape_drawtext, escape_filter_path

logger = logging.getLogger("video_editor.pipeline")

OUTPUT_DIMS: Dict[str, Tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

CAPTION_SIZE_FRAC = {"small": 0.040, "medium": 0.055, "large": 0.072}
HEADLINE_SIZE_FRAC = {"small": 0.055, "medium": 0.075, "large": 0.095}

CAPTION_POSITION_FRAC = {"top": 0.12, "center": 0.46, "bottom": 0.78}
HEADLINE_POSITION_FRAC = {"top": 0.10, "center": 0.44}


class PipelineError(RuntimeError):
    """Erro esperado do pipeline (config inválida, ffmpeg falhou, etc.) —
    sempre com mensagem clara o suficiente pra mostrar direto ao usuário."""


def _run(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    logger.info("RUN: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------

def probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    proc = _run(cmd, timeout=30)
    if proc.returncode != 0:
        raise PipelineError(f"ffprobe falhou em {path.name}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)

    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if video_stream is None:
        raise PipelineError(f"{path.name} não tem stream de vídeo.")

    fmt = data["format"]
    fr = video_stream.get("r_frame_rate", "0/1")
    try:
        num, den = fr.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except Exception:
        fps = 0.0

    return {
        "duration": float(fmt.get("duration", 0.0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": round(fps, 3),
        "has_audio": audio_stream is not None,
        "codec": video_stream.get("codec_name"),
    }


# --------------------------------------------------------------------------
# Extração de frames pra revisão de estilo
# --------------------------------------------------------------------------

def extract_frames(video_path: Path, out_dir: Path, count: int = 8) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = probe(video_path)
    duration = meta["duration"] or 1.0
    frames: List[Path] = []
    for i in range(count):
        ts = duration * (i + 0.5) / count
        out_path = out_dir / f"frame_{i:02d}.jpg"
        cmd = [
            "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-q:v", "3", str(out_path),
        ]
        proc = _run(cmd, timeout=30)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            frames.append(out_path)
        else:
            logger.warning("Falha ao extrair frame em t=%.2fs: %s", ts, proc.stderr[-300:])
    if not frames:
        raise PipelineError(
            f"Não consegui extrair nenhum frame de {video_path.name} — "
            "o arquivo pode estar corrompido ou não é um vídeo válido."
        )
    return frames


# --------------------------------------------------------------------------
# Transcrição (faster-whisper, word-level timestamps)
# --------------------------------------------------------------------------

@dataclass
class Word:
    word: str
    start: float
    end: float


_MODEL_CACHE: dict = {}


def transcribe(video_path: Path, model_size: str = "small", language: str = "pt") -> List[Word]:
    from faster_whisper import WhisperModel

    last_exc: Optional[Exception] = None
    for size in [model_size, "base", "tiny"]:
        try:
            if size not in _MODEL_CACHE:
                logger.info("Carregando modelo whisper '%s'...", size)
                _MODEL_CACHE[size] = WhisperModel(size, device="cpu", compute_type="int8")
            model = _MODEL_CACHE[size]
            segments, _info = model.transcribe(
                str(video_path), word_timestamps=True, language=language, vad_filter=True,
            )
            words: List[Word] = []
            for seg in segments:
                for w in (seg.words or []):
                    text = w.word.strip()
                    if text:
                        words.append(Word(word=text, start=w.start, end=w.end))
            if size != model_size:
                logger.warning(
                    "Modelo '%s' não pôde ser usado, caiu pro fallback '%s'.",
                    model_size, size,
                )
            return words
        except Exception as exc:  # download falhou, sem internet, modelo corrompido, etc.
            last_exc = exc
            logger.warning("Falha ao transcrever com modelo '%s': %s", size, exc)
            continue

    raise PipelineError(
        f"Não consegui transcrever o vídeo com nenhum modelo whisper (small/base/tiny). "
        f"Último erro: {last_exc}"
    )


# --------------------------------------------------------------------------
# Config de estilo vindo do frontend
# --------------------------------------------------------------------------

@dataclass
class CaptionConfig:
    enabled: bool = True
    position: str = "bottom"
    font: str = "Poppins ExtraBold"
    size: str = "medium"
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFE14D"
    outline: bool = True
    max_words_per_chunk: int = 3


@dataclass
class HeadlineConfig:
    enabled: bool = True
    text: str = ""
    font: str = "Anton"
    size: str = "large"
    color: str = "#FFFFFF"
    background_box: bool = True
    background_color: str = "#A63D40"
    position: str = "top"
    duration_seconds: float = 3.0


@dataclass
class StyleConfig:
    output_format: str = "9:16"
    caption: CaptionConfig = field(default_factory=CaptionConfig)
    headline: HeadlineConfig = field(default_factory=HeadlineConfig)

    @staticmethod
    def from_dict(d: dict) -> "StyleConfig":
        cap = CaptionConfig(**{**CaptionConfig().__dict__, **d.get("caption", {})})
        head = HeadlineConfig(**{**HeadlineConfig().__dict__, **d.get("headline", {})})
        return StyleConfig(
            output_format=d.get("output_format", "9:16"),
            caption=cap,
            headline=head,
        )


def _hex_to_ffcolor(hex_color: str, alpha: Optional[float] = None) -> str:
    h = hex_color.strip().lstrip("#")
    if len(h) not in (6, 8):
        h = "FFFFFF"
    ff = f"0x{h[:6]}"
    if alpha is not None:
        ff += f"@{alpha:.2f}"
    return ff


def _measure(font_file: str, size_px: int, text: str) -> float:
    font = ImageFont.truetype(font_file, size_px)
    return font.getlength(text)


# --------------------------------------------------------------------------
# Legenda estilo CapCut (drawtext em camadas, sem depender de casamento de
# nome de fonte do libass — cada camada usa fontfile= direto no .ttf)
# --------------------------------------------------------------------------

def _group_words(words: List[Word], max_words: int, max_chars: int = 20) -> List[List[Word]]:
    chunks: List[List[Word]] = []
    cur: List[Word] = []
    cur_len = 0
    for w in words:
        wlen = len(w.word)
        would_len = cur_len + (1 if cur else 0) + wlen
        if cur and (len(cur) >= max_words or would_len > max_chars):
            chunks.append(cur)
            cur, cur_len = [], 0
            would_len = wlen
        cur.append(w)
        cur_len = would_len
    if cur:
        chunks.append(cur)
    return chunks


def build_caption_filters(
    words: List[Word], cfg: CaptionConfig, target_w: int, target_h: int
) -> List[str]:
    if not cfg.enabled or not words:
        return []

    font_file = font_path(cfg.font)
    font_file_escaped = escape_filter_path(font_file)
    size_px = max(18, int(target_h * CAPTION_SIZE_FRAC.get(cfg.size, 0.055)))
    y = int(target_h * CAPTION_POSITION_FRAC.get(cfg.position, 0.78))

    text_color = _hex_to_ffcolor(cfg.text_color)
    highlight_color = _hex_to_ffcolor(cfg.highlight_color)
    border = "borderw=3:bordercolor=0x000000@0.75:" if cfg.outline else ""

    chunks = _group_words(words, cfg.max_words_per_chunk)
    filters: List[str] = []

    max_line_w = target_w * 0.94
    # ffmpeg's between(t,min,max) inclui os dois limites — sem essa margem,
    # quando um chunk termina exatamente onde o próximo começa (comum, já que
    # vem direto dos timestamps do whisper), os dois ficam desenhados juntos
    # por 1 frame (efeito de "dupla exposição" de texto).
    EPS = 0.02

    for chunk in chunks:
        chunk_start = chunk[0].start
        chunk_end = chunk[-1].end
        line_text = " ".join(w.word for w in chunk)

        # ajusta o tamanho da fonte pra baixo só se essa linha específica não
        # couber na largura do vídeo (evita corte nas bordas em chunks longos)
        chunk_size_px = size_px
        chunk_space_w = _measure(font_file, chunk_size_px, " ")
        total_w = sum(_measure(font_file, chunk_size_px, w.word) for w in chunk) + chunk_space_w * (len(chunk) - 1)
        if total_w > max_line_w:
            scale = max_line_w / total_w
            chunk_size_px = max(14, int(size_px * scale))
            chunk_space_w = _measure(font_file, chunk_size_px, " ")
            total_w = sum(_measure(font_file, chunk_size_px, w.word) for w in chunk) + chunk_space_w * (len(chunk) - 1)

        start_x = max(0.0, (target_w - total_w) / 2)
        base_text = escape_drawtext(line_text)
        base_end = max(chunk_start + 0.01, chunk_end - EPS)
        filters.append(
            f"drawtext=fontfile='{font_file_escaped}':text='{base_text}':"
            f"fontsize={chunk_size_px}:fontcolor={text_color}:{border}"
            f"x={start_x:.1f}:y={y}:"
            f"enable='between(t,{chunk_start:.3f},{base_end:.3f})'"
        )

        # camadas de destaque: uma palavra por vez, por cima da base
        cursor_x = start_x
        for i, w in enumerate(chunk):
            word_w = _measure(font_file, chunk_size_px, w.word)
            end_t = chunk[i + 1].start if i + 1 < len(chunk) else chunk_end
            end_t = max(w.start + 0.01, end_t - EPS)
            word_text = escape_drawtext(w.word)
            filters.append(
                f"drawtext=fontfile='{font_file_escaped}':text='{word_text}':"
                f"fontsize={chunk_size_px}:fontcolor={highlight_color}:{border}"
                f"x={cursor_x:.1f}:y={y}:"
                f"enable='between(t,{w.start:.3f},{end_t:.3f})'"
            )
            cursor_x += word_w + chunk_space_w

    return filters


# --------------------------------------------------------------------------
# Headline
# --------------------------------------------------------------------------

def _wrap_two_lines(text: str) -> List[str]:
    """Quebra em até 2 linhas, no espaço mais próximo do meio do texto."""
    words = text.split(" ")
    if len(words) < 2:
        return [text]
    lens = [len(w) for w in words]
    total = sum(lens) + (len(words) - 1)
    best_i, best_diff, acc = 1, None, 0
    for i in range(1, len(words)):
        acc += lens[i - 1] + (1 if i > 1 else 0)
        diff = abs((total - acc) - acc)
        if best_diff is None or diff < best_diff:
            best_diff, best_i = diff, i
    return [" ".join(words[:best_i]), " ".join(words[best_i:])]


def build_headline_filter(cfg: HeadlineConfig, target_w: int, target_h: int) -> List[str]:
    """Gera 1 filtro drawtext por linha (1 ou 2 linhas), encolhendo a fonte
    (e quebrando em 2 linhas se precisar) pra sempre caber na largura do vídeo
    — nunca cortar a headline nas bordas."""
    raw_text = cfg.text.strip()
    if not cfg.enabled or not raw_text:
        return []

    font_file_raw = font_path(cfg.font)
    font_file = escape_filter_path(font_file_raw)
    base_size = max(24, int(target_h * HEADLINE_SIZE_FRAC.get(cfg.size, 0.075)))
    min_size = max(20, int(target_h * 0.03))
    max_w = target_w * 0.88
    y0 = int(target_h * HEADLINE_POSITION_FRAC.get(cfg.position, 0.10))
    color = _hex_to_ffcolor(cfg.color)

    size = base_size
    while _measure(font_file_raw, size, raw_text) > max_w and size > min_size:
        size -= 2

    lines = [raw_text]
    if _measure(font_file_raw, size, raw_text) > max_w:
        lines = _wrap_two_lines(raw_text)
        size = base_size
        while (
            any(_measure(font_file_raw, size, l) > max_w for l in lines)
            and size > min_size
        ):
            size -= 2

    box = ""
    if cfg.background_box:
        box_color = _hex_to_ffcolor(cfg.background_color, alpha=0.88)
        box = f"box=1:boxcolor={box_color}:boxborderw=18:"

    line_height = int(size * 1.3)
    filters: List[str] = []
    for idx, line in enumerate(lines):
        line_w = _measure(font_file_raw, size, line)
        x = max(0.0, (target_w - line_w) / 2)
        y = y0 + idx * line_height
        text = escape_drawtext(line)
        filters.append(
            f"drawtext=fontfile='{font_file}':text='{text}':fontsize={size}:"
            f"fontcolor={color}:{box}"
            f"x={x:.1f}:y={y}:"
            f"enable='between(t,0,{cfg.duration_seconds:.2f})'"
        )
    return filters


# --------------------------------------------------------------------------
# Filtergraph completo + render
# --------------------------------------------------------------------------

def build_filter_script(
    words: List[Word], style: StyleConfig, script_path: Path
) -> Tuple[int, int]:
    target_w, target_h = OUTPUT_DIMS.get(style.output_format, OUTPUT_DIMS["9:16"])

    parts = [
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h}"
    ]
    parts.extend(build_caption_filters(words, style.caption, target_w, target_h))
    parts.extend(build_headline_filter(style.headline, target_w, target_h))

    script_path.write_text(",\n".join(parts), encoding="utf-8")
    return target_w, target_h


def render(
    input_path: Path, filter_script: Path, output_path: Path,
    start: Optional[float] = None, duration: Optional[float] = None,
    timeout: int = 1800,
) -> None:
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(input_path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-filter_script:v", str(filter_script),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        raise PipelineError(
            "ffmpeg falhou ao renderizar "
            f"{output_path.name}. Log:\n{proc.stderr[-4000:]}"
        )


# --------------------------------------------------------------------------
# Validação do arquivo final
# --------------------------------------------------------------------------

def validate_output(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        raise PipelineError(f"{path.name} não existe ou está vazio.")

    proc = _run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        timeout=30,
    )
    streams = proc.stdout.strip().splitlines()
    if "video" not in streams:
        raise PipelineError(f"{path.name} não tem stream de vídeo.")
    if "audio" not in streams:
        raise PipelineError(f"{path.name} não tem stream de áudio.")

    meta = probe(path)

    decode = _run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=300)
    if decode.stderr.strip():
        raise PipelineError(
            f"ffmpeg encontrou erros ao decodificar {path.name}:\n{decode.stderr[-3000:]}"
        )

    return {"duration": meta["duration"], "width": meta["width"], "height": meta["height"]}
