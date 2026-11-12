import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, Flex, Heading, Table, Text, TextField } from "@radix-ui/themes";
import { Eraser, Plus, RefreshCw, Trash2 } from "lucide-react";
import { client, errorMessage, formatBytes, formatDate, unwrap } from "../api/client";
import { ConfirmButton } from "../components/ConfirmButton";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";

export function AccountsPage() {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState("");
  const [email, setEmail] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => unwrap(await client.GET("/api/accounts")),
    refetchInterval: 5000,
  });

  const cleanupsQuery = useQuery({
    queryKey: ["cleanups"],
    queryFn: async () =>
      unwrap(await client.GET("/api/cleanups", { params: { query: { limit: 200 } } })),
    refetchInterval: 5000,
  });

  const importAccount = useMutation({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/accounts", {
          body: {
            label: label.trim(),
            email: email.trim() || undefined,
            refresh_token: refreshToken.trim(),
          },
        }),
      ),
    onSuccess: () => {
      setRefreshToken("");
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const accountAction = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "refresh" | "enable" | "disable" }) => {
      switch (action) {
        case "refresh":
          return unwrap(await client.POST("/api/accounts/{account_id}/refresh", { params: { path: { account_id: id } } }));
        case "enable":
          return unwrap(await client.POST("/api/accounts/{account_id}/enable", { params: { path: { account_id: id } } }));
        case "disable":
          return unwrap(await client.POST("/api/accounts/{account_id}/disable", { params: { path: { account_id: id } } }));
      }
    },
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const emptyTrash = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await client.POST("/api/accounts/{account_id}/trash/empty", {
          params: { path: { account_id: id }, query: { force: true } },
        }),
      ),
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["cleanups"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const deleteAccount = useMutation({
    mutationFn: async (id: string) =>
      unwrap(await client.DELETE("/api/accounts/{account_id}", { params: { path: { account_id: id } } })),
    onSuccess: () => {
      setFormError(null);
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
      void queryClient.invalidateQueries({ queryKey: ["cleanups"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const dispatchCleanups = useMutation({
    mutationFn: async () => unwrap(await client.POST("/api/cleanups/dispatch")),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cleanups"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const accounts = accountsQuery.data ?? [];
  const cleanups = cleanupsQuery.data ?? [];

  return (
    <>
      <PageHeader
        title="账号"
        description="人工注册后导入 refresh_token；应用只保存加密凭据，不保存登录密码，也不自动注册或过验证码。"
      />

      <Card size="2" mb="5">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            importAccount.mutate();
          }}
        >
          <Flex direction="column" gap="3">
            <Flex gap="3" wrap="wrap">
              <label style={{ flex: 1, minWidth: 200 }}>
                <Text as="div" size="2" weight="medium" mb="1">
                  名称
                </Text>
                <TextField.Root
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  placeholder="例如：备用账号 1"
                  required
                  maxLength={120}
                />
              </label>
              <label style={{ flex: 1, minWidth: 200 }}>
                <Text as="div" size="2" weight="medium" mb="1">
                  邮箱（可选）
                </Text>
                <TextField.Root
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  type="email"
                  placeholder="account@example.com"
                />
              </label>
            </Flex>
            <label>
              <Text as="div" size="2" weight="medium" mb="1">
                refresh_token
              </Text>
              <TextField.Root
                value={refreshToken}
                onChange={(event) => setRefreshToken(event.target.value)}
                type="password"
                placeholder="粘贴 refresh_token"
                required
                autoComplete="new-password"
              />
            </label>
            <Flex justify="end">
              <Button type="submit" disabled={importAccount.isPending || !label.trim() || !refreshToken.trim()}>
                <Plus size={15} aria-hidden />
                {importAccount.isPending ? "导入中" : "导入账号"}
              </Button>
            </Flex>
          </Flex>
        </form>
        <ErrorBanner message={formError} />
      </Card>

      <ErrorBanner message={accountsQuery.isError ? errorMessage(accountsQuery.error) : null} />

      <Card size="2" style={{ padding: 0 }}>
        <div className="table-scroll">
          <Table.Root variant="surface">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeaderCell>账号</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>状态</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>配额</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>今日转存</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>最近使用 / 冷却</Table.ColumnHeaderCell>
                <Table.ColumnHeaderCell>操作</Table.ColumnHeaderCell>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {accounts.map((account) => {
                const quotaFree =
                  account.quota_limit != null && account.quota_usage != null
                    ? account.quota_limit - account.quota_usage
                    : null;
                return (
                  <Table.Row key={account.id}>
                    <Table.Cell>
                      <Text as="div" weight="medium">
                        {account.label}
                      </Text>
                      <Text as="div" size="1" color="gray" className="mono">
                        {account.email || account.sub || account.id}
                      </Text>
                      {account.last_error ? (
                        <Text as="div" size="1" color="red" mt="1">
                          {account.last_error}
                        </Text>
                      ) : null}
                    </Table.Cell>
                    <Table.Cell>
                      <StatusBadge status={account.state} />
                      {account.lease_task_id ? (
                        <Text as="div" size="1" color="blue" mt="1">
                          已租用
                        </Text>
                      ) : null}
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="2">{quotaFree != null ? formatBytes(quotaFree) : "未知"}</Text>
                      <Text as="div" size="1" color="gray">
                        已用 {formatBytes(account.quota_usage ?? 0)} / {formatBytes(account.quota_limit ?? 0)}
                      </Text>
                      <Text as="div" size="1" color="gray">
                        云下载 {account.cloud_download_used ?? 0}/{account.cloud_download_limit ?? "—"}
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Badge variant="soft" color="gray">
                        {account.daily_restore_count}
                      </Badge>
                      <Text as="div" size="1" color="gray">
                        {account.daily_date ?? "—"}
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Text as="div" size="1">
                        {formatDate(account.last_used_at)}
                      </Text>
                      {account.cooldown_until ? (
                        <Text as="div" size="1" color="amber">
                          冷却至 {formatDate(account.cooldown_until)}
                        </Text>
                      ) : null}
                    </Table.Cell>
                    <Table.Cell>
                      <Flex gap="1" wrap="wrap">
                        <Button
                          size="1"
                          variant="soft"
                          disabled={accountAction.isPending}
                          onClick={() => accountAction.mutate({ id: account.id, action: "refresh" })}
                        >
                          <RefreshCw size={13} aria-hidden /> 刷新
                        </Button>
                        {account.state === "DISABLED" || account.state === "INVALID" ? (
                          <Button
                            size="1"
                            variant="soft"
                            color="green"
                            disabled={accountAction.isPending}
                            onClick={() => accountAction.mutate({ id: account.id, action: "enable" })}
                          >
                            启用
                          </Button>
                        ) : (
                          <Button
                            size="1"
                            variant="soft"
                            color="amber"
                            disabled={accountAction.isPending}
                            onClick={() => accountAction.mutate({ id: account.id, action: "disable" })}
                          >
                            停用
                          </Button>
                        )}
                        <ConfirmButton
                          title="清空回收站？"
                          description="会永久删除该账号回收站里的全部内容，包括非本应用创建的文件，并释放被占用的配额。"
                          confirmLabel="清空"
                          color="amber"
                          disabled={emptyTrash.isPending}
                          onConfirm={() => emptyTrash.mutate(account.id)}
                        >
                          <Eraser size={13} aria-hidden /> 清空回收站
                        </ConfirmButton>
                        <ConfirmButton
                          title="删除账号？"
                          description="仅无租约且无未完成清理任务时可删除；历史任务保留账号名称快照。"
                          confirmLabel="删除"
                          disabled={deleteAccount.isPending}
                          onConfirm={() => deleteAccount.mutate(account.id)}
                        >
                          <Trash2 size={13} aria-hidden /> 删除
                        </ConfirmButton>
                      </Flex>
                    </Table.Cell>
                  </Table.Row>
                );
              })}
              {accounts.length === 0 && !accountsQuery.isLoading ? (
                <Table.Row>
                  <Table.Cell colSpan={6}>
                    <Text color="gray">尚未导入账号。</Text>
                  </Table.Cell>
                </Table.Row>
              ) : null}
            </Table.Body>
          </Table.Root>
        </div>
      </Card>

      <section className="report-section" aria-labelledby="cleanups">
        <Flex justify="between" align="center" mb="3" gap="3" wrap="wrap">
          <div>
            <Heading as="h2" id="cleanups" size="4">
              云盘清理队列
            </Heading>
            <Text as="p" size="1" color="gray">
              删除失败的云盘文件会单独重试，不与下载结果混在一起。
            </Text>
          </div>
          <Button variant="soft" onClick={() => dispatchCleanups.mutate()} disabled={dispatchCleanups.isPending}>
            <RefreshCw size={14} aria-hidden /> 立即清理
          </Button>
        </Flex>
        <ErrorBanner message={cleanupsQuery.isError ? errorMessage(cleanupsQuery.error) : null} />
        <Card size="1" style={{ padding: 0 }}>
          <div className="table-scroll">
            <Table.Root variant="surface">
              <Table.Header>
                <Table.Row>
                  <Table.ColumnHeaderCell>云盘文件</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>状态</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>尝试</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>下次重试</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>错误</Table.ColumnHeaderCell>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {cleanups.map((cleanup) => (
                  <Table.Row key={cleanup.id}>
                    <Table.Cell className="mono">{cleanup.drive_file_id}</Table.Cell>
                    <Table.Cell>
                      <StatusBadge status={cleanup.state} />
                    </Table.Cell>
                    <Table.Cell>{cleanup.attempts}</Table.Cell>
                    <Table.Cell>{formatDate(cleanup.next_attempt_at)}</Table.Cell>
                    <Table.Cell>
                      <Text size="1" color={cleanup.last_error ? "red" : "gray"}>
                        {cleanup.last_error ?? "—"}
                      </Text>
                    </Table.Cell>
                  </Table.Row>
                ))}
                {cleanups.length === 0 ? (
                  <Table.Row>
                    <Table.Cell colSpan={5}>
                      <Text color="gray">清理队列为空。</Text>
                    </Table.Cell>
                  </Table.Row>
                ) : null}
              </Table.Body>
            </Table.Root>
          </div>
        </Card>
      </section>
    </>
  );
}
