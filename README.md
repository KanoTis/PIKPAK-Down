# PikPak 分享套件

单容器、单用户的 PikPak 分享下载与血缘溯源 Web 应用。

- **下载**：先解析分享并展示文件/文件夹，勾选需要的内容后再创建任务；非视频或 `anonymous_play_range=1.00` 的视频走匿名直链；否则从账号池转存、取登录态 `web_content_link` 后**立即删除云盘文件**，再下载并校验 `size + Merkle-gcid`。
- **血缘**：全量血缘图从分享起点枚举全部文件，按 `params.original_share_id` / `original_file_id` 逐层去重展开上游分享，聚合文件级证据为分享级边；同时保留单目标直系链、文件夹链、tenant refs、同指纹副本、匿名窗口与终止点。血缘页先解析分享并勾选文件/文件夹，再创建运行；图页面使用 Cytoscape.js 渲染，按深度分层，每个层级一列纵向排列；默认只绘制聚焦线路，悬停节点高亮关联节点（连接过多时不铺线），点击节点打开详情。未展开的分享显示“未展开分享”和来源文件路径，可用“补齐名称”只拉取分享元数据、不展开内容。
- **账号池**：只导入人工取得的 `refresh_token`；不自动注册、不处理验证码、不保存登录密码。

## 快速启动

```bash
python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"
cp .env.example .env   # 填入 APP_SECRET_KEY
docker compose up --build        # 使用当前目录 docker-compose.yml
```

打开 `http://127.0.0.1:8080`。Compose 只绑定 `127.0.0.1`，不映射 aria2 RPC 6800。

开发环境如果只想用旧的本机构建配置：

```bash
docker compose -f compose.dev.yaml up --build
```

CI 会把镜像推送到 `ghcr.io/kanotis/pikpak-down`；生产环境可直接拉取：

```bash
docker compose pull && docker compose up -d
```

私有 GHCR 包需要先 `docker login ghcr.io`。

## 镜像构建（GitHub Actions）

`.github/workflows/docker.yml` 在推送到 `master` / `main`、打 `v*` 标签或手动触发时构建 `linux/amd64` 镜像并推送到 GHCR：

```text
ghcr.io/kanotis/pikpak-down:latest
ghcr.io/kanotis/pikpak-down:master
ghcr.io/kanotis/pikpak-down:sha-<commit>
ghcr.io/kanotis/pikpak-down:<version>   # v1.2.3 标签
```

Pull Request 只构建、不推送。首次推送后到仓库的 Packages 页面把包可见性设为 Private/Public 即可。

## 本地开发

```bash
uv sync --group dev
pnpm --dir frontend install --frozen-lockfile
export APP_SECRET_KEY="<base64url-32-byte-key>"
export DATA_DIR="./data"
PYTHONPATH=backend uv run uvicorn app.main:app --reload --port 8080
pnpm --dir frontend dev
```

生成前端 API 类型：

```bash
PYTHONPATH=backend APP_SECRET_KEY=... DATA_DIR=./data-dev uv run python scripts/dump_openapi.py > frontend/openapi.json
pnpm --dir frontend gen:api
```

## 配置

必填 `APP_SECRET_KEY`（base64url 32 字节）；数据目录默认 `/data`，SQLite 位于 `/data/pikpak.db`。其余配置见 `.env.example`。

启动时创建 `/data/{tmp,files,evidence,state}` 并执行 Alembic migration。只运行一个 Uvicorn worker，调度器与 aria2 都在同一进程生命周期内管理。

## API 摘要

- `POST /api/share-preview`：解析分享并分页浏览目录，供前端勾选
- `GET /api/settings`、`PATCH /api/settings`：运行时调整下载并发、单任务连接数与血缘预算，持久化到 `app_settings`
- `POST/GET /api/download-jobs`、`GET /api/download-jobs/{id}`、`GET /api/download-jobs/{id}/files`、`POST /api/download-jobs/{id}/cancel`、`DELETE /api/download-jobs/{id}`
- `GET /api/downloads/{id}`、`POST /api/downloads/{id}/pause|resume|retry|cancel`、`GET /api/downloads/{id}/file`
- `GET/POST /api/accounts`、`POST /api/accounts/{id}/refresh|enable|disable`、`DELETE /api/accounts/{id}`、`GET /api/cleanups`
- `POST /api/lineage-runs`：`graph: true` 创建血缘图运行；可带 `items` 只展开勾选的文件/文件夹，不传则遍历整个分享
- `GET /api/lineage-runs`、`GET /api/lineage-runs/{id}`（报告内含 `graph.nodes` / `graph.edges`）
- `GET /api/lineage-runs/{id}/graph/shares/{share_id}`：节点详情、来源文件、分享内容、派生去向
- `POST /api/lineage-runs/{id}/graph/enrich`：为预算截断的节点补齐分享名称（不枚举内容）

全量图预算按深度推进：先完成当前层的分享与目录，再进入下一层；不会在 L1 未完成时展开 L2。同一层内按分享逐个展开，单个超大分享可能先耗尽预算，导致同层其他分享和更深层节点被标记为“预算截断”。
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

- 设置页：`/settings`，调整任务并发（1–16）、单任务连接数（aria2 split / 每服务器连接，1–16）、血缘条目/请求/分享/目录预算；保存后立即生效并跨重启保留。
- 后端测试：`32 passed`（captcha 向量、Merkle 向量、窗口并集、下载/转存/删除顺序、清理重试、匿名/账号/文件夹下载、预览与勾选下载、嵌套目录选择、断点重启、状态机、错误契约、血缘直系/文件夹/tenant refs/窗口、全量图 BFS 去重与图 API）。
- 删除：下载任务与血缘运行在列表页可删除；运行中的任务需先取消。删除下载任务会移除任务记录，删除不再被其他任务引用的下载文件，云端待清理项继续执行；删除血缘运行会移除节点/边/快照/图数据与证据目录。
- 全量图 UI：真实分享回归得到 83 个分享节点、48,641 个文件、1,359 个目录；层级为 L0 起点 → L1 直接上游 → L2 二级上游 → L3 三级上游。验证图例筛选、缩放/适应画布、节点详情来源文件、分享内容分页（500 → 1,000）。命中 `LINEAGE_MAX_ITEMS=50000` 时报告 `budget_exhausted`，未展开节点标记为“预算截断”并在详情提示。
- Docker 端到端：`docker build` 成功；容器以 UID 10001 运行；`/api/health` 200；SSE 事件补发；账号转存后删除时间早于 aria2 首个请求；浏览器保存文件 Merkle 与声明一致；1440×900 与 390×844 页面检查通过。
- 真实账号 restore 冒烟未执行：未提供专用 `LIVE_REFRESH_TOKEN` / `LIVE_SHARE_URL`；不以 fake 结果替代实网结论。

## 安全与限制

- 凭据、pass code、`pass_code_token`、`web_content_link` 均以 AES-GCM 加密落库；日志与 API 不返回秘密。
- 单用户、可信网络假设；公网部署需在反向代理增加 TLS 与访问认证。
- 提取码链路按 `pass_code` + `pass_code_token` 实现，但无真实提取码样本，报告保留 `passcode_unverified`。
- `global_file_kind=1/2` 跨账号 restore 未闭环；拿不到 wcl 时返回 `UNSUPPORTED_GLOBAL_FILE`。
- 真实账号 restore 冒烟需要专用测试 `LIVE_REFRESH_TOKEN` 与小文件 `LIVE_SHARE_URL`；未提供时不执行，也不以 fake 结果替代实网结论。
