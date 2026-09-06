"""
Download e validação de fontes gratuitas (Google Fonts, licença SIL OFL) usadas
na legenda e na headline. Nunca depende de fonte já instalada no sistema —
tudo é baixado para ASSETS_DIR e referenciado por caminho absoluto.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List

import httpx

logger = logging.getLogger("video_editor.fonts")

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Algumas famílias do google/fonts migraram totalmente para variable font (sem
# mais pasta static/): o download cai pro arquivo [wght].ttf e, sem pin, o
# FreeType renderiza a instância padrão (ex: "Thin"), não o peso pedido no nome
# do arquivo. Depois de baixar, fixamos o eixo de peso com fonttools pra virar
# um TTF estático de verdade, no peso correto — ferramenta gratuita (MIT),
# 100% local.
VARIABLE_WEIGHT_PIN: Dict[str, int] = {
    "Oswald-Bold.ttf": 700,
    "Montserrat-ExtraBold.ttf": 800,
}


def _pin_variable_weight(path: Path, wght: int) -> None:
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
    except ImportError:
        logger.warning(
            "fonttools não instalado — não foi possível fixar o peso de %s; "
            "a fonte pode renderizar num peso errado.", path.name
        )
        return
    try:
        font = TTFont(str(path))
        if "fvar" not in font:
            return  # já é estática, nada a fazer
        instantiateVariableFont(font, {"wght": wght}, updateFontNames=True, inplace=True)
        font.save(str(path))
        logger.info("Peso wght=%s fixado em %s (fonte variável -> estática)", wght, path.name)
    except Exception as exc:
        logger.warning("Falha ao fixar peso de %s: %s", path.name, exc)

# nome lógico -> lista de URLs candidatas (primeira que baixar e validar, vence)
FONT_SOURCES: Dict[str, List[str]] = {
    "Anton-Regular.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
    ],
    "BebasNeue-Regular.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/BebasNeue-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf",
    ],
    "Oswald-Bold.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/static/Oswald-Bold.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/Oswald%5Bwght%5D.ttf",
        "https://github.com/google/fonts/raw/main/ofl/oswald/static/Oswald-Bold.ttf",
    ],
    "Montserrat-ExtraBold.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/static/Montserrat-ExtraBold.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
        "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-ExtraBold.ttf",
    ],
    "Poppins-ExtraBold.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-ExtraBold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf",
    ],
}

# nome amigável exposto no frontend -> arquivo local
FONT_CHOICES: Dict[str, str] = {
    "Anton": "Anton-Regular.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
    "Oswald Bold": "Oswald-Bold.ttf",
    "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf",
    "Poppins ExtraBold": "Poppins-ExtraBold.ttf",
}


def _is_valid_font_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        out = subprocess.run(
            ["file", "-b", str(path)], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        out = ""
    if any(tag in out for tag in ("TrueType", "OpenType", "font")):
        return True
    # fallback: assinatura binária de TTF/OTF quando `file` não estiver disponível
    try:
        head = path.read_bytes()[:4]
        return head in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")
    except Exception:
        return False


def _download_one(out_path: Path, urls: List[str]) -> bool:
    for url in urls:
        try:
            logger.info("Baixando %s de %s", out_path.name, url)
            with httpx.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
            if _is_valid_font_file(out_path):
                return True
            logger.warning("%s baixado mas não parece uma fonte válida", out_path.name)
            out_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Falha ao baixar %s de %s: %s", out_path.name, url, exc)
            out_path.unlink(missing_ok=True)
    return False


def ensure_fonts() -> Dict[str, object]:
    """Garante que todas as fontes estejam baixadas e válidas.

    Retorna {"ok": bool, "failed": [nomes], "available": {nome_amigavel: caminho_absoluto}}.
    Nunca lança exceção — falha de UMA fonte não deve travar o servidor inteiro,
    mas fica registrada para o usuário ser avisado.
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    failed: List[str] = []

    for filename, urls in FONT_SOURCES.items():
        out_path = ASSETS_DIR / filename
        already_had = _is_valid_font_file(out_path)
        if not already_had and not _download_one(out_path, urls):
            failed.append(filename)
            continue
        if not already_had and filename in VARIABLE_WEIGHT_PIN:
            _pin_variable_weight(out_path, VARIABLE_WEIGHT_PIN[filename])

    available: Dict[str, str] = {}
    for friendly, filename in FONT_CHOICES.items():
        path = ASSETS_DIR / filename
        if _is_valid_font_file(path):
            available[friendly] = str(path.resolve())

    return {"ok": len(failed) == 0, "failed": failed, "available": available}


def font_path(friendly_name: str) -> str:
    """Caminho absoluto para a fonte escolhida, com fallback pra primeira disponível."""
    filename = FONT_CHOICES.get(friendly_name)
    if filename:
        path = ASSETS_DIR / filename
        if _is_valid_font_file(path):
            return str(path.resolve())
    for filename in FONT_CHOICES.values():
        path = ASSETS_DIR / filename
        if _is_valid_font_file(path):
            return str(path.resolve())
    raise RuntimeError(
        "Nenhuma fonte local disponível em assets/fonts — baixe as fontes antes de renderizar."
    )
