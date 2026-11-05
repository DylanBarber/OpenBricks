"use client";

import { useEffect, useState } from "react";
import { Monitor, Moon, Sun } from "lucide-react";

type Theme = "system" | "light" | "dark";

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");
  const [ready, setReady] = useState(false);
  useEffect(() => {
    try { const saved = localStorage.getItem("openbricks-theme"); if (saved === "light" || saved === "dark") setTheme(saved); } catch { /* System theme remains available when storage is blocked. */ }
    setReady(true);
  }, []);
  useEffect(() => {
    if (!ready) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => { document.documentElement.dataset.theme = theme === "system" ? media.matches ? "dark" : "light" : theme; };
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [theme, ready]);
  function change(value: Theme) { setTheme(value); try { localStorage.setItem("openbricks-theme", value); } catch { /* The selected theme still applies for this session. */ } }
  return <label className="theme-picker" title="Appearance">{theme === "dark" ? <Moon size={15} /> : theme === "light" ? <Sun size={15} /> : <Monitor size={15} />}<select aria-label="Color theme" value={theme} onChange={event => change(event.target.value as Theme)}><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select></label>;
}
