import { els, escapeHtml } from "./core.js";

const marked = globalThis.marked;
const DOMPurify = globalThis.DOMPurify;
const hljs = globalThis.hljs;
const renderMathInElement = globalThis.renderMathInElement;

if (typeof marked !== "undefined") {
  marked.setOptions({ gfm: true, breaks: true });
}

/** Закрыть незакрытый fence, чтобы стрим не ломал разметку. */
export function closeOpenFences(text) {
  const n = (String(text).match(/```/g) || []).length;
  return n % 2 === 1 ? text + "\n```" : text;
}

export function hasOpenFence(text) {
  return ((String(text).match(/```/g) || []).length % 2) === 1;
}

export function renderMarkdown(text, { streaming = false } = {}) {
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

export function langFromCode(codeEl) {
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

export function codePlainText(preOrCode) {
  const code = preOrCode.querySelector?.("code") || preOrCode;
  const clone = code.cloneNode(true);
  clone.querySelectorAll(".stream-caret").forEach((n) => n.remove());
  return clone.textContent || "";
}

export const ICON_COPY =
  '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M5.75 2.5A1.75 1.75 0 0 0 4 4.25v7a.75.75 0 0 1-1.5 0v-7A3.25 3.25 0 0 1 5.75 1h5a.75.75 0 0 1 0 1.5h-5zm1.5 3A1.75 1.75 0 0 0 5.5 7.25v6c0 .966.784 1.75 1.75 1.75h5A1.75 1.75 0 0 0 14 13.25v-6A1.75 1.75 0 0 0 12.25 5.5h-5zm0 1.5h5a.25.25 0 0 1 .25.25v6a.25.25 0 0 1-.25.25h-5a.25.25 0 0 1-.25-.25v-6a.25.25 0 0 1 .25-.25z"/></svg>';
export const ICON_CHECK =
  '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M12.53 4.22a.75.75 0 0 1 0 1.06l-5.25 5.25a.75.75 0 0 1-1.06 0L4.22 8.53a.75.75 0 0 1 1.06-1.06L6.75 9l4.72-4.72a.75.75 0 0 1 1.06 0z"/></svg>';

export const LANG_ALIASES = {
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

export function normalizeLangClass(code) {
  const lang = langFromCode(code);
  if (!lang) return;
  const mapped = LANG_ALIASES[lang.toLowerCase()] || lang.toLowerCase();
  if (!/^[a-z0-9_+#.-]+$/i.test(mapped)) return;
  const cls = String(code.className || "").replace(/(?:^|\s)(?:language|lang)-[^\s]+/gi, "").trim();
  code.className = cls;
  code.classList.add("language-" + mapped);
}

export function highlightCodeBlocks(el, { streaming = false, text = "" } = {}) {
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

export function enhanceCodeBlocks(el) {
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

export function placeStreamCaret(el, text) {
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

export function setMsgBody(el, text, { streaming = false } = {}) {
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
export function renderMathInMsg(el) {
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

export async function copyCodeBlock(btn) {
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
