(() => {
  const $ = (sel) => document.querySelector(sel);

  const els = {
    app: $(".app"),
    chatList: $("#chat-list"),
    chatTitle: $("#chat-title"),
    messages: $("#messages"),
    input: $("#input"),
    btnSend: $("#btn-send"),
    btnNew: $("#btn-new"),
    tierSelect: $("#tier-select"),
    health: $("#health"),
    statusLine: $("#status-line"),
    btnModels: $("#btn-models"),
    modelsModal: $("#models-modal"),
    modelsList: $("#models-list"),
    modelsStatus: $("#models-status"),
    btnModelsClose: $("#btn-models-close"),
    btnModelsHelp: $("#btn-models-help"),
    modelsHelp: $("#models-help"),
    routerModelSelect: $("#router-model-select"),
    plannerModelSelect: $("#planner-model-select"),
    scFlagRefusal: $("#sc-flag-refusal"),
    scFlagUncertain: $("#sc-flag-uncertain"),
    scFlagIrrelevant: $("#sc-flag-irrelevant"),
    scRefusalPatterns: $("#sc-refusal-patterns"),
    scUncertainPatterns: $("#sc-uncertain-patterns"),
    planEnabled: $("#plan-enabled"),
    planMaxSteps: $("#plan-max-steps"),
    planClassifierPrompt: $("#plan-classifier-prompt"),
    planPlannerPrompt: $("#plan-planner-prompt"),
    planComposerPrompt: $("#plan-composer-prompt"),
    btnModelsSave: $("#btn-models-save"),
    btnModelsAdd: $("#btn-models-add"),
    btnModelsReset: $("#btn-models-reset"),
    addSlotTier: $("#add-slot-tier"),
    addSlotModel: $("#add-slot-model"),
    addSlotModelOr: $("#add-slot-model-or"),
    addSlotRank: $("#add-slot-rank"),
    addSlotCtxOh: $("#add-slot-ctx-oh"),
    addSlotMaxCtx: $("#add-slot-max-ctx"),
    addSlotPrompt: $("#add-slot-prompt"),
    orApiKey: $("#or-api-key"),
    orKeyStatus: $("#or-key-status"),
    btnOrKeySave: $("#btn-or-key-save"),
    btnOrKeyClear: $("#btn-or-key-clear"),
    monitorPanel: $("#monitor-panel"),
    panelResizer: $("#panel-resizer"),
    btnPanelToggle: $("#btn-panel-toggle"),
    metricsInterval: $("#metrics-interval"),
    metricsOllama: $("#metrics-ollama"),
    metricsGpu: $("#metrics-gpu"),
    metricsTemp: $("#metrics-temp"),
    metricsRam: $("#metrics-ram"),
    metricsCpu: $("#metrics-cpu"),
    metricsNote: $("#metrics-note"),
  };

  const LS_PANEL_OPEN = "qwen.monitor.open";
  const LS_PANEL_WIDTH = "qwen.monitor.width";
  const LS_METRICS_INTERVAL = "qwen.monitor.interval";
  const LS_CHART_HEIGHTS = "qwen.monitor.chartHeights";
  const PANEL_MIN = 220;
  const PANEL_MAX_CAP = 960;
  const PANEL_DEFAULT = 300;
  const HISTORY_LEN = 60;
  const SPARK_H_MIN = 28;
  const SPARK_H_MAX = 280;
  const SPARK_H_DEFAULT = 48;

  const state = {
    chats: [],
    activeId: null,
    busy: false,
    selectGen: 0,
    ollamaModels: [],
    slots: [], // пул моделей (compat имя)
    fixedTiers: [],
    routerModel: "",
    plannerModel: "",
    selfcheckDefaults: null,
    planDefaults: null,
    panelOpen: true,
    panelWidth: PANEL_DEFAULT,
    metricsTimer: null,
    metricsInflight: false,
    metricsHistory: {
      cpu: [],
      ram: [],
      gpuUtil: [],
      gpuMem: [],
      gpuTemp: [],
      memTemp: [],
    },
    chartHeights: {
      "spark-gpu": SPARK_H_DEFAULT,
      "spark-vram": SPARK_H_DEFAULT,
      "spark-temp-gpu": SPARK_H_DEFAULT,
      "spark-temp-mem": SPARK_H_DEFAULT,
      "spark-ram": SPARK_H_DEFAULT,
      "spark-cpu": SPARK_H_DEFAULT,
    },
  };

  /** Выбор в чате: Auto | force_model=<pool id> */
  function forceSelection() {
    const v = els.tierSelect.value;
    if (!v || v === "auto") return { force_model: null, force_tier: null };
    return { force_model: v, force_tier: null };
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  if (typeof marked !== "undefined") {
    marked.setOptions({ gfm: true, breaks: true });
  }

  /** Закрыть незакрытый fence, чтобы стрим не ломал разметку. */
  function closeOpenFences(text) {
    const n = (String(text).match(/```/g) || []).length;
    return n % 2 === 1 ? text + "\n```" : text;
  }

  function hasOpenFence(text) {
    return ((String(text).match(/```/g) || []).length % 2) === 1;
  }

  function renderMarkdown(text, { streaming = false } = {}) {
    let src = text == null ? "" : String(text);
    if (streaming) src = closeOpenFences(src);
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      return escapeHtml(src).replace(/\n/g, "<br>");
    }
    const html = marked.parse(src, { async: false });
    return DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "option"],
      FORBID_ATTR: ["style", "srcset"],
    });
  }

  function langFromCode(codeEl) {
    const cls = codeEl?.getAttribute?.("class") || codeEl?.className || "";
    const m = String(cls).match(/(?:^|\s)(?:language|lang)-([^\s]+)/i);
    if (!m) return "";
    let raw = m[1];
    try {
      if (/%[0-9A-Fa-f]{2}/.test(raw)) raw = decodeURIComponent(raw);
    } catch (_) {
      /* битый percent-encoding — оставляем как есть */
    }
    return raw.replace(/[^a-zA-Z0-9_+#.-]/g, "").slice(0, 40);
  }

  function codePlainText(preOrCode) {
    const code = preOrCode.querySelector?.("code") || preOrCode;
    const clone = code.cloneNode(true);
    clone.querySelectorAll(".stream-caret").forEach((n) => n.remove());
    return clone.textContent || "";
  }

  const ICON_COPY =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M5.75 2.5A1.75 1.75 0 0 0 4 4.25v7a.75.75 0 0 1-1.5 0v-7A3.25 3.25 0 0 1 5.75 1h5a.75.75 0 0 1 0 1.5h-5zm1.5 3A1.75 1.75 0 0 0 5.5 7.25v6c0 .966.784 1.75 1.75 1.75h5A1.75 1.75 0 0 0 14 13.25v-6A1.75 1.75 0 0 0 12.25 5.5h-5zm0 1.5h5a.25.25 0 0 1 .25.25v6a.25.25 0 0 1-.25.25h-5a.25.25 0 0 1-.25-.25v-6a.25.25 0 0 1 .25-.25z"/></svg>';
  const ICON_CHECK =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M12.53 4.22a.75.75 0 0 1 0 1.06l-5.25 5.25a.75.75 0 0 1-1.06 0L4.22 8.53a.75.75 0 0 1 1.06-1.06L6.75 9l4.72-4.72a.75.75 0 0 1 1.06 0z"/></svg>';

  const LANG_ALIASES = {
    "c++": "cpp",
    cplusplus: "cpp",
    "c#": "csharp",
    cs: "csharp",
    "f#": "fsharp",
    js: "javascript",
    ts: "typescript",
    py: "python",
    rb: "ruby",
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    yml: "yaml",
    golang: "go",
    kt: "kotlin",
    rs: "rust",
    text: "plaintext",
    txt: "plaintext",
  };

  function normalizeLangClass(code) {
    const lang = langFromCode(code);
    if (!lang) return;
    const mapped = LANG_ALIASES[lang.toLowerCase()] || lang.toLowerCase();
    if (!/^[a-z0-9_+#.-]+$/i.test(mapped)) return;
    const cls = String(code.className || "").replace(/(?:^|\s)(?:language|lang)-[^\s]+/gi, "").trim();
    code.className = cls;
    code.classList.add("language-" + mapped);
  }

  function highlightCodeBlocks(el, { streaming = false, text = "" } = {}) {
    if (typeof hljs === "undefined") return;
    const codes = [...el.querySelectorAll("pre code")];
    const skipLast = streaming && hasOpenFence(text);
    for (let i = 0; i < codes.length; i++) {
      if (skipLast && i === codes.length - 1) continue;
      const code = codes[i];
      normalizeLangClass(code);
      try {
        hljs.highlightElement(code);
      } catch (_) {
        /* неизвестный язык — оставляем plain */
      }
    }
  }

  function enhanceCodeBlocks(el) {
    for (const pre of [...el.querySelectorAll("pre")]) {
      if (pre.parentElement?.classList.contains("code-block")) continue;
      const code = pre.querySelector("code");
      const lang = langFromCode(code);

      const wrap = document.createElement("div");
      wrap.className = "code-block";

      const header = document.createElement("div");
      header.className = "code-block-header";

      const langEl = document.createElement("span");
      langEl.className = "code-lang";
      langEl.textContent = lang || "";
      if (!lang) langEl.hidden = true;

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "code-copy";
      copyBtn.title = "Копировать";
      copyBtn.setAttribute("aria-label", "Копировать код");
      copyBtn.innerHTML = ICON_COPY;

      header.appendChild(langEl);
      header.appendChild(copyBtn);

      pre.replaceWith(wrap);
      wrap.appendChild(header);
      wrap.appendChild(pre);
    }
  }

  function placeStreamCaret(el, text) {
    const caret = document.createElement("span");
    caret.className = "stream-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "▍";
    if (hasOpenFence(text)) {
      const blocks = el.querySelectorAll(".code-block pre, pre");
      const hostPre = blocks[blocks.length - 1];
      const host = hostPre?.querySelector("code") || hostPre;
      if (host) {
        host.appendChild(caret);
        return;
      }
    }
    el.appendChild(caret);
  }

  function setMsgBody(el, text, { streaming = false } = {}) {
    const src = text == null ? "" : String(text);
    el.innerHTML = renderMarkdown(src, { streaming });
    for (const table of el.querySelectorAll("table")) {
      if (table.parentElement?.classList.contains("md-table-wrap")) continue;
      const wrap = document.createElement("div");
      wrap.className = "md-table-wrap";
      table.replaceWith(wrap);
      wrap.appendChild(table);
    }
    for (const a of el.querySelectorAll("a[href]")) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
    for (const img of el.querySelectorAll("img[src]")) {
      const srcAttr = img.getAttribute("src") || "";
      if (/^https?:\/\//i.test(srcAttr)) {
        img.removeAttribute("src");
        img.setAttribute("alt", img.getAttribute("alt") || "[изображение]");
        img.classList.add("blocked-remote-img");
      }
    }
    enhanceCodeBlocks(el);
    highlightCodeBlocks(el, { streaming, text: src });
    renderMathInMsg(el);
    if (streaming) placeStreamCaret(el, src);
  }

  /** LaTeX $…$ / $$…$$ → KaTeX (после sanitize; в code/pre не трогаем). */
  function renderMathInMsg(el) {
    if (typeof renderMathInElement !== "function") return;
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        throwOnError: false,
        ignoredTags: [
          "script",
          "noscript",
          "style",
          "textarea",
          "pre",
          "code",
          "kbd",
          "samp",
          "annotation",
          "annotation-xml",
        ],
        ignoredClasses: ["code-block", "code-copy"],
      });
    } catch (_) {
      /* неполная формула при стриме — оставляем сырой текст */
    }
  }

  async function copyCodeBlock(btn) {
    const block = btn.closest(".code-block");
    const pre = block?.querySelector("pre");
    if (!pre) return;
    const text = codePlainText(pre);
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } finally {
        ta.remove();
      }
    }
    btn.classList.add("copied");
    btn.title = "Скопировано";
    btn.innerHTML = ICON_CHECK;
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
      btn.classList.remove("copied");
      btn.title = "Копировать";
      btn.innerHTML = ICON_COPY;
    }, 1500);
  }

  function setBusy(busy) {
    state.busy = busy;
    els.btnSend.disabled = busy || !els.input.value.trim();
    els.input.disabled = busy;
    els.btnNew.disabled = busy;
    els.tierSelect.disabled = busy;
    if (els.btnModels) els.btnModels.disabled = busy;
    els.chatList.classList.toggle("busy", busy);
  }

  function autosize() {
    const el = els.input;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try {
        const j = await res.json();
        const d = j.detail;
        if (typeof d === "string") msg = d;
        else if (Array.isArray(d))
          msg = d.map((x) => x.msg || JSON.stringify(x)).join("; ");
        else if (d != null) msg = JSON.stringify(d);
        else if (j.message) msg = j.message;
      } catch (_) {
        /* ignore */
      }
      throw new Error(msg);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res;
  }

  function populateTierSelect(pool) {
    const prev = els.tierSelect.value || "auto";
    els.tierSelect.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "auto";
    auto.textContent = "Auto";
    els.tierSelect.appendChild(auto);
    const list = [...(pool || [])].sort((a, b) => {
      const ra = a.tier ? Number(a.rank ?? 0) : 999;
      const rb = b.tier ? Number(b.rank ?? 0) : 999;
      return ra - rb || String(a.label || a.model).localeCompare(String(b.label || b.model));
    });
    for (const m of list) {
      if (!(m.model || "").trim()) continue;
      const opt = document.createElement("option");
      opt.value = m.id;
      const tierBit = m.tier ? `${m.tier}·r${m.rank ?? "?"}` : "вручную";
      const prov = m.provider === "openrouter" ? "OR" : "Ollama";
      opt.textContent = `${m.model || m.id} · ${prov} (${tierBit})`;
      els.tierSelect.appendChild(opt);
    }
    const ok = [...els.tierSelect.options].some((o) => o.value === prev);
    els.tierSelect.value = ok ? prev : "auto";
  }

  function fillTierSelect(selectEl, selected) {
    if (!selectEl) return;
    const prev = selected !== undefined && selected !== null ? selected : selectEl.value || "";
    selectEl.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "вне роутинга";
    selectEl.appendChild(none);
    const src =
      state.fixedTiers.length > 0
        ? state.fixedTiers
        : [
            "tiny",
            "nano",
            "small",
            "mid",
            "large",
            "heavy",
            "xlarge",
            "coder",
            "ultra",
            "frontier",
          ].map((id, i) => ({ id, rank: i, label: id }));
    const list = [...src].sort(
      (a, b) => (a.rank ?? 0) - (b.rank ?? 0) || String(a.id).localeCompare(String(b.id))
    );
    for (const t of list) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.id;
      selectEl.appendChild(opt);
    }
    if (prev === "" || prev === null) {
      selectEl.value = "";
    } else if (prev && [...selectEl.options].some((o) => o.value === prev)) {
      selectEl.value = prev;
    }
  }

  /** Короткий тег модели для статус-строки: 0.8b, 4b, claude-sonnet-4… */
  function shortModelTag(model) {
    const raw = String(model || "").trim();
    if (!raw) return "?";
    const size = raw.match(/(\d+(?:\.\d+)?)\s*([bBmM])\b/);
    if (size) return `${size[1]}${size[2].toLowerCase()}`;
    const base = raw.includes("/") ? raw.split("/").pop() : raw;
    return (base || raw).slice(0, 28);
  }

  /** Цепочка слотов по rank: id, если размер модели дублируется. */
  function orchestraChainText(slots) {
    const list = [...(slots || [])]
      .filter((s) => (s.model || "").trim())
      .sort(
        (a, b) => (a.rank ?? 0) - (b.rank ?? 0) || String(a.id).localeCompare(String(b.id))
      );
    const tags = [];
    const seenSizes = new Set();
    for (const s of list) {
      const size = shortModelTag(s.model);
      let tag = size;
      if (seenSizes.has(size)) tag = s.id || size;
      else seenSizes.add(size);
      if (tags.length && tags[tags.length - 1] === tag) continue;
      tags.push(tag);
    }
    return tags.length ? tags.join(" → ") : "—";
  }

  function idleStatusLine(slots) {
    const src = slots || state.slots;
    return `localhost · оркестр ${orchestraChainText(src)} · adaptive ctx`;
  }

  function setIdleStatusLine(slots) {
    if (slots) state.slots = (slots || []).map((s) => ({ ...s }));
    if (!state.busy) els.statusLine.textContent = idleStatusLine(state.slots);
  }

  function fillModelSelect(selectEl, selected, opts) {
    const allowEmpty = !!(opts && opts.allowEmpty);
    selectEl.innerHTML = "";
    if (allowEmpty) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "— не назначена —";
      selectEl.appendChild(empty);
    }
    const models = state.ollamaModels.length
      ? state.ollamaModels
      : selected
        ? [selected]
        : [];
    if (!models.length && !allowEmpty) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Нет моделей Ollama";
      selectEl.appendChild(opt);
      return;
    }
    for (const name of models) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      selectEl.appendChild(opt);
    }
    if (selected && models.includes(selected)) {
      selectEl.value = selected;
    } else if (selected) {
      const opt = document.createElement("option");
      opt.value = selected;
      opt.textContent = selected + " (не установлена)";
      selectEl.appendChild(opt);
      selectEl.value = selected;
    } else if (allowEmpty) {
      selectEl.value = "";
    }
  }

  function selectedAddProvider() {
    const el = document.querySelector('input[name="add-provider"]:checked');
    return el ? el.value : "ollama";
  }

  function syncAddProviderUi() {
    const prov = selectedAddProvider();
    const isOr = prov === "openrouter";
    els.addSlotModel.hidden = isOr;
    els.addSlotModelOr.hidden = !isOr;
    if (isOr && Number(els.addSlotRank.value) < 4 && els.addSlotTier && els.addSlotTier.value) {
      els.addSlotRank.value = "5";
    }
    if (els.addSlotRank && els.addSlotTier) {
      els.addSlotRank.disabled = !(els.addSlotTier.value || "").trim();
    }
  }

  function syncCardProviderUi(card) {
    const provSel = card.querySelector(".slot-provider");
    const prov = (provSel && provSel.value) || card.dataset.provider || "ollama";
    card.dataset.provider = prov;
    const wrap = card.querySelector(".slot-model-wrap");
    if (!wrap) return;
    const current =
      (card.querySelector(".slot-model") && card.querySelector(".slot-model").value) || "";
    wrap.innerHTML = "";
    const modelLab = document.createElement("label");
    modelLab.className = "field-label";
    modelLab.textContent = prov === "openrouter" ? "Модель OpenRouter" : "Модель";
    let modelCtrl;
    if (prov === "openrouter") {
      modelCtrl = document.createElement("input");
      modelCtrl.className = "text-input slot-model";
      modelCtrl.type = "text";
      modelCtrl.value = current;
      modelCtrl.placeholder = "provider/model-id или пусто";
    } else {
      modelCtrl = document.createElement("select");
      modelCtrl.className = "tier-select slot-model";
      fillModelSelect(modelCtrl, current, { allowEmpty: true });
    }
    wrap.appendChild(modelLab);
    wrap.appendChild(modelCtrl);
  }

  function renderOpenRouterStatus(providers) {
    const or = (providers && providers.openrouter) || {};
    const st = els.orKeyStatus;
    if (!st) return;
    if (or.configured) {
      st.className = "or-key-status ok";
      if (or.from_env) {
        st.textContent = "ключ задан (переменная окружения OPENROUTER_API_KEY)";
      } else {
        st.textContent = "ключ задан (secrets.json)";
      }
    } else {
      st.className = "or-key-status";
      st.textContent = "не задан — слоты OpenRouter недоступны";
    }
  }

  async function saveOpenRouterKey() {
    const key = (els.orApiKey.value || "").trim();
    if (!key) {
      setModelsStatus("Введите ключ OpenRouter", "err");
      return;
    }
    setModelsStatus("Сохранение ключа…");
    try {
      const data = await api("/api/settings/providers/openrouter", {
        method: "PUT",
        body: JSON.stringify({ api_key: key }),
      });
      els.orApiKey.value = "";
      renderOpenRouterStatus(data.providers);
      setModelsStatus("Ключ OpenRouter сохранён", "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message, "err");
    }
  }

  async function clearOpenRouterKey() {
    if (!confirm("Очистить ключ OpenRouter из secrets.json? (env не трогаем)")) return;
    setModelsStatus("Очистка ключа…");
    try {
      const data = await api("/api/settings/providers/openrouter", {
        method: "PUT",
        body: JSON.stringify({ clear: true }),
      });
      els.orApiKey.value = "";
      renderOpenRouterStatus(data.providers);
      setModelsStatus("Ключ очищен", "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message, "err");
    }
  }

  function setModelsStatus(text, kind) {
    els.modelsStatus.textContent = text || "";
    els.modelsStatus.className = "modal-status" + (kind ? " " + kind : "");
  }

  function defaultRankFor(tierId) {
    const t = (state.fixedTiers || []).find((x) => x.id === tierId);
    if (t && t.rank != null) return Number(t.rank);
    return 0;
  }

  function defaultCtxOhFor(tierId) {
    const t = (state.fixedTiers || []).find((x) => x.id === tierId);
    if (t && t.ctx_overhead_pct != null) return Number(t.ctx_overhead_pct);
    const fallback = {
      tiny: 300,
      nano: 200,
      small: 100,
      mid: 50,
    };
    return fallback[tierId] != null ? fallback[tierId] : 0;
  }

  function defaultMaxCtxFor(tierId) {
    const t = (state.fixedTiers || []).find((x) => x.id === tierId);
    if (t && "max_ctx" in t) {
      return t.max_ctx != null ? Number(t.max_ctx) : "";
    }
    if (tierId === "tiny" || tierId === "nano" || tierId === "small") return 4096;
    return "";
  }

  function applyAddFormCtxDefaults() {
    const tier = ((els.addSlotTier && els.addSlotTier.value) || "").trim();
    if (els.addSlotCtxOh) {
      els.addSlotCtxOh.value = String(defaultCtxOhFor(tier || null));
    }
    if (els.addSlotMaxCtx) {
      const mx = defaultMaxCtxFor(tier || null);
      els.addSlotMaxCtx.value = mx === "" || mx == null ? "" : String(mx);
    }
  }

  function fillCardTierSelect(selectEl, selected) {
    selectEl.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "вне роутинга";
    selectEl.appendChild(none);
    const src =
      state.fixedTiers.length > 0
        ? state.fixedTiers
        : [
            "tiny",
            "nano",
            "small",
            "mid",
            "large",
            "heavy",
            "xlarge",
            "coder",
            "ultra",
            "frontier",
          ].map((id, i) => ({ id, rank: i }));
    const list = [...src].sort(
      (a, b) => (a.rank ?? 0) - (b.rank ?? 0) || String(a.id).localeCompare(String(b.id))
    );
    for (const t of list) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.id;
      selectEl.appendChild(opt);
    }
    if (selected) selectEl.value = selected;
    else selectEl.value = "";
  }

  function syncCardTierRank(card) {
    const tierSel = card.querySelector(".slot-tier");
    const rankInp = card.querySelector(".slot-rank");
    if (!tierSel || !rankInp) return;
    const hasTier = !!(tierSel.value || "").trim();
    rankInp.disabled = !hasTier;
    if (!hasTier) {
      rankInp.value = "";
      rankInp.placeholder = "—";
    } else if (rankInp.value === "" || rankInp.value == null) {
      rankInp.value = String(defaultRankFor(tierSel.value));
    }
  }

  function renderModelsList(pool) {
    state.slots = (pool || []).map((s) => ({ ...s }));
    setIdleStatusLine(state.slots);
    fillTierSelect(els.addSlotTier, (els.addSlotTier && els.addSlotTier.value) || "");
    els.modelsList.innerHTML = "";
    const sorted = [...state.slots].sort((a, b) => {
      const ra = a.tier != null ? Number(a.rank ?? 0) : 999;
      const rb = b.tier != null ? Number(b.rank ?? 0) : 999;
      return (
        ra - rb ||
        String(a.model || "").localeCompare(String(b.model || "")) ||
        String(a.id).localeCompare(String(b.id))
      );
    });
    for (const slot of sorted) {
      const card = document.createElement("div");
      const noTier = !(slot.tier || "").trim();
      card.className = noTier ? "slot-card unbound" : "slot-card";
      card.dataset.id = slot.id;
      card.dataset.provider = slot.provider || "ollama";

      const head = document.createElement("div");
      head.className = "slot-card-head";

      const title = document.createElement("span");
      title.className = "slot-id";
      title.textContent = slot.model || slot.id;
      title.title = slot.id;
      head.appendChild(title);

      const headRight = document.createElement("div");
      headRight.className = "slot-card-head-right";

      if (noTier) {
        const badge = document.createElement("span");
        badge.className = "slot-unbound-badge";
        badge.textContent = "вручную";
        badge.title = "Без тира — только ручной выбор в чате";
        headRight.appendChild(badge);
      }

      const provSel = document.createElement("select");
      provSel.className = "tier-select slot-provider";
      provSel.title = "Провайдер";
      for (const [val, lab] of [
        ["ollama", "Ollama"],
        ["openrouter", "OpenRouter"],
      ]) {
        const o = document.createElement("option");
        o.value = val;
        o.textContent = lab;
        provSel.appendChild(o);
      }
      provSel.value = slot.provider === "openrouter" ? "openrouter" : "ollama";
      provSel.addEventListener("change", () => syncCardProviderUi(card));
      headRight.appendChild(provSel);

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-ghost danger btn-slot-delete";
      del.textContent = "×";
      del.title = "Убрать из пула";
      del.addEventListener("click", () => removeSlot(card.dataset.id));
      headRight.appendChild(del);

      head.appendChild(headRight);
      card.appendChild(head);

      const meta = document.createElement("div");
      meta.className = "slot-meta";

      const modelWrap = document.createElement("div");
      modelWrap.className = "field-with-label slot-model-wrap";
      const modelLab = document.createElement("label");
      modelLab.className = "field-label";
      modelLab.textContent = "Модель";
      let modelCtrl;
      if ((slot.provider || "ollama") === "openrouter") {
        modelCtrl = document.createElement("input");
        modelCtrl.className = "text-input slot-model";
        modelCtrl.type = "text";
        modelCtrl.value = slot.model || "";
        modelCtrl.placeholder = "provider/model-id";
        modelCtrl.addEventListener("input", () => {
          title.textContent = modelCtrl.value.trim() || slot.id;
        });
      } else {
        modelCtrl = document.createElement("select");
        modelCtrl.className = "tier-select slot-model";
        fillModelSelect(modelCtrl, slot.model || "");
        modelCtrl.addEventListener("change", () => {
          title.textContent = modelCtrl.value || slot.id;
        });
      }
      modelWrap.appendChild(modelLab);
      modelWrap.appendChild(modelCtrl);

      const tierWrap = document.createElement("div");
      tierWrap.className = "field-with-label tier-field";
      const tierLab = document.createElement("label");
      tierLab.className = "field-label";
      tierLab.textContent = "Тир";
      const tierSel = document.createElement("select");
      tierSel.className = "tier-select slot-tier";
      tierSel.title = "Тир для Auto или без роутинга";
      fillCardTierSelect(tierSel, slot.tier || "");
      tierSel.addEventListener("change", () => {
        syncCardTierRank(card);
        card.classList.toggle("unbound", !(tierSel.value || "").trim());
        const badge = headRight.querySelector(".slot-unbound-badge");
        if (!(tierSel.value || "").trim()) {
          if (!badge) {
            const b = document.createElement("span");
            b.className = "slot-unbound-badge";
            b.textContent = "вручную";
            headRight.insertBefore(b, provSel);
          }
        } else if (badge) {
          badge.remove();
        }
      });
      tierWrap.appendChild(tierLab);
      tierWrap.appendChild(tierSel);

      const rankWrap = document.createElement("div");
      rankWrap.className = "field-with-label rank-field";
      const rankLab = document.createElement("label");
      rankLab.className = "field-label";
      rankLab.textContent = "Rank";
      const rankInp = document.createElement("input");
      rankInp.className = "text-input rank-input slot-rank";
      rankInp.type = "number";
      rankInp.min = "0";
      rankInp.max = "9";
      rankInp.value = noTier ? "" : String(slot.rank ?? defaultRankFor(slot.tier));
      rankInp.title = "Сила / порядок эскалации";
      rankInp.disabled = noTier;
      rankWrap.appendChild(rankLab);
      rankWrap.appendChild(rankInp);

      meta.appendChild(modelWrap);
      meta.appendChild(tierWrap);
      meta.appendChild(rankWrap);
      card.appendChild(meta);

      const ctxRow = document.createElement("div");
      ctxRow.className = "slot-ctx-row";

      const ohWrap = document.createElement("div");
      ohWrap.className = "field-with-label ctx-oh-field";
      const ohLab = document.createElement("label");
      ohLab.className = "field-label";
      ohLab.textContent = "Запас ctx %";
      const ohInp = document.createElement("input");
      ohInp.className = "text-input rank-input slot-ctx-oh";
      ohInp.type = "number";
      ohInp.min = "0";
      ohInp.max = "900";
      ohInp.value = String(slot.ctx_overhead_pct ?? 0);
      ohInp.title = "0 = минимум; 300 = база×4";
      ohWrap.appendChild(ohLab);
      ohWrap.appendChild(ohInp);

      const maxWrap = document.createElement("div");
      maxWrap.className = "field-with-label ctx-max-field";
      const maxLab = document.createElement("label");
      maxLab.className = "field-label";
      maxLab.textContent = "max ctx";
      const maxInp = document.createElement("input");
      maxInp.className = "text-input rank-input slot-max-ctx";
      maxInp.type = "number";
      maxInp.min = "256";
      maxInp.max = "32768";
      maxInp.step = "256";
      maxInp.placeholder = "8192";
      maxInp.value =
        slot.max_ctx != null && slot.max_ctx !== "" ? String(slot.max_ctx) : "";
      maxInp.title = "Потолок num_ctx для этой модели (пусто = 8192)";
      maxWrap.appendChild(maxLab);
      maxWrap.appendChild(maxInp);

      ctxRow.appendChild(ohWrap);
      ctxRow.appendChild(maxWrap);
      card.appendChild(ctxRow);

      const promptInp = document.createElement("textarea");
      promptInp.className = "prompt-input slot-prompt";
      promptInp.rows = 2;
      promptInp.value = slot.router_prompt || "";
      promptInp.placeholder = "Промпт для роутера (когда выбирать)…";
      card.appendChild(promptInp);

      els.modelsList.appendChild(card);
    }
  }

  function collectSlotsFromDom() {
    const out = [];
    const seen = new Set();
    for (const card of els.modelsList.querySelectorAll(".slot-card")) {
      const id = card.dataset.id;
      if (!id) continue;
      if (seen.has(id)) {
        throw new Error(`Дубликат id «${id}»`);
      }
      seen.add(id);
      const modelEl = card.querySelector(".slot-model");
      const provEl = card.querySelector(".slot-provider");
      const tierEl = card.querySelector(".slot-tier");
      const rankEl = card.querySelector(".slot-rank");
      const ohEl = card.querySelector(".slot-ctx-oh");
      const maxEl = card.querySelector(".slot-max-ctx");
      const provider =
        (provEl && provEl.value) || card.dataset.provider || "ollama";
      const model = ((modelEl && modelEl.value) || "").trim();
      if (!model) {
        throw new Error("У каждой записи пула должна быть модель");
      }
      const tier = ((tierEl && tierEl.value) || "").trim() || null;
      const ohRaw = ohEl && ohEl.value !== "" ? Number(ohEl.value) : 0;
      const maxRaw = maxEl && String(maxEl.value || "").trim();
      out.push({
        id,
        model,
        label: model,
        router_prompt: card.querySelector(".slot-prompt").value.trim(),
        tier,
        rank: tier
          ? Number(rankEl && rankEl.value) || defaultRankFor(tier)
          : null,
        provider,
        ctx_overhead_pct: Number.isFinite(ohRaw) ? Math.max(0, Math.min(900, ohRaw)) : 0,
        max_ctx: maxRaw ? Number(maxRaw) || null : null,
      });
    }
    return out;
  }

  function poolFromPayload(data) {
    if (!data) return [];
    if (Array.isArray(data.pool) && data.pool.length && typeof data.pool[0] === "object") {
      return data.pool;
    }
    if (Array.isArray(data.slots) && data.slots.length && typeof data.slots[0] === "object") {
      return data.slots;
    }
    // settings PUT response: models = пул; health: models = теги Ollama (строки)
    if (
      Array.isArray(data.models) &&
      data.models.length &&
      typeof data.models[0] === "object"
    ) {
      return data.models;
    }
    return [];
  }

  async function loadSettings() {
    const data = await api("/api/settings");
    state.routerModel = data.router_model || "";
    state.plannerModel = data.planner_model || "";
    state.fixedTiers = data.fixed_tiers || [];
    state.selfcheckDefaults = (data.defaults && data.defaults.selfcheck) || null;
    state.planDefaults = (data.defaults && data.defaults.plan) || null;
    fillSelfcheckForm(data.selfcheck, state.selfcheckDefaults);
    fillPlanForm(data.plan, state.planDefaults);
    const pool = poolFromPayload(data);
    renderModelsList(pool);
    populateTierSelect(pool);
    fillModelSelect(els.addSlotModel, els.addSlotModel.value || "");
    fillModelSelect(els.routerModelSelect, state.routerModel);
    if (els.plannerModelSelect) {
      fillModelSelect(els.plannerModelSelect, state.plannerModel);
    }
    fillTierSelect(els.addSlotTier, (els.addSlotTier && els.addSlotTier.value) || "");
    renderOpenRouterStatus(data.providers);
    syncAddProviderUi();
    return data;
  }

  function patternsToText(list) {
    return Array.isArray(list) ? list.join("\n") : "";
  }

  function parsePatternLines(text) {
    return String(text || "")
      .split(/\r?\n/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean)
      .filter((s, i, arr) => arr.indexOf(s) === i);
  }

  function fillSelfcheckForm(sc, defaults) {
    const cfg = sc || {};
    const def = defaults || {};
    if (els.scFlagRefusal) {
      els.scFlagRefusal.checked = cfg.flag_refusal !== false;
    }
    if (els.scFlagUncertain) {
      els.scFlagUncertain.checked = cfg.flag_uncertain !== false;
    }
    if (els.scFlagIrrelevant) {
      els.scFlagIrrelevant.checked = cfg.flag_irrelevant !== false;
    }
    if (els.scRefusalPatterns) {
      const list =
        cfg.refusal_patterns_effective ||
        cfg.refusal_patterns ||
        def.refusal_patterns ||
        [];
      els.scRefusalPatterns.value = patternsToText(list);
      els.scRefusalPatterns.dataset.usingDefault = cfg.using_default_refusal_patterns
        ? "1"
        : "0";
    }
    if (els.scUncertainPatterns) {
      const list =
        cfg.uncertain_patterns_effective ||
        cfg.uncertain_patterns ||
        def.uncertain_patterns ||
        [];
      els.scUncertainPatterns.value = patternsToText(list);
      els.scUncertainPatterns.dataset.usingDefault = cfg.using_default_uncertain_patterns
        ? "1"
        : "0";
    }
  }

  function collectSelfcheckFromDom() {
    const refusal = parsePatternLines(
      els.scRefusalPatterns ? els.scRefusalPatterns.value : ""
    );
    const uncertain = parsePatternLines(
      els.scUncertainPatterns ? els.scUncertainPatterns.value : ""
    );
    const def = state.selfcheckDefaults || {};
    const defRefusal = Array.isArray(def.refusal_patterns) ? def.refusal_patterns : [];
    const defUncertain = Array.isArray(def.uncertain_patterns)
      ? def.uncertain_patterns
      : [];

    const sameList = (a, b) =>
      a.length === b.length && a.every((x, i) => x === b[i]);

    // Если список совпадает со встроенным — храним null (builtins), иначе свой
    const refusalOut = sameList(refusal, defRefusal) ? null : refusal;
    const uncertainOut = sameList(uncertain, defUncertain) ? null : uncertain;

    return {
      flag_refusal: !!(els.scFlagRefusal && els.scFlagRefusal.checked),
      flag_uncertain: !!(els.scFlagUncertain && els.scFlagUncertain.checked),
      flag_irrelevant: !!(els.scFlagIrrelevant && els.scFlagIrrelevant.checked),
      refusal_patterns: refusalOut,
      uncertain_patterns: uncertainOut,
    };
  }

  function fillPlanForm(plan, defaults) {
    const cfg = plan || {};
    const def = defaults || {};
    if (els.planEnabled) {
      els.planEnabled.checked = cfg.enabled !== false;
    }
    if (els.planMaxSteps) {
      const n = Number(cfg.max_steps);
      els.planMaxSteps.value = Number.isFinite(n) && n >= 1 ? String(n) : "4";
    }
    const fillPrompt = (el, effectiveKey, rawKey, usingKey) => {
      if (!el) return;
      el.value = cfg[effectiveKey] || cfg[rawKey] || def[rawKey] || "";
      el.dataset.usingDefault = cfg[usingKey] ? "1" : "0";
    };
    fillPrompt(
      els.planClassifierPrompt,
      "classifier_prompt_effective",
      "classifier_prompt",
      "using_default_classifier_prompt"
    );
    fillPrompt(
      els.planPlannerPrompt,
      "planner_prompt_effective",
      "planner_prompt",
      "using_default_planner_prompt"
    );
    fillPrompt(
      els.planComposerPrompt,
      "composer_prompt_effective",
      "composer_prompt",
      "using_default_composer_prompt"
    );
  }

  function collectPlanFromDom() {
    const def = state.planDefaults || {};
    const sameText = (a, b) => String(a || "").trim() === String(b || "").trim();
    const pickPrompt = (el, defKey) => {
      const val = el ? el.value : "";
      return sameText(val, def[defKey] || "") ? null : val;
    };
    let maxSteps = 4;
    if (els.planMaxSteps) {
      const n = Number(els.planMaxSteps.value);
      if (Number.isFinite(n)) maxSteps = Math.max(2, Math.min(6, Math.round(n)));
    }
    return {
      enabled: !!(els.planEnabled && els.planEnabled.checked),
      max_steps: maxSteps,
      classifier_prompt: pickPrompt(els.planClassifierPrompt, "classifier_prompt"),
      planner_prompt: pickPrompt(els.planPlannerPrompt, "planner_prompt"),
      composer_prompt: pickPrompt(els.planComposerPrompt, "composer_prompt"),
    };
  }

  function toggleModelsHelp() {
    const open = els.modelsHelp.hidden;
    els.modelsHelp.hidden = !open;
    els.btnModelsHelp.setAttribute("aria-expanded", open ? "true" : "false");
  }

  async function openModelsModal() {
    setModelsStatus("");
    els.modelsHelp.hidden = true;
    els.btnModelsHelp.setAttribute("aria-expanded", "false");
    try {
      await refreshHealth();
      await loadSettings();
      els.modelsModal.hidden = false;
    } catch (e) {
      setModelsStatus(e.message, "err");
      els.modelsModal.hidden = false;
    }
  }

  function closeModelsModal() {
    els.modelsModal.hidden = true;
  }

  async function saveModels() {
    setModelsStatus("Сохранение…");
    try {
      const models = collectSlotsFromDom();
      const data = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          models,
          router_model: els.routerModelSelect.value || null,
          planner_model: els.plannerModelSelect ? els.plannerModelSelect.value || null : null,
          selfcheck: collectSelfcheckFromDom(),
          plan: collectPlanFromDom(),
        }),
      });
      state.routerModel = data.router_model || "";
      state.plannerModel = data.planner_model || "";
      state.fixedTiers = data.fixed_tiers || state.fixedTiers;
      state.selfcheckDefaults = (data.defaults && data.defaults.selfcheck) || state.selfcheckDefaults;
      state.planDefaults = (data.defaults && data.defaults.plan) || state.planDefaults;
      fillSelfcheckForm(data.selfcheck, state.selfcheckDefaults);
      fillPlanForm(data.plan, state.planDefaults);
      const pool = poolFromPayload(data);
      renderModelsList(pool);
      populateTierSelect(pool);
      fillModelSelect(els.routerModelSelect, state.routerModel);
      if (els.plannerModelSelect) {
        fillModelSelect(els.plannerModelSelect, state.plannerModel);
      }
      setModelsStatus("Сохранено", "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message, "err");
    }
  }

  async function persistDraftSlots() {
    const models = collectSlotsFromDom();
    if (!models.length) return null;
    return api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        models,
        router_model: els.routerModelSelect.value || null,
        planner_model: els.plannerModelSelect ? els.plannerModelSelect.value || null : null,
        selfcheck: collectSelfcheckFromDom(),
        plan: collectPlanFromDom(),
      }),
    });
  }

  async function addModelSlot() {
    const provider = selectedAddProvider();
    const tier = ((els.addSlotTier && els.addSlotTier.value) || "").trim() || null;
    const model =
      provider === "openrouter"
        ? els.addSlotModelOr.value.trim()
        : els.addSlotModel.value.trim();
    if (!model) {
      setModelsStatus(
        provider === "openrouter"
          ? "Укажите id модели OpenRouter"
          : "Выберите модель Ollama",
        "err"
      );
      return;
    }
    setModelsStatus("Добавление…");
    try {
      await persistDraftSlots();
      const ohRaw = Number(els.addSlotCtxOh && els.addSlotCtxOh.value);
      const maxRaw =
        els.addSlotMaxCtx && String(els.addSlotMaxCtx.value || "").trim() !== ""
          ? Number(els.addSlotMaxCtx.value)
          : null;
      const body = {
        model,
        provider,
        tier,
        label: model,
        router_prompt: els.addSlotPrompt.value.trim() || null,
        rank:
          tier && Number.isFinite(Number(els.addSlotRank.value))
            ? Number(els.addSlotRank.value)
            : null,
        ctx_overhead_pct: Number.isFinite(ohRaw)
          ? Math.max(0, Math.min(900, ohRaw))
          : 0,
        max_ctx:
          maxRaw != null && Number.isFinite(maxRaw)
            ? Math.max(256, Math.min(32768, maxRaw))
            : null,
      };
      const data = await api("/api/settings/models", {
        method: "POST",
        body: JSON.stringify(body),
      });
      state.routerModel = data.router_model || state.routerModel;
      state.plannerModel = data.planner_model || state.plannerModel;
      state.fixedTiers = data.fixed_tiers || state.fixedTiers;
      const pool = poolFromPayload(data);
      renderModelsList(pool);
      populateTierSelect(pool);
      fillModelSelect(els.routerModelSelect, state.routerModel);
      if (els.plannerModelSelect) {
        fillModelSelect(els.plannerModelSelect, state.plannerModel);
      }
      renderOpenRouterStatus(data.providers);
      els.addSlotPrompt.value = "";
      els.addSlotModelOr.value = "";
      if (els.addSlotCtxOh) els.addSlotCtxOh.value = "0";
      if (els.addSlotMaxCtx) els.addSlotMaxCtx.value = "";
      applyAddFormCtxDefaults();
      setModelsStatus(`Добавлено: ${model}`, "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message, "err");
    }
  }

  async function removeSlot(id) {
    if (!id) return;
    if ((state.slots || []).length <= 1) {
      setModelsStatus(
        "Нельзя удалить последнюю модель — в пуле должна остаться хотя бы одна",
        "err"
      );
      return;
    }
    setModelsStatus("Удаление…");
    try {
      await persistDraftSlots();
      const data = await api(`/api/settings/models/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      state.fixedTiers = data.fixed_tiers || state.fixedTiers;
      const pool = poolFromPayload(data);
      renderModelsList(pool);
      populateTierSelect(pool);
      setModelsStatus(`Удалено: ${id}`, "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message || "Не удалось удалить модель", "err");
    }
  }

  async function resetModels() {
    if (!confirm("Сбросить пул моделей, промпты, самопроверку и план к значениям по умолчанию?")) return;
    setModelsStatus("Сброс…");
    try {
      const data = await api("/api/settings/reset", { method: "POST", body: "{}" });
      state.routerModel = data.router_model || "";
      state.plannerModel = data.planner_model || "";
      state.fixedTiers = data.fixed_tiers || [];
      state.selfcheckDefaults = (data.defaults && data.defaults.selfcheck) || null;
      state.planDefaults = (data.defaults && data.defaults.plan) || null;
      fillSelfcheckForm(data.selfcheck, state.selfcheckDefaults);
      fillPlanForm(data.plan, state.planDefaults);
      const pool = poolFromPayload(data);
      renderModelsList(pool);
      populateTierSelect(pool);
      fillModelSelect(els.routerModelSelect, state.routerModel);
      if (els.plannerModelSelect) {
        fillModelSelect(els.plannerModelSelect, state.plannerModel);
      }
      setModelsStatus("Сброшено к defaults", "ok");
      await refreshHealth();
    } catch (e) {
      setModelsStatus(e.message, "err");
    }
  }

  async function refreshHealth() {
    try {
      const h = await api("/api/health");
      state.ollamaModels = h.models || [];
      const pool = poolFromPayload(h);
      if (pool.length) {
        populateTierSelect(pool);
        setIdleStatusLine(pool);
      } else if (h.tiers) {
        const fromTiers = Object.entries(h.tiers).map(([id, model]) => ({
          id,
          model,
          tier: id,
          label: `${id} · ${model}`,
        }));
        populateTierSelect(fromTiers);
        setIdleStatusLine(fromTiers);
      }
      if (!h.ollama && h.missing && h.missing.length) {
        els.health.className = "health err";
        els.health.textContent =
          "Нет доступных моделей" + (h.error ? `: ${h.error}` : "");
        return;
      }
      if (h.missing && h.missing.length) {
        els.health.className = "health err";
        els.health.textContent = "Нет моделей: " + h.missing.join(", ");
        return;
      }
      // h.ok == есть ≥1 тир; missing_optional — мягкое замечание, не ломает ok
      const bits = [];
      if (h.ollama) bits.push("Ollama");
      if (h.providers && h.providers.openrouter && h.providers.openrouter.configured) {
        bits.push("OpenRouter");
      }
      bits.push(h.ok === false ? "не готова" : "готова");
      if (h.missing_optional && h.missing_optional.length) {
        bits.push("нет: " + h.missing_optional.slice(0, 4).join(", "));
        if (h.missing_optional.length > 4) bits.push("…");
      }
      els.health.className = h.ok === false ? "health err" : "health ok";
      els.health.textContent = bits.join(" · ");
    } catch (e) {
      els.health.className = "health err";
      els.health.textContent = "Сервер: " + e.message;
    }
  }

  const ICON_CLEAR =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8.09 2.09a1.75 1.75 0 0 1 2.47 0l3.35 3.35a1.75 1.75 0 0 1 0 2.47L8.4 13.42A2.25 2.25 0 0 1 6.81 14H2.75a.75.75 0 0 1-.75-.75V9.19c0-.6.24-1.17.66-1.59l5.43-5.51zm1.41.88a.25.25 0 0 0-.35 0L4.2 8.1l3.7 3.7 5.05-5.05a.25.25 0 0 0 0-.35L8.5 2.97zM3.5 9.4v3.1h2.9L3.5 9.4z"/></svg>';
  const ICON_DELETE =
    '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M6.5 1.75A.75.75 0 0 1 7.25 1h1.5a.75.75 0 0 1 .75.75V3h3.75a.75.75 0 0 1 0 1.5h-.34l-.7 8.05A1.75 1.75 0 0 1 10.47 14H5.53a1.75 1.75 0 0 1-1.74-1.45L3.09 4.5H2.75a.75.75 0 0 1 0-1.5H6.5V1.75zM5.1 4.5l.68 7.85a.25.25 0 0 0 .25.2h4.94a.25.25 0 0 0 .25-.2l.68-7.85H5.1zm1.65 1.75a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5a.75.75 0 0 1 .75-.75zm2.5 0a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5a.75.75 0 0 1 .75-.75z"/></svg>';

  function renderChatList() {
    els.chatList.innerHTML = "";
    for (const c of state.chats) {
      const row = document.createElement("div");
      row.className = "chat-row" + (c.id === state.activeId ? " active" : "");
      row.dataset.id = c.id;

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chat-item";
      btn.textContent = c.title || "New Chat";
      btn.title = c.title || "New Chat";
      btn.addEventListener("click", () => selectChat(c.id));

      const actions = document.createElement("div");
      actions.className = "chat-actions";

      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "chat-action";
      clearBtn.title = "Очистить";
      clearBtn.setAttribute("aria-label", "Очистить чат");
      clearBtn.innerHTML = ICON_CLEAR;
      clearBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        clearChat(c.id);
      });

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "chat-action danger";
      delBtn.title = "Удалить";
      delBtn.setAttribute("aria-label", "Удалить чат");
      delBtn.innerHTML = ICON_DELETE;
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(c.id);
      });

      actions.appendChild(clearBtn);
      actions.appendChild(delBtn);
      row.appendChild(btn);
      row.appendChild(actions);
      els.chatList.appendChild(row);
    }
  }

  function metaChips(meta) {
    if (!meta) return "";
    const chips = [];
    if (meta.tier) chips.push(`<span class="chip accent">${escapeHtml(meta.tier)}</span>`);
    if (meta.model) chips.push(`<span class="chip">${escapeHtml(meta.model)}</span>`);
    if (meta.need_web) chips.push(`<span class="chip">web</span>`);
    if (meta.need_local_time) chips.push(`<span class="chip">время ПК</span>`);
    if (meta.need_calc) chips.push(`<span class="chip">счёт ПК</span>`);
    if (meta.planned) {
      const n = Array.isArray(meta.plan_steps) ? meta.plan_steps.length : 0;
      chips.push(
        `<span class="chip">${n ? `план: ${escapeHtml(n)} шагов` : "план"}</span>`
      );
    }
    if (meta.num_ctx) chips.push(`<span class="chip">ctx ${escapeHtml(meta.num_ctx)}</span>`);
    if (meta.used_history === false) chips.push(`<span class="chip">без истории</span>`);
    else if (meta.used_history === true) chips.push(`<span class="chip">история</span>`);
    if (meta.escalated) chips.push(`<span class="chip warn">escalated</span>`);
    if (meta.attempts > 1)
      chips.push(`<span class="chip warn">попыток: ${escapeHtml(meta.attempts)}</span>`);
    if (meta.problems && meta.problems.length)
      chips.push(`<span class="chip warn">${escapeHtml(meta.problems.join(", "))}</span>`);
    else if (meta.checked) chips.push(`<span class="chip">проверено</span>`);
    if (meta.route_reason)
      chips.push(`<span class="chip" title="${escapeHtml(meta.route_reason)}">${escapeHtml(meta.route_reason)}</span>`);
    return chips.length ? `<div class="msg-meta">${chips.join("")}</div>` : "";
  }

  function appendMessage(role, content, meta, { streaming = false } = {}) {
    const wrap = document.createElement("article");
    wrap.className = "msg " + role + (streaming ? " streaming" : "");
    wrap.innerHTML = `
      <div class="msg-role">${role === "user" ? "You" : "Orchestra"}</div>
      <div class="msg-plan-stack"></div>
      <div class="msg-body"></div>
      <div class="msg-tools"></div>
      <div class="msg-meta-slot"></div>
    `;
    hydratePlanStack(wrap.querySelector(".msg-plan-stack"), meta);
    if (content) setMsgBody(wrap.querySelector(":scope > .msg-body"), content);
    wrap.querySelector(".msg-meta-slot").innerHTML = metaChips(meta);
    els.messages.appendChild(wrap);
    els.messages.scrollTop = els.messages.scrollHeight;
    return wrap;
  }

  function renderMessages(messages) {
    els.messages.innerHTML = "";
    for (const m of messages || []) {
      appendMessage(m.role, m.content, m.meta);
    }
  }

  async function loadChats() {
    state.chats = await api("/api/chats");
    renderChatList();
  }

  async function selectChat(id) {
    if (state.busy) return;
    const gen = ++state.selectGen;
    state.activeId = id;
    const data = await api(`/api/chats/${id}`);
    if (gen !== state.selectGen || state.activeId !== id) return;
    els.chatTitle.textContent = data.title || "New Chat";
    renderMessages(data.messages);
    renderChatList();
    setBusy(false);
  }

  async function ensureChat() {
    if (state.activeId) return state.activeId;
    const chat = await api("/api/chats", { method: "POST", body: "{}" });
    state.chats.unshift(chat);
    state.activeId = chat.id;
    els.chatTitle.textContent = chat.title;
    renderChatList();
    return chat.id;
  }

  async function newChat() {
    if (state.busy) return;
    setBusy(true);
    try {
      const chat = await api("/api/chats", { method: "POST", body: "{}" });
      state.chats.unshift(chat);
      state.activeId = chat.id;
      els.chatTitle.textContent = chat.title;
      els.messages.innerHTML = "";
      renderChatList();
    } finally {
      setBusy(false);
      els.input.focus();
    }
  }

  async function clearChat(id) {
    id = id || state.activeId;
    if (!id || state.busy) return;
    if (!confirm("Очистить историю этого чата?")) return;
    state.selectGen += 1;
    try {
      await api(`/api/chats/${id}/clear`, { method: "POST", body: "{}" });
      if (id === state.activeId) {
        els.messages.innerHTML = "";
        els.chatTitle.textContent = "New Chat";
      }
      await loadChats();
    } catch (e) {
      els.statusLine.textContent = "Ошибка: " + e.message;
    }
  }

  async function deleteChat(id) {
    id = id || state.activeId;
    if (!id || state.busy) return;
    if (!confirm("Удалить этот чат?")) return;
    state.selectGen += 1;
    const wasActive = id === state.activeId;
    try {
      await api(`/api/chats/${id}`, { method: "DELETE" });
      if (wasActive) {
        state.activeId = null;
        els.messages.innerHTML = "";
        els.chatTitle.textContent = "New Chat";
      }
      await loadChats();
      if (wasActive) {
        if (state.chats.length) await selectChat(state.chats[0].id);
        else renderChatList();
      }
    } catch (e) {
      els.statusLine.textContent = "Ошибка: " + e.message;
    }
  }

  function parseSseChunk(buffer) {
    const events = [];
    const parts = buffer.split(/\r?\n\r?\n/);
    const rest = parts.pop() || "";
    for (const block of parts) {
      if (!block.trim()) continue;
      let event = "message";
      const dataLines = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      const raw = dataLines.join("\n");
      let data = raw;
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = { message: raw || "bad sse json" };
        event = "error";
      }
      if (data == null || typeof data !== "object") {
        data = { message: String(raw || "bad sse payload") };
        event = "error";
      }
      events.push({ event, data });
    }
    return { events, rest };
  }

  function flushBodyRender(ctx, { streaming = false } = {}) {
    if (ctx._raf) {
      cancelAnimationFrame(ctx._raf);
      ctx._raf = 0;
    }
    setMsgBody(ctx.bodyEl, ctx.full, { streaming });
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function scheduleBodyRender(ctx) {
    if (ctx._raf) return;
    ctx._raf = requestAnimationFrame(() => {
      ctx._raf = 0;
      setMsgBody(ctx.bodyEl, ctx.full, { streaming: true });
      els.messages.scrollTop = els.messages.scrollHeight;
    });
  }

  function stepIdOf(data) {
    if (data == null) return "";
    if (data.id != null && data.id !== "") return String(data.id);
    if (data.step_id != null && data.step_id !== "") return String(data.step_id);
    if (data.index != null && data.index !== "") return String(data.index);
    return "";
  }

  function setFoldOpen(fold, open) {
    if (!fold) return;
    fold.open = !!open;
    const head = fold.querySelector(".think-fold-head");
    if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function attachFoldToggle(fold) {
    const head = fold.querySelector(".think-fold-head");
    if (!head) return;
    head.setAttribute("aria-expanded", fold.open ? "true" : "false");
    if (head.dataset.foldBound) return;
    head.dataset.foldBound = "1";
    fold.addEventListener("toggle", () => {
      head.setAttribute("aria-expanded", fold.open ? "true" : "false");
    });
  }

  function planHeadLabel(data, steps) {
    const n = (steps || []).length;
    const awaiting = data && data.awaiting;
    const bits = ["План"];
    if (n) bits.push(n === 1 ? "1 шаг" : n < 5 ? n + " шага" : n + " шагов");
    if (awaiting) bits.push("утвердите");
    return bits.join(" · ");
  }

  function planStepLi(s, i) {
    const title = escapeHtml(s.title || `Шаг ${i + 1}`);
    const prompt = s.prompt
      ? `<div class="plan-step-prompt">${escapeHtml(s.prompt)}</div>`
      : "";
    const tier = s.tier_hint || s.tier
      ? `<span class="chip">${escapeHtml(s.tier_hint || s.tier)}</span>`
      : "";
    const sid = escapeHtml(String(s.id != null ? s.id : i + 1));
    const role = s.block_role
      ? `<span class="chip">${escapeHtml(s.block_role === "cpu" ? "счёт ПК" : "формула")}</span>`
      : "";
    return `<li data-step-id="${sid}"><div class="plan-step-head"><span class="plan-step-title">${title}</span>${tier}${role}</div>${prompt}</li>`;
  }

  function renderPlanStepsHtml(steps) {
    const list = steps || [];
    let html = "";
    let i = 0;
    while (i < list.length) {
      const s = list[i];
      if (s && s.block === "calc") {
        const group = [];
        while (i < list.length && list[i] && list[i].block === "calc") {
          group.push(list[i]);
          i += 1;
        }
        const title = escapeHtml((group[0] && group[0].block_title) || "Расчёт");
        html += `<li class="plan-block-li">
          <div class="plan-block plan-block-calc">
            <div class="plan-block-head">
              <span class="plan-block-icon" aria-hidden="true">ƒx</span>
              <span class="plan-block-title">${title}</span>
              <span class="chip">формула → счёт ПК</span>
            </div>
            <ol class="plan-block-sub">${group.map((g, gi) => planStepLi(g, gi)).join("")}</ol>
          </div>
        </li>`;
        continue;
      }
      html += planStepLi(s, i);
      i += 1;
    }
    return html;
  }

  function renderPlanFold(host, data, { open = true, live = false } = {}) {
    const steps = Array.isArray(data.steps) ? data.steps : Array.isArray(data.plan_steps) ? data.plan_steps : [];
    const fold = document.createElement("details");
    fold.className = "think-fold plan-fold";
    const goal = data.goal || data.plan_goal
      ? `<div class="plan-card-goal">${escapeHtml(data.goal || data.plan_goal)}</div>`
      : "";
    const actions = live
      ? `<div class="plan-card-actions">
        <button type="button" class="btn btn-send" data-plan-action="approve">Выполнить план</button>
        <button type="button" class="btn btn-ghost" data-plan-action="reject">Ответить без плана</button>
      </div>`
      : "";
    fold.innerHTML = `
      <summary class="think-fold-head">
        <span class="think-fold-chevron" aria-hidden="true"></span>
        <span class="think-fold-title">${escapeHtml(planHeadLabel(data, steps))}</span>
      </summary>
      <div class="think-fold-body">
        ${goal}
        <ol class="plan-card-steps">${renderPlanStepsHtml(steps)}</ol>
        ${actions}
      </div>
    `;
    fold.open = !!open;
    attachFoldToggle(fold);
    host.appendChild(fold);
    return fold;
  }

  function hydratePlanStack(stackEl, meta) {
    if (!stackEl || !meta || !meta.planned) return;
    const steps = Array.isArray(meta.plan_steps) ? meta.plan_steps : [];
    if (!steps.length && !meta.plan_goal) return;
    renderPlanFold(stackEl, { goal: meta.plan_goal, steps, awaiting: false }, { open: false });
    steps.forEach((s, i) => {
      const rec = buildStepFold(stackEl, {
        id: s.id != null ? s.id : i + 1,
        index: i + 1,
        title: s.title,
        tier: s.tier,
        model: s.model,
        prompt: s.prompt,
        block: s.block,
        block_title: s.block_title,
        block_role: s.block_role,
      });
      rec.el.classList.remove("is-streaming");
      setFoldOpen(rec.el, false);
      if (s.failed) rec.el.classList.add("is-failed");
      if (s.text) setMsgBody(rec.answerEl, s.text);
      if (s.note) {
        const line = document.createElement("div");
        line.className = "tool-line";
        line.textContent = s.note;
        rec.chainEl.appendChild(line);
      }
    });
  }

  function calcBlockHost(host, data) {
    const bid = (data && data.block) || "";
    if (bid !== "calc" || !host) return host;
    const key = String(bid);
    let box = host.querySelector(`.plan-block[data-block="${key}"]`);
    if (!box) {
      box = document.createElement("div");
      box.className = "plan-block plan-block-calc";
      box.dataset.block = key;
      const title = escapeHtml((data && data.block_title) || "Расчёт");
      box.innerHTML = `
        <div class="plan-block-head">
          <span class="plan-block-icon" aria-hidden="true">ƒx</span>
          <span class="plan-block-title">${title}</span>
          <span class="chip">формула → счёт ПК</span>
        </div>
        <div class="plan-block-items"></div>
      `;
      host.appendChild(box);
    }
    return box.querySelector(".plan-block-items") || host;
  }

  function buildStepFold(host, data) {
    const sid = stepIdOf(data) || "1";
    const fold = document.createElement("details");
    fold.className = "think-fold step-fold is-streaming";
    fold.dataset.stepId = sid;
    const title = data.title || `Шаг ${data.index || sid}`;
    const idx = data.index != null ? data.index : sid;
    const chips = [];
    if (data.tier) chips.push(`<span class="chip">${escapeHtml(String(data.tier))}</span>`);
    if (data.model) chips.push(`<span class="chip">${escapeHtml(String(data.model))}</span>`);
    const prompt = data.prompt
      ? `<div class="think-fold-prompt">${escapeHtml(data.prompt)}</div>`
      : `<div class="think-fold-prompt" hidden></div>`;
    fold.innerHTML = `
      <summary class="think-fold-head">
        <span class="think-fold-chevron" aria-hidden="true"></span>
        <span class="think-fold-title">${escapeHtml(`${idx}. ${title}`)}</span>
        <span class="think-fold-chips">${chips.join("")}</span>
      </summary>
      <div class="think-fold-body">
        ${prompt}
        <div class="think-fold-chain"></div>
        <div class="msg-body think-fold-md"></div>
      </div>
    `;
    fold.open = true;
    attachFoldToggle(fold);
    calcBlockHost(host, data).appendChild(fold);
    return {
      el: fold,
      answerEl: fold.querySelector(".think-fold-md"),
      chainEl: fold.querySelector(".think-fold-chain"),
      promptEl: fold.querySelector(".think-fold-prompt"),
      chipsEl: fold.querySelector(".think-fold-chips"),
      titleEl: fold.querySelector(".think-fold-title"),
      bodyEl: fold.querySelector(".think-fold-body"),
      full: "",
      _raf: 0,
    };
  }

  function promptForStep(ctx, sid) {
    const steps = (ctx.liveMeta && ctx.liveMeta.plan_steps) || [];
    const hit = steps.find((s) => String(s.id != null ? s.id : "") === String(sid));
    return hit && hit.prompt ? hit.prompt : "";
  }

  function stepMeta(ctx, sid) {
    const steps = (ctx.liveMeta && ctx.liveMeta.plan_steps) || [];
    return steps.find((s) => String(s.id != null ? s.id : "") === String(sid)) || {};
  }

  function ensureStepFold(ctx, data) {
    const sid = stepIdOf(data);
    if (sid && ctx.stepFolds[sid]) return ctx.stepFolds[sid];
    const meta = stepMeta(ctx, sid);
    const rec = buildStepFold(ctx.planStackEl, {
      ...meta,
      ...data,
      prompt: data.prompt || meta.prompt || promptForStep(ctx, sid),
    });
    ctx.stepFolds[sid] = rec;
    ctx.activeStepId = sid;
    return rec;
  }

  function stepRecFor(ctx, data) {
    const sid = stepIdOf(data) || ctx.activeStepId;
    if (sid && ctx.stepFolds[sid]) return ctx.stepFolds[sid];
    if (ctx.activeStepId && ctx.stepFolds[ctx.activeStepId]) {
      return ctx.stepFolds[ctx.activeStepId];
    }
    return null;
  }

  function flushStepRender(rec, { streaming = false } = {}) {
    if (!rec) return;
    if (rec._raf) {
      cancelAnimationFrame(rec._raf);
      rec._raf = 0;
    }
    setMsgBody(rec.answerEl, rec.full, { streaming });
    if (rec.bodyEl && streaming) rec.bodyEl.scrollTop = rec.bodyEl.scrollHeight;
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function scheduleStepRender(rec) {
    if (!rec || rec._raf) return;
    rec._raf = requestAnimationFrame(() => {
      rec._raf = 0;
      setMsgBody(rec.answerEl, rec.full, { streaming: true });
      if (rec.bodyEl) rec.bodyEl.scrollTop = rec.bodyEl.scrollHeight;
      els.messages.scrollTop = els.messages.scrollHeight;
    });
  }

  function appendChainLine(host, text) {
    if (!host || !text) return;
    const line = document.createElement("div");
    line.className = "tool-line";
    line.textContent = text;
    host.appendChild(line);
  }

  function chainHost(ctx, data) {
    const rec = stepRecFor(ctx, data);
    if (rec) return rec.chainEl;
    return ctx.toolsEl;
  }

  function updateStepChips(rec, data) {
    if (!rec || !rec.chipsEl) return;
    const chips = [];
    if (data.tier) chips.push(`<span class="chip">${escapeHtml(String(data.tier))}</span>`);
    if (data.model) chips.push(`<span class="chip">${escapeHtml(String(data.model))}</span>`);
    if (chips.length) rec.chipsEl.innerHTML = chips.join("");
  }

  function finishStepFold(ctx, data) {
    const rec = stepRecFor(ctx, data);
    if (!rec) return;
    if (data.text && data.text.length >= (rec.full || "").length) {
      rec.full = data.text;
    }
    rec.el.classList.remove("is-streaming");
    setFoldOpen(rec.el, false);
    if (data.failed) rec.el.classList.add("is-failed");
    if (data.note) appendChainLine(rec.chainEl, data.note);
    updateStepChips(rec, data);
    flushStepRender(rec, { streaming: false });
  }

  function renderPlanCard(ctx, data) {
    const fold = renderPlanFold(ctx.planStackEl, { ...data, awaiting: true }, { open: true, live: true });
    fold.querySelectorAll("[data-plan-action]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        submitPlanAction(ctx, btn.getAttribute("data-plan-action"));
      });
    });
    ctx.planCard = fold;
  }

  async function submitPlanAction(ctx, action) {
    if (!ctx.chatId || ctx.planDecided) return;
    ctx.planDecided = true;
    const buttons = ctx.planCard ? ctx.planCard.querySelectorAll("[data-plan-action]") : [];
    buttons.forEach((b) => {
      b.disabled = true;
    });
    try {
      await api(`/api/chats/${ctx.chatId}/plan`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
    } catch (e) {
      ctx.planDecided = false;
      buttons.forEach((b) => {
        b.disabled = false;
      });
      els.statusLine.textContent = "План: " + (e.message || "ошибка");
    }
  }

  function applySseEvent(event, data, ctx) {
    const { toolsEl, metaSlot } = ctx;
    if (event === "meta") {
      const stepScope = data.scope === "step";
      if ((data.phase === "retry" || data.phase === "restore") && !stepScope) {
        ctx.full = "";
        flushBodyRender(ctx, { streaming: true });
      }
      if (data.phase === "retry" && data.problems && data.problems.length && !stepScope) {
        appendChainLine(toolsEl, `повтор (${data.problems.join(", ")}) → ${data.model || ""}`);
      }
      if (data.phase === "retry" && stepScope) {
        const rec = stepRecFor(ctx, data);
        if (rec) {
          rec.full = "";
          rec.el.classList.add("is-streaming");
          setFoldOpen(rec.el, true);
          appendChainLine(
            rec.chainEl,
            `повтор (${(data.problems || []).join(", ")}) → ${data.model || ""}`
          );
          flushStepRender(rec, { streaming: true });
        }
      }
      if (data.phase === "restore" && !stepScope) {
        ctx.liveMeta = { ...ctx.liveMeta, ...data, problems: [] };
        appendChainLine(toolsEl, `восстановлен лучший ответ (${data.model || ""})`);
      } else if (data.phase === "restore" && stepScope) {
        const rec = stepRecFor(ctx, data);
        if (rec) {
          rec.full = "";
          rec.el.classList.add("is-streaming");
          setFoldOpen(rec.el, true);
          appendChainLine(rec.chainEl, `восстановлен лучший ответ (${data.model || ""})`);
          flushStepRender(rec, { streaming: true });
        }
      } else if (data.phase === "plan_ready") {
        const steps = Array.isArray(data.steps) ? data.steps : [];
        ctx.liveMeta = {
          ...ctx.liveMeta,
          planned: true,
          plan_goal: data.goal || "",
          plan_steps: steps,
        };
        renderPlanCard(ctx, data);
      } else if (data.phase === "plan_accepted") {
        if (ctx.planCard) {
          ctx.planCard.classList.add("is-running");
          const actions = ctx.planCard.querySelector(".plan-card-actions");
          if (actions) actions.remove();
          const titleEl = ctx.planCard.querySelector(".think-fold-title");
          if (titleEl) {
            titleEl.textContent = planHeadLabel(
              { awaiting: false },
              (ctx.liveMeta && ctx.liveMeta.plan_steps) || []
            );
          }
          setFoldOpen(ctx.planCard, false);
        }
      } else if (data.phase === "plan_rejected") {
        ctx.liveMeta = { ...ctx.liveMeta, planned: false };
        if (ctx.planCard) {
          ctx.planCard.classList.add("is-rejected");
          const actions = ctx.planCard.querySelector(".plan-card-actions");
          if (actions) actions.remove();
          const note = document.createElement("div");
          note.className = "plan-card-note";
          note.textContent = "План отклонён — один ответ";
          const body = ctx.planCard.querySelector(".think-fold-body");
          if (body) body.appendChild(note);
          else ctx.planCard.appendChild(note);
          setFoldOpen(ctx.planCard, false);
        }
      } else if (data.phase === "plan_step") {
        if (ctx.planCard) {
          ctx.planCard.querySelectorAll("li.is-current").forEach((el) => {
            el.classList.remove("is-current");
          });
          const sid = stepIdOf(data);
          const li = ctx.planCard.querySelector(`li[data-step-id="${sid}"]`);
          if (li) li.classList.add("is-current");
        }
        const rec = ensureStepFold(ctx, data);
        rec.el.classList.add("is-streaming");
        setFoldOpen(rec.el, true);
        updateStepChips(rec, data);
      } else if (data.phase === "plan_step_done") {
        finishStepFold(ctx, data);
      } else if (data.phase === "compose") {
        ctx.liveMeta = { ...ctx.liveMeta, ...data, planned: true };
        Object.values(ctx.stepFolds || {}).forEach((rec) => {
          if (rec.el.classList.contains("is-streaming")) {
            rec.el.classList.remove("is-streaming");
            setFoldOpen(rec.el, false);
            flushStepRender(rec, { streaming: false });
          }
        });
        if (ctx.planCard) setFoldOpen(ctx.planCard, false);
      } else if (data.phase === "plan") {
        ctx.liveMeta = { ...ctx.liveMeta, ...data };
      } else if (stepScope) {
        const rec = stepRecFor(ctx, data);
        if (rec && (data.model || data.tier)) updateStepChips(rec, data);
      } else if (!stepScope) {
        ctx.liveMeta = { ...ctx.liveMeta, ...data };
      }
      metaSlot.innerHTML = metaChips(ctx.liveMeta);
      const bits = [];
      if (ctx.liveMeta.tier) bits.push(ctx.liveMeta.tier);
      if (ctx.liveMeta.model) bits.push(ctx.liveMeta.model);
      if (ctx.liveMeta.need_web) bits.push("web");
      if (ctx.liveMeta.need_local_time) bits.push("время ПК");
      if (ctx.liveMeta.need_calc) bits.push("счёт ПК");
      if (ctx.liveMeta.planned && Array.isArray(ctx.liveMeta.plan_steps) && ctx.liveMeta.plan_steps.length) {
        bits.push("план: " + ctx.liveMeta.plan_steps.length + " шагов");
      }
      if (ctx.liveMeta.num_ctx) bits.push("ctx " + ctx.liveMeta.num_ctx);
      if (ctx.liveMeta.used_history === false) bits.push("без истории");
      if (data.phase === "retry") bits.push("повтор");
      else if (data.phase === "restore") bits.push("restore");
      else if (data.phase === "plan_ready") bits.push("утвердите план");
      else if (data.phase === "plan_accepted") bits.push("план");
      else if (data.phase === "plan_rejected") bits.push("без плана");
      else if (data.phase === "plan_step") bits.push("шаг " + (data.index || ""));
      else if (data.phase === "plan_step_done") bits.push("шаг готов");
      else if (data.phase === "compose") bits.push("сборка");
      else if (ctx.liveMeta.escalated) bits.push("escalate");
      els.statusLine.textContent = bits.join(" · ") || "Генерация…";
    } else if (event === "check") {
      if (data.scope === "step") {
        if (!data.ok) {
          appendChainLine(
            chainHost(ctx, data),
            `self-check: ${(data.problems || []).join(", ")}${data.note ? " — " + data.note : ""}`
          );
        }
        return;
      }
      ctx.liveMeta = {
        ...ctx.liveMeta,
        checked: data.checked != null ? !!data.checked : false,
        problems: data.ok ? [] : data.problems || [],
      };
      metaSlot.innerHTML = metaChips(ctx.liveMeta);
      if (!data.ok) {
        appendChainLine(
          toolsEl,
          `self-check: ${(data.problems || []).join(", ")}${data.note ? " — " + data.note : ""}`
        );
      }
    } else if (event === "token") {
      if (data.scope === "step") {
        const rec = stepRecFor(ctx, data) || ensureStepFold(ctx, data);
        rec.el.classList.add("is-streaming");
        rec.full += data.text || "";
        scheduleStepRender(rec);
      } else {
        ctx.full += data.text || "";
        scheduleBodyRender(ctx);
      }
    } else if (event === "tool") {
      const args =
        typeof data.arguments === "string"
          ? data.arguments
          : JSON.stringify(data.arguments || {});
      appendChainLine(chainHost(ctx, data), `tool ${data.name}(${args})`);
    } else if (event === "done") {
      ctx.doneReceived = true;
      ctx.liveMeta = {
        tier: data.tier,
        model: data.model,
        need_web: data.need_web,
        need_local_time: data.need_local_time,
        need_calc: data.need_calc,
        route_reason: data.route_reason,
        escalated: data.escalated,
        attempts: data.attempts,
        checked: data.checked,
        problems: data.problems || [],
        num_ctx: data.num_ctx,
        used_history: data.used_history,
        context_reason: data.context_reason,
        planned: !!data.planned,
        plan_goal: data.plan_goal || ctx.liveMeta.plan_goal || "",
        plan_steps: data.plan_steps || [],
      };
      if (data.text) {
        ctx.full = data.text;
      }
      Object.values(ctx.stepFolds || {}).forEach((rec) => {
        rec.el.classList.remove("is-streaming");
        setFoldOpen(rec.el, false);
        flushStepRender(rec, { streaming: false });
      });
      if (ctx.planCard) setFoldOpen(ctx.planCard, false);
      flushBodyRender(ctx, { streaming: false });
      metaSlot.innerHTML = metaChips(ctx.liveMeta);
      els.statusLine.textContent = [
        data.tier,
        data.model,
        data.need_web ? "web" : null,
        data.need_local_time ? "время ПК" : null,
        data.need_calc ? "счёт ПК" : null,
        data.planned && data.plan_steps && data.plan_steps.length
          ? `план: ${data.plan_steps.length} шагов`
          : null,
        data.num_ctx ? `ctx ${data.num_ctx}` : null,
        data.used_history === false ? "без истории" : null,
        data.escalated ? "escalated" : null,
        data.attempts > 1 ? `попыток: ${data.attempts}` : null,
        data.checked ? "проверено" : null,
      ]
        .filter(Boolean)
        .join(" · ");
    } else if (event === "error") {
      throw new Error(data.message || "Ошибка оркестра");
    }
  }

  async function send() {
    const text = els.input.value.trim();
    if (!text || state.busy) return;

    setBusy(true);
    els.statusLine.textContent = "Роутинг…";

    let chatId;
    let asst = null;
    const ctx = {
      liveMeta: {},
      full: "",
      bodyEl: null,
      toolsEl: null,
      metaSlot: null,
      planStackEl: null,
      planCard: null,
      stepFolds: {},
      activeStepId: null,
      doneReceived: false,
    };

    try {
      chatId = await ensureChat();
      els.input.value = "";
      autosize();
      appendMessage("user", text);

      asst = appendMessage("assistant", "", null, { streaming: true });
      ctx.bodyEl = asst.querySelector(":scope > .msg-body");
      ctx.toolsEl = asst.querySelector(".msg-tools");
      ctx.metaSlot = asst.querySelector(".msg-meta-slot");
      ctx.planStackEl = asst.querySelector(".msg-plan-stack");
      ctx.chatId = chatId;

      const res = await fetch(`/api/chats/${chatId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text, ...forceSelection() }),
      });
      if (!res.ok) {
        const err = await res.text();
        throw new Error(err || res.statusText);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = parseSseChunk(buf);
        buf = rest;

        for (const { event, data } of events) {
          applySseEvent(event, data, ctx);
        }
      }
      buf += decoder.decode();
      if (buf.trim()) {
        const { events } = parseSseChunk(buf + "\n\n");
        for (const { event, data } of events) {
          applySseEvent(event, data, ctx);
        }
      }

      if (!ctx.doneReceived) {
        throw new Error("Соединение оборвалось до завершения ответа");
      }

      asst.classList.remove("streaming");
      flushBodyRender(ctx, { streaming: false });
      await loadChats();
      const active = state.chats.find((c) => c.id === chatId);
      if (active) els.chatTitle.textContent = active.title;
    } catch (e) {
      if (asst) asst.classList.remove("streaming");
      if (ctx._raf) {
        cancelAnimationFrame(ctx._raf);
        ctx._raf = 0;
      }
      if (ctx.bodyEl && ctx.metaSlot) {
        const errLine = "Ошибка: " + e.message;
        ctx.full = ctx.full ? ctx.full + "\n\n" + errLine : errLine;
        flushBodyRender(ctx, { streaming: false });
        els.statusLine.textContent = "Ошибка";
        ctx.metaSlot.innerHTML = `<div class="msg-meta"><span class="chip warn">error</span></div>`;
      } else {
        els.statusLine.textContent = "Ошибка: " + (e.message || e);
      }
    } finally {
      setBusy(false);
      els.input.focus();
    }
  }

  els.input.addEventListener("input", () => {
    autosize();
    els.btnSend.disabled = state.busy || !els.input.value.trim();
  });

  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  els.btnSend.addEventListener("click", send);
  els.btnNew.addEventListener("click", newChat);
  els.btnModels.addEventListener("click", openModelsModal);
  els.btnModelsClose.addEventListener("click", closeModelsModal);
  els.btnModelsHelp.addEventListener("click", toggleModelsHelp);
  els.btnModelsSave.addEventListener("click", saveModels);
  els.btnModelsAdd.addEventListener("click", addModelSlot);
  els.btnModelsReset.addEventListener("click", resetModels);
  if (els.btnOrKeySave) els.btnOrKeySave.addEventListener("click", saveOpenRouterKey);
  if (els.btnOrKeyClear) els.btnOrKeyClear.addEventListener("click", clearOpenRouterKey);
  document.querySelectorAll('input[name="add-provider"]').forEach((el) => {
    el.addEventListener("change", syncAddProviderUi);
  });
  if (els.addSlotTier) {
    els.addSlotTier.addEventListener("change", () => {
      const hasTier = !!(els.addSlotTier.value || "").trim();
      if (els.addSlotRank) {
        els.addSlotRank.disabled = !hasTier;
        if (hasTier && !els.addSlotRank.value) {
          els.addSlotRank.value = String(defaultRankFor(els.addSlotTier.value));
        }
      }
      applyAddFormCtxDefaults();
    });
  }
  els.messages.addEventListener("click", (e) => {
    const btn = e.target.closest(".code-copy");
    if (!btn || !els.messages.contains(btn)) return;
    e.preventDefault();
    copyCodeBlock(btn);
  });
  els.modelsModal.addEventListener("click", (e) => {
    if (e.target === els.modelsModal) closeModelsModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.modelsModal.hidden) closeModelsModal();
  });

  /* —— Monitor panel —— */

  function pushHistory(key, value) {
    const arr = state.metricsHistory[key];
    if (!arr) return;
    const n = value == null || Number.isNaN(value) ? null : Number(value);
    arr.push(n);
    while (arr.length > HISTORY_LEN) arr.shift();
  }

  function drawSparkline(canvas, values, color) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 200;
    const cssH = canvas.clientHeight || 36;
    if (canvas.width !== Math.floor(cssW * dpr) || canvas.height !== Math.floor(cssH * dpr)) {
      canvas.width = Math.floor(cssW * dpr);
      canvas.height = Math.floor(cssH * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const pts = values.filter((v) => v != null);
    if (pts.length < 2) {
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.35;
      ctx.beginPath();
      ctx.moveTo(0, cssH * 0.7);
      ctx.lineTo(cssW, cssH * 0.7);
      ctx.stroke();
      ctx.globalAlpha = 1;
      return;
    }

    const min = 0;
    const max = Math.max(100, ...pts);
    const pad = 2;
    const n = values.length;
    const coords = [];
    for (let i = 0; i < n; i++) {
      const v = values[i];
      if (v == null) {
        coords.push(null);
        continue;
      }
      const x = (i / Math.max(1, n - 1)) * cssW;
      const y = cssH - pad - ((v - min) / (max - min)) * (cssH - pad * 2);
      coords.push({ x, y });
    }

    ctx.fillStyle = color;
    ctx.globalAlpha = 0.12;
    ctx.beginPath();
    let fillStarted = false;
    let first = null;
    let last = null;
    for (const c of coords) {
      if (!c) {
        if (fillStarted && first && last) {
          ctx.lineTo(last.x, cssH);
          ctx.lineTo(first.x, cssH);
          ctx.closePath();
          ctx.fill();
          ctx.beginPath();
        }
        fillStarted = false;
        first = null;
        last = null;
        continue;
      }
      if (!fillStarted) {
        ctx.moveTo(c.x, c.y);
        first = c;
        fillStarted = true;
      } else {
        ctx.lineTo(c.x, c.y);
      }
      last = c;
    }
    if (fillStarted && first && last) {
      ctx.lineTo(last.x, cssH);
      ctx.lineTo(first.x, cssH);
      ctx.closePath();
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let strokeStarted = false;
    for (const c of coords) {
      if (!c) {
        strokeStarted = false;
        continue;
      }
      if (!strokeStarted) {
        ctx.moveTo(c.x, c.y);
        strokeStarted = true;
      } else {
        ctx.lineTo(c.x, c.y);
      }
    }
    ctx.stroke();
  }

  function pctBar(cls, pct) {
    const p = Math.max(0, Math.min(100, pct == null ? 0 : Number(pct)));
    return `<div class="monitor-bar ${cls}"><span style="width:${p}%"></span></div>`;
  }

  function fmtPct(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return `${Number(v).toFixed(0)}%`;
  }

  function fmtTemp(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return `${Math.round(Number(v))}°C`;
  }

  function tempBar(cls, temp, scaleMax) {
    const max = scaleMax && scaleMax > 0 ? Number(scaleMax) : 100;
    const t = temp == null || Number.isNaN(Number(temp)) ? null : Number(temp);
    const p = t == null ? 0 : Math.max(0, Math.min(100, (t / max) * 100));
    let level = "";
    if (t != null) {
      if (t >= max * 0.95 || t >= 90) level = " hot";
      else if (t >= max * 0.85 || t >= 75) level = " warm";
      else level = " ok";
    }
    return `<div class="monitor-bar temp${level} ${cls}"><span style="width:${p}%"></span></div>`;
  }

  function sparkHeight(id) {
    const h = state.chartHeights[id];
    return h != null ? h : SPARK_H_DEFAULT;
  }

  function sparkHtml(id, title) {
    const h = sparkHeight(id);
    const t = title ? ` title="${escapeHtml(title)}"` : "";
    return `<div class="monitor-spark-wrap" data-spark="${escapeHtml(id)}" style="--spark-h:${h}px">
      <canvas class="monitor-spark" id="${escapeHtml(id)}"${t}></canvas>
      <div class="spark-resizer" data-spark="${escapeHtml(id)}" title="Тяните, чтобы изменить высоту"></div>
    </div>`;
  }

  function chartColor(varName, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || fallback;
  }

  function paintSpark(id, values, color) {
    drawSparkline(document.getElementById(id), values, color);
  }

  function saveChartHeights() {
    try {
      localStorage.setItem(LS_CHART_HEIGHTS, JSON.stringify(state.chartHeights));
    } catch (_) {
      /* ignore */
    }
  }

  function loadChartHeights() {
    try {
      const raw = localStorage.getItem(LS_CHART_HEIGHTS);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return;
      for (const [k, v] of Object.entries(parsed)) {
        const n = Number(v);
        if (n >= SPARK_H_MIN && n <= SPARK_H_MAX) state.chartHeights[k] = Math.round(n);
      }
    } catch (_) {
      /* ignore */
    }
  }

  function fmtModelSize(mb) {
    if (mb == null || Number.isNaN(Number(mb))) return "—";
    const n = Number(mb);
    const gb = n / 1024;
    if (gb < 0.1) return `${Math.round(n)} МБ`;
    return `${gb.toFixed(1)} ГБ`;
  }

  function renderOllama(data, gpus) {
    const box = els.metricsOllama;
    if (!data || !data.ok) {
      box.innerHTML = `<div class="monitor-empty">Ollama: ${escapeHtml(
        (data && data.error) || "недоступна"
      )}</div>`;
      return;
    }
    const models = data.models || [];
    if (!models.length) {
      box.innerHTML = `<div class="monitor-empty">Нет загруженных моделей</div>`;
      return;
    }
    // size_vram/size — доля модели на GPU, не заполненность VRAM карты
    const gpuTotalMb =
      Array.isArray(gpus) && gpus[0] ? Number(gpus[0].mem_total_mb) || 0 : 0;
    box.innerHTML = models
      .map((m) => {
        const gpuPct = Math.round((m.gpu_ratio || 0) * 100);
        const cpuPct = Math.round((m.cpu_ratio || 0) * 100);
        const sizeMb = Number(m.size_mb) || 0;
        const vramMb = Number(m.size_vram_mb) || 0;
        const cpuMb = Math.max(0, sizeMb - vramMb);
        const cardPct =
          gpuTotalMb > 0 ? Math.min(100, Math.round((100 * vramMb) / gpuTotalMb)) : null;

        let placeShort;
        let layersLabel;
        if (m.place === "GPU" || gpuPct >= 99) {
          placeShort = "GPU";
          layersLabel = "слои: все на GPU";
        } else if (m.place === "CPU" || gpuPct <= 1) {
          placeShort = "CPU";
          layersLabel = "слои: все на CPU";
        } else {
          placeShort = "hybrid";
          layersLabel = `слои: ${gpuPct}% GPU · ${cpuPct}% CPU`;
        }

        const footprint =
          cpuMb > 1
            ? `${fmtModelSize(vramMb)} GPU + ${fmtModelSize(cpuMb)} RAM`
            : `${fmtModelSize(sizeMb)} модель`;
        const cardShare =
          cardPct != null ? ` · ${cardPct}% VRAM карты` : "";
        const detail = `${layersLabel} · ${footprint}${cardShare}`;
        const splitTitle = `Размещение слоёв модели (не заполненность памяти): ${layersLabel}`;

        return `<div class="monitor-card">
          <div class="monitor-metric-head">
            <span class="monitor-metric-name" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</span>
            <span class="monitor-metric-value">${escapeHtml(placeShort)}</span>
          </div>
          <div class="monitor-split" title="${escapeHtml(splitTitle)}">
            <span class="gpu-part" style="width:${gpuPct}%"></span>
            <span class="cpu-part" style="width:${cpuPct}%"></span>
          </div>
          <div class="monitor-place">${escapeHtml(detail)}</div>
        </div>`;
      })
      .join("");
  }

  function renderGpu(gpus) {
    const box = els.metricsGpu;
    if (!gpus || !gpus.length) {
      pushHistory("gpuUtil", null);
      pushHistory("gpuMem", null);
      box.innerHTML = `<div class="monitor-empty">Нет данных GPU (нужен nvidia-smi)</div>
        ${sparkHtml("spark-gpu", "Загрузка GPU %")}`;
      paintSpark("spark-gpu", state.metricsHistory.gpuUtil, chartColor("--chart-gpu", "#4a9eff"));
      return;
    }
    const g = gpus[0];
    pushHistory("gpuUtil", g.util_percent);
    pushHistory("gpuMem", g.mem_percent);
    const usedGb = (g.mem_used_mb / 1024).toFixed(1);
    const totalGb = (g.mem_total_mb / 1024).toFixed(1);
    box.innerHTML = `
      <div class="monitor-card">
        <div class="monitor-metric-head">
          <span class="monitor-metric-name" title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</span>
          <span class="monitor-metric-value">${fmtPct(g.util_percent)}</span>
        </div>
        ${pctBar("gpu", g.util_percent)}
        <div class="monitor-metric-head">
          <span>VRAM</span>
          <span class="monitor-metric-value">${usedGb} / ${totalGb} ГБ · ${fmtPct(g.mem_percent)}</span>
        </div>
        ${pctBar("vram", g.mem_percent)}
        ${sparkHtml("spark-gpu", "Загрузка GPU %")}
        ${sparkHtml("spark-vram", "VRAM %")}
      </div>`;
    paintSpark("spark-gpu", state.metricsHistory.gpuUtil, chartColor("--chart-gpu", "#4a9eff"));
    paintSpark("spark-vram", state.metricsHistory.gpuMem, chartColor("--chart-vram", "#7ec699"));
  }

  function renderTemps(gpus) {
    const box = els.metricsTemp;
    if (!gpus || !gpus.length) {
      pushHistory("gpuTemp", null);
      pushHistory("memTemp", null);
      box.innerHTML = `<div class="monitor-empty">Нет данных температуры (нужен nvidia-smi)</div>
        ${sparkHtml("spark-temp-gpu", "GPU °C")}`;
      paintSpark(
        "spark-temp-gpu",
        state.metricsHistory.gpuTemp,
        chartColor("--chart-temp-gpu", "#f0a050")
      );
      return;
    }
    const g = gpus[0];
    pushHistory("gpuTemp", g.temp_gpu_c != null ? Number(g.temp_gpu_c) : null);
    pushHistory("memTemp", g.temp_memory_c != null ? Number(g.temp_memory_c) : null);
    const scale = g.temp_max_op_c || g.temp_shutdown_c || 100;
    const memScale = g.temp_memory_max_c || scale;
    const limits = [
      g.temp_target_c != null ? `цель ${fmtTemp(g.temp_target_c)}` : null,
      g.temp_max_op_c != null ? `макс ${fmtTemp(g.temp_max_op_c)}` : null,
      g.temp_slowdown_c != null ? `троттлинг ${fmtTemp(g.temp_slowdown_c)}` : null,
      g.temp_shutdown_c != null ? `выкл. ${fmtTemp(g.temp_shutdown_c)}` : null,
    ]
      .filter(Boolean)
      .join(" · ");

    const memRow =
      g.temp_memory_c != null
        ? `${tempBar("temp-mem", g.temp_memory_c, memScale)}
        ${sparkHtml("spark-temp-mem", "Memory °C")}`
        : `<div class="monitor-place">не отдаёт драйвер (часто на GDDR; HBM обычно есть)</div>`;

    const tlimit =
      g.temp_tlimit_c != null
        ? `<div class="monitor-metric-head">
            <span class="monitor-metric-name">T.Limit</span>
            <span class="monitor-metric-value">${fmtTemp(g.temp_tlimit_c)}</span>
          </div>`
        : "";

    box.innerHTML = `
      <div class="monitor-card">
        <div class="monitor-metric-head">
          <span class="monitor-metric-name" title="${escapeHtml(g.name || "GPU")}">GPU (ядро)</span>
          <span class="monitor-metric-value">${fmtTemp(g.temp_gpu_c)}</span>
        </div>
        ${tempBar("temp-gpu", g.temp_gpu_c, scale)}
        ${sparkHtml("spark-temp-gpu", "GPU °C")}
        <div class="monitor-metric-head">
          <span class="monitor-metric-name" title="Температура цепей видеопамяти">Память (цепи VRAM)</span>
          <span class="monitor-metric-value">${fmtTemp(g.temp_memory_c)}</span>
        </div>
        ${memRow}
        ${tlimit}
        ${limits ? `<div class="monitor-place">${escapeHtml(limits)}</div>` : ""}
      </div>`;
    paintSpark(
      "spark-temp-gpu",
      state.metricsHistory.gpuTemp,
      chartColor("--chart-temp-gpu", "#f0a050")
    );
    if (g.temp_memory_c != null) {
      paintSpark(
        "spark-temp-mem",
        state.metricsHistory.memTemp,
        chartColor("--chart-temp-mem", "#e8c96a")
      );
    }
  }

  function renderRam(ram) {
    const box = els.metricsRam;
    const pct = ram && ram.percent != null ? ram.percent : null;
    pushHistory("ram", pct);
    if (pct == null) {
      box.innerHTML = `<div class="monitor-empty">Нет данных RAM</div>`;
      return;
    }
    box.innerHTML = `
      <div class="monitor-card">
        <div class="monitor-metric-head">
          <span class="monitor-metric-name">Оперативная память</span>
          <span class="monitor-metric-value">${ram.used_gb} / ${ram.total_gb} ГБ · ${fmtPct(pct)}</span>
        </div>
        ${pctBar("ram", pct)}
        ${sparkHtml("spark-ram", "RAM %")}
      </div>`;
    paintSpark("spark-ram", state.metricsHistory.ram, chartColor("--chart-ram", "#d4a574"));
  }

  function renderCpu(cpu) {
    const box = els.metricsCpu;
    const pct = cpu && cpu.percent != null ? cpu.percent : null;
    pushHistory("cpu", pct);
    if (pct == null) {
      box.innerHTML = `<div class="monitor-empty">Нет данных CPU</div>`;
      return;
    }
    const cores = cpu.count != null ? ` · ${cpu.count} ядр.` : "";
    box.innerHTML = `
      <div class="monitor-card">
        <div class="monitor-metric-head">
          <span class="monitor-metric-name">CPU${escapeHtml(cores)}</span>
          <span class="monitor-metric-value">${fmtPct(pct)}</span>
        </div>
        ${pctBar("cpu", pct)}
        ${sparkHtml("spark-cpu", "CPU %")}
      </div>`;
    paintSpark("spark-cpu", state.metricsHistory.cpu, chartColor("--chart-cpu", "#e06c75"));
  }

  async function refreshMetrics() {
    if (!state.panelOpen || state.metricsInflight) return;
    state.metricsInflight = true;
    try {
      const data = await api("/api/metrics");
      if (!state.panelOpen) return;
      renderOllama(data.ollama, data.gpu);
      renderGpu(data.gpu);
      renderTemps(data.gpu);
      renderRam(data.ram);
      renderCpu(data.cpu);
      if (data.note) {
        els.metricsNote.hidden = false;
        els.metricsNote.textContent = data.note;
      } else {
        els.metricsNote.hidden = true;
        els.metricsNote.textContent = "";
      }
    } catch (e) {
      if (state.panelOpen) {
        els.metricsOllama.innerHTML = `<div class="monitor-empty">Ошибка: ${escapeHtml(e.message)}</div>`;
      }
    } finally {
      state.metricsInflight = false;
    }
  }

  function stopMetricsPoll() {
    if (state.metricsTimer) {
      clearTimeout(state.metricsTimer);
      state.metricsTimer = null;
    }
  }

  function startMetricsPoll() {
    stopMetricsPoll();
    if (!state.panelOpen) return;
    const tick = async () => {
      await refreshMetrics();
      if (!state.panelOpen) return;
      const ms = Number(els.metricsInterval.value) || 2000;
      state.metricsTimer = setTimeout(tick, ms);
    };
    tick();
  }

  function panelMaxWidth() {
    // sidebar (~260) + минимальный чат (~320)
    return Math.max(PANEL_MIN, Math.min(PANEL_MAX_CAP, window.innerWidth - 580));
  }

  function applyPanelWidth(px) {
    const w = Math.max(PANEL_MIN, Math.min(panelMaxWidth(), Math.round(px)));
    state.panelWidth = w;
    document.documentElement.style.setProperty("--panel-width", `${w}px`);
    return w;
  }

  function syncToggleButton() {
    const btn = els.btnPanelToggle;
    if (!btn) return;
    if (state.panelOpen) {
      btn.textContent = "›";
      btn.title = "Свернуть панель";
      btn.setAttribute("aria-label", "Свернуть панель");
      btn.setAttribute("aria-expanded", "true");
    } else {
      btn.textContent = "‹";
      btn.title = "Показать монитор";
      btn.setAttribute("aria-label", "Показать монитор");
      btn.setAttribute("aria-expanded", "false");
    }
  }

  function setPanelOpen(open) {
    state.panelOpen = !!open;
    els.app.classList.toggle("panel-collapsed", !state.panelOpen);
    syncToggleButton();
    try {
      localStorage.setItem(LS_PANEL_OPEN, state.panelOpen ? "1" : "0");
    } catch (_) {
      /* ignore */
    }
    if (state.panelOpen) {
      applyPanelWidth(state.panelWidth);
      startMetricsPoll();
    } else {
      stopMetricsPoll();
    }
  }

  function initPanelResize() {
    let dragging = false;
    let startX = 0;
    let startW = 0;

    const onMove = (e) => {
      if (!dragging) return;
      const dx = startX - e.clientX;
      applyPanelWidth(startW + dx);
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      els.app.classList.remove("resizing");
      try {
        localStorage.setItem(LS_PANEL_WIDTH, String(state.panelWidth));
      } catch (_) {
        /* ignore */
      }
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };

    els.panelResizer.addEventListener("pointerdown", (e) => {
      if (!state.panelOpen) return;
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      startW = state.panelWidth;
      els.app.classList.add("resizing");
      els.panelResizer.setPointerCapture?.(e.pointerId);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });

    window.addEventListener("resize", () => {
      if (state.panelOpen) applyPanelWidth(state.panelWidth);
    });
  }

  function initSparkResize() {
    let dragging = false;
    let sparkId = null;
    let startY = 0;
    let startH = 0;

    const historyKey = {
      "spark-gpu": "gpuUtil",
      "spark-vram": "gpuMem",
      "spark-temp-gpu": "gpuTemp",
      "spark-temp-mem": "memTemp",
      "spark-ram": "ram",
      "spark-cpu": "cpu",
    };
    const colorKey = {
      "spark-gpu": ["--chart-gpu", "#4a9eff"],
      "spark-vram": ["--chart-vram", "#7ec699"],
      "spark-temp-gpu": ["--chart-temp-gpu", "#f0a050"],
      "spark-temp-mem": ["--chart-temp-mem", "#e8c96a"],
      "spark-ram": ["--chart-ram", "#d4a574"],
      "spark-cpu": ["--chart-cpu", "#e06c75"],
    };

    const repaint = (id) => {
      const hk = historyKey[id];
      const ck = colorKey[id];
      if (!hk || !ck) return;
      paintSpark(id, state.metricsHistory[hk], chartColor(ck[0], ck[1]));
    };

    const onMove = (e) => {
      if (!dragging || !sparkId) return;
      const dy = e.clientY - startY;
      const h = Math.max(SPARK_H_MIN, Math.min(SPARK_H_MAX, Math.round(startH + dy)));
      state.chartHeights[sparkId] = h;
      const wrap = els.monitorPanel.querySelector(`.monitor-spark-wrap[data-spark="${sparkId}"]`);
      if (wrap) wrap.style.setProperty("--spark-h", `${h}px`);
      repaint(sparkId);
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      sparkId = null;
      els.app.classList.remove("spark-resizing");
      saveChartHeights();
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };

    els.monitorPanel.addEventListener("pointerdown", (e) => {
      const handle = e.target.closest(".spark-resizer");
      if (!handle || !els.monitorPanel.contains(handle)) return;
      e.preventDefault();
      e.stopPropagation();
      sparkId = handle.getAttribute("data-spark");
      if (!sparkId) return;
      dragging = true;
      startY = e.clientY;
      startH = sparkHeight(sparkId);
      els.app.classList.add("spark-resizing");
      handle.setPointerCapture?.(e.pointerId);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });
  }

  function initMonitorPanel() {
    loadChartHeights();
    let open = true;
    let width = PANEL_DEFAULT;
    let interval = "2000";
    try {
      const o = localStorage.getItem(LS_PANEL_OPEN);
      if (o === "0") open = false;
      const w = Number(localStorage.getItem(LS_PANEL_WIDTH));
      if (w >= PANEL_MIN && w <= PANEL_MAX_CAP) width = w;
      const iv = localStorage.getItem(LS_METRICS_INTERVAL);
      if (iv && [...els.metricsInterval.options].some((opt) => opt.value === iv)) {
        interval = iv;
      }
    } catch (_) {
      /* ignore */
    }
    applyPanelWidth(width);
    els.metricsInterval.value = interval;
    els.btnPanelToggle.addEventListener("click", () => setPanelOpen(!state.panelOpen));
    els.metricsInterval.addEventListener("change", () => {
      try {
        localStorage.setItem(LS_METRICS_INTERVAL, els.metricsInterval.value);
      } catch (_) {
        /* ignore */
      }
      startMetricsPoll();
    });
    initPanelResize();
    initSparkResize();
    setPanelOpen(open);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopMetricsPoll();
      else if (state.panelOpen) startMetricsPoll();
    });
  }

  async function init() {
    initMonitorPanel();
    await refreshHealth();
    setInterval(refreshHealth, 30000);
    try {
      await loadSettings();
    } catch (_) {
      /* health уже заполнил select */
    }
    await loadChats();
    if (state.chats.length) {
      await selectChat(state.chats[0].id);
    } else {
      await newChat();
    }
    setBusy(false);
    els.input.focus();
  }

  init().catch((e) => {
    els.health.className = "health err";
    els.health.textContent = e.message;
  });
})();
