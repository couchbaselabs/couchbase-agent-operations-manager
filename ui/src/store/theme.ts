import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

// Keep this in sync with the inline bootstrap script in index.html, which
// sets data-theme on <html> before React mounts to avoid a flash of the
// wrong theme.
const STORAGE_KEY = "agent-operations-theme";

export const DEFAULT_THEME: Theme = "light";

export function getStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

// No stored preference yet - light. The operating system's
// `prefers-color-scheme` is deliberately not consulted: this is an
// appliance console that people screenshot, demo and support each other
// through, and it should look the same on a first visit whatever machine
// it's opened on. Dark is opt-in via the toggle, and once chosen it
// persists in localStorage.
export function getPreferredTheme(): Theme {
  return getStoredTheme() ?? DEFAULT_THEME;
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

function persistTheme(theme: Theme) {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Best-effort - theme just won't persist across reloads.
  }
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => getPreferredTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    persistTheme(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggleTheme };
}
