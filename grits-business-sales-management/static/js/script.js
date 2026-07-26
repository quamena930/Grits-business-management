/**
 * Grits — shared front-end behaviour.
 * No frameworks: this is a small app and vanilla JS keeps it that way.
 * Covers: dark/light theme toggle, toast auto-dismiss, a reusable
 * "are you sure?" confirmation modal (replacing native confirm()), and a
 * live image preview on the product form.
 */

const THEME_KEY = "grits-theme";

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}

function applyThemeIcon(button) {
  if (!button) return;
  const isDark = currentTheme() === "dark";
  button.textContent = isDark ? "\u2600\ufe0f" : "\ud83c\udf19";
  button.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
}

function initThemeToggle() {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  applyThemeIcon(btn);
  btn.addEventListener("click", function () {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (err) {
      /* localStorage unavailable (private browsing etc.) - theme just
         won't persist across page loads. Not worth interrupting the
         user for. */
    }
    applyThemeIcon(btn);
  });
}

function initToasts() {
  const toasts = document.querySelectorAll(".toast");
  toasts.forEach(function (toast) {
    const closeBtn = toast.querySelector(".toast-close");
    const dismiss = function () {
      toast.classList.add("toast-leaving");
      setTimeout(function () {
        toast.remove();
      }, 200);
    };
    if (closeBtn) closeBtn.addEventListener("click", dismiss);
    setTimeout(dismiss, 5000);
  });
}

let _confirmTargetForm = null;

/** Used inline as: onsubmit="return requestConfirm(this, 'Delete X?')" */
function requestConfirm(form, message) {
  _confirmTargetForm = form;
  const backdrop = document.getElementById("confirmModalBackdrop");
  const body = document.getElementById("confirmModalBody");
  if (!backdrop || !body) {
    // Modal markup missing for some reason - fall back rather than
    // silently doing nothing.
    return confirm(message);
  }
  body.textContent = message;
  backdrop.hidden = false;
  return false; // always block the native submit; the modal takes over
}

function initConfirmModal() {
  const backdrop = document.getElementById("confirmModalBackdrop");
  if (!backdrop) return;
  const cancelBtn = document.getElementById("confirmModalCancel");
  const confirmBtn = document.getElementById("confirmModalConfirm");

  const close = function () {
    backdrop.hidden = true;
    _confirmTargetForm = null;
  };

  if (cancelBtn) cancelBtn.addEventListener("click", close);
  backdrop.addEventListener("click", function (event) {
    if (event.target === backdrop) close();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !backdrop.hidden) close();
  });
  if (confirmBtn) {
    confirmBtn.addEventListener("click", function () {
      const form = _confirmTargetForm;
      backdrop.hidden = true;
      _confirmTargetForm = null;
      if (form) form.submit(); // .submit() bypasses onsubmit, no re-loop
    });
  }
}

function initImagePreview() {
  const input = document.getElementById("productImageInput");
  const preview = document.getElementById("productImagePreview");
  if (!input || !preview) return;
  input.addEventListener("change", function () {
    const file = input.files && input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function (event) {
      preview.src = event.target.result;
      preview.hidden = false;
    };
    reader.readAsDataURL(file);
  });
}

document.addEventListener("DOMContentLoaded", function () {
  initThemeToggle();
  initToasts();
  initConfirmModal();
  initImagePreview();
});
