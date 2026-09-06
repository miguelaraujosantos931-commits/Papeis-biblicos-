"""Escape de texto para filtros do ffmpeg (drawtext). Nunca montar o filtro
com texto cru — todo texto do usuário/transcrição passa por aqui antes de
entrar em qualquer string de filtro."""


def escape_drawtext(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "’")  # apóstrofo reto -> aspas tipográficas
    text = text.replace("%", "\\%")
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    return text


def escape_filter_path(path: str) -> str:
    """Escapa um caminho de arquivo pra uso dentro de uma string de filtro
    (ex: o argumento de fontfile= ou o nome de arquivo do ass filter)."""
    path = path.replace("\\", "\\\\")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path
