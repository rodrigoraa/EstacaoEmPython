(() => {
  const savedTheme = localStorage.getItem("theme");
  const theme = savedTheme || "light";
  document.documentElement.classList.toggle("theme-dark", theme === "dark");
  document.documentElement.dataset.theme = theme;
})();

