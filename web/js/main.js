import { autosize, els, setBusy, state } from "./core.js";
import { copyCodeBlock } from "./markdown.js";
import { applyAddFormCtxDefaults, defaultRankFor } from "./models-list.js";
import { syncAddProviderUi } from "./models-select.js";
import {
  addModelSlot,
  clearOpenRouterKey,
  closeModelsModal,
  loadSettings,
  openModelsModal,
  resetModels,
  saveModels,
  saveOpenRouterKey,
  toggleModelsHelp,
} from "./models-save.js";
import {
  clearChat,
  deleteChat,
  loadChats,
  newChat,
  refreshHealth,
  selectChat,
} from "./chat-list.js";
import { send } from "./chat-sse.js";
import { initMonitorPanel } from "./monitor-panel.js";

els.input.addEventListener("input", () => {
  autosize();
  els.btnSend.disabled = state.busy || !els.input.value.trim();
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

els.btnSend.addEventListener("click", send);
els.btnNew.addEventListener("click", newChat);
els.btnModels.addEventListener("click", openModelsModal);
els.btnModelsClose.addEventListener("click", closeModelsModal);
els.btnModelsHelp.addEventListener("click", toggleModelsHelp);
els.btnModelsSave.addEventListener("click", saveModels);
els.btnModelsAdd.addEventListener("click", addModelSlot);
els.btnModelsReset.addEventListener("click", resetModels);
if (els.btnOrKeySave) els.btnOrKeySave.addEventListener("click", saveOpenRouterKey);
if (els.btnOrKeyClear) els.btnOrKeyClear.addEventListener("click", clearOpenRouterKey);
document.querySelectorAll('input[name="add-provider"]').forEach((el) => {
  el.addEventListener("change", syncAddProviderUi);
});
if (els.addSlotTier) {
  els.addSlotTier.addEventListener("change", () => {
    const hasTier = !!(els.addSlotTier.value || "").trim();
    if (els.addSlotRank) {
      els.addSlotRank.disabled = !hasTier;
      if (hasTier && !els.addSlotRank.value) {
        els.addSlotRank.value = String(defaultRankFor(els.addSlotTier.value));
      }
    }
    applyAddFormCtxDefaults();
  });
}
els.messages.addEventListener("click", (e) => {
  const btn = e.target.closest(".code-copy");
  if (!btn || !els.messages.contains(btn)) return;
  e.preventDefault();
  copyCodeBlock(btn);
});
els.modelsModal.addEventListener("click", (e) => {
  if (e.target === els.modelsModal) closeModelsModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.modelsModal.hidden) closeModelsModal();
});

/* —— Monitor panel —— */

export async function init() {
  initMonitorPanel();
  await refreshHealth();
  setInterval(refreshHealth, 30000);
  try {
    await loadSettings();
  } catch (_) {
    /* health уже заполнил select */
  }
  await loadChats();
  if (state.chats.length) {
    await selectChat(state.chats[0].id);
  } else {
    await newChat();
  }
  setBusy(false);
  els.input.focus();
}

init().catch((e) => {
  els.health.className = "health err";
  els.health.textContent = e.message;
});
