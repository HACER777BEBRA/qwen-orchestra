import { autosize, els, forceSelection, setBusy, state } from "./core.js";
import { clearAttachments } from "./attach.js";
import {
  appendMessage,
  ensureChat,
  formatElapsed,
  loadChats,
  metaChips,
  parseSseChunk,
} from "./chat-list.js";
import {
  appendChainLine,
  chainHost,
  ensureStepFold,
  finishStepFold,
  flushBodyRender,
  flushStepRender,
  planHeadLabel,
  renderPlanCard,
  scheduleBodyRender,
  scheduleStepRender,
  setFoldOpen,
  stepIdOf,
  stepRecFor,
  updateStepChips,
} from "./chat-plan.js";

export function applySseEvent(event, data, ctx) {
  const { toolsEl, metaSlot } = ctx;
  if (event === "meta") {
    const stepScope = data.scope === "step";
    if ((data.phase === "retry" || data.phase === "restore") && !stepScope) {
      ctx.full = "";
      flushBodyRender(ctx, { streaming: true });
    }
    if (data.phase === "vision") {
      ctx.liveMeta = { ...ctx.liveMeta, vision_model: data.vision_model };
      metaSlot.innerHTML = metaChips(ctx.liveMeta);
      els.statusLine.textContent = `Читаю картинку · ${data.vision_model || "vision"}…`;
      return;
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
      appendChainLine(
        toolsEl,
        data.reason === "math-patch"
          ? "исправлено число (CPU)"
          : `восстановлен лучший ответ (${data.model || ""})`
      );
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
    if (data.elapsed_ms != null) ctx.liveMeta.elapsed_ms = data.elapsed_ms;
    if (ctx.liveMeta.elapsed_ms != null) bits.push(formatElapsed(ctx.liveMeta.elapsed_ms));
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
      elapsed_ms: data.elapsed_ms,
      vision_model: data.vision_model || ctx.liveMeta.vision_model,
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
      data.elapsed_ms != null ? formatElapsed(data.elapsed_ms) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  } else if (event === "error") {
    throw new Error(data.message || "Ошибка оркестра");
  }
}

export async function send() {
  const text = els.input.value.trim();
  const attached = state.attachments.slice();
  if ((!text && !attached.length) || state.busy) return;

  setBusy(true);
  els.statusLine.textContent = attached.length ? "Картинка…" : "Роутинг…";

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
    clearAttachments();
    appendMessage(
      "user",
      text || "[изображение]",
      attached.length ? { images: attached.map((a) => a.dataUrl) } : null
    );

    asst = appendMessage("assistant", "", null, { streaming: true });
    ctx.bodyEl = asst.querySelector(":scope > .msg-body");
    ctx.toolsEl = asst.querySelector(".msg-tools");
    ctx.metaSlot = asst.querySelector(".msg-meta-slot");
    ctx.planStackEl = asst.querySelector(".msg-plan-stack");
    ctx.chatId = chatId;

    const res = await fetch(`/api/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: text,
        images: attached.map((a) => ({ name: a.name, data: a.dataUrl })),
        ...forceSelection(),
      }),
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
