import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, Checkbox, Flex, Table, Text } from "@radix-ui/themes";
import { ChevronRight, File, Folder, Search, X } from "lucide-react";
import {
  client,
  errorMessage,
  formatBytes,
  unwrap,
  type PreviewItem,
  type SelectionItem,
  type SharePreview,
} from "../api/client";
import { ErrorBanner } from "./ErrorBanner";
import { StatusBadge } from "./StatusBadge";

interface Crumb {
  id: string | null;
  name: string;
}

function joinPath(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

interface Props {
  url: string;
  passCode: string;
  onSelectionChange: (items: SelectionItem[]) => void;
}

export function LineageSharePicker({ url, passCode, onSelectionChange }: Props) {
  const [preview, setPreview] = useState<SharePreview | null>(null);
  const [breadcrumbs, setBreadcrumbs] = useState<Crumb[]>([]);
  const [selected, setSelected] = useState<Map<string, SelectionItem>>(new Map());
  const [loading, setLoading] = useState(false);
  const [folderLoading, setFolderLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPreview(null);
    setBreadcrumbs([]);
    setSelected(new Map());
    setError(null);
  }, [url, passCode]);

  useEffect(() => {
    onSelectionChange(Array.from(selected.values()));
  }, [selected, onSelectionChange]);

  const parseShare = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: { url: url.trim(), pass_code: passCode.trim() || undefined },
        }),
      )) as SharePreview;
      setPreview(result);
      setSelected(new Map());
      const crumbs: Crumb[] = [{ id: null, name: result.title || result.share_id }];
      if (result.folder_id) {
        crumbs.push({
          id: result.folder_id,
          name: result.folder_name || result.title || "根目录",
        });
      }
      setBreadcrumbs(crumbs);
    } catch (cause) {
      setPreview(null);
      setBreadcrumbs([]);
      setSelected(new Map());
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  };

  const openFolder = async (item: PreviewItem) => {
    if (!preview) return;
    setFolderLoading(true);
    setError(null);
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
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setFolderLoading(false);
    }
  };

  const goCrumb = async (index: number) => {
    const crumb = breadcrumbs[index];
    if (!crumb) return;
    setFolderLoading(true);
    setError(null);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: {
            url: url.trim(),
            pass_code: passCode.trim() || undefined,
            folder_id: crumb.id ?? undefined,
            folder_name: crumb.id ? crumb.name : undefined,
          },
        }),
      )) as SharePreview;
      setPreview(result);
      setBreadcrumbs((current) => current.slice(0, index + 1));
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setFolderLoading(false);
    }
  };

  const loadMore = async () => {
    if (!preview?.next_page_token) return;
    setFolderLoading(true);
    try {
      const result = (await unwrap(
        await client.POST("/api/share-preview", {
          body: {
            url: url.trim(),
            pass_code: passCode.trim() || undefined,
            folder_id: preview.folder_id ?? undefined,
            folder_name: preview.folder_name ?? undefined,
            page_token: preview.next_page_token,
          },
        }),
      )) as SharePreview;
      setPreview({ ...result, items: [...preview.items, ...result.items] });
    } catch (cause) {
      setError(errorMessage(cause));
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

  const selectedItems = Array.from(selected.values());

  return (
    <Card size="2" mb="4">
      <Flex justify="between" align="center" gap="3" wrap="wrap">
        <div>
          <Text as="div" size="2" weight="medium">
            选择要溯源的分享内容
          </Text>
          <Text as="div" size="1" color="gray">
            只展开勾选的文件或文件夹子树，再沿它们的来源继续向上游遍历。
          </Text>
        </div>
        <Button type="button" variant="soft" onClick={() => void parseShare()} disabled={loading || !url.trim()}>
          <Search size={14} aria-hidden />
          {loading ? "解析中" : "解析分享"}
        </Button>
      </Flex>

      <ErrorBanner message={error} />

      {preview ? (
        <>
          <Flex justify="between" align="start" gap="3" wrap="wrap" mt="3" mb="3">
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
                  <Table.ColumnHeaderCell>名称</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>类型</Table.ColumnHeaderCell>
                  <Table.ColumnHeaderCell>大小</Table.ColumnHeaderCell>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {preview.items.map((item) => {
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
                          </div>
                        </Flex>
                      </Table.Cell>
                      <Table.Cell>
                        <Badge variant="soft" color={isFolder ? "amber" : item.is_video ? "violet" : "gray"}>
                          {isFolder ? "文件夹" : item.is_video ? "视频" : "文件"}
                        </Badge>
                      </Table.Cell>
                      <Table.Cell>{isFolder ? "—" : formatBytes(item.size)}</Table.Cell>
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

          {preview.next_page_token ? (
            <Flex justify="center" mt="3">
              <Button variant="soft" onClick={() => void loadMore()} disabled={folderLoading}>
                {folderLoading ? "加载中…" : "加载更多"}
              </Button>
            </Flex>
          ) : null}

          <Text as="div" size="1" color="gray" mt="3">
            已选择 {selectedItems.length} 项；选择文件夹会展开其整个子树。
          </Text>
        </>
      ) : null}
    </Card>
  );
}
