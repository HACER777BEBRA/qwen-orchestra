import { els, state } from "./core.js";
import {
  fillModelSelect,
  fillTierSelect,
  setIdleStatusLine,
  syncCardProviderUi,
} from "./models-select.js";

export function setModelsStatus(text, kind) {
  els.modelsStatus.textContent = text || "";
  els.modelsStatus.className = "modal-status" + (kind ? " " + kind : "");
}

export function defaultRankFor(tierId) {
  const t = (state.fixedTiers || []).find((x) => x.id === tierId);
  if (t && t.rank != null) return Number(t.rank);
  return 0;
}

export function defaultCtxOhFor(tierId) {
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

export function defaultMaxCtxFor(tierId) {
  const t = (state.fixedTiers || []).find((x) => x.id === tierId);
  if (t && "max_ctx" in t) {
    return t.max_ctx != null ? Number(t.max_ctx) : "";
  }
  if (tierId === "tiny" || tierId === "nano" || tierId === "small") return 4096;
  return "";
}

export function applyAddFormCtxDefaults() {
  const tier = ((els.addSlotTier && els.addSlotTier.value) || "").trim();
  if (els.addSlotCtxOh) {
    els.addSlotCtxOh.value = String(defaultCtxOhFor(tier || null));
  }
  if (els.addSlotMaxCtx) {
    const mx = defaultMaxCtxFor(tier || null);
    els.addSlotMaxCtx.value = mx === "" || mx == null ? "" : String(mx);
  }
}

export function fillCardTierSelect(selectEl, selected) {
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

export function syncCardTierRank(card) {
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

export function renderModelsList(pool) {
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
    del.addEventListener("click", () => {
      import("./models-save.js").then((m) => m.removeSlot(card.dataset.id));
    });
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

export function collectSlotsFromDom() {
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

export function poolFromPayload(data) {
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

