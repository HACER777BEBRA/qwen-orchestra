import { api, els, escapeHtml, setBusy, state } from "./core.js";
import { setMsgBody } from "./markdown.js";
import { populateTierSelect, setIdleStatusLine } from "./models-select.js";
import { poolFromPayload } from "./models-list.js";
import { hydratePlanStack } from "./chat-plan.js";

export async function refreshHealth() {
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

export const ICON_CLEAR =
  '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8.09 2.09a1.75 1.75 0 0 1 2.47 0l3.35 3.35a1.75 1.75 0 0 1 0 2.47L8.4 13.42A2.25 2.25 0 0 1 6.81 14H2.75a.75.75 0 0 1-.75-.75V9.19c0-.6.24-1.17.66-1.59l5.43-5.51zm1.41.88a.25.25 0 0 0-.35 0L4.2 8.1l3.7 3.7 5.05-5.05a.25.25 0 0 0 0-.35L8.5 2.97zM3.5 9.4v3.1h2.9L3.5 9.4z"/></svg>';
export const ICON_DELETE =
  '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M6.5 1.75A.75.75 0 0 1 7.25 1h1.5a.75.75 0 0 1 .75.75V3h3.75a.75.75 0 0 1 0 1.5h-.34l-.7 8.05A1.75 1.75 0 0 1 10.47 14H5.53a1.75 1.75 0 0 1-1.74-1.45L3.09 4.5H2.75a.75.75 0 0 1 0-1.5H6.5V1.75zM5.1 4.5l.68 7.85a.25.25 0 0 0 .25.2h4.94a.25.25 0 0 0 .25-.2l.68-7.85H5.1zm1.65 1.75a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5a.75.75 0 0 1 .75-.75zm2.5 0a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5a.75.75 0 0 1 .75-.75z"/></svg>';

export function renderChatList() {
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

export function formatElapsed(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1000) return n + "мс";
  return (n / 1000).toFixed(1).replace(/\.0$/, "") + "с";
}

export function metaChips(meta) {
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
  if (meta.elapsed_ms != null && meta.elapsed_ms !== "")
    chips.push(`<span class="chip">${escapeHtml(formatElapsed(meta.elapsed_ms))}</span>`);
  return chips.length ? `<div class="msg-meta">${chips.join("")}</div>` : "";
}

export function appendMessage(role, content, meta, { streaming = false } = {}) {
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

export function renderMessages(messages) {
  els.messages.innerHTML = "";
  for (const m of messages || []) {
    appendMessage(m.role, m.content, m.meta);
  }
}

export async function loadChats() {
  state.chats = await api("/api/chats");
  renderChatList();
}

export async function selectChat(id) {
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

export async function ensureChat() {
  if (state.activeId) return state.activeId;
  const chat = await api("/api/chats", { method: "POST", body: "{}" });
  state.chats.unshift(chat);
  state.activeId = chat.id;
  els.chatTitle.textContent = chat.title;
  renderChatList();
  return chat.id;
}

export async function newChat() {
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

export async function clearChat(id) {
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

export async function deleteChat(id) {
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

export function parseSseChunk(buffer) {
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

