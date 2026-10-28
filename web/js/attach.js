import { canSend, els, state } from "./core.js";

const MAX_IMAGES = 4;
const MAX_BYTES = 12 * 1024 * 1024; // как vision.MAX_INPUT_BYTES на сервере
const OK_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"]);

function note(msg) {
  els.statusLine.textContent = msg;
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

function renderStrip() {
  const strip = els.attachStrip;
  strip.hidden = !state.attachments.length;
  strip.innerHTML = "";
  state.attachments.forEach((a, i) => {
    const item = document.createElement("div");
    item.className = "attach-item";
    const img = document.createElement("img");
    img.src = a.dataUrl;
    img.alt = a.name;
    img.title = a.name;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "attach-remove";
    rm.textContent = "×";
    rm.title = "Убрать";
    rm.addEventListener("click", () => {
      state.attachments.splice(i, 1);
      renderStrip();
    });
    item.append(img, rm);
    strip.appendChild(item);
  });
  els.btnSend.disabled = state.busy || !canSend();
}

export function clearAttachments() {
  state.attachments = [];
  renderStrip();
}

export async function addFiles(files) {
  for (const file of files) {
    if (state.attachments.length >= MAX_IMAGES) {
      note(`Не больше ${MAX_IMAGES} картинок за раз`);
      break;
    }
    if (!OK_TYPES.has(file.type)) {
      note(`Формат не поддерживается: ${file.name || file.type || "файл"} (JPEG, PNG, WebP, GIF, BMP)`);
      continue;
    }
    if (file.size > MAX_BYTES) {
      note(`Слишком большой файл: ${file.name} (> ${MAX_BYTES / 1024 / 1024} МБ)`);
      continue;
    }
    try {
      state.attachments.push({ name: file.name || "image", dataUrl: await readAsDataUrl(file) });
    } catch (_) {
      note(`Не удалось прочитать ${file.name}`);
    }
  }
  renderStrip();
}

export function initAttachments() {
  els.btnAttach.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", async () => {
    await addFiles(Array.from(els.fileInput.files || []));
    els.fileInput.value = "";
  });
  els.input.addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
  });
  const stop = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };
  ["dragenter", "dragover"].forEach((t) =>
    els.composer.addEventListener(t, (e) => {
      stop(e);
      els.composer.classList.add("is-drop");
    })
  );
  ["dragleave", "drop"].forEach((t) =>
    els.composer.addEventListener(t, (e) => {
      stop(e);
      els.composer.classList.remove("is-drop");
    })
  );
  els.composer.addEventListener("drop", (e) => {
    if (!state.busy) addFiles(Array.from(e.dataTransfer?.files || []));
  });
}
