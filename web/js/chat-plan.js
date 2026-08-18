import { api, els, escapeHtml } from "./core.js";
import { setMsgBody } from "./markdown.js";

export function flushBodyRender(ctx, { streaming = false } = {}) {
  if (ctx._raf) {
    cancelAnimationFrame(ctx._raf);
    ctx._raf = 0;
  }
  setMsgBody(ctx.bodyEl, ctx.full, { streaming });
  els.messages.scrollTop = els.messages.scrollHeight;
}

export function scheduleBodyRender(ctx) {
  if (ctx._raf) return;
  ctx._raf = requestAnimationFrame(() => {
    ctx._raf = 0;
    setMsgBody(ctx.bodyEl, ctx.full, { streaming: true });
    els.messages.scrollTop = els.messages.scrollHeight;
  });
}

export function stepIdOf(data) {
  if (data == null) return "";
  if (data.id != null && data.id !== "") return String(data.id);
  if (data.step_id != null && data.step_id !== "") return String(data.step_id);
  if (data.index != null && data.index !== "") return String(data.index);
  return "";
}

export function setFoldOpen(fold, open) {
  if (!fold) return;
  fold.open = !!open;
  const head = fold.querySelector(".think-fold-head");
  if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
}

export function attachFoldToggle(fold) {
  const head = fold.querySelector(".think-fold-head");
  if (!head) return;
  head.setAttribute("aria-expanded", fold.open ? "true" : "false");
  if (head.dataset.foldBound) return;
  head.dataset.foldBound = "1";
  fold.addEventListener("toggle", () => {
    head.setAttribute("aria-expanded", fold.open ? "true" : "false");
  });
}

export function planHeadLabel(data, steps) {
  const n = (steps || []).length;
  const awaiting = data && data.awaiting;
  const bits = ["План"];
  if (n) bits.push(n === 1 ? "1 шаг" : n < 5 ? n + " шага" : n + " шагов");
  if (awaiting) bits.push("утвердите");
  return bits.join(" · ");
}

export function planStepLi(s, i) {
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

export function renderPlanStepsHtml(steps) {
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

export function renderPlanFold(host, data, { open = true, live = false } = {}) {
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

export function hydratePlanStack(stackEl, meta) {
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

export function calcBlockHost(host, data) {
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

export function buildStepFold(host, data) {
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

export function promptForStep(ctx, sid) {
  const steps = (ctx.liveMeta && ctx.liveMeta.plan_steps) || [];
  const hit = steps.find((s) => String(s.id != null ? s.id : "") === String(sid));
  return hit && hit.prompt ? hit.prompt : "";
}

export function stepMeta(ctx, sid) {
  const steps = (ctx.liveMeta && ctx.liveMeta.plan_steps) || [];
  return steps.find((s) => String(s.id != null ? s.id : "") === String(sid)) || {};
}

export function ensureStepFold(ctx, data) {
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

export function stepRecFor(ctx, data) {
  const sid = stepIdOf(data) || ctx.activeStepId;
  if (sid && ctx.stepFolds[sid]) return ctx.stepFolds[sid];
  if (ctx.activeStepId && ctx.stepFolds[ctx.activeStepId]) {
    return ctx.stepFolds[ctx.activeStepId];
  }
  return null;
}

export function flushStepRender(rec, { streaming = false } = {}) {
  if (!rec) return;
  if (rec._raf) {
    cancelAnimationFrame(rec._raf);
    rec._raf = 0;
  }
  setMsgBody(rec.answerEl, rec.full, { streaming });
  if (rec.bodyEl && streaming) rec.bodyEl.scrollTop = rec.bodyEl.scrollHeight;
  els.messages.scrollTop = els.messages.scrollHeight;
}

export function scheduleStepRender(rec) {
  if (!rec || rec._raf) return;
  rec._raf = requestAnimationFrame(() => {
    rec._raf = 0;
    setMsgBody(rec.answerEl, rec.full, { streaming: true });
    if (rec.bodyEl) rec.bodyEl.scrollTop = rec.bodyEl.scrollHeight;
    els.messages.scrollTop = els.messages.scrollHeight;
  });
}

export function appendChainLine(host, text) {
  if (!host || !text) return;
  const line = document.createElement("div");
  line.className = "tool-line";
  line.textContent = text;
  host.appendChild(line);
}

export function chainHost(ctx, data) {
  const rec = stepRecFor(ctx, data);
  if (rec) return rec.chainEl;
  return ctx.toolsEl;
}

export function updateStepChips(rec, data) {
  if (!rec || !rec.chipsEl) return;
  const chips = [];
  if (data.tier) chips.push(`<span class="chip">${escapeHtml(String(data.tier))}</span>`);
  if (data.model) chips.push(`<span class="chip">${escapeHtml(String(data.model))}</span>`);
  if (chips.length) rec.chipsEl.innerHTML = chips.join("");
}

export function finishStepFold(ctx, data) {
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

export function renderPlanCard(ctx, data) {
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

export async function submitPlanAction(ctx, action) {
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
