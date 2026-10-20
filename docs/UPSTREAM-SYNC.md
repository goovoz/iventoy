# 上游同步机制说明

本仓库是 [dockers-x/iventoy](https://github.com/dockers-x/iventoy) 的 fork，
但**版本更新不依赖 GitHub parent**（parent 长期停滞在 1.0.39），而是直接跟随
iVentoy 官方发布源：[ventoy/PXE releases](https://github.com/ventoy/PXE/releases)。

## 两个上游的角色

| 上游 | 角色 | 同步方式 |
|---|---|---|
| `ventoy/PXE` | iVentoy 官方发布源，版本真相 | `.github/workflows/track-iventoy-release.yml` 每日自动检查 |
| `dockers-x/iventoy` | GitHub parent，Docker 打包改进来源 | 手动 merge（见下文） |

## 自动版本跟踪（track-iventoy-release.yml）

触发方式：

- `schedule`：每天 18:37 UTC（北京时间 02:37）自动运行
- `workflow_dispatch`：Actions → "Track iVentoy Upstream Release" → Run workflow；
  可选输入 `version` 手动钉住某个版本（留空则自动获取上游最新稳定版）

每次运行做四件事：

1. **取上游最新稳定版**（`releases/latest`，排除 pre-release/draft）
2. **与仓库当前版本比对**（读 `Dockerfile` 的 `IVENTOY_VERSION` 默认值）——
   一致则空跑退出，不产生任何推送
3. **校验发布资产齐全**：确认上游 release 里存在
   `iventoy-<V>-linux-x86_64-free.tar.gz` 与 `iventoy-<V>-linux-arm64-trial.tar.gz`，
   缺任何一个即报错终止（防止上游改名后推进坏版本）
4. **更新并发布**（仅在上游有新版本时）：
   - 用 sed 替换 `Dockerfile` / `Makefile` / `docker-compose.yml` / `README.md` 中的版本引用
   - 若替换后 `git diff` 为空则报错终止（防止引用模式悄悄失效）
   - 提交推送到 `main`，创建 `v<X.Y.Z>` 标签（已存在则跳过）
   - 通过 `workflow_dispatch` 派发 `docker-publish.yml` 构建多架构镜像并发布
     （`workflow_dispatch` 是 GITHUB_TOKEN 触发级联的法定例外，能正常创建新 run）

## 镜像发布链路（docker-publish.yml）

- 触发：`v*` 标签推送、`workflow_dispatch`（tracker 自动派发或手动）
- 版本来源：tag 事件取 tag；其余触发取 `Dockerfile` 当前默认版本
- 发布目标：`ghcr.io/goovoz/iventoy`；配置了 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`
  secrets 时同时发布到 Docker Hub（fork 不继承上游 secrets，未配置时自动跳过 Docker Hub）
- 标签策略：每次非 PR 构建刷新 `latest`，tag 构建额外打 `<major>.<minor>.<patch>` 等语义化标签

## 底包选型（为什么 final 必须是 debian:bookworm-slim）

iVentoy 官方预编译二进制与 `lib/lin64/` 下的捆绑库全部是 glibc 链接（实证：`ldd` 只依赖
`libc.so.6` / `libpthread.so.0` / `libdl.so.2`，其余全部解析到捆绑库）。Alpine 使用 musl libc，
没有 glibc，二进制加载即失败——**final 镜像没有选择，必须 glibc 系，不能用 Alpine 瘦身**。

当前结构：

| 阶段 | 底包 | 职责 |
|---|---|---|
| builder | `alpine:3.20`（3.5MB） | 仅用 busybox wget/tar 下载解压，不执行二进制 |
| final | `debian:bookworm-slim`（26.9MB） | 只承载 `/iventoy` 运行时，零 apt 包 |

- debian 优于 ubuntu：更小、包更少、支持期更长（Debian 12 到 2028+，Ubuntu 22.04 标准支持 2027-04 截止）
- builder 用官方 alpine，不用任何个人 convenience 镜像（供应链最小化）
- 镜像内刻意不装 curl/wget：健康检查用 `/proc/net/tcp{,6}` 探测 26000（0x6590）的 LISTEN 状态，零额外依赖
- entrypoint 启动前做 `ldd` 自检：上游某版若不再捆绑某个 .so，容器快速失败并列出缺失项

## 手动同步 dockers-x（GitHub parent）

parent 若恢复更新，可手动把它的提交合进来：

```bash
git remote add upstream https://github.com/dockers-x/iventoy.git
git fetch upstream
git checkout main
git merge upstream/main
# 冲突通常只出现在版本字符串上，以本仓库（跟随官方发布）为准
```

## 改动分层纪律

- **L0**：新增自有文件 —— 必须登记进 `.github/fork-paths.txt`
  （当前：`track-iventoy-release.yml`、`fork-paths.txt`、`UPSTREAM-SYNC.md`）
- **L1**：对上游文件的最小改动 —— 数量卡死，只增不改语义，代码里留
  `[iventoy-fork]` 锚点注释（当前仅 `docker-publish.yml` 三处）
- **L2**：改写上游逻辑 —— 禁止

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| tracker 每天空跑 | 上游无新发布 | 正常，无需处理 |
| tracker 报错「无法从上游解析出版本号」 | 上游 tag 命名变更 | 人工确认新版命名，调整 tracker |
| tracker 报错「缺少构建所需资产」 | 上游资产命名变更 | 人工确认后调整 Dockerfile 的下载 URL 模式与 tracker 校验列表 |
| tracker 报错「没有任何文件被改动」 | 版本引用模式失效 | 检查四个文件的版本字符串形态是否被重构 |
| 镜像构建失败 | 上游新版本的运行依赖变化 | 看 docker-publish 日志，修 Dockerfile 后手动派发重建 |
