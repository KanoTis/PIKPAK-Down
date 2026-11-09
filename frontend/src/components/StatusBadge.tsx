import { Badge } from "@radix-ui/themes";

type Tone = "green" | "red" | "amber" | "blue" | "gray" | "cyan" | "violet" | "orange" | "indigo";

const STATUS: Record<string, { color: Tone; label: string }> = {
  DONE: { color: "green", label: "已完成" },
  COMPLETED: { color: "green", label: "已完成" },
  COMPLETED_WITH_ERRORS: { color: "amber", label: "部分失败" },
  FAILED: { color: "red", label: "失败" },
  CANCELLED: { color: "gray", label: "已取消" },
  PAUSED: { color: "amber", label: "已暂停" },
  DISCOVERED: { color: "gray", label: "待处理" },
  PLANNING: { color: "indigo", label: "规划中" },
  ANON_DOWNLOADING: { color: "blue", label: "匿名下载" },
  ACCOUNT_QUEUED: { color: "orange", label: "等待账号" },
  RESTORING: { color: "blue", label: "转存中" },
  WCL_READY: { color: "cyan", label: "直链就绪" },
  ACCOUNT_DOWNLOADING: { color: "blue", label: "账号下载" },
  VERIFYING: { color: "violet", label: "校验中" },
  ENUMERATING: { color: "indigo", label: "枚举中" },
  READY: { color: "blue", label: "已就绪" },
  ACTIVE: { color: "green", label: "可用" },
  COOLDOWN: { color: "amber", label: "冷却中" },
  INVALID: { color: "red", label: "失效" },
  DISABLED: { color: "gray", label: "已停用" },
  PENDING: { color: "gray", label: "待处理" },
  RETRYING: { color: "amber", label: "重试中" },
  QUEUED: { color: "gray", label: "排队中" },
  RESOLVING: { color: "blue", label: "解析中" },
  DIRECT_CHAIN: { color: "blue", label: "直系链" },
  SWEEPING: { color: "blue", label: "聚合引用" },
  PROBING: { color: "violet", label: "窗口探测" },
  OK: { color: "green", label: "正常" },
  DELETED: { color: "red", label: "已删除" },
  EXPIRED: { color: "red", label: "已过期" },
  SENSITIVE_RESOURCE: { color: "red", label: "敏感资源" },
  DEPTH_LIMIT: { color: "amber", label: "深度上限" },
  ERROR: { color: "red", label: "错误" },
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const key = status ?? "";
  const entry = STATUS[key] ?? { color: "gray" as Tone, label: key || "未知" };
  return (
    <Badge color={entry.color} variant="soft">
      {entry.label}
    </Badge>
  );
}
