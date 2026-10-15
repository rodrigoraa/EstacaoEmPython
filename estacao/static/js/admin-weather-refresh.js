(() => {
  const page = document.querySelector("[data-admin-auto-refresh]");
  if (!page) return;

  const status = page.querySelector("[data-admin-refresh-status]");
  const interval = 60000;
  const idleInterval = 10000;
  const loadedAt = performance.now();
  let interactedAt = loadedAt;
  let pointerDown = false;
  let formChanged = false;
  let submitting = false;
  let reloading = false;

  const setStatus = (message) => {
    if (status && status.textContent !== message) status.textContent = message;
  };
  const interacted = () => { interactedAt = performance.now(); };
  const busy = () => {
    const focused = document.activeElement;
    const selection = window.getSelection();
    return pointerDown || formChanged || submitting ||
      document.querySelector("dialog[open]") ||
      (selection && !selection.isCollapsed) ||
      (focused && (focused.isContentEditable ||
        focused.closest("form, input, textarea, select, button"))) ||
      performance.now() - interactedAt < idleInterval;
  };

  document.addEventListener("pointerdown", () => { pointerDown = true; interacted(); });
  document.addEventListener("pointerup", () => { pointerDown = false; interacted(); });
  document.addEventListener("pointercancel", () => { pointerDown = false; interacted(); });
  document.addEventListener("keydown", interacted);
  document.addEventListener("focusin", interacted);
  for (const type of ["input", "change"]) {
    document.addEventListener(type, (event) => {
      interacted();
      if (event.target.closest("form")) formChanged = true;
    });
  }
  document.addEventListener("submit", () => { submitting = true; }, true);
  document.addEventListener("click", (event) => {
    if (submitting && event.target.closest("[data-admin-refresh-now]")) event.preventDefault();
  });
  window.addEventListener("blur", () => { pointerDown = false; });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      submitting = false;
      reloading = false;
      interacted();
    }
  });

  const refreshWhenReady = () => {
    if (document.visibilityState !== "visible" || reloading) return;
    if (busy()) {
      setStatus("Atualização automática pausada durante a interação.");
      return;
    }
    setStatus("Atualização automática a cada 60 s.");
    if (performance.now() - loadedAt >= interval) {
      reloading = true;
      window.location.reload();
    }
  };

  setStatus("Atualização automática a cada 60 s.");
  window.setInterval(refreshWhenReady, 1000);
  document.addEventListener("visibilitychange", refreshWhenReady);
})();
