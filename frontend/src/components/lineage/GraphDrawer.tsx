import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, Box, Button, Flex, Tabs, Text } from "@radix-ui/themes";
import {
  ArrowDownRight,
  ArrowUpRight,
  Copy,
  ExternalLink,
  File,
  Folder,
  X,
} from "lucide-react";
import {
  client,
  errorMessage,
  formatBytes,
  unwrap,
  type GraphEvidenceGroup,
  type GraphItem,
  type GraphNode,
  type GraphShareDetail,
} from "../../api/client";
import { StatusBadge } from "../StatusBadge";
import { CATEGORY_META, categoryForNode, graphStateLabel } from "./graphTheme";

interface Props {
  runId: string;
  node: GraphNode;
  detailEnabled: boolean;
  truncated: boolean;
  onClose: () => void;
  onNavigate: (shareId: string) => void;
}

function ItemRow({ item }: { item: GraphItem }) {
  const isFolder = item.kind === "drive#folder";
  return (
    <div className="graph-drawer__item">
      <span className="graph-drawer__item-icon">
        {isFolder ? <Folder size={14} aria-hidden /> : <File size={14} aria-hidden />}
      </span>
      <div className="graph-drawer__item-main">
        <Text as="div" size="2" weight="medium" className="graph-drawer__item-name">
          {item.name || item.file_id}
        </Text>
        <Text as="div" size="1" color="gray" className="mono">
          {item.path || item.file_id}
        </Text>
        {item.original_share_id ? (
          <Text as="div" size="1" color="gray">
            来源 {item.original_share_id}
            {item.original_file_id ? ` / ${item.original_file_id}` : ""}
          </Text>
        ) : null}
      </div>
      <Text size="1" color="gray">
        {isFolder ? "目录" : formatBytes(item.size)}
      </Text>
    </div>
  );
}

function EvidenceGroup({
  group,
  direction,
  onNavigate,
}: {
  group: GraphEvidenceGroup;
  direction: "incoming" | "outgoing";
  onNavigate: (shareId: string) => void;
}) {
  const visible = group.files.slice(0, 30);
  return (
    <div className="graph-evidence">
      <button type="button" className="graph-evidence__head" onClick={() => onNavigate(group.share_id)}>
        <span className="graph-evidence__icon">
          {direction === "incoming" ? (
            <ArrowDownRight size={14} aria-hidden />
          ) : (
            <ArrowUpRight size={14} aria-hidden />
          )}
        </span>
        <span className="graph-evidence__title">{group.title || group.share_id}</span>
        <span className="graph-evidence__id">{group.share_id}</span>
        <Badge size="1" variant="soft" color="gray">
          {group.file_count} 文件
        </Badge>
        <span className="graph-evidence__bytes">{formatBytes(group.total_bytes)}</span>
      </button>
      <div className="graph-evidence__files">
        {visible.map((item) => (
          <ItemRow key={`${group.share_id}-${item.file_id}`} item={item} />
        ))}
        {group.files.length < group.file_count || visible.length < group.files.length ? (
          <Box p="2">
            <Text as="div" size="1" color="gray">
              {visible.length < group.files.length
                ? `仅展示前 ${visible.length} 项`
                : `共 ${group.file_count} 项`}
            </Text>
          </Box>
        ) : null}
      </div>
    </div>
  );
}

export function GraphDrawer({ runId, node, detailEnabled, truncated, onClose, onNavigate }: Props) {
  const category = categoryForNode(node);
  const meta = CATEGORY_META[category];
  const Icon = meta.icon;

  const detailQuery = useQuery({
    queryKey: ["graph-share", runId, node.share_id],
    enabled: detailEnabled,
    queryFn: async () =>
      (await unwrap(
        await client.GET("/api/lineage-runs/{run_id}/graph/shares/{share_id}", {
          params: {
            path: { run_id: runId, share_id: node.share_id },
            query: { limit: 500 },
          },
        }),
      )) as unknown as GraphShareDetail,
    staleTime: 5000,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const detail = detailQuery.data;
  const current = detail?.node ?? node;
  const defaultTab = current.incoming_count > 0 ? "incoming" : "content";
  const [contentItems, setContentItems] = useState<GraphItem[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  useEffect(() => {
    setContentItems(detail?.content.items ?? []);
    setNextOffset(detail?.content.next_offset ?? null);
    setMoreError(null);
  }, [detail]);

  const items = contentItems;
  const incoming = detail?.incoming ?? [];
  const outgoing = detail?.outgoing ?? [];

  const loadMore = async () => {
    if (nextOffset == null || loadingMore) return;
    setLoadingMore(true);
    try {
      const next = (await unwrap(
        await client.GET("/api/lineage-runs/{run_id}/graph/shares/{share_id}", {
          params: {
            path: { run_id: runId, share_id: node.share_id },
            query: { limit: 500, offset: nextOffset },
          },
        }),
      )) as unknown as GraphShareDetail;
      setContentItems((currentItems) => [...currentItems, ...next.content.items]);
      setNextOffset(next.content.next_offset);
    } catch (error) {
      setMoreError(errorMessage(error));
    } finally {
      setLoadingMore(false);
    }
  };

  const copyId = () => {
    void navigator.clipboard?.writeText(node.share_id);
  };

  const metaRows = useMemo(
    () => [
      ["深度", `L${current.depth}`],
      ["名称", current.name_known === false ? "未知（未展开）" : "已获取"],
      ["状态", current.status || "—"],
      ["遍历", graphStateLabel(current.state)],
      ["属主", current.owner || "—"],
      ["文件", `${current.file_count}`],
      ["目录", `${current.folder_count}`],
      ["根类型", current.root_kind === "drive#folder" ? "文件夹" : current.root_kind === "drive#file" ? "文件" : "—"],
      ["根条目", current.file_num || "—"],
    ],
    [current],
  );

  return (
    <div className="graph-drawer-backdrop" onMouseDown={onClose}>
      <aside
        className="graph-drawer"
        aria-label="血缘节点详情"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="graph-drawer__header">
          <div className="graph-drawer__eyebrow">
            <span className="graph-drawer__category" style={{ color: meta.color }}>
              <Icon size={14} aria-hidden />
              {meta.label}
            </span>
            <span className="graph-drawer__depth">L{current.depth}</span>
            <button type="button" className="graph-drawer__close" onClick={onClose} aria-label="关闭">
              <X size={16} aria-hidden />
            </button>
          </div>
          <h2 className="graph-drawer__title">{current.title || current.share_id}</h2>
          <div className="graph-drawer__idrow">
            <span className="mono">{current.share_id}</span>
            <button type="button" className="icon-button" onClick={copyId} aria-label="复制分享 ID">
              <Copy size={13} aria-hidden />
            </button>
            <a
              className="icon-button"
              href={current.url}
              target="_blank"
              rel="noreferrer"
              aria-label="打开 PikPak 分享"
            >
              <ExternalLink size={13} aria-hidden />
            </a>
          </div>
          <Flex gap="2" wrap="wrap" mt="2" align="center">
            <StatusBadge status={current.status ?? undefined} />
            <Badge variant="soft" color="gray">
              {graphStateLabel(current.state)}
            </Badge>
            <Badge variant="soft" color="gray">
              来源 {current.incoming_count}
            </Badge>
            <Badge variant="soft" color="gray">
              去向 {current.outgoing_count}
            </Badge>
          </Flex>
          {current.source_hint && current.name_known === false ? (
            <Text as="div" size="1" color="gray" mt="2" className="graph-drawer__source-hint">
              来源文件：{current.source_hint}
            </Text>
          ) : null}
          {current.error_message ? (
            <Text as="div" size="1" color="red" mt="2">
              {current.error_message}
            </Text>
          ) : null}
        </header>

        <div className="graph-drawer__meta">
          {metaRows.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>

        {!detailEnabled ? (
          <div className="graph-drawer__notice">
            这是单目标链运行，没有枚举该分享的完整内容。回到血缘页创建“全量血缘图”后，可查看具体来源文件与分享内容。
          </div>
        ) : null}
        {detailQuery.isError ? (
          <div className="graph-drawer__notice">{errorMessage(detailQuery.error)}</div>
        ) : null}

        {detailEnabled ? (
          <Tabs.Root defaultValue={defaultTab} className="graph-drawer__tabs">
            <Tabs.List>
              <Tabs.Trigger value="incoming">来源文件 ({current.incoming_count})</Tabs.Trigger>
              <Tabs.Trigger value="content">分享内容 ({detail?.content.total ?? current.file_count + current.folder_count})</Tabs.Trigger>
              <Tabs.Trigger value="outgoing">派生去向 ({current.outgoing_count})</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content value="incoming" className="graph-drawer__panel">
              {detailQuery.isLoading ? (
                <Text size="2" color="gray">
                  加载中…
                </Text>
              ) : incoming.length === 0 ? (
                <Text size="2" color="gray">
                  没有其他分享的文件指向该节点。
                </Text>
              ) : (
                incoming.map((group) => (
                  <EvidenceGroup
                    key={group.share_id}
                    group={group}
                    direction="incoming"
                    onNavigate={onNavigate}
                  />
                ))
              )}
            </Tabs.Content>
            <Tabs.Content value="content" className="graph-drawer__panel">
              {detailQuery.isLoading ? (
                <Text size="2" color="gray">
                  加载中…
                </Text>
              ) : items.length === 0 ? (
                <Text size="2" color="gray">
                  {current.state === "DEPTH_LIMIT"
                    ? "超过血缘深度上限，未展开内容。"
                    : truncated
                      ? "预算已截断，未展开该分享的内容。"
                      : current.state === "PENDING" || current.state === "ENUMERATING"
                        ? "正在遍历该分享，稍后刷新。"
                        : "该分享没有枚举到内容。"}
                </Text>
              ) : (
                <div className="graph-drawer__items">
                  {items.map((item) => (
                    <ItemRow key={`${item.share_id}-${item.file_id}`} item={item} />
                  ))}
                  {moreError ? (
                    <Box p="2">
                      <Text as="div" size="1" color="red">
                        {moreError}
                      </Text>
                    </Box>
                  ) : null}
                  {detail && detail.content.total > items.length ? (
                    <Box p="2">
                      <Flex align="center" justify="between" gap="2">
                        <Text size="1" color="gray">
                          已展示 {items.length} / {detail.content.total} 项
                        </Text>
                        <Button
                          size="1"
                          variant="soft"
                          onClick={() => void loadMore()}
                          disabled={loadingMore || nextOffset == null}
                        >
                          {loadingMore ? "加载中…" : "加载更多"}
                        </Button>
                      </Flex>
                    </Box>
                  ) : null}
                </div>
              )}
            </Tabs.Content>
            <Tabs.Content value="outgoing" className="graph-drawer__panel">
              {detailQuery.isLoading ? (
                <Text size="2" color="gray">
                  加载中…
                </Text>
              ) : outgoing.length === 0 ? (
                <Text size="2" color="gray">
                  该分享的文件没有指向其他分享。
                </Text>
              ) : (
                outgoing.map((group) => (
                  <EvidenceGroup
                    key={group.share_id}
                    group={group}
                    direction="outgoing"
                    onNavigate={onNavigate}
                  />
                ))
              )}
            </Tabs.Content>
          </Tabs.Root>
        ) : null}

        <footer className="graph-drawer__footer">
          <Button variant="soft" color="gray" onClick={onClose}>
            关闭
          </Button>
          <Button variant="soft" asChild>
            <a href={current.url} target="_blank" rel="noreferrer">
              <ExternalLink size={14} aria-hidden /> 打开分享
            </a>
          </Button>
        </footer>
      </aside>
    </div>
  );
}
