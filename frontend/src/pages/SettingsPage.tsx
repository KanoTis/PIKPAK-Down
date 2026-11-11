import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Card, Flex, Text, TextField } from "@radix-ui/themes";
import { Check, Save, Settings2 } from "lucide-react";
import { client, errorMessage, unwrap } from "../api/client";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageHeader } from "../components/PageHeader";

type DuplicatePolicy = "rename" | "overwrite" | "skip";

interface RuntimeSettings {
  download_max_concurrency: number;
  download_plan_concurrency: number;
  task_connections: number;
  duplicate_name_policy: DuplicatePolicy;
  lineage_max_items: number;
  lineage_max_requests: number;
  lineage_max_shares: number;
  lineage_max_folders: number;
}

type NumericKey = Exclude<keyof RuntimeSettings, "duplicate_name_policy">;

const FIELDS: {
  key: NumericKey;
  label: string;
  hint: string;
  min: number;
  max: number;
  step?: number;
}[] = [
  {
    key: "download_max_concurrency",
    label: "任务并发",
    hint: "同时进行的下载文件数，调低不会中断已开始的任务。",
    min: 1,
    max: 16,
  },
  {
    key: "download_plan_concurrency",
    label: "文件规划并发",
    hint: "同时解析文件元数据的数量；受任务并发槽位限制，建议与任务并发设为相同值。",
    min: 1,
    max: 16,
  },
  {
    key: "task_connections",
    label: "单任务连接数",
    hint: "每个文件使用的 aria2 split / 每服务器连接数；只影响新任务。",
    min: 1,
    max: 16,
  },
  {
    key: "lineage_max_items",
    label: "溯源条目预算",
    hint: "单次血缘运行最多枚举的文件与目录数。",
    min: 1000,
    max: 1_000_000,
    step: 1000,
  },
  {
    key: "lineage_max_requests",
    label: "溯源请求预算",
    hint: "单次运行最多向上游发出的请求数。",
    min: 100,
    max: 100_000,
    step: 100,
  },
  {
    key: "lineage_max_shares",
    label: "溯源分享预算",
    hint: "单次运行最多探测的分享数。",
    min: 10,
    max: 10_000,
  },
  {
    key: "lineage_max_folders",
    label: "溯源目录预算",
    hint: "单次运行最多展开的目录数。",
    min: 100,
    max: 100_000,
    step: 100,
  },
];

function formFrom(data: RuntimeSettings): RuntimeSettings {
  return { ...data };
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ["runtime-settings"],
    queryFn: async () =>
      (await unwrap(
        await client.GET("/api/settings"),
      )) as unknown as RuntimeSettings,
  });
  const [form, setForm] = useState<RuntimeSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQuery.data) {
      setForm(formFrom(settingsQuery.data));
    }
  }, [settingsQuery.data]);

  const save = useMutation({
    mutationFn: async (values: RuntimeSettings) =>
      (await unwrap(
        await client.PATCH("/api/settings", { body: values }),
      )) as unknown as RuntimeSettings,
    onSuccess: (data) => {
      setError(null);
      setSaved(true);
      setForm(formFrom(data));
      queryClient.setQueryData(["runtime-settings"], data);
    },
    onError: (cause) => {
      setSaved(false);
      setError(errorMessage(cause));
    },
  });

  const update = (key: NumericKey, raw: string) => {
    setForm((current) => {
      if (!current) return current;
      const value = Number(raw);
      return { ...current, [key]: Number.isFinite(value) ? value : 0 };
    });
    setSaved(false);
  };

  if (settingsQuery.isLoading || !form) {
    return <Text color="gray">加载中…</Text>;
  }

  return (
    <>
      <PageHeader
        title="设置"
        description="调整下载并发、单任务连接数与血缘预算；保存后立即生效，并写入数据库，容器重启后仍然保留。"
        actions={
          <Button
            onClick={() => save.mutate(form)}
            disabled={save.isPending}
          >
            {save.isPending ? <Save size={15} aria-hidden /> : <Check size={15} aria-hidden />}
            {save.isPending ? "保存中" : "保存设置"}
          </Button>
        }
      />

      <ErrorBanner message={error ?? (settingsQuery.isError ? errorMessage(settingsQuery.error) : null)} />
      {saved ? (
        <div style={{ marginBottom: 16 }}>
          <Callout.Root color="green" size="1">
            <Callout.Icon>
              <Settings2 size={16} aria-hidden />
            </Callout.Icon>
            <Callout.Text>设置已保存并生效。</Callout.Text>
          </Callout.Root>
        </div>
      ) : null}

      <div className="settings-grid">
        <Card size="2">
          <Text as="div" size="3" weight="bold" mb="1">
            下载
          </Text>
          <Text as="div" size="1" color="gray" mb="4">
            控制调度器与 aria2 的并发参数。
          </Text>
          {FIELDS.slice(0, 3).map((field) => (
            <SettingField
              key={field.key}
              field={field}
              value={form[field.key]}
              onChange={(value) => update(field.key, value)}
            />
          ))}
          <label className="settings-field">
            <Text as="div" size="2" weight="medium">
              同名但 MD5 不同
            </Text>
            <select
              className="settings-select"
              value={form.duplicate_name_policy}
              onChange={(event) => {
                setForm((current) =>
                  current
                    ? {
                        ...current,
                        duplicate_name_policy: event.target.value as DuplicatePolicy,
                      }
                    : current,
                );
                setSaved(false);
              }}
            >
              <option value="rename">重命名，保留两份（默认）</option>
              <option value="overwrite">覆盖已有文件</option>
              <option value="skip">跳过，保留已有文件</option>
            </select>
            <Text as="div" size="1" color="gray" mt="1">
              目标目录已有同名文件且内容哈希不同时的处理方式。
            </Text>
          </label>
        </Card>

        <Card size="2">
          <Text as="div" size="3" weight="bold" mb="1">
            血缘预算
          </Text>
          <Text as="div" size="1" color="gray" mb="4">
            预算按层级推进；修改后从下一次检查起生效。
          </Text>
          {FIELDS.slice(3).map((field) => (
            <SettingField
              key={field.key}
              field={field}
              value={form[field.key]}
              onChange={(value) => update(field.key, value)}
            />
          ))}
        </Card>
      </div>
    </>
  );
}

function SettingField({
  field,
  value,
  onChange,
}: {
  field: (typeof FIELDS)[number];
  value: number;
  onChange: (value: string) => void;
}) {
  return (
    <label className="settings-field">
      <Flex justify="between" align="baseline" gap="2">
        <Text as="div" size="2" weight="medium">
          {field.label}
        </Text>
        <Text as="span" size="1" color="gray">
          {field.min}–{field.max}
        </Text>
      </Flex>
      <TextField.Root
        type="number"
        min={field.min}
        max={field.max}
        step={field.step}
        value={String(value)}
        onChange={(event) => onChange(event.target.value)}
        mt="1"
      />
      <Text as="div" size="1" color="gray" mt="1">
        {field.hint}
      </Text>
    </label>
  );
}
