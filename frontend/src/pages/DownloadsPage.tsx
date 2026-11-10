import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Flex,
  Table,
  Text,
  TextField,
} from "@radix-ui/themes";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronRight,
  Download,
  File,
  Folder,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import {
  client,
  errorMessage,
  formatBytes,
  percent,
  unwrap,
  type DownloadJob,
  type PreviewItem,
  type SelectionItem,
  type SharePreview,
} from "../api/client";
import { ConfirmButton } from "../components/ConfirmButton";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";

interface Crumb {
  id: string | null;
  name: string;
}

interface DownloadTargets {
  root: string;
  items: { path: string; label: string }[];
}

type SortKey = "name" | "kind" | "size";
type SortDir = "asc" | "desc";

function compareItems(a: PreviewItem, b: PreviewItem, key: SortKey): number {
  if (key === "name") {
    return a.name.localeCompare(b.name, "zh-CN", {
      numeric: true,
      sensitivity: "base",
    });
  }
  if (key === "kind") {
    const rank = (item: PreviewItem) =>
      item.kind === "drive#folder" ? 0 : item.is_video ? 1 : 2;
    return rank(a) - rank(b);
  }
  const aFolder = a.kind === "drive#folder";
  const bFolder = b.kind === "drive#folder";
  if (aFolder !== bFolder) return aFolder ? 1 : -1;
  return (a.size ?? 0) - (b.size ?? 0);
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
}) {
  const Icon = active ? (dir === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
  return (
    <button
      type="button"
      className={`sort-header${active ? " is-active" : ""}`}
      onClick={onClick}
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
    >
      {label}
      <Icon size={12} aria-hidden />
    </button>
  );
}

function joinPath(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

export function DownloadsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [passCode, setPassCode] = useState("");
  const [preview, setPreview] = useState<SharePreview | null>(null);
  const [rootPreview, setRootPreview] = useState<SharePreview | null>(null);
  const [breadcrumbs, setBreadcrumbs] = useState<Crumb[]>([]);
  const [rootBreadcrumbs, setRootBreadcrumbs] = useState<Crumb[]>([]);
  const [selected, setSelected] = useState<Map<string, SelectionItem>>(new Map());
  const [formError, setFormError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [folderLoading, setFolderLoading] = useState(false);
  const [targetSubdir, setTargetSubdir] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const targetsQuery = useQuery({
    queryKey: ["download-targets"],
    queryFn: async () =>
      (await unwrap(await client.GET("/api/download-targets", {}))) as unknown as DownloadTargets,
    staleTime: 15000,
  });

  const jobsQuery = useQuery({
    queryKey: ["download-jobs"],
    queryFn: async () =>
      unwrap(await client.GET("/api/download-jobs", { params: { query: { limit: 100 } } })),
    refetchInterval: 5000,
  });

  const deleteJob = useMutation({
    mutationFn: async (jobId: string) =>
      unwrap(
        await client.DELETE("/api/download-jobs/{job_id}", {
          params: { path: { job_id: jobId } },
        }),
      ),
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const createJob = useMutation({
    mutationFn: async () => {
      const items = Array.from(selected.values());
      if (items.length === 0) throw new Error("请先选择要下载的文件或文件夹");
      return unwrap(
        await client.POST("/api/download-jobs", {
          body: {
            url: url.trim(),
            pass_code: passCode.trim() || undefined,
            items,
            target_subdir: targetSubdir.trim() || undefined,
          },
        }),
      );
    },
    onSuccess: (job) => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["download-jobs"] });
      void navigate({ to: "/downloads/$jobId", params: { jobId: job.id } });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const resetPreview = () => {
    setPreview(null);
    setRootPreview(null);
    setBreadcrumbs([]);
    setRootBreadcrumbs([]);
    setSelected(new Map());
  };

  const parseShare = async () => {
    setPreviewLoading(true);
    setFormError(null);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: { url: url.trim(), pass_code: passCode.trim() || undefined },
        }),
      )) as SharePreview;
      setPreview(result);
      setRootPreview(result);
      setSelected(new Map());
      if (result.folder_id) {
        const crumbs = [
          { id: null, name: result.title || result.share_id },
          { id: result.folder_id, name: result.folder_name || result.title || "根目录" },
        ];
        setBreadcrumbs(crumbs);
        setRootBreadcrumbs(crumbs);
      } else {
        const crumbs = [{ id: null, name: result.title || result.share_id }];
        setBreadcrumbs(crumbs);
        setRootBreadcrumbs(crumbs);
      }
    } catch (error) {
      resetPreview();
      setFormError(errorMessage(error));
    } finally {
      setPreviewLoading(false);
    }
  };

  const openFolder = async (item: PreviewItem) => {
    if (!preview) return;
    setFolderLoading(true);
    setFormError(null);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: {
            url: url.trim(),
            pass_code: passCode.trim() || undefined,
            folder_id: item.id,
            folder_name: item.name,
          },
        }),
      )) as SharePreview;
      setPreview(result);
      setBreadcrumbs((current) => [...current, { id: item.id, name: item.name }]);
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setFolderLoading(false);
    }
  };

  const goCrumb = async (index: number) => {
    if (index === 0) {
      if (rootPreview) {
        setPreview(rootPreview);
        setBreadcrumbs(rootBreadcrumbs);
      }
      return;
    }
    const crumb = breadcrumbs[index];
    if (!crumb.id) return;
    setFolderLoading(true);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: {
            url: url.trim(),
            pass_code: passCode.trim() || undefined,
            folder_id: crumb.id,
            folder_name: crumb.name,
          },
        }),
      )) as SharePreview;
      setPreview(result);
      setBreadcrumbs((current) => current.slice(0, index + 1));
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setFolderLoading(false);
    }
  };

  const currentPath = useMemo(
    () =>
      breadcrumbs
        .filter((crumb) => crumb.id !== null)
        .map((crumb) => crumb.name)
        .join("/"),
    [breadcrumbs],
  );

  const toggleItem = (item: PreviewItem) => {
    setSelected((previous) => {
      const next = new Map(previous);
      if (next.has(item.id)) {
        next.delete(item.id);
      } else {
        next.set(item.id, {
          file_id: item.id,
          kind: item.kind,
          name: item.name,
          relative_path: joinPath(currentPath, item.name),
          size: item.size,
        });
      }
      return next;
    });
  };

  const allCurrentSelected =
    Boolean(preview?.items.length) &&
    preview!.items.every((item) => selected.has(item.id));

  const toggleAll = () => {
    if (!preview) return;
    setSelected((previous) => {
      const next = new Map(previous);
      if (allCurrentSelected) {
        for (const item of preview.items) next.delete(item.id);
      } else {
        for (const item of preview.items) {
          next.set(item.id, {
            file_id: item.id,
            kind: item.kind,
            name: item.name,
            relative_path: joinPath(currentPath, item.name),
            size: item.size,
          });
        }
      }
      return next;
    });
  };

  const sortedItems = useMemo(() => {
    const items = [...(preview?.items ?? [])];
    items.sort((a, b) => {
      const result = compareItems(a, b, sortKey);
      return sortDir === "asc" ? result : -result;
    });
    return items;
  }, [preview, sortKey, sortDir]);

  const changeSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  const selectedItems = Array.from(selected.values());
  const selectedBytes = selectedItems.reduce((sum, item) => sum + (item.size ?? 0), 0);
  const canDownload = Boolean(
    preview && preview.share_status === "OK" && selectedItems.length > 0,
  );

  const jobs = jobsQuery.data ?? [];
  const active = jobs.filter(
    (job) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"].includes(job.status),
  );
  const completed = jobs.filter(
    (job) => job.status === "COMPLETED" || job.status === "COMPLETED_WITH_ERRORS",
  );
  const totalBytes = jobs.reduce((sum, job) => sum + (job.total_bytes ?? 0), 0);

  const openJob = (job: DownloadJob) => {
    void navigate({ to: "/downloads/$jobId", params: { jobId: job.id } });
  };

  return (
    <>
      <PageHeader
        title="下载"
        description="先解析分享内容，勾选需要的文件或文件夹，再创建下载任务。不会默认下载整个文件夹。"
      />

      <Card size="2" mb="4">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void parseShare();
          }}
        >
          <div className="form-grid">
            <label>
              <Text as="div" size="2" weight="medium" mb="1">
                分享链接
              </Text>
              <TextField.Root
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  resetPreview();
                }}
                placeholder="https://mypikpak.com/s/..."
                required
                type="url"
                autoComplete="off"
              />
            </label>
            <Flex gap="2" align="end">
              <label style={{ minWidth: 150 }}>
                <Text as="div" size="2" weight="medium" mb="1">
                  提取码（可选）
                </Text>
                <TextField.Root
                  value={passCode}
                  onChange={(event) => {
                    setPassCode(event.target.value);
                    resetPreview();
                  }}
                  placeholder="pwd"
                  autoComplete="off"
                />
              </label>
              <Button type="submit" disabled={previewLoading || !url.trim()}>
                <Plus size={15} aria-hidden />
                {previewLoading ? "解析中" : "解析分享"}
              </Button>
            </Flex>
          </div>
          <ErrorBanner message={formError} />
        </form>
      </Card>

      {preview ? (
        <Card size="2" mb="4">
          <Flex justify="between" align="start" gap="3" wrap="wrap" mb="3">
            <div style={{ minWidth: 0 }}>
              <Flex align="center" gap="2" wrap="wrap">
                <Text weight="bold" size="3">
                  {preview.title || preview.share_id}
                </Text>
                <StatusBadge status={preview.share_status} />
                <Badge variant="soft" color="gray">
                  {preview.items.length} 项
                </Badge>
              </Flex>
              <Text as="div" size="1" color="gray" className="mono" mt="1">
                {preview.share_id}
              </Text>
              {preview.share_status !== "OK" ? (
                <Text as="div" size="1" color="red" mt="1">
                  {preview.share_status_text || "分享不可用"}
                </Text>
              ) : null}
            </div>
            <Flex gap="2" wrap="wrap">
              <Button
                size="2"
                variant="soft"
                color="gray"
                onClick={toggleAll}
                disabled={preview.items.length === 0}
              >
                {allCurrentSelected ? "取消全选" : "全选当前目录"}
              </Button>
              <Button
                size="2"
                variant="soft"
                color="gray"
                onClick={() => setSelected(new Map())}
                disabled={selectedItems.length === 0}
              >
                <X size={14} aria-hidden /> 清空
              </Button>
              <Button
                size="2"
                onClick={() => createJob.mutate()}
                disabled={!canDownload || createJob.isPending}
              >
                <Download size={15} aria-hidden />
                {createJob.isPending
                  ? "创建中"
                  : `下载选中 (${selectedItems.length})`}
              </Button>
            </Flex>
          </Flex>

          <Flex align="center" gap="1" wrap="wrap" mb="3" aria-label="目录路径">
            {breadcrumbs.map((crumb, index) => (
              <Flex key={`${crumb.id ?? "root"}-${index}`} align="center" gap="1">
                {index > 0 ? <ChevronRight size={14} color="var(--gray-9)" aria-hidden /> : null}
                <Button
                  size="1"
                  variant="ghost"
                  color={index === breadcrumbs.length - 1 ? "gray" : "blue"}
                  onClick={() => void goCrumb(index)}
                  disabled={folderLoading}
                >
                  {crumb.name}
                </Button>
              </Flex>
            ))}
          </Flex>

          <div className="table-scroll">
            <Table.Root variant="surface" className="preview-table">
              <Table.Header>
                <Table.Row>
                  <Table.ColumnHeaderCell style={{ width: 42 }}>
                    <Checkbox
                      checked={allCurrentSelected}
                      onCheckedChange={toggleAll}
                      aria-label="全选当前目录"
                    />
                  </Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>
                    <SortHeader
                      label="名称"
                      active={sortKey === "name"}
                      dir={sortDir}
                      onClick={() => changeSort("name")}
                    />
                  </Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>
                    <SortHeader
                      label="类型"
                      active={sortKey === "kind"}
                      dir={sortDir}
                      onClick={() => changeSort("kind")}
                    />
                  </Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>
                    <SortHeader
                      label="大小"
                      active={sortKey === "size"}
                      dir={sortDir}
                      onClick={() => changeSort("size")}
                    />
                  </Table.ColumnHeaderCell>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {sortedItems.map((item) => {
                  const isFolder = item.kind === "drive#folder";
                  return (
                    <Table.Row key={item.id}>
                      <Table.Cell>
                        <Checkbox
                          checked={selected.has(item.id)}
                          onCheckedChange={() => toggleItem(item)}
                          aria-label={`选择 ${item.name}`}
                        />
                      </Table.Cell>
                      <Table.Cell>
                        <Flex align="center" gap="2">
                          {isFolder ? (
                            <Folder size={16} color="var(--amber-10)" aria-hidden />
                          ) : (
                            <File size={16} color="var(--gray-10)" aria-hidden />
                          )}
                          <div style={{ minWidth: 0 }}>
                            {isFolder ? (
                              <button
                                type="button"
                                className="link-button"
                                onClick={() => void openFolder(item)}
                                disabled={folderLoading}
                              >
                                {item.name}
                              </button>
                            ) : (
                              <Text as="div" weight="medium">
                                {item.name}
                              </Text>
                            )}
                            {item.hash ? (
                              <Text as="div" size="1" color="gray" className="mono preview-hash">
                                {item.hash}
                              </Text>
                            ) : null}
                            {!isFolder && item.mime_type ? (
                              <Text as="div" size="1" color="gray">
                                {item.mime_type}
                              </Text>
                            ) : null}
                          </div>
                        </Flex>
                      </Table.Cell>
                      <Table.Cell>
                        <Badge variant="soft" color={isFolder ? "amber" : item.is_video ? "violet" : "gray"}>
                          {isFolder ? "文件夹" : item.is_video ? "视频" : "文件"}
                        </Badge>
                      </Table.Cell>
                      <Table.Cell>
                        {isFolder ? (
                          <Text size="1" color="gray" title="PikPak 不返回文件夹大小；需要递归枚举才能统计">
                            —
                          </Text>
                        ) : (
                          formatBytes(item.size)
                        )}
                      </Table.Cell>
                    </Table.Row>
                  );
                })}
                {preview.items.length === 0 ? (
                  <Table.Row>
                    <Table.Cell colSpan={4}>
                      <Text color="gray" size="2">
                        当前目录为空。
                      </Text>
                    </Table.Cell>
                  </Table.Row>
                ) : null}
              </Table.Body>
            </Table.Root>
          </div>

          <div className="target-picker">
            <label>
              <Text as="div" size="2" weight="medium" mb="1">
                下载位置
              </Text>
              <TextField.Root
                value={targetSubdir}
                onChange={(event) => setTargetSubdir(event.target.value)}
                placeholder="留空为根目录，或输入子目录，如 Movies/2026"
                list="download-targets"
                autoComplete="off"
              />
              <datalist id="download-targets">
                {(targetsQuery.data?.items ?? []).map((item) => (
                  <option key={item.path || "root"} value={item.path}>
                    {item.label}
                  </option>
                ))}
              </datalist>
            </label>
            <Text as="div" size="1" color="gray" mt="1">
              相对挂载目录 {targetsQuery.data?.root ?? "/downloads"}；目录不存在会自动创建，分享的文件夹结构会保留在下面。
            </Text>
          </div>

          <Flex justify="between" align="center" gap="3" wrap="wrap" mt="3">
            <Text size="1" color="gray">
              已选择 {selectedItems.length} 项
              {selectedBytes > 0 ? ` · 文件大小约 ${formatBytes(selectedBytes)}` : ""}
            </Text>
            <Text size="1" color="gray">
              选择文件夹会下载其整个子树；文件与文件夹可混合选择。
            </Text>
          </Flex>
        </Card>
      ) : (
        <Card size="2" mb="4">
          <Text color="gray" size="2">
            输入分享链接后点击“解析分享”，这里会展示文件与文件夹，勾选后再下载。
          </Text>
        </Card>
      )}

      <div className="grid-cards" style={{ marginBottom: 20 }}>
        <Card size="2">
          <Text size="1" color="gray">
            任务总数
          </Text>
          <Text as="div" size="6" weight="bold">
            {jobs.length}
          </Text>
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            进行中
          </Text>
          <Text as="div" size="6" weight="bold">
            {active.length}
          </Text>
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            已完成
          </Text>
          <Text as="div" size="6" weight="bold">
            {completed.length}
          </Text>
        </Card>
        <Card size="2">
          <Text size="1" color="gray">
            声明总量
          </Text>
          <Text as="div" size="6" weight="bold">
            {formatBytes(totalBytes)}
          </Text>
        </Card>
      </div>

      <ErrorBanner message={jobsQuery.isError ? errorMessage(jobsQuery.error) : null} />

      <Card size="2" style={{ padding: 0 }}>
        <div className="table-scroll">
          <Table.Root variant="surface">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeaderCell>分享 / 根名称</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>状态</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>文件进度</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>字节</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>创建时间</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell style={{ width: 100 }}>操作</Table.ColumnHeaderCell>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {jobs.map((job) => (
                <Table.Row
                  key={job.id}
                  className="row-link"
                  tabIndex={0}
                  onClick={() => openJob(job)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") openJob(job);
                  }}
                >
                  <Table.Cell>
                    <Text as="div" weight="medium">
                      {job.root_name || job.share_id}
                    </Text>
                    <Text as="div" size="1" color="gray" className="mono">
                      {job.share_id}
                    </Text>
                    <Text as="div" size="1" color="gray">
                      位置 /{job.target_subdir || ""}
                    </Text>
                  </Table.Cell>
                  <Table.Cell>
                    <StatusBadge status={job.status} />
                    {job.error_code ? (
                      <Text as="div" size="1" color="red" mt="1">
                        {job.error_code}
                      </Text>
                    ) : null}
                  </Table.Cell>
                  <Table.Cell>
                    <Flex align="center" gap="2">
                      <Badge
                        variant="soft"
                        color={
                          job.completed_files === job.total_files && job.total_files > 0
                            ? "green"
                            : "gray"
                        }
                      >
                        {job.completed_files}/{job.total_files}
                      </Badge>
                      <Text size="1" color="gray">
                        {percent(job.completed_files, job.total_files)}%
                      </Text>
                    </Flex>
                    {job.folder_total > 0 ? (
                      <Text as="div" size="1" color="gray" mt="1">
                        目录 {job.folder_done}/{job.folder_total}
                      </Text>
                    ) : null}
                  </Table.Cell>
                  <Table.Cell>
                    <Text size="2">{formatBytes(job.downloaded_bytes)}</Text>
                    <Text as="div" size="1" color="gray">
                      / {formatBytes(job.total_bytes)}
                    </Text>
                  </Table.Cell>
                  <Table.Cell>
                    <Text size="1" color="gray">
                      {job.created_at
                        ? new Date(job.created_at).toLocaleString("zh-CN", { hour12: false })
                        : "—"}
                    </Text>
                  </Table.Cell>
                  <Table.Cell>
                    <div
                      className="row-actions"
                      onClick={(event) => event.stopPropagation()}
                      onKeyDown={(event) => event.stopPropagation()}
                    >
                      <ConfirmButton
                        title="删除下载任务？"
                        description="会删除任务记录与文件任务；不再被其他任务引用的下载文件也会一并删除，云端待清理项会继续执行。"
                        confirmLabel="删除"
                        disabled={
                          deleteJob.isPending ||
                          !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"].includes(job.status)
                        }
                        onConfirm={() => deleteJob.mutate(job.id)}
                      >
                        <Trash2 size={14} aria-hidden /> 删除
                      </ConfirmButton>
                    </div>
                  </Table.Cell>
                </Table.Row>
              ))}
              {jobs.length === 0 && !jobsQuery.isLoading ? (
                <Table.Row>
                  <Table.Cell colSpan={6}>
                    <Flex align="center" gap="2" py="4" justify="center">
                      <Download size={16} aria-hidden />
                      <Text color="gray">还没有下载任务，解析分享并选择内容开始。</Text>
                    </Flex>
                  </Table.Cell>
                </Table.Row>
              ) : null}
            </Table.Body>
          </Table.Root>
        </div>
      </Card>
    </>
  );
}
