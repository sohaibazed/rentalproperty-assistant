// Chat page helpers: Enter to send (with a mouse), show attached file names, stream the live reply.
// The page works without this file (it falls back to a 3-second refresh).
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const thread = document.getElementById("thread");
  const scrollDown = () => { if (thread) thread.scrollTop = thread.scrollHeight; };
  scrollDown();

  const form = document.getElementById("composer");
  if (form) {
    const textarea = form.querySelector("textarea");
    const files = form.querySelector('input[type="file"]');
    const names = document.getElementById("file-names");
    const send = form.querySelector(".send");
    // A phone has no Shift+Enter worth using: there Enter makes a new line and the button sends.
    const mouse = window.matchMedia("(pointer: fine)").matches;

    textarea.addEventListener("keydown", (e) => {
      if (mouse && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (textarea.value.trim() || files.files.length) form.requestSubmit();
      }
    });
    textarea.addEventListener("input", () => {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 240) + "px";
    });
    files.addEventListener("change", () => {
      names.textContent = "";
      for (const f of files.files) {
        const chip = document.createElement("span");
        chip.className = "att";
        chip.textContent = "📎 " + f.name;
        names.appendChild(chip);
      }
    });
    form.addEventListener("submit", () => { send.disabled = true; send.textContent = "…"; });
    if (mouse && !textarea.disabled) textarea.focus();   // on a phone, focus opens the keyboard
  }

  const live = document.getElementById("live");
  if (live && window.EventSource) {
    const text = document.getElementById("live-text");
    const status = document.getElementById("live-status");
    const source = new EventSource(live.dataset.events);
    source.onmessage = (e) => {
      const data = JSON.parse(e.data);
      text.textContent = data.text;          // plain text while streaming; formatted on reload
      status.textContent = data.status;
      scrollDown();
    };
    source.addEventListener("done", () => { source.close(); window.location.reload(); });
    source.onerror = () => { source.close(); setTimeout(() => window.location.reload(), 2000); };
  }
});
