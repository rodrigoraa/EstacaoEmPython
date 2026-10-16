/* Confirma operações e impede submissões repetidas enquanto a página navega. */
(() => {
  "use strict";
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (event.defaultPrevented) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    // Desabilitar depois da serialização preserva o name/value do botão enviado.
    window.setTimeout(() => {
      Array.from(form.elements).filter((element) =>
        (element instanceof HTMLButtonElement || element instanceof HTMLInputElement) && element.type === "submit"
      ).forEach((button) => {
        if (!button.disabled) {
          button.dataset.formGuardDisabled = "true";
          button.disabled = true;
        }
      });
    }, 0);
  });
  window.addEventListener("pageshow", () => {
    document.querySelectorAll('form[data-submitting="true"]').forEach((form) => {
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
    });
    document.querySelectorAll('[data-form-guard-disabled="true"]').forEach((button) => {
      button.disabled = false;
      delete button.dataset.formGuardDisabled;
    });
  });
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-open-dialog], [data-close-dialog]");
    if (!trigger) return;
    if (trigger.dataset.openDialog) {
      document.getElementById(trigger.dataset.openDialog)?.showModal();
    } else {
      trigger.closest("dialog")?.close();
    }
  });
})();
