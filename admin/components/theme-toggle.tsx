"use client";

import { useEffect, useSyncExternalStore } from "react";

type ThemeMode = "light" | "dark" | "system";

const STORAGE_KEY = "ai-centre-theme";
const CHANGE_EVENT = "ai-centre-theme-change";

function storedMode(): ThemeMode {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

function applyTheme(mode: ThemeMode) {
  const isDark = mode === "dark" || (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  document.documentElement.dataset.themeMode = mode;
  document.documentElement.style.colorScheme = isDark ? "dark" : "light";
}

function ThemeIcon({ mode }: { mode: ThemeMode }) {
  if (mode === "light") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.5" /><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42" /></svg>;
  }
  if (mode === "dark") {
    return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.4 15.1A8.5 8.5 0 0 1 8.9 3.6 8.5 8.5 0 1 0 20.4 15.1Z" /></svg>;
  }
  return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8M12 17v4" /></svg>;
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const mode = useSyncExternalStore(subscribe, storedMode, () => "system");

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const syncSystem = () => {
      if (storedMode() === "system") applyTheme("system");
    };
    media.addEventListener("change", syncSystem);
    return () => media.removeEventListener("change", syncSystem);
  }, []);

  function select(nextMode: ThemeMode) {
    window.localStorage.setItem(STORAGE_KEY, nextMode);
    applyTheme(nextMode);
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }

  const options: Array<{ mode: ThemeMode; label: string }> = [
    { mode: "light", label: "浅色模式" },
    { mode: "dark", label: "深色模式" },
    { mode: "system", label: "跟随系统" },
  ];

  return (
    <div className={compact ? "theme-toggle is-compact" : "theme-toggle"} role="group" aria-label="界面主题">
      {options.map((option) => (
        <button
          type="button"
          key={option.mode}
          className={mode === option.mode ? "is-active" : ""}
          aria-label={option.label}
          aria-pressed={mode === option.mode}
          title={option.label}
          onClick={() => select(option.mode)}
        >
          <ThemeIcon mode={option.mode} />
          {!compact && <span>{option.label.replace("模式", "")}</span>}
        </button>
      ))}
    </div>
  );
}
