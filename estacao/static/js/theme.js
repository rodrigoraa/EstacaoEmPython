function aplicarTema(theme) {
  const isDark = theme === "dark";
  document.documentElement.classList.toggle("theme-dark", isDark);
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  document
    .querySelectorAll("#themeToggleDesktop i, #themeToggleMobile i")
    .forEach((icon) => {
      icon.className = isDark ? "fa-solid fa-sun" : "fa-solid fa-moon";
    });
}

function alternarTema() {
  const currentTheme =
    document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  aplicarTema(currentTheme === "dark" ? "light" : "dark");
}

function toggleMenu() {
  const menu = document.getElementById("menuLateral");
  const overlay = document.getElementById("menuOverlay");
  const button = document.getElementById("menuToggleMobile");
  const isOpen = !document.body.classList.contains("menu-open");
  menu?.classList.toggle("-translate-x-full");
  overlay?.classList.toggle("hidden");
  document.body.classList.toggle("menu-open", isOpen);
  button?.setAttribute("aria-expanded", String(isOpen));
}

document.addEventListener("DOMContentLoaded", () => {
  aplicarTema(document.documentElement.dataset.theme || "light");
  document.getElementById("themeToggleDesktop")?.addEventListener("click", alternarTema);
  document.getElementById("themeToggleMobile")?.addEventListener("click", alternarTema);
  document.getElementById("menuToggleMobile")?.addEventListener("click", toggleMenu);
  document.getElementById("menuOverlay")?.addEventListener("click", toggleMenu);
  document.getElementById("menuCloseMobile")?.addEventListener("click", toggleMenu);
});

