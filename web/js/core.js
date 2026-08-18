export const $ = (sel) => document.querySelector(sel);

export const els = {
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

export const LS_PANEL_OPEN = "qwen.monitor.open";
export const LS_PANEL_WIDTH = "qwen.monitor.width";
export const LS_METRICS_INTERVAL = "qwen.monitor.interval";
export const LS_CHART_HEIGHTS = "qwen.monitor.chartHeights";
export const PANEL_MIN = 220;
export const PANEL_MAX_CAP = 960;
export const PANEL_DEFAULT = 300;
export const HISTORY_LEN = 60;
export const SPARK_H_MIN = 28;
export const SPARK_H_MAX = 280;
export const SPARK_H_DEFAULT = 48;

export const state = {
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
export function forceSelection() {
  const v = els.tierSelect.value;
  if (!v || v === "auto") return { force_model: null, force_tier: null };
  return { force_model: v, force_tier: null };
}

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function setBusy(busy) {
  state.busy = busy;
  els.btnSend.disabled = busy || !els.input.value.trim();
  els.input.disabled = busy;
  els.btnNew.disabled = busy;
  els.tierSelect.disabled = busy;
  if (els.btnModels) els.btnModels.disabled = busy;
  els.chatList.classList.toggle("busy", busy);
}

export function autosize() {
  const el = els.input;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

export async function api(path, opts = {}) {
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
