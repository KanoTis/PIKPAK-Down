import {
  Ban,
  CheckCircle2,
  CircleDashed,
  Flag,
  Layers,
  Loader,
  Scissors,
  type LucideIcon,
} from "lucide-react";
import type { GraphNode } from "../../api/client";

export type GraphCategory =
  | "start"
  | "done"
  | "active"
  | "pending"
  | "unavailable"
  | "depth"
  | "budget";

export interface CategoryMeta {
  label: string;
  color: string;
  icon: LucideIcon;
}

export const CATEGORY_ORDER: GraphCategory[] = [
  "start",
  "done",
  "active",
  "pending",
  "unavailable",
  "depth",
  "budget",
];

export const CATEGORY_META: Record<GraphCategory, CategoryMeta> = {
  start: { label: "起点", color: "#16a34a", icon: Flag },
  done: { label: "已展开", color: "#2563eb", icon: CheckCircle2 },
  active: { label: "遍历中", color: "#0891b2", icon: Loader },
  pending: { label: "待展开", color: "#64748b", icon: CircleDashed },
  unavailable: { label: "不可读", color: "#dc2626", icon: Ban },
  depth: { label: "深度上限", color: "#7c3aed", icon: Layers },
  budget: { label: "预算截断", color: "#d97706", icon: Scissors },
};

export function categoryForNode(node: GraphNode): GraphCategory {
  if (node.role === "start") return "start";
  if (node.state === "BUDGET") return "budget";
  if (node.state === "DEPTH_LIMIT") return "depth";
  if (node.state === "UNAVAILABLE") return "unavailable";
  if (node.state === "PENDING") return "pending";
  if (node.state === "ENUMERATING") return "active";
  const status = (node.status ?? "").toUpperCase();
  if (status && status !== "OK") return "unavailable";
  return "done";
}

export function categoryCounts(nodes: GraphNode[]): Record<GraphCategory, number> {
  const counts = Object.fromEntries(CATEGORY_ORDER.map((key) => [key, 0])) as Record<
    GraphCategory,
    number
  >;
  for (const node of nodes) counts[categoryForNode(node)] += 1;
  return counts;
}

export function graphStateLabel(state: string): string {
  switch (state) {
    case "PENDING":
      return "待展开";
    case "ENUMERATING":
      return "遍历中";
    case "DONE":
      return "已展开";
    case "UNAVAILABLE":
      return "不可读";
    case "DEPTH_LIMIT":
      return "深度上限";
    case "BUDGET":
      return "预算截断";
    default:
      return state;
  }
}

export function depthLabel(depth: number): string {
  if (depth === 0) return "起点";
  if (depth === 1) return "直接上游";
  if (depth === 2) return "二级上游";
  if (depth === 3) return "三级上游";
  return `${depth} 级上游`;
}
