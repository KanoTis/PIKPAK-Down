import { Outlet } from "@tanstack/react-router";
import { Theme } from "@radix-ui/themes";
import { useState } from "react";
import { AppNav } from "./AppNav";

export function AppShell() {
  const [dark, setDark] = useState<boolean>(() => {
    const saved = window.localStorage.getItem("pikpak-theme");
    return saved === "dark";
  });

  const toggle = () => {
    setDark((value) => {
      const next = !value;
      window.localStorage.setItem("pikpak-theme", next ? "dark" : "light");
      return next;
    });
  };

  return (
    <Theme appearance={dark ? "dark" : "light"} accentColor="blue" grayColor="slate" radius="medium">
      <div className="app-shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">P</span>
            <span>
              <div style={{ fontWeight: 650, fontSize: 14 }}>PikPak 分享套件</div>
              <div className="muted" style={{ fontSize: 11 }}>
                下载与血缘
              </div>
            </span>
          </div>
          <AppNav />
          <button
            type="button"
            onClick={toggle}
            style={{
              marginTop: "auto",
              border: "1px solid var(--gray-a5)",
              background: "transparent",
              color: "var(--gray-11)",
              borderRadius: 8,
              padding: "8px 10px",
              cursor: "pointer",
              fontSize: 12,
            }}
            aria-label={dark ? "切换到浅色主题" : "切换到深色主题"}
          >
            {dark ? "浅色主题" : "深色主题"}
          </button>
        </aside>
        <main className="main">
          <Outlet />
        </main>
      </div>
    </Theme>
  );
}
