# Agente Editor de Vídeo — versão web

App web (backend real + frontend) que implementa o agente de edição de
vídeo descrito no skill original: você envia um **vídeo bruto** e um
**vídeo de referência**, revisa o estilo antes de processar, e o servidor
gera um vídeo final com legenda automática (palavra destacada, estilo
CapCut), headline no início e corte 9:16 — usando só ferramentas gratuitas
e locais (ffmpeg, Whisper, fontes do Google Fonts).

Diferente do skill original (que roda dentro de uma sessão do Claude Code
e faz a "análise visual" da referência via IA olhando os frames), esta
versão web substitui essa etapa por um **formulário de revisão de estilo**:
o servidor extrai e mostra os frames da referência + metadados técnicos, e
você mesmo escolhe fonte, cores, posição da legenda e texto da headline
antes de confirmar. É o mesmo "gate de confirmação" do skill, só que
manual em vez de automático — processamento de vídeo pesado (ffmpeg,
Whisper) não roda no navegador, então não tem como isso ser 100% client-side.

## Arquitetura

```
video-editor-app/
  backend/
    app/
      main.py       — API FastAPI (upload, confirmação, status, download)
      jobs.py        — estado dos jobs em memória + worker em thread
      pipeline.py    — ffprobe, extração de frames, transcrição (faster-whisper),
                       geração da legenda/headline via drawtext, render, validação
      fonts.py       — download automático das fontes (Google Fonts, SIL OFL)
      text_escape.py — escape de texto pros filtros do ffmpeg
    requirements.txt
    assets/fonts/    — fontes baixadas automaticamente no 1º start (gitignored)
    jobs_data/       — arquivos de cada job: input, frames, output (gitignored)
  frontend/
    index.html, app.js, styles.css — wizard de 3 passos, sem build step
```

### Por que legenda via `drawtext` em camadas, e não `.ass`/libass

O skill original sugere `.ass` com `fontsdir`. Na prática, boa parte das
famílias do Google Fonts (Montserrat, Oswald, etc.) migraram pra
**variable font** e não têm mais arquivo estático por peso — então o nome
interno da fonte não bate com "Montserrat ExtraBold", e o libass cai pra
um peso errado (ex: "Thin"). Aqui isso é resolvido em duas partes:

1. `fonts.py` detecta fonte variável e usa `fonttools` (grátis, MIT) pra
   instanciar um TTF estático no peso certo, com o nome corrigido.
2. A legenda é feita com múltiplas camadas de `drawtext` (uma "base" com a
   frase inteira, e uma por palavra pra destacar) usando `fontfile=`
   apontando direto pro `.ttf` — sem depender de casamento de nome de
   fonte em nenhum momento.

Isso foi validado visualmente durante o desenvolvimento (ver commits/testes).

## Como rodar localmente

Requisitos de sistema:

- Python 3.9+
- `ffmpeg` e `ffprobe` no PATH (`apt-get install -y ffmpeg` no Debian/Ubuntu)
- ~1–2GB de disco livre (modelos do Whisper são baixados no 1º uso)
- Acesso de saída à internet para `raw.githubusercontent.com` (fontes) e
  `huggingface.co` (pesos do modelo Whisper) — só é necessário na primeira
  vez que cada recurso é usado; depois fica em cache local.

```bash
cd video-editor-app/backend
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Abra `http://localhost:8000` — o próprio FastAPI já serve o frontend
estático (pasta `../frontend`) nesse mesmo endereço.

**Importante rodar com `--workers 1`**: o estado dos jobs vive em memória
no processo; com mais de 1 worker cada requisição pode cair num processo
diferente e "perder" o job. Pra escalar de verdade, trocar o dict em
`jobs.py` por um banco (Redis/Postgres) e o `threading.Thread` por uma
fila de verdade (Celery/RQ) — dá pra fazer sem mudar a API pública.

## Deploy em produção (checklist)

- Rodar atrás de um reverse proxy (nginx/Caddy) com HTTPS e
  `client_max_body_size` alto (vídeos podem passar de 100MB facilmente).
- Restringir `allow_origins` no CORS do `main.py` (hoje está `["*"]` pra
  facilitar desenvolvimento).
- Ter um processo de limpeza pra `backend/jobs_data/` (cada job guarda o
  vídeo bruto + referência + saída; sem limpeza o disco enche).
- Servidor precisa ter CPU suficiente pra ffmpeg + Whisper — CPU-only
  funciona (usamos `compute_type="int8"`), mas é bem mais lento que com
  GPU. Pra volumes altos, considerar um servidor com GPU e trocar
  `device="cpu"` por `device="cuda"` em `pipeline.py::transcribe`.
- O limite de upload por vídeo é 2GB (`MAX_UPLOAD_BYTES` em `main.py`) —
  ajuste conforme necessário, junto com o limite do proxy.

## Fluxo da API

1. `POST /api/jobs` (multipart: `bruto`, `referencia`) — roda ffprobe nos
   dois vídeos, extrai 8 frames da referência, devolve o job com status
   `awaiting_confirmation` + metadados + lista de frames.
2. `GET /api/jobs/{id}/frames/{arquivo}` — serve cada frame extraído.
3. `POST /api/jobs/{id}/confirm` (JSON: `style`, `model_size`, `language`)
   — dispara em background: transcrição → teste de 10s → render completo
   → validação. Pode ser chamado de novo depois de um erro, sem reenviar
   os vídeos, pra ajustar o estilo e tentar de novo.
4. `GET /api/jobs/{id}` — poll de status (`transcribing`,
   `rendering_test`, `rendering_full`, `validating`, `done`, `error`).
5. `GET /api/jobs/{id}/download` — baixa `video-final.mp4` quando o status
   é `done`.
6. `GET /api/health` — checa ffmpeg/ffprobe/faster-whisper e status das
   fontes. `GET /api/fonts` — lista as fontes disponíveis pro frontend.

### Formato do `style` (POST confirm)

```json
{
  "output_format": "9:16",
  "caption": {
    "enabled": true,
    "position": "bottom",
    "font": "Poppins ExtraBold",
    "size": "medium",
    "text_color": "#FFFFFF",
    "highlight_color": "#FFE14D",
    "outline": true,
    "max_words_per_chunk": 3
  },
  "headline": {
    "enabled": true,
    "text": "Não é sorte, é fé!",
    "font": "Anton",
    "size": "large",
    "color": "#FFFFFF",
    "background_box": true,
    "background_color": "#A63D40",
    "position": "top",
    "duration_seconds": 3
  }
}
```

## Limitações conhecidas

- A "leitura visual" da referência (que no skill original é feita pela IA
  olhando os frames) aqui é feita por você: o app mostra os frames e os
  metadados, mas quem escolhe fonte/cor/posição é o usuário no formulário.
- Sem GPU, transcrição com o modelo `small` de um vídeo de alguns minutos
  pode levar bem mais tempo que a duração do próprio vídeo. Use `tiny`/`base`
  pra testar rápido.
- Estado dos jobs é em memória (ver seção de deploy) — reiniciar o
  processo perde jobs em andamento.
