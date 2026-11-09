import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Button, Card, Checkbox, Flex, Table, Tabs, Text, TextField } from "@radix-ui/themes";
import { GitBranch, Plus, Trash2 } from "lucide-react";
import { client, errorMessage, formatDate, unwrap, type LineageRun, type SelectionItem } from "../api/client";
import { ConfirmButton } from "../components/ConfirmButton";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { LineageSharePicker } from "../components/LineageSharePicker";

export function LineagePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState("share");
  const [input, setInput] = useState("");
  const [fileId, setFileId] = useState("");
  const [passCode, setPassCode] = useState("");
  const [hash, setHash] = useState("");
  const [size, setSize] = useState("");
  const [includeTree, setIncludeTree] = useState(true);
  const [probeWindows, setProbeWindows] = useState(false);
  const [graphMode, setGraphMode] = useState(true);
  const [selectedItems, setSelectedItems] = useState<SelectionItem[]>([]);
  const [formError, setFormError] = useState<string | null>(null);

  const runsQuery = useQuery({
    queryKey: ["lineage-runs"],
    queryFn: async () =>
      unwrap(await client.GET("/api/lineage-runs", { params: { query: { limit: 100 } } })),
    refetchInterval: 5000,
  });

  const deleteRun = useMutation({
    mutationFn: async (runId: string) =>
      unwrap(
        await client.DELETE("/api/lineage-runs/{run_id}", {
          params: { path: { run_id: runId } },
        }),
      ),
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const createRun = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        include_tree: includeTree,
        probe_windows: probeWindows,
      };
      if (tab === "fingerprint") {
        body.hash = hash.trim();
        body.size = Number(size);
      } else {
        if (graphMode && selectedItems.length === 0) {
          throw new Error("请先解析分享并勾选要溯源的文件或文件夹");
        }
        body.input = input.trim();
        body.graph = graphMode;
        if (graphMode) body.items = selectedItems;
        if (!graphMode && fileId.trim()) body.file_id = fileId.trim();
        if (passCode.trim()) body.pass_code = passCode.trim();
      }
      return unwrap(await client.POST("/api/lineage-runs", { body: body as never }));
    },
    onSuccess: (run) => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["lineage-runs"] });
      void navigate({ to: "/lineage/$runId", params: { runId: run.id } });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const runs = runsQuery.data ?? [];

  return (
    <>
      <PageHeader
        title="血缘溯源"
        description="先解析分享并勾选文件或文件夹，再沿 original_share_id / original_file_id 逐层去重展开；也可按指纹检索副本与匿名窗口。"
      />

      <Card size="2" mb="5">
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List>
            <Tabs.Trigger value="share">分享 / 文件</Tabs.Trigger>
            <Tabs.Trigger value="fingerprint">内容指纹</Tabs.Trigger>
          </Tabs.List>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              createRun.mutate();
            }}
          >
            <Tabs.Content value="share">
              <Flex direction="column" gap="3" mt="3">
                <label>
                  <Text as="div" size="2" weight="medium" mb="1">
                    分享链接或 share_id
                  </Text>
                  <TextField.Root
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                    placeholder="https://mypikpak.com/s/... 或裸 share_id"
                    required={tab === "share"}
                  />
                </label>
                <Flex gap="3" wrap="wrap">
                  <label style={{ minWidth: 260, flex: 1 }}>
                    <Text as="div" size="2" weight="medium" mb="1">
                      分享内 file_id（可选）
                    </Text>
                    <TextField.Root
                      value={fileId}
                      onChange={(event) => setFileId(event.target.value)}
                      placeholder="留空则使用分享根条目"
                      disabled={graphMode}
                    />
                  </label>
                  <label style={{ minWidth: 180 }}>
                    <Text as="div" size="2" weight="medium" mb="1">
                      提取码（可选）
                    </Text>
                    <TextField.Root
                      value={passCode}
                      onChange={(event) => setPassCode(event.target.value)}
                      placeholder="pwd"
                    />
                  </label>
                </Flex>
              </Flex>
            </Tabs.Content>
            <Tabs.Content value="fingerprint">
              <Flex direction="column" gap="3" mt="3">
                <label>
                  <Text as="div" size="2" weight="medium" mb="1">
                    Merkle-gcid
                  </Text>
                  <TextField.Root
                    value={hash}
                    onChange={(event) => setHash(event.target.value)}
                    placeholder="大写 SHA1"
                    required={tab === "fingerprint"}
                  />
                </label>
                <label>
                  <Text as="div" size="2" weight="medium" mb="1">
                    字节大小
                  </Text>
                  <TextField.Root
                    value={size}
                    onChange={(event) => setSize(event.target.value)}
                    type="number"
                    min="0"
                    required={tab === "fingerprint"}
                  />
                </label>
                <Text size="1" color="gray">
                  指纹模式只检索历史副本索引，不伪造 target 或直系链。
                </Text>
              </Flex>
            </Tabs.Content>

            <Flex gap="4" mt="4" wrap="wrap" align="center">
              <Text as="label" size="2">
                <Flex gap="2" align="center">
                  <Checkbox
                    checked={tab === "share" && graphMode}
                    disabled={tab !== "share"}
                    onCheckedChange={(value) => setGraphMode(value === true)}
                  />
                  按选中内容溯源（推荐）
                </Flex>
              </Text>
              <Text as="label" size="2">
                <Flex gap="2" align="center">
                  <Checkbox
                    checked={includeTree}
                    disabled={tab === "share" && graphMode}
                    onCheckedChange={(value) => setIncludeTree(value === true)}
                  />
                  枚举文件夹树（tenant refs）
                </Flex>
              </Text>
              <Text as="label" size="2">
                <Flex gap="2" align="center">
                  <Checkbox
                    checked={probeWindows}
                    disabled={tab === "share" && graphMode}
                    onCheckedChange={(value) => setProbeWindows(value === true)}
                  />
                  探测匿名窗口（CDN Range）
                </Flex>
              </Text>
              <Button
                type="submit"
                disabled={
                  createRun.isPending ||
                  (tab === "share" && graphMode && selectedItems.length === 0)
                }
                ml="auto"
              >
                <Plus size={15} aria-hidden />
                {createRun.isPending
                  ? "创建中"
                  : tab === "share" && graphMode
                    ? `开始溯源 (${selectedItems.length})`
                    : "开始溯源"}
              </Button>
            </Flex>
          </form>
        </Tabs.Root>
        <ErrorBanner message={formError} />
      </Card>

      {tab === "share" && graphMode ? (
        <LineageSharePicker
          url={input}
          passCode={passCode}
          onSelectionChange={setSelectedItems}
        />
      ) : null}

      <ErrorBanner message={runsQuery.isError ? errorMessage(runsQuery.error) : null} />

      <Card size="2" style={{ padding: 0 }}>
        <div className="table-scroll">
          <Table.Root variant="surface">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeaderCell>输入</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>状态</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>请求 / 条目</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>创建时间</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell style={{ width: 100 }}>操作</Table.ColumnHeaderCell>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {runs.map((run) => (
                <LineageRow
                  key={run.id}
                  run={run}
                  deleting={deleteRun.isPending}
                  onDelete={() => deleteRun.mutate(run.id)}
                  onOpen={() => void navigate({ to: "/lineage/$runId", params: { runId: run.id } })}
                />
              ))}
              {runs.length === 0 && !runsQuery.isLoading ? (
                <Table.Row>
                  <Table.Cell colSpan={5}>
                    <Flex align="center" gap="2" py="4" justify="center">
                      <GitBranch size={16} aria-hidden />
                      <Text color="gray">还没有溯源记录。</Text>
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

const TERMINAL_RUNS = new Set(["DONE", "FAILED", "CANCELLED"]);

function LineageRow({
  run,
  deleting,
  onDelete,
  onOpen,
}: {
  run: LineageRun;
  deleting: boolean;
  onDelete: () => void;
  onOpen: () => void;
}) {
  return (
    <Table.Row
      className="row-link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => event.key === "Enter" && onOpen()}
    >
      <Table.Cell>
        <Text as="div" weight="medium">
          {run.input_type === "fingerprint"
            ? "内容指纹"
            : run.input_type === "share_graph"
              ? `血缘图 · ${run.share_id}`
              : run.share_id}
        </Text>
        <Text as="div" size="1" color="gray" className="mono">
          {run.file_id ?? run.hash ?? run.id}
        </Text>
      </Table.Cell>
      <Table.Cell>
        <StatusBadge status={run.status} />
        {run.truncated ? (
          <Text as="div" size="1" color="amber" mt="1">
            预算截断
          </Text>
        ) : null}
      </Table.Cell>
      <Table.Cell>
        {run.requests} / {run.items_enumerated}
      </Table.Cell>
      <Table.Cell>
        <Text size="1" color="gray">
          {formatDate(run.created_at)}
        </Text>
      </Table.Cell>
      <Table.Cell>
        <div
          className="row-actions"
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <ConfirmButton
            title="删除溯源记录？"
            description="会删除该运行的节点、快照、证据文件与图数据；正在运行的任务需要先取消。"
            confirmLabel="删除"
            disabled={deleting || !TERMINAL_RUNS.has(run.status)}
            onConfirm={onDelete}
          >
            <Trash2 size={14} aria-hidden /> 删除
          </ConfirmButton>
        </div>
      </Table.Cell>
    </Table.Row>
  );
}
