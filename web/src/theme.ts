export type Theme = "light" | "dark";

export function getTheme(): Theme {
  try { return localStorage.getItem("theme") === "dark" ? "dark" : "light"; } catch { return "light"; }
}

export function setTheme(t: Theme) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch { /* 저장소 사용 불가 */ }
}
