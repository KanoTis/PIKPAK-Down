import { useDeferredValue, useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import {
  Badge,
  Button,
  Card,
  Flex,
  Progress,
  Table,
  Text,
  TextField,
} from "@radix-ui/themes";
import { ArrowLeft, Download, Pause, Play, RotateCcw, X } from "lucide-react";

import {
  client,
  errorMessage,
  formatBytes,
  formatDate,
  formatSpeed,
  percent,
  unwrap,
  type DownloadTask,
} from "../api/client";
import { ConfirmButton } from "../components/ConfirmButton";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";

type TaskCategory = "downloading" | "queued" | "failed" | "success";

const TASK_TABS: { key: TaskCategory; label: string }[] = [
  { key: "downloading", label: "下载" },
  { key: "queued", label: "队列" },
  { key: "failed", label: "失败" },
  { key: "success", label: "成功" },
];

async function taskAction(taskId: string, action: "pause" | "resume" | "retry" | "cancel") {
  switch (action) {
    case "pause":
      return unwrap(await client.POST("/api/downloads/{task_id}/pause", { params: { path: { task_id: taskId } } }));
    case "resume":
      return unwrap(await client.POST("/api/downloads/{task_id}/resume", { params: { path: { task_id: taskId } } }));
    case "retry":
      return unwrap(await client.POST("/api/downloads/{task_id}/retry", { params: { path: { task_id: taskId } } }));
    case "cancel":
      return unwrap(await client.POST("/api/downloads/{task_id}/cancel", { params: { path: { task_id: taskId } } }));
  }
}

export function DownloadJobPage() {
  const { jobId } = useParams({ from: "/downloads/$jobId" });
  const queryClient = useQueryClient();
  const [category, setCategory] = useState<TaskCategory>("downloading");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const jobQuery = useQuery({
    queryKey: ["download-job", jobId],
    queryFn: async () =>
      unwrap(await client.GET("/api/download-jobs/{job_id}", { params: { path: { job_id: jobId } } })),
    refetchInterval: 4000,
  });

  const filesQuery = useInfiniteQuery({
    queryKey: ["job-files", jobId, category, deferredSearch],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await client.GET("/api/download-jobs/{job_id}/files", {
          params: {
            path: { job_id: jobId },
            query: {
              limit: 50,
              cursor: pageParam ?? undefined,
              category,
              q: deferredSearch.trim() || undefined,
            },
          },
        }),
      ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: 4000,
  });

  const action = useMutation({
    mutationFn: ({ taskId, name }: { taskId: string; name: "pause" | "resume" | "retry" | "cancel" }) =>
      taskAction(taskId, name),
    onSuccess: () => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["job-files", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["download-job", jobId] });
    },
    onError: (error) => setActionError(errorMessage(error)),
  });

  const cancelJob = useMutation({
    mutationFn: async () =>
      unwrap(await client.POST("/api/download-jobs/{job_id}/cancel", { params: { path: { job_id: jobId } } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["download-job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job-files", jobId] });
    },
    onError: (error) => setActionError(errorMessage(error)),
  });

  const retryJob = useMutation({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/download-jobs/{job_id}/retry", {
          params: { path: { job_id: jobId } },
        }),
      ),
    onSuccess: () => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["download-job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job-files", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
    },
    onError: (error) => setActionError(errorMessage(error)),
  });

  const job = jobQuery.data;
  const tasks = filesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const initializedCategory = useRef(false);

  useEffect(() => {
    if (!job || initializedCategory.current) return;
    initializedCategory.current = true;
    if (job.status === "COMPLETED" || job.status === "COMPLETED_WITH_ERRORS") {
      setCategory("success");
    } else if (job.status === "FAILED" || job.status === "CANCELLED") {
      setCategory("failed");
    } else if ((job.active_files ?? 0) === 0 && (job.queued_files ?? 0) > 0) {
      setCategory("queued");
    }
  }, [job]);

  if (jobQuery.isLoading) {
    return <Text color="gray">加载中…</Text>;
  }
  if (!job) {
    return <ErrorBanner message={jobQuery.isError ? errorMessage(jobQuery.error) : "任务不存在"} />;
  }

  const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"].includes(job.status);
  const categoryCounts: Record<TaskCategory, number> = {
    downloading: job.active_files ?? 0,
    queued: job.queued_files ?? 0,
    failed: (job.failed_files ?? 0) + (job.cancelled_files ?? 0),
    success: job.completed_files ?? 0,
  };

  return (
    <>
      <PageHeader
        title={job.root_name || job.share_id}
        description={`分享 ${job.share_id} · 位置 /${job.target_subdir || ""} · 任务 ${job.id}`}
        actions={
          <>
            <Button variant="soft" color="gray" asChild>
              <a href="/downloads">
                <ArrowLeft size={15} aria-hidden />
                返回
              </a>
            </Button>
            {(job.failed_files > 0 || job.status === "FAILED" || job.status === "COMPLETED_WITH_ERRORS") ? (
              <Button
                variant="soft"
                color="blue"
                onClick={() => retryJob.mutate()}
                disabled={retryJob.isPending}
              >
                <RotateCcw size={14} aria-hidden />
                {retryJob.isPending ? "重试中" : "一键重试失败文件"}
              </Button>
            ) : null}
            {!terminal ? (
              <ConfirmButton
                label="取消任务"
                title="取消整个下载任务？"
                description="将停止所有文件下载并清理临时文件；已转存到云盘的文件会尽量删除。"
                confirmLabel="取消任务"
                onConfirm={() => cancelJob.mutate()}
              >
                <X size={14} aria-hidden /> 取消
              </ConfirmButton>
            ) : null}
          </>
        }
      />

      <ErrorBanner message={actionError} />
      <ErrorBanner message={job.error_message} />

      <div className="grid-cards" style={{ marginBottom: 20 }}>
        <Card size="2">
          <Text size="1" color="gray">
            状态
          </Text>
          <Flex align="center" gap="2" mt="1">
            <StatusBadge status={job.status} />
          </Flex>
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            文件完成
          </Text>
          <Text as="div" size="6" weight="bold">
            {job.completed_files}/{job.total_files}
          </Text>
          <Progress value={percent(job.completed_files, job.total_files)} aria-label="文件完成进度" />
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            目录完成
          </Text>
          <Text as="div" size="6" weight="bold">
            {job.folder_done}/{job.folder_total}
          </Text>
          <Progress value={percent(job.folder_done, job.folder_total)} aria-label="目录枚举进度" />
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            已下载 / 总量
          </Text>
          <Text as="div" size="5" weight="bold">
            {formatBytes(job.downloaded_bytes)}
          </Text>
          <Text size="1" color="gray">
            共 {formatBytes(job.total_bytes)}
          </Text>
        </Card>
      </div>

      <div className="job-tabs" role="tablist" aria-label="文件分类">
        {TASK_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={category === tab.key}
            className={`job-tab${category === tab.key ? " is-active" : ""}`}
            onClick={() => {
              setCategory(tab.key);
              setExpanded(null);
            }}
          >
            {tab.label}
            <span>{categoryCounts[tab.key]}</span>
          </button>
        ))}
      </div>

      <Card size="2" mb="4">
        <Flex gap="3" wrap="wrap" align="end">
          <label style={{ minWidth: 220 }}>
            <Text as="div" size="1" color="gray" mb="1">
              搜索文件名
            </Text>
            <TextField.Root
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="输入文件名"
              type="search"
            />
          </label>
          <Badge variant="soft" color="gray">
            当前 {tasks.length} 项
          </Badge>
        </Flex>
      </Card>

      <ErrorBanner message={filesQuery.isError ? errorMessage(filesQuery.error) : null} />

      <Card size="2" style={{ padding: 0 }}>
        <div className="table-scroll">
          <Table.Root variant="surface">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeaderCell>文件</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>状态</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>进度</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>路线 / 账号</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>大小</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>操作</Table.ColumnHeaderCell>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {tasks.map((task) => {
                const isExpanded = expanded === task.id;
                return (
                  <TaskRows
                    key={task.id}
                    task={task}
                    isExpanded={isExpanded}
                    onToggle={() => setExpanded(isExpanded ? null : task.id)}
                    onAction={(name) => action.mutate({ taskId: task.id, name })}
                    pending={action.isPending}
                  />
                );
              })}
              {tasks.length === 0 && !filesQuery.isLoading ? (
                <Table.Row>
                  <Table.Cell colSpan={6}>
                    <Text color="gray">没有符合条件的文件。</Text>
                  </Table.Cell>
                </Table.Row>
              ) : null}
            </Table.Body>
          </Table.Root>
        </div>
        {filesQuery.hasNextPage ? (
          <Flex justify="center" p="3">
            <Button
              variant="soft"
              onClick={() => void filesQuery.fetchNextPage()}
              disabled={filesQuery.isFetchingNextPage}
            >
              {filesQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
            </Button>
          </Flex>
        ) : null}
      </Card>
    </>
  );
}

function TaskRows({
  task,
  isExpanded,
  onToggle,
  onAction,
  pending,
}: {
  task: DownloadTask;
  isExpanded: boolean;
  onToggle: () => void;
  onAction: (name: "pause" | "resume" | "retry" | "cancel") => void;
  pending: boolean;
}) {
  const progress = percent(task.bytes_done, task.size);
  return (
    <>
      <Table.Row className="row-link" onClick={onToggle} tabIndex={0} onKeyDown={(event) => event.key === "Enter" && onToggle()}>
        <Table.Cell>
          <Text as="div" weight="medium">
            {task.name}
          </Text>
          <Text as="div" size="1" color="gray" className="mono">
            {task.target_path || task.relative_path || task.share_file_id}
          </Text>
        </Table.Cell>
        <Table.Cell>
          <StatusBadge status={task.status} />
          {task.queue_reason ? (
            <Text as="div" size="1" color="orange" mt="1">
              {task.queue_reason}
            </Text>
          ) : null}
        </Table.Cell>
        <Table.Cell style={{ minWidth: 160 }}>
          <Progress value={progress} aria-label={`${task.name} 下载进度`} />
          <Text as="div" size="1" color="gray" mt="1">
            {progress}% · {formatBytes(task.bytes_done)} / {formatBytes(task.size)}
            {task.speed ? ` · ${formatSpeed(task.speed)}` : ""}
          </Text>
        </Table.Cell>
        <Table.Cell>
          <Badge
            variant="soft"
            color={
              task.route === "tar"
                ? "violet"
                : task.route === "account"
                  ? "orange"
                  : task.route === "anonymous"
                    ? "blue"
                    : "gray"
            }
          >
            {task.route === "tar" ? "tar 通道" : task.route ?? "未规划"}
          </Badge>
          {task.account_label ? (
            <Text as="div" size="1" color="gray" mt="1">
              {task.account_label}
            </Text>
          ) : null}
        </Table.Cell>
        <Table.Cell>{formatBytes(task.size)}</Table.Cell>
        <Table.Cell>
          <Flex gap="1" wrap="wrap" onClick={(event) => event.stopPropagation()}>
            {["DISCOVERED", "PLANNING", "ACCOUNT_QUEUED", "ANON_DOWNLOADING", "ACCOUNT_DOWNLOADING"].includes(task.status) ? (
              <Button size="1" variant="soft" color="amber" disabled={pending} onClick={() => onAction("pause")}>
                <Pause size={13} aria-hidden /> 暂停
              </Button>
            ) : null}
            {task.status === "PAUSED" ? (
              <Button size="1" variant="soft" color="blue" disabled={pending} onClick={() => onAction("resume")}>
                <Play size={13} aria-hidden /> 继续
              </Button>
            ) : null}
            {["FAILED", "CANCELLED"].includes(task.status) ? (
              <Button size="1" variant="soft" color="blue" disabled={pending} onClick={() => onAction("retry")}>
                <RotateCcw size={13} aria-hidden /> 重试
              </Button>
            ) : null}
            {task.status === "DONE" ? (
              <Button size="1" variant="soft" color="green" asChild>
                <a href={`/api/downloads/${task.id}/file`} onClick={(event) => event.stopPropagation()}>
                  <Download size={13} aria-hidden /> 保存
                </a>
              </Button>
            ) : null}
            {!["DONE", "CANCELLED"].includes(task.status) ? (
              <Button size="1" variant="soft" color="red" disabled={pending} onClick={() => onAction("cancel")}>
                <X size={13} aria-hidden /> 取消
              </Button>
            ) : null}
          </Flex>
        </Table.Cell>
      </Table.Row>
      {isExpanded ? (
        <Table.Row className="expanded-row">
          <Table.Cell colSpan={6}>
            <div className="detail-grid">
              <div>
                <Text as="div" size="1" color="gray" mb="1">
                  指纹
                </Text>
                <div className="mono">{task.expected_hash || "—"}</div>
                <Text as="div" size="1" color="gray" mt="2">
                  校验分块：{task.verified_chunk ?? "—"}
                </Text>
                <Text as="div" size="1" color="gray">
                  尝试：{task.attempts} · 校验重试：{task.verify_attempts} · 下载周期：{task.download_cycles}
                </Text>
              </div>
              <div>
                <Text as="div" size="1" color="gray" mb="1">
                  账号路线
                </Text>
                <Text as="div" size="1">
                  账号：{task.account_label ?? "—"}
                </Text>
                <Text as="div" size="1">
                  云盘文件：<span className="mono">{task.drive_file_id ?? "—"}</span>
                </Text>
                <Text as="div" size="1">
                  云盘删除：{formatDate(task.remote_deleted_at)}
                </Text>
                <Text as="div" size="1">
                  直链过期：{task.source_url_expire ? formatDate(new Date(task.source_url_expire * 1000).toISOString()) : "—"}
                </Text>
              </div>
              <div>
                <Text as="div" size="1" color="gray" mb="1">
                  时间线
                </Text>
                <dl className="timeline">
                  <dt>创建</dt>
                  <dd>{formatDate(task.created_at)}</dd>
                  <dt>开始</dt>
                  <dd>{formatDate(task.started_at)}</dd>
                  <dt>完成</dt>
                  <dd>{formatDate(task.finished_at)}</dd>
                  <dt>更新时间</dt>
                  <dd>{formatDate(task.finished_at ?? task.started_at ?? task.created_at)}</dd>
                </dl>
              </div>
              <div>
                <Text as="div" size="1" color="gray" mb="1">
                  错误
                </Text>
                <Text as="div" size="1" color={task.error_code ? "red" : "gray"}>
                  {task.error_code ? `${task.error_code}: ${task.error_message ?? ""}` : "无"}
                </Text>
                <Text as="div" size="1" color="gray" mt="2">
                  来源：{task.source_share_id ?? "—"} / {task.source_file_id ?? "—"}
                </Text>
              </div>
            </div>
          </Table.Cell>
        </Table.Row>
      ) : null}
    </>
  );
}
