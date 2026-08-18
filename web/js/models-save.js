import { els, state, api } from "./core.js";
import {
  fillModelSelect,
  fillTierSelect,
  populateTierSelect,
  renderOpenRouterStatus,
  selectedAddProvider,
  syncAddProviderUi,
} from "./models-select.js";
import {
  applyAddFormCtxDefaults,
  collectSlotsFromDom,
  defaultCtxOhFor,
  defaultMaxCtxFor,
  defaultRankFor,
  fillCardTierSelect,
  poolFromPayload,
  renderModelsList,
  setModelsStatus,
} from "./models-list.js";
import { refreshHealth } from "./chat-list.js";

export async function loadSettings() {
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

export function patternsToText(list) {
  return Array.isArray(list) ? list.join("\n") : "";
}

export function parsePatternLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .filter((s, i, arr) => arr.indexOf(s) === i);
}

export function fillSelfcheckForm(sc, defaults) {
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

export function collectSelfcheckFromDom() {
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

export function fillPlanForm(plan, defaults) {
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

export function collectPlanFromDom() {
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

export function toggleModelsHelp() {
  const open = els.modelsHelp.hidden;
  els.modelsHelp.hidden = !open;
  els.btnModelsHelp.setAttribute("aria-expanded", open ? "true" : "false");
}

export async function openModelsModal() {
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

export function closeModelsModal() {
  els.modelsModal.hidden = true;
}

export async function saveModels() {
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

export async function persistDraftSlots() {
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

export async function addModelSlot() {
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

export async function removeSlot(id) {
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

export async function resetModels() {
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

export async function saveOpenRouterKey() {
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

export async function clearOpenRouterKey() {
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
