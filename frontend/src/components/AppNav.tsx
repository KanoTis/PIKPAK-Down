import { Link } from "@tanstack/react-router";
import { Download, GitBranch, Settings2, Users } from "lucide-react";
import { useLiveEvents } from "../hooks/useLiveEvents";

const ITEMS = [
  { to: "/downloads", label: "下载", icon: Download },
  { to: "/lineage", label: "血缘", icon: GitBranch },
  { to: "/accounts", label: "账号", icon: Users },
  { to: "/settings", label: "设置", icon: Settings2 },
] as const;

export function AppNav() {
  useLiveEvents();
  return (
    <nav className="nav" aria-label="主导航">
      {ITEMS.map((item) => {
        const Icon = item.icon;
        return (
          <Link
            key={item.to}
            to={item.to}
            aria-label={item.label}
            className="nav-link"
            activeProps={{ className: "nav-link active" }}
            activeOptions={{ exact: false }}
          >
            <Icon size={16} aria-hidden />
            <span className="nav-text">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
