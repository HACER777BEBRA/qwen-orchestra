import {
  els,
  LS_CHART_HEIGHTS,
  LS_METRICS_INTERVAL,
  LS_PANEL_OPEN,
  LS_PANEL_WIDTH,
  PANEL_DEFAULT,
  PANEL_MAX_CAP,
  PANEL_MIN,
  SPARK_H_DEFAULT,
  SPARK_H_MAX,
  SPARK_H_MIN,
  state,
} from "./core.js";
import {
  chartColor,
  loadChartHeights,
  paintSpark,
  refreshMetrics,
  saveChartHeights,
} from "./monitor-draw.js";

export function stopMetricsPoll() {
  if (state.metricsTimer) {
    clearTimeout(state.metricsTimer);
    state.metricsTimer = null;
  }
}

export function startMetricsPoll() {
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

export function panelMaxWidth() {
  // sidebar (~260) + минимальный чат (~320)
  return Math.max(PANEL_MIN, Math.min(PANEL_MAX_CAP, window.innerWidth - 580));
}

export function applyPanelWidth(px) {
  const w = Math.max(PANEL_MIN, Math.min(panelMaxWidth(), Math.round(px)));
  state.panelWidth = w;
  document.documentElement.style.setProperty("--panel-width", `${w}px`);
  return w;
}

export function syncToggleButton() {
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

export function setPanelOpen(open) {
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

export function initPanelResize() {
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

export function initSparkResize() {
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

export function initMonitorPanel() {
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
