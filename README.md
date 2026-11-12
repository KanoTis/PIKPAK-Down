# PikPak 分享套件

单容器、单用户的 PikPak 分享下载与血缘溯源 Web 应用。

- **下载**：先解析分享并展示全部文件/文件夹（自动翻页，不再需要“加载更多”），可按名称/类型/大小正反排序，默认按名称升序；勾选需要的内容、选择挂载下载目录中的目标位置后再创建任务。同一分享 + 同一目标位置会合并到现有任务，而不是新建；任务页按下载/队列/失败/成功四个分类展示，同一分类内重复分享合并为一行并显示合并任务数；任务子页也按同样四个分类筛选文件。非视频或 `anonymous_play_range=1.00` 的视频走匿名直链；否则从账号池转存、取登录态 `web_content_link` 后**立即删除云盘文件**，再下载并校验 `size + Merkle-gcid`。最终文件按分享文件夹结构写入 `/downloads/<目标子目录>/<相对路径>`，临时分片放在独立的 `/cache/<task-id>/`。
- **血缘**：全量血缘图从分享起点枚举勾选的文件/文件夹；上游分享只解析目标文件通过 `params.original_share_id` / `original_file_id` 指向的具体文件，**不展开同一上游分享里的其他文件/目录**，因此不会把无关的 C 文件继续溯源。聚合文件级证据为分享级边；同时保留单目标直系链、文件夹链、tenant refs、同指纹副本、匿名窗口与终止点。血缘页先解析分享并勾选文件/文件夹，再创建运行；图页面使用 Cytoscape.js 渲染，按深度分层，每个层级一列纵向排列；默认只绘制聚焦线路，悬停节点高亮关联节点（连接过多时不铺线），点击节点打开详情。未展开的分享显示“未展开分享”和来源文件路径，可用“补齐名称”只拉取分享元数据、不展开内容。
- **账号池**：只导入人工取得的 `refresh_token`；不自动注册、不处理验证码、不保存登录密码。tar 打包目录 `pikpak_tar_*` 和转存目录先 `batchTrash` 整棵子树，再 `batchDelete` 永久删除（PIKPAK 对活跃的非空目录直接 `batchDelete` 不会可靠释放配额）；账号配额不足时先清理残留目录再判断 `NO_QUOTA`。应用不会自动清空账号自己的 PIKPAK 回收站，回收站占用配额时需在 PIKPAK 客户端手动清空。

## 快速启动

```bash
python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"
cp .env.example .env   # 填入 APP_SECRET_KEY
mkdir -p downloads cache
docker compose up --build        # 使用当前目录 docker-compose.yml
```

打开 `http://127.0.0.1:8080`。Compose 只绑定 `127.0.0.1`，不映射 aria2 RPC 6800。

默认挂载：

```text
./downloads -> /downloads   最终文件，按分享目录结构
./cache     -> /cache       下载临时分片，完成/取消后清理
pikpak-data -> /data        SQLite、证据、旧对象存储
```

可用 `DOWNLOADS_HOST_DIR` / `CACHE_HOST_DIR` 改宿主路径。Linux 上如果宿主机目录不是 UID 10001 所有，容器入口会尝试 chown；也可以提前执行 `sudo chown -R 10001:10001 downloads cache`。

开发环境如果只想用旧的本机构建配置：

```bash
docker compose -f compose.dev.yaml up --build
```

CI 会把镜像推送到 `ghcr.io/kanotis/pikpak-share-suite`；生产环境可直接拉取：

```bash
docker compose pull && docker compose up -d
```

如果 GHCR 包被设为私有，拉取前需要先 `docker login ghcr.io`。

## 镜像构建（GitHub Actions）

`.github/workflows/docker.yml` 在推送到 `master` / `main`、打 `v*` 标签或手动触发时构建 `linux/amd64` 镜像并推送到 GHCR：

```text
ghcr.io/kanotis/pikpak-share-suite:latest
ghcr.io/kanotis/pikpak-share-suite:master
ghcr.io/kanotis/pikpak-share-suite:sha-<commit>
ghcr.io/kanotis/pikpak-share-suite:<version>   # v1.2.3 标签
```

Pull Request 只构建、不推送。首次推送后到仓库的 Packages 页面把包可见性设为 Private/Public 即可。

镜像为 Alpine 多阶段构建：前端在 Node 阶段编译，Python 依赖在独立 `deps` 阶段用 uv 安装，运行时只保留 venv、后端和静态文件，不包含 Node、uv 或构建工具。本地 amd64 实测从约 344MB 降到约 115MB。

## 本地开发

```bash
uv sync --group dev
pnpm --dir frontend install --frozen-lockfile
export APP_SECRET_KEY="<base64url-32-byte-key>"
export DATA_DIR="./data"
export DOWNLOADS_DIR="./downloads"
export CACHE_DIR="./cache"
PYTHONPATH=backend uv run uvicorn app.main:app --reload --port 8080
pnpm --dir frontend dev
```

生成前端 API 类型：

```bash
PYTHONPATH=backend APP_SECRET_KEY=... DATA_DIR=./data-dev uv run python scripts/dump_openapi.py > frontend/openapi.json
pnpm --dir frontend gen:api
```

## 配置

必填 `APP_SECRET_KEY`（base64url 32 字节）。默认目录：

```text
DATA_DIR=/data           SQLite /data/pikpak.db、证据、旧对象
DOWNLOADS_DIR=/downloads 最终下载目录
CACHE_DIR=/cache         下载临时目录
```

`DUPLICATE_NAME_POLICY`（`rename` / `overwrite` / `skip`，默认 `rename`）、`DOWNLOAD_MAX_CONCURRENCY`、`ARIA2_SPLIT`、`LINEAGE_MAX_*` 等都可省略，代码默认值与 `.env.example` 一致；也可在 `/settings` 页面运行时调整并持久化。

启动时创建 `DATA_DIR`、`DOWNLOADS_DIR`、`CACHE_DIR` 与证据/状态目录，并执行 Alembic migration。只运行一个 Uvicorn worker，调度器与 aria2 都在同一进程生命周期内管理。

## 队列与规划并发

- 调度器每 1 秒触发一次，全局单实例；同一时刻只有一个 dispatch 循环。
- 分享枚举每轮最多处理 5 个任务，逐个执行；每个任务每次最多展开 10 个目录。
- 文件规划每轮最多 `min(download_plan_concurrency, download_max_concurrency - 当前下载中任务数)` 个；这些任务由独立 worker 并行执行，默认规划并发 4、下载并发 2，因此默认同时规划 2 个。把「任务并发」和「文件规划并发」一起调高才会获得完整并行度。
- 账号转存交接每轮最多 3 个，逐个执行。
- API 限速：匿名接口 `ANON_API_QPS=5`，分享探测 `SHARE_PROBE_QPS=2`，Range 探测 `WINDOW_PROBE_QPS=2`。
- 相同内容（size + PikPak hash）已在本地完成过时，规划阶段直接复用本地文件：优先硬链接，失败则复制；不会再发文件下载请求。若同一内容已有任务在规划/下载/校验，其他任务会进入 `DUPLICATE_WAITING`，等首个任务完成后本地复用，避免同一内容并发下载多份。

大量重复文件优先按以下顺序调优：

1. 设置页把「任务并发」和「文件规划并发」都提高到 4–8（最多 16）。规划并发受下载槽位限制，两者一起调才有意义。
2. 如果瓶颈在元数据请求，调高 `ANON_API_QPS`，例如 10–20；仍被限流再回退。
3. 账号转存任务需要更多账号；单账号的 lease 会限制交接速度。
4. 分享目录特别大时，瓶颈在枚举阶段，而不是规划阶段。

## API 摘要

- `POST /api/share-preview`：解析分享并分页浏览目录，供前端勾选
- `GET /api/download-targets`：列出挂载下载目录下可选的目标子目录
- `GET /api/settings`、`PATCH /api/settings`：运行时调整下载并发、单任务连接数与血缘预算，持久化到 `app_settings`
- `POST/GET /api/download-jobs`（创建时 `target_subdir` 选择下载位置；同一分享与目标位置自动合并选择项）、`GET /api/download-jobs/{id}`、`GET /api/download-jobs/{id}/files`、`POST /api/download-jobs/{id}/cancel`、`DELETE /api/download-jobs/{id}`
- `GET /api/downloads/{id}`、`POST /api/downloads/{id}/pause|resume|retry|cancel`、`GET /api/downloads/{id}/file`
- `GET/POST /api/accounts`、`POST /api/accounts/{id}/refresh|enable|disable|reconcile`、`DELETE /api/accounts/{id}`、`GET /api/cleanups`。`reconcile` 会删除该账号上已完成/失败/取消任务留下的转存目录和 `pikpak_tar_*` 目录，并返回清理前后的 quota 明细。
- `POST /api/lineage-runs`：`graph: true` 创建血缘图运行；可带 `items` 只展开勾选的文件/文件夹，不传则遍历整个起始分享。无论哪种方式，上游分享都只解析目标文件引用的具体文件，不展开同分享的其他内容
- `GET /api/lineage-runs`、`GET /api/lineage-runs/{id}`（报告内含 `graph.nodes` / `graph.edges`）
- `GET /api/lineage-runs/{id}/graph/shares/{share_id}`：节点详情、来源文件、分享内容、派生去向
- `POST /api/lineage-runs/{id}/graph/enrich`：为预算截断的节点补齐分享名称（不枚举内容）
- `POST /api/lineage-runs/{id}/resume`：调高血缘预算后继续已截断的全量图运行；会把 `ENUMERATING`/`BUDGET` 的分享和目录重置为待处理，保留已有节点与证据

全量图预算按深度推进：先完成当前层的分享与起始分享目录，再进入下一层；不会在 L1 未完成时展开 L2。同一层内按分享逐个展开；上游分享只请求目标文件，不再枚举整份分享。
- `GET /api/lineage-runs/{id}/copies`、`POST /api/lineage-runs/{id}/cancel`、`DELETE /api/lineage-runs/{id}`、`GET /api/lineage-runs/{id}/export?format=json|markdown|dot`
- `GET /api/events`：SSE，支持 `Last-Event-ID` / `after` 补发
- `GET /api/health`：DB、aria2、数据目录可写

## 测试

```bash
uv run pytest backend/tests/test_captcha.py backend/tests/test_verifier.py backend/tests/test_planner.py backend/tests/test_windows.py -q
uv run pytest backend/tests/integration -q
```

集成测试使用 `backend/tests/fake_pikpak_server.py`（协议 API + Range CDN）和真实 aria2。Windows 本地测试需要 `ARIA2C_PATH` 指向 `aria2c.exe`；Docker 镜像自带 aria2。

## 验证记录

- 账号队列：任务先于账号创建时保持 `ACCOUNT_QUEUED`；账号加入后调度器会自动重新领取。任务自己持有的租约会直接复用；指向已删除/已结束任务的陈旧租约会自动清理，避免账号页显示“已租用”但任务仍等待。
- 设置页：`/settings`，调整任务并发（1–16）、文件规划并发（1–16）、单任务连接数（aria2 split / 每服务器连接，1–16）、血缘条目/请求/分享/目录预算；保存后立即生效并跨重启保留。
- 文件夹大小：PikPak 列表接口对文件夹只返回 0，真实大小需要递归枚举；预览默认显示 `—`，不阻塞列表加载。
- 同名冲突：规划阶段已检查一次；下载/解包前再次检查，因为长任务期间目标可能被其他任务或外部程序改变。tar 路线会在解包前完成冲突处理，避免解出后再丢弃。按设置处理：`rename` 保留两份，`overwrite` 覆盖，`skip` 保留已有文件并在任务上标记 `DUPLICATE_SKIPPED`。
- tar 单连接：tar 不支持多连接下载，tar 路线始终以 `single_connection=True` 启动 aria2，不做 multipart/Range 探测。账号普通文件签发遇到 `daily_download_size_exceeded` 时，自动走 tar 归档通道：restore 副本 → 根目录专用打包目录（`parent_id=""`，`*` 只用于列表）→ `files/pack` → 轮询 pack 任务 → 签发 tar 直链（配额检查豁免）→ 单连接下载 tar → **解包前先按 `rename`/`overwrite`/`skip` 处理目标重名** → 在 `/downloads` 目标目录内解出隐藏临时文件 `.<task-id>.part` → 按原始 `size + Merkle-gcid` 校验 → 同目录 `os.replace` 原子改名为最终文件。不再写入 cache 的 `extracted.bin`。tar 下载在任务详情中显示为 `tar 通道`。
- 失败重试：下载页「失败」分类提供「一键重试全部失败」，任务子页提供「一键重试失败文件」；只重置失败/取消的文件，不重新下载已完成文件。
- 下载结构：最终文件写入 `DOWNLOADS_DIR/<target_subdir>/<分享相对路径>`，文件夹选择保留子树；临时文件在 `CACHE_DIR/<task-id>/download.bin`，成功后原子移动/跨挂载复制，失败或取消时删除。aria2 对 PIKPAK CDN 允许过期/不受信证书（`check-certificate=false`），因为落盘后仍按 `size + Merkle-gcid` 强校验，证书不是完整性边界。
- 后端测试：`54 passed`（captcha 向量、Merkle 向量、窗口并集、下载/转存/删除顺序、清理重试、匿名/账号/文件夹下载、预览与勾选下载、嵌套目录选择、断点重启、状态机、错误契约、血缘直系/文件夹/tenant refs/窗口、全量图 BFS 去重与图 API）。
- 删除：下载任务与血缘运行在列表页可删除；运行中的任务需先取消。删除下载任务会移除任务记录，删除不再被其他任务引用的下载文件，云端待清理项继续执行；删除血缘运行会移除节点/边/快照/图数据与证据目录。
- 全量图 UI：集成回归覆盖 L0 起点 → L1 直接上游 → L2 二级上游，验证图例筛选、缩放/适应画布、节点详情来源文件与分享内容。旧基线（83 个分享、48,641 个文件）来自“上游分享全量展开”，改为只解析目标文件后不再适用，需要按新语义重跑真实分享基线。命中 `LINEAGE_MAX_ITEMS=50000` 时报告 `budget_exhausted`，未展开节点标记为“预算截断”并在详情提示；调高预算后可点“继续运行”，不必重建。
- Docker 端到端：`docker build` 成功；容器以 UID 10001 运行；`/api/health` 200；SSE 事件补发；账号转存后删除时间早于 aria2 首个请求；浏览器保存文件 Merkle 与声明一致；1440×900 与 390×844 页面检查通过。
- 真实账号 restore 冒烟未执行：未提供专用 `LIVE_REFRESH_TOKEN` / `LIVE_SHARE_URL`；不以 fake 结果替代实网结论。

## 安全与限制

- 凭据、pass code、`pass_code_token`、`web_content_link` 均以 AES-GCM 加密落库；日志与 API 不返回秘密。
- 单用户、可信网络假设；公网部署需在反向代理增加 TLS 与访问认证。
- 提取码链路按 `pass_code` + `pass_code_token` 实现，但无真实提取码样本，报告保留 `passcode_unverified`。
- `global_file_kind=1/2` 跨账号 restore 未闭环；拿不到 wcl 时返回 `UNSUPPORTED_GLOBAL_FILE`。
- 真实账号 restore 冒烟需要专用测试 `LIVE_REFRESH_TOKEN` 与小文件 `LIVE_SHARE_URL`；未提供时不执行，也不以 fake 结果替代实网结论。
