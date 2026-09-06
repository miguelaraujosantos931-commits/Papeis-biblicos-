(function () {
  "use strict";

  const API = ""; // mesmo servidor (main.py serve o frontend + /api juntos)
  let currentJobId = null;
  let pollTimer = null;

  const el = (id) => document.getElementById(id);

  function showStep(n) {
    [1, 2, 3].forEach((i) => {
      el("step" + i).classList.toggle("hidden", i !== n);
    });
    document.querySelectorAll(".steps .step").forEach((s) => {
      const stepN = Number(s.dataset.step);
      s.classList.toggle("active", stepN === n);
      s.classList.toggle("done", stepN < n);
    });
  }

  function alertBox(container, type, message) {
    container.innerHTML =
      '<div class="alert alert-' + type + '">' + message + "</div>";
  }
  function clearBox(container) {
    container.innerHTML = "";
  }

  // ------------------------------------------------------------------
  // Health check + lista de fontes
  // ------------------------------------------------------------------
  async function init() {
    try {
      const health = await fetch(API + "/api/health").then((r) => r.json());
      const banner = el("healthBanner");
      const missing = [];
      if (!health.dependencies.ffmpeg) missing.push("ffmpeg");
      if (!health.dependencies.ffprobe) missing.push("ffprobe");
      if (!health.dependencies.faster_whisper) missing.push("faster-whisper");
      if (missing.length) {
        alertBox(
          banner,
          "error",
          "O servidor está sem: <strong>" +
            missing.join(", ") +
            "</strong>. Instale essas dependências no backend antes de continuar (veja o README)."
        );
      } else if (!health.fonts.ok) {
        alertBox(
          banner,
          "warning",
          "Algumas fontes não puderam ser baixadas no servidor: " +
            health.fonts.failed.join(", ") +
            ". Vai ser usada uma fonte alternativa no lugar."
        );
      }
    } catch (e) {
      alertBox(
        el("healthBanner"),
        "error",
        "Não consegui falar com o servidor da API. Ele está rodando?"
      );
    }

    try {
      const fonts = await fetch(API + "/api/fonts").then((r) => r.json());
      fillFontSelect("capFont", fonts.caption_fonts, "Poppins ExtraBold");
      fillFontSelect("headFont", fonts.headline_fonts, "Anton");
    } catch (e) {
      // segue com os <select> vazios; o backend ainda usa um fallback interno
    }
  }

  function fillFontSelect(selectId, fonts, preferred) {
    const sel = el(selectId);
    sel.innerHTML = "";
    (fonts || []).forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === preferred) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  // ------------------------------------------------------------------
  // Passo 1: upload
  // ------------------------------------------------------------------
  el("uploadForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    clearBox(el("uploadError"));

    const bruto = el("brutoInput").files[0];
    const referencia = el("refInput").files[0];
    if (!bruto || !referencia) return;

    const btn = el("uploadBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Analisando...';

    try {
      const fd = new FormData();
      fd.append("bruto", bruto);
      fd.append("referencia", referencia);

      const resp = await fetch(API + "/api/jobs", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Falha ao enviar os vídeos.");

      currentJobId = data.id;
      populateStep2(data);
      showStep(2);
    } catch (e) {
      alertBox(el("uploadError"), "error", e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Analisar referência";
    }
  });

  // ------------------------------------------------------------------
  // Passo 2: revisão de estilo
  // ------------------------------------------------------------------
  function populateStep2(job) {
    const grid = el("framesGrid");
    grid.innerHTML = "";
    (job.frames || []).forEach((filename) => {
      const img = document.createElement("img");
      img.src = API + "/api/jobs/" + job.id + "/frames/" + filename;
      img.alt = "Frame da referência";
      grid.appendChild(img);
    });

    const warnBox = el("durationWarning");
    clearBox(warnBox);
    if (job.duration_warning) {
      alertBox(warnBox, "warning", job.duration_warning);
    }

    const meta = job.bruto_meta,
      ref = job.referencia_meta;
    el("metaTable").innerHTML =
      "<tr><th></th><th>Bruto</th><th>Referência</th></tr>" +
      row("Resolução", meta.width + "x" + meta.height, ref.width + "x" + ref.height) +
      row("Duração", meta.duration.toFixed(1) + "s", ref.duration.toFixed(1) + "s") +
      row("FPS", meta.fps, ref.fps) +
      row("Áudio", meta.has_audio ? "sim" : "não", ref.has_audio ? "sim" : "não");

    function row(label, a, b) {
      return "<tr><th>" + label + "</th><td>" + a + "</td><td>" + b + "</td></tr>";
    }
  }

  el("backToStep1").addEventListener("click", () => {
    showStep(1);
  });

  el("styleForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    clearBox(el("confirmError"));

    const style = {
      output_format: el("outputFormat").value,
      caption: {
        enabled: el("capEnabled").checked,
        position: el("capPosition").value,
        font: el("capFont").value,
        size: el("capSize").value,
        text_color: el("capTextColor").value,
        highlight_color: el("capHighlightColor").value,
        outline: el("capOutline").checked,
        max_words_per_chunk: Number(el("capMaxWords").value) || 3,
      },
      headline: {
        enabled: el("headEnabled").checked,
        text: el("headText").value,
        font: el("headFont").value,
        size: el("headSize").value,
        color: el("headColor").value,
        background_box: el("headBox").checked,
        background_color: el("headBgColor").value,
        position: el("headPosition").value,
        duration_seconds: Number(el("headDuration").value) || 3,
      },
    };

    const btn = el("confirmBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Enviando...';

    try {
      const resp = await fetch(API + "/api/jobs/" + currentJobId + "/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          style: style,
          model_size: el("modelSize").value,
          language: "pt",
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Falha ao confirmar o estilo.");

      showStep(3);
      resetProgressUI();
      startPolling();
    } catch (e) {
      alertBox(el("confirmError"), "error", e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Confirmar e gerar vídeo";
    }
  });

  // ------------------------------------------------------------------
  // Passo 3: acompanhamento
  // ------------------------------------------------------------------
  const STATUS_ORDER = [
    "queued",
    "transcribing",
    "rendering_test",
    "rendering_full",
    "validating",
    "done",
  ];

  function resetProgressUI() {
    el("doneBox").classList.add("hidden");
    clearBox(el("processingError"));
    document.querySelectorAll("#progressList li").forEach((li) => {
      li.classList.remove("current", "past");
    });
  }

  function updateProgressUI(status) {
    const idx = STATUS_ORDER.indexOf(status);
    document.querySelectorAll("#progressList li").forEach((li) => {
      const liIdx = STATUS_ORDER.indexOf(li.dataset.status);
      li.classList.toggle("past", liIdx >= 0 && liIdx < idx);
      li.classList.toggle("current", liIdx === idx);
    });
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 2500);
    pollStatus();
  }

  async function pollStatus() {
    try {
      const job = await fetch(API + "/api/jobs/" + currentJobId).then((r) => r.json());
      updateProgressUI(job.status);

      if (job.status === "done") {
        clearInterval(pollTimer);
        el("doneBox").classList.remove("hidden");
        const videoUrl = API + "/api/jobs/" + currentJobId + "/download";
        el("resultVideo").src = videoUrl;
        el("downloadLink").href = videoUrl;
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        alertBox(
          el("processingError"),
          "error",
          "<strong>Falha:</strong> " +
            (job.error || "erro desconhecido") +
            '<br><br><button class="btn btn-secondary" id="retryBtn" type="button">Ajustar estilo e tentar de novo</button>'
        );
        const retryBtn = el("retryBtn");
        if (retryBtn) {
          retryBtn.addEventListener("click", () => showStep(2));
        }
      }
    } catch (e) {
      // erro de rede pontual — mantém o polling, não trava a UI
    }
  }

  el("startOverBtn").addEventListener("click", () => {
    currentJobId = null;
    el("uploadForm").reset();
    el("styleForm").reset();
    showStep(1);
  });

  showStep(1);
  init();
})();
