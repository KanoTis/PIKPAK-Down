import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { Badge, Button, Card, Flex, Text } from "@radix-ui/themes";
import { ArrowLeft, Download, GitBranch, Play, RefreshCw, X } from "lucide-react";
import {
  client,
  errorMessage,
  formatDate,
  unwrap,
  type GraphEdge,
  type GraphNode,
  type LineageGraph,
  type LineageRun,
} from "../api/client";
import { ConfirmButton } from "../components/ConfirmButton";
import { ErrorBanner, InfoBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { GraphDrawer } from "../components/lineage/GraphDrawer";
import { LineageGraph as LineageGraphCanvas } from "../components/lineage/LineageGraph";
import { depthLabel } from "../components/lineage/graphTheme";

interface ReportNode {
  node: string;
  role: string;
  share_id?: string | null;
  file_id?: string | null;
  name?: string | null;
  size?: number | null;
  hash?: string | null;
  kind?: string | null;
  platform?: string | null;
  depth?: number;
  confidence?: string | null;
  reason?: string | null;
}

interface TenantRef {
  share_id: string;
  ref_count: number;
  share_status?: string | null;
  seconds?: number | null;
  owner?: string | null;
}

interface Report {
  target: {
    share_id?: string | null;
    file_id?: string | null;
    name?: string | null;
    size?: number | null;
    hash?: string | null;
    kind?: string | null;
    mime_type?: string | null;
    type?: string;
  };
  direct_chain: ReportNode[];
  folder_chain: ReportNode[];
  tenant_refs: TenantRef[];
  copies: unknown[];
  anonymous_windows: Record<string, unknown>;
  terminations: { node: string; reason: string; confidence: string; detail?: string | null }[];
  stats: { requests?: number; shares?: number; items?: number; folders?: number; duration_ms?: number };
  evidence: { raw_dir?: string; count?: number };
  probed_at?: string | null;
  truncated: boolean;
  passcode_unverified: boolean;
  error?: { code?: string; message?: string } | null;
  graph?: LineageGraph;
}

const TERMINAL = new Set(["DONE", "FAILED", "CANCELLED"]);

function fallbackGraph(run: LineageRun, report: Report): LineageGraph | null {
  const startShare = run.share_id;
  if (!startShare) return null;
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();
  const addNode = (node: GraphNode) => {
    if (!nodes.has(node.id)) nodes.set(node.id, node);
    return nodes.get(node.id)!;
  };
  const makeNode = (
    shareId: string,
    depth: number,
    role: string,
    title?: string | null,
    status?: string | null,
  ): GraphNode => ({
    id: `share:${shareId}`,
    share_id: shareId,
    depth,
    role,
    state: "DONE",
    status: status ?? "OK",
    status_text: null,
    title: title || shareId,
    owner: null,
    file_num: null,
    anonymous_play_seconds: null,
    root_kind: null,
    root_file_id: null,
    root_name: null,
    file_count: 0,
    folder_count: 0,
    incoming_count: 0,
    outgoing_count: 0,
    error_code: null,
    error_message: null,
    url: `https://mypikpak.com/s/${shareId}`,
    preview: [],
  });
  const addEdge = (from: string, to: string, count: number) => {
    if (from === to) return;
    const id = `${from}->${to}`;
    const existing = edges.get(id);
    if (existing) {
      existing.file_count += count;
      return;
    }
    edges.set(id, {
      id,
      from,
      to,
      from_share_id: from.replace("share:", ""),
      to_share_id: to.replace("share:", ""),
      file_count: count,
      total_bytes: 0,
      files: [],
      target_status: "OK",
      target_state: "DONE",
    });
  };

  const start = addNode(
    makeNode(startShare, 0, "start", report.target.name || startShare, "OK"),
  );
  const chains = [report.direct_chain, report.folder_chain];
  for (const chain of chains) {
    let previous: GraphNode | null = null;
    for (const entry of chain) {
      if (!entry.share_id) continue;
      const node: GraphNode =
        entry.share_id === startShare
          ? start
          : addNode(
              makeNode(
                entry.share_id,
                entry.depth ?? (previous ? previous.depth + 1 : 1),
                entry.role === "target" ? "start" : "parent",
                entry.name || entry.share_id,
                "OK",
              ),
            );
      if (previous && previous.id !== node.id) addEdge(previous.id, node.id, 1);
      previous = node;
    }
  }
  for (const ref of report.tenant_refs) {
    const node = addNode(
      makeNode(ref.share_id, 1, "parent", ref.share_id, ref.share_status ?? "OK"),
    );
    node.file_count += ref.ref_count;
    addEdge(start.id, node.id, ref.ref_count);
  }
  for (const edge of edges.values()) {
    const target = nodes.get(edge.to);
    if (target) {
      target.incoming_count += edge.file_count;
      target.outgoing_count += 0;
    }
    const source = nodes.get(edge.from);
    if (source) source.outgoing_count += edge.file_count;
  }
  return {
    start: start.id,
    start_share_id: startShare,
    nodes: Array.from(nodes.values()),
    edges: Array.from(edges.values()),
    stats: {
      shares: nodes.size,
      edges: edges.size,
      files: 0,
      folders: 0,
      max_depth: Math.max(0, ...Array.from(nodes.values()).map((node) => node.depth)),
      requests: report.stats.requests ?? 0,
      truncated: report.truncated,
      termination: null,
    },
  };
}

export function LineageRunPage() {
  const { runId } = useParams({ from: "/lineage/$runId" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const runQuery = useQuery({
    queryKey: ["lineage-run", runId],
    queryFn: async () =>
      (await unwrap(
        await client.GET("/api/lineage-runs/{run_id}", {
          params: { path: { run_id: runId } },
        }),
      )) as unknown as { run: LineageRun; report: Report },
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status && TERMINAL.has(status) ? false : 2500;
    },
  });

  const cancelRun = useMutation({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/lineage-runs/{run_id}/cancel", {
          params: { path: { run_id: runId } },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lineage-run", runId] });
      void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
    },
  });

  const resumeRun = useMutation({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/lineage-runs/{run_id}/resume", {
          params: { path: { run_id: runId } },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lineage-run", runId] });
      void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
    },
  });

  const enrichNames = useMutation({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/lineage-runs/{run_id}/graph/enrich", {
          params: { path: { run_id: runId } },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lineage-run", runId] });
    },
  });

  const rebuild = useMutation({
    mutationFn: async () => {
      const run = runQuery.data?.run;
      if (!run?.share_id) throw new Error("缺少分享 ID");
      return unwrap(
        await client.POST("/api/lineage-runs", {
          body: {
            input: `https://mypikpak.com/s/${run.share_id}`,
            graph: true,
            include_tree: true,
            probe_windows: false,
          },
        }),
      );
    },
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
      void navigate({ to: "/lineage/$runId", params: { runId: run.id } });
    },
  });

  const data = runQuery.data;
  const run = data?.run;
  const report = data?.report;
  const isGraphRun = Boolean(report?.graph);
  const graph = useMemo(
    () => report?.graph ?? (report && run ? fallbackGraph(run, report) : null),
    [report, run],
  );
  const selectedNode = useMemo(
    () => graph?.nodes.find((node) => node.id === selectedId) ?? null,
    [graph, selectedId],
  );
  const layerCounts = useMemo(() => {
    const counts = new Map<number, number>();
    for (const node of graph?.nodes ?? []) {
      counts.set(node.depth, (counts.get(node.depth) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => a[0] - b[0]);
  }, [graph]);

  useEffect(() => {
    if (selectedId && graph && !graph.nodes.some((node) => node.id === selectedId)) {
      setSelectedId(null);
    }
  }, [graph, selectedId]);

  if (runQuery.isLoading) {
    return <Text color="gray">加载中…</Text>;
  }
  if (runQuery.isError) {
    return <ErrorBanner message={errorMessage(runQuery.error)} />;
  }
  if (!run || !report) {
    return <ErrorBanner message="溯源任务不存在" />;
  }

  const terminal = TERMINAL.has(run.status);
  const startNode = graph?.nodes.find((node) => node.id === graph.start);
  const title = startNode?.title || report.target.name || run.share_id || "血缘图";
  const startShare = run.share_id;

  return (
    <div className="lineage-run-page">
      <PageHeader
        title={title}
        description={`运行 ${run.id} · 起点 ${startShare ?? "—"} · 状态 ${run.status}`}
        actions={
          <>
            <Button variant="soft" color="gray" asChild>
              <a href="/lineage">
                <ArrowLeft size={15} aria-hidden /> 返回
              </a>
            </Button>
            {isGraphRun && report?.truncated ? (
              <Button
                variant="soft"
                color="green"
                onClick={() => resumeRun.mutate()}
                disabled={resumeRun.isPending}
              >
                <Play size={14} aria-hidden />
                {resumeRun.isPending ? "继续中" : "继续运行"}
              </Button>
            ) : null}
            {isGraphRun && graph?.nodes.some((node) => node.name_known === false) ? (
              <Button
                variant="soft"
                onClick={() => enrichNames.mutate()}
                disabled={enrichNames.isPending}
              >
                <RefreshCw size={14} aria-hidden />
                {enrichNames.isPending ? "补齐中" : "补齐名称"}
              </Button>
            ) : null}
            {!isGraphRun && startShare && !report.passcode_unverified ? (
              <Button
                variant="soft"
                onClick={() => rebuild.mutate()}
                disabled={rebuild.isPending}
              >
                <GitBranch size={15} aria-hidden />
                {rebuild.isPending ? "创建中" : "创建全量血缘图"}
              </Button>
            ) : null}
            <Button variant="soft" asChild>
              <a href={`/api/lineage-runs/${run.id}/export?format=json`} download>
                <Download size={14} aria-hidden /> JSON
              </a>
            </Button>
            {!terminal ? (
              <ConfirmButton
                label="取消运行"
                title="取消溯源运行？"
                description="已保存的节点、快照与证据会保留，后续可重新创建运行。"
                confirmLabel="取消运行"
                onConfirm={() => cancelRun.mutate()}
              />
            ) : null}
          </>
        }
      />

      <ErrorBanner message={report.error?.message} />
      {report.truncated ? (
        <div style={{ marginBottom: 10 }}>
          <InfoBanner message="预算已耗尽：图为部分结果。调高 lineage 预算后点“继续运行”，或重新创建运行。" />
        </div>
      ) : null}
      {!isGraphRun ? (
        <div style={{ marginBottom: 10 }}>
          <InfoBanner message="当前是单目标链运行，图只展示该链与已探测的旁系引用。点“创建全量血缘图”可遍历分享内全部文件并继续向上游展开。" />
        </div>
      ) : null}

      {layerCounts.length > 1 ? (
        <div className="graph-layers" aria-label="血缘层级">
          <span className="graph-layers__title">层级</span>
          {layerCounts.map(([depth, count]) => (
            <span key={depth} className={`graph-layer-chip graph-layer-chip--${Math.min(depth, 3)}`}>
              L{depth} {depthLabel(depth)}
              <b>{count}</b>
            </span>
          ))}
          <span className="graph-layers__hint">同层是并列来源，不是同一条链</span>
        </div>
      ) : null}

      <div className="graph-run-stats">
        <Badge variant="soft" color="gray">
          {graph?.stats.shares ?? 0} 分享
        </Badge>
        <Badge variant="soft" color="gray">
          {graph?.stats.edges ?? 0} 派生
        </Badge>
        <Badge variant="soft" color="gray">
          {graph?.stats.files ?? 0} 文件
        </Badge>
        <Badge variant="soft" color="gray">
          {graph?.stats.folders ?? 0} 目录
        </Badge>
        <Badge variant="soft" color="gray">
          深度 {graph?.stats.max_depth ?? 0}
        </Badge>
        <Text size="1" color="gray">
          请求 {graph?.stats.requests ?? run.requests} · 快照 {formatDate(report.probed_at)}
        </Text>
        <StatusBadge status={run.status} />
      </div>

      {graph ? (
        <div className="graph-page">
          <LineageGraphCanvas
            key={run.id}
            graph={graph}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>
      ) : (
        <Card size="2">
          <Flex align="center" gap="2">
            <GitBranch size={16} aria-hidden />
            <Text color="gray">该运行没有可绘制的节点。</Text>
          </Flex>
        </Card>
      )}

      {selectedNode ? (
        <GraphDrawer
          runId={run.id}
          node={selectedNode}
          detailEnabled={isGraphRun}
          truncated={report.truncated}
          onClose={() => setSelectedId(null)}
          onNavigate={(shareId) => setSelectedId(`share:${shareId}`)}
        />
      ) : null}

      {!terminal ? (
        <div className="graph-run-hint">
          <RefreshCw size={13} aria-hidden />
          正在遍历，节点会随进度自动出现
        </div>
      ) : null}
      {terminal &&
      graph &&
      graph.nodes.length === 1 &&
      (startNode?.file_count ?? 0) === 0 &&
      (startNode?.folder_count ?? 0) === 0 ? (
        <div className="graph-run-hint">
          <X size={13} aria-hidden /> 起点分享没有枚举到可展开的内容
        </div>
      ) : null}
      {graph && graph.stats.files === 0 && graph.nodes.length > 1 ? (
        <Text as="div" size="1" color="gray" mt="2">
          图中文件数为 0 表示只探测到分享级引用，尚未枚举文件；可重建全量图。
        </Text>
      ) : null}
    </div>
  );
}
