import { els, state } from "./core.js";

export function populateTierSelect(pool) {
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

export function fillTierSelect(selectEl, selected) {
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
export function shortModelTag(model) {
  const raw = String(model || "").trim();
  if (!raw) return "?";
  const size = raw.match(/(\d+(?:\.\d+)?)\s*([bBmM])\b/);
  if (size) return `${size[1]}${size[2].toLowerCase()}`;
  const base = raw.includes("/") ? raw.split("/").pop() : raw;
  return (base || raw).slice(0, 28);
}

/** Цепочка слотов по rank: id, если размер модели дублируется. */
export function orchestraChainText(slots) {
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

export function idleStatusLine(slots) {
  const src = slots || state.slots;
  return `localhost · оркестр ${orchestraChainText(src)} · adaptive ctx`;
}

export function setIdleStatusLine(slots) {
  if (slots) state.slots = (slots || []).map((s) => ({ ...s }));
  if (!state.busy) els.statusLine.textContent = idleStatusLine(state.slots);
}

export function fillModelSelect(selectEl, selected, opts) {
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

export function selectedAddProvider() {
  const el = document.querySelector('input[name="add-provider"]:checked');
  return el ? el.value : "ollama";
}

export function syncAddProviderUi() {
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

export function syncCardProviderUi(card) {
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

export function renderOpenRouterStatus(providers) {
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

