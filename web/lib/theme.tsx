"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/* 테마는 세 상태다. 토글이 두 상태뿐이면 시스템 설정으로 되돌아갈 방법이
 * 없어진다 — 한 번 수동으로 고르면 영영 수동이 된다. */
export type Theme = "system" | "light" | "dark";

const STORAGE_KEY = "theme";

/* <head>에서 페인트 전에 실행된다. 이게 없으면 저장된 테마가 적용되기 전에
 * 기본 팔레트가 한 프레임 그려져 화면이 번쩍인다. */
export const themeInitScript = `(function(){try{var t=localStorage.getItem("${STORAGE_KEY}");if(t==="light"||t==="dark"){document.documentElement.setAttribute("data-theme",t)}}catch(e){}})()`;

function apply(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", theme);
  }
}

type ThemeContextValue = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  /* 서버 렌더에서는 localStorage를 볼 수 없으므로 "system"으로 시작하고,
   * 마운트 후에 저장된 값으로 맞춘다. 화면은 이미 위 스크립트가 칠해뒀다. */
  const [theme, setThemeState] = useState<Theme>("system");

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark") {
        setThemeState(stored);
      }
    } catch {
      /* 저장소가 막혀 있으면 시스템 설정을 따른다. */
    }
  }, []);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    apply(next);
    try {
      if (next === "system") {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      /* 이번 세션에만 적용되고 만다. 기능을 막을 이유는 아니다. */
    }
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) {
    throw new Error("useTheme must be used inside ThemeProvider");
  }
  return value;
}
