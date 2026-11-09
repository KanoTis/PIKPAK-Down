import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export const client = createClient<paths>({ baseUrl: "" });

export type DownloadJob = components["schemas"]["DownloadJobOut"];
export type DownloadTask = components["schemas"]["DownloadTaskOut"];
export type TaskList = components["schemas"]["TaskListOut"];
export type Account = components["schemas"]["AccountOut"];
export type Cleanup = components["schemas"]["CleanupOut"];
export type LineageRun = components["schemas"]["LineageRunOut"];

export interface GraphItem {
  id: string;
  share_id: string;
  file_id: string;
  kind: string;
  name: string;
  path: string;
  parent_id?: string | null;
  size: number;
  hash?: string | null;
  mime_type?: string | null;
  is_video: boolean;
  original_share_id?: string | null;
  original_file_id?: string | null;
}

export interface GraphNode {
  id: string;
  share_id: string;
  depth: number;
  role: string;
  state: string;
  status?: string | null;
  status_text?: string | null;
  title: string;
  name_known?: boolean;
  source_hint?: string | null;
  owner?: string | null;
  file_num?: string | null;
  anonymous_play_seconds?: number | null;
  root_kind?: string | null;
  root_file_id?: string | null;
  root_name?: string | null;
  file_count: number;
  folder_count: number;
  incoming_count: number;
  outgoing_count: number;
  error_code?: string | null;
  error_message?: string | null;
  url: string;
  preview?: GraphItem[];
}

export interface GraphEdge {
  id: string;
  from: string;
  to: string;
  from_share_id: string;
  to_share_id: string;
  file_count: number;
  total_bytes: number;
  files: GraphItem[];
  target_status?: string | null;
  target_state?: string | null;
}

export interface LineageGraph {
  start: string;
  start_share_id?: string | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: {
    shares: number;
    edges: number;
    files: number;
    folders: number;
    max_depth: number;
    requests: number;
    truncated: boolean;
    termination?: string | null;
  };
}

export interface GraphEvidenceGroup {
  share_id: string;
  title: string;
  status?: string | null;
  state?: string | null;
  depth?: number | null;
  file_count: number;
  total_bytes: number;
  files: GraphItem[];
}

export interface GraphShareDetail {
  node: GraphNode;
  content: {
    total: number;
    offset: number;
    limit: number;
    next_offset: number | null;
    items: GraphItem[];
  };
  incoming: GraphEvidenceGroup[];
  outgoing: GraphEvidenceGroup[];
}
export type SharePreview = components["schemas"]["SharePreviewOut"];
export type PreviewItem = components["schemas"]["PreviewItemOut"];
export type SelectionItem = components["schemas"]["DownloadSelectionItem"];

export class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  details: unknown;

  constructor(status: number, code: string, message: string, retryable = false, details: unknown = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

export async function unwrap<T>(result: Result<T>): Promise<T> {
  if (result.error !== undefined) {
    const body = result.error as { error?: { code?: string; message?: string; retryable?: boolean; details?: unknown } };
    const info = body?.error ?? {};
    throw new ApiError(
      result.response.status,
      info.code ?? `HTTP_${result.response.status}`,
      info.message ?? "请求失败",
      Boolean(info.retryable),
      info.details ?? {},
    );
  }
  return result.data as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

const TERMINAL: Record<string, true> = { DONE: true, FAILED: true, CANCELLED: true };

export function isTerminal(status: string): boolean {
  return TERMINAL[status] === true;
}

export function formatBytes(value: number | null | undefined): string {
  const bytes = value ?? 0;
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

export function formatSpeed(value: number | null | undefined): string {
  return `${formatBytes(value)}/s`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function percent(done: number, total: number): number {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}
