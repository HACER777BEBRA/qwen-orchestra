import { api, els, escapeHtml, HISTORY_LEN, SPARK_H_DEFAULT, state } from "./core.js";

export function pushHistory(key, value) {
  const arr = state.metricsHistory[key];
  if (!arr) return;
  const n = value == null || Number.isNaN(value) ? null : Number(value);
  arr.push(n);
  while (arr.length > HISTORY_LEN) arr.shift();
}

export function drawSparkline(canvas, values, color) {
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

export function pctBar(cls, pct) {
  const p = Math.max(0, Math.min(100, pct == null ? 0 : Number(pct)));
  return `<div class="monitor-bar ${cls}"><span style="width:${p}%"></span></div>`;
}

export function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(0)}%`;
}

export function fmtTemp(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return `${Math.round(Number(v))}°C`;
}

export function tempBar(cls, temp, scaleMax) {
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

export function sparkHeight(id) {
  const h = state.chartHeights[id];
  return h != null ? h : SPARK_H_DEFAULT;
}

export function sparkHtml(id, title) {
  const h = sparkHeight(id);
  const t = title ? ` title="${escapeHtml(title)}"` : "";
  return `<div class="monitor-spark-wrap" data-spark="${escapeHtml(id)}" style="--spark-h:${h}px">
    <canvas class="monitor-spark" id="${escapeHtml(id)}"${t}></canvas>
    <div class="spark-resizer" data-spark="${escapeHtml(id)}" title="Тяните, чтобы изменить высоту"></div>
  </div>`;
}

export function chartColor(varName, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || fallback;
}

export function paintSpark(id, values, color) {
  drawSparkline(document.getElementById(id), values, color);
}

export function saveChartHeights() {
  try {
    localStorage.setItem(LS_CHART_HEIGHTS, JSON.stringify(state.chartHeights));
  } catch (_) {
    /* ignore */
  }
}

export function loadChartHeights() {
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

export function fmtModelSize(mb) {
  if (mb == null || Number.isNaN(Number(mb))) return "—";
  const n = Number(mb);
  const gb = n / 1024;
  if (gb < 0.1) return `${Math.round(n)} МБ`;
  return `${gb.toFixed(1)} ГБ`;
}

export function renderOllama(data, gpus) {
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

export function renderGpu(gpus) {
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

export function renderTemps(gpus) {
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

export function renderRam(ram) {
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

export function renderCpu(cpu) {
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

export async function refreshMetrics() {
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

