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

每次运行依次做：

1. **取上游最新稳定版**（`releases/latest`，排除 pre-release/draft）。手动输入的版本经 env 传入 shell，不直接插值（避免命令注入）
2. **与仓库当前版本比对**（读 `Dockerfile` 的 `IVENTOY_VERSION` 默认值）——
   一致则空跑退出，不产生任何推送
3. **校验发布资产齐全**：确认上游 release 里存在
   `iventoy-<V>-linux-x86_64-free.tar.gz` 与 `iventoy-<V>-linux-arm64-trial.tar.gz`，
   缺任何一个即报错终止（防止上游改名后推进坏版本）
4. **更新并发布**（仅在上游有新版本时）：
   - 用 sed 替换 `Dockerfile` / `README.md` 中的版本引用
     （`docker-compose.yml` 固定使用 `:latest`；`Makefile` 的 `VERSION` 从 Dockerfile 现读，
     两者都不含版本号，无需替换）
   - **逐文件校验**：上面两个文件里任何一个没被改到就报错终止（防止引用模式悄悄失效）
   - 提交推送到 `main`，创建 `v<X.Y.Z>` 标签（已存在则跳过）
   - 通过 `workflow_dispatch` 派发 `docker-publish.yml` 构建多架构镜像并发布
     （`workflow_dispatch` 是 GITHUB_TOKEN 触发级联的法定例外，能正常创建新 run，
     但令牌必须同时具备 `actions: write`，否则接口返回 403）
   - **等构建跑完并校验结论**：轮询该 run 最多 20 分钟，只有 `conclusion == success`
     才算跟踪成功；构建失败会让 tracker 直接变红并给出 run 链接，避免
     「代码更新了但镜像没更新」却不报警（2026-10-21 的 1.0.46 故障就是这种静默失败）
5. **对账（每次都做，与是否有新版本无关）**：用匿名 token 查
   `ghcr.io/v2/goovoz/iventoy/manifests/<当前版本>`。
   - 返回 404 = 「版本号已 bump，但镜像没发出去」（构建失败 / 派发 403 / 被清理），
     此时**即使上游没有新版本也会重新派发构建**，失败照常上报
   - 返回 200 才允许空跑
   - 查询本身失败（401 / 5xx / 网络）记为 `unknown`，本轮不做判定，避免误触发构建风暴
   - 这一条是补 2026-10-21 那类故障的**自愈能力**：当时 bump 与 tag 都成功了、只有派发 403，
     而第二天起 `current == latest` 就永远空跑，镜像会静默停在 1.0.45

## 镜像发布链路（docker-publish.yml）

- 触发：`vX.Y.Z` 标签推送（其他 tag 不触发）、`workflow_dispatch`（tracker 自动派发或手动）
- 版本来源：tag 事件取 tag；其余触发取 `Dockerfile` 当前默认版本。**解析失败直接中止**，
  不再静默回落到默认值（那会产出 tag 与内容不符的镜像）
- 发布目标：`ghcr.io/goovoz/iventoy`；配置了 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`
  secrets 时同时发布到 Docker Hub（fork 不继承上游 secrets，未配置时自动跳过 Docker Hub）
- 标签策略：`latest` + 精确版本号（如 `1.0.47`），不产 major/minor 浮动标签。
  `latest` 只在「tag 版本 == Dockerfile 默认版本」或 dispatch 时刷新，推一个旧 tag
  不会把 `latest` 回退到旧版本
- 发布前先跑 `tests/test-container-entrypoint.sh`（`needs: test`），不通过不构建
- gha 缓存显式指定 `scope=iventoy`：默认按 ref 隔离，否则每个新 tag 都是冷启动

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
- builder 底包随 alpine 的 2 年支持期滚动：`3.20` 已于 **2026-04-01 EOL**，现为 `3.24`
- builder 用官方 alpine，不用任何个人 convenience 镜像（供应链最小化）
- 下载完整性：上游自 1.0.47 起随 release 附 `sha256.txt`，Dockerfile 会取来校验 tar.gz；
  取不到（旧版本重建）只告警不失败
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
  （当前：`track-iventoy-release.yml`、`cleanup-package.yml`、`ci-validate.yml`、
  `fork-paths.txt`、`UPSTREAM-SYNC.md`、`.dockerignore`、`tests/test-container-entrypoint.sh`）
- **L1**：对上游文件的改动 —— 代码里留 `[iventoy-fork]` 锚点注释。现阶段范围：
  `docker-publish.yml`（发布链路）、`Dockerfile`（多阶段重构 + sha256 校验）、
  `files/docker-entrypoint.sh`（ldd 自检）、`docker-compose.yml` / `Makefile` / `README.md`。
  ⚠️ 其中 `Dockerfile` 已是**整体重写**（不是「最小改动」），按旧口径会被归成 L2。
  保留为 L1 的理由：它仍然只做「把上游二进制装进 glibc 底包」这一件事，没有改 iVentoy 行为；
  代价是**每次上游版本变更都要人工看一眼 Dockerfile 是否还能正常构建**
- **L2**：改写上游逻辑 —— 禁止

## CI 校验（ci-validate.yml）

- 每次 push / PR：`shellcheck`（entrypoint + tests）、`yamllint`（全部 workflow + compose）、
  `hadolint`（Dockerfile，仅 error 级失败）、`tests/test-container-entrypoint.sh`
- `workflow_dispatch` 或 `fix/**` 分支：额外**真实构建一次多架构镜像（不推送）**，
  把 sha256 校验、解包结构断言这些只能靠构建验证的逻辑也纳入验证
- 发布链路（docker-publish）也 `needs: test`，自测不过不会构建、更不会推 latest

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| tracker 每天空跑 | 上游无新发布 | 正常，无需处理 |
| tracker 报错「无法从上游解析出版本号」 | 上游 tag 命名变更 | 人工确认新版命名，调整 tracker |
| tracker 报错「缺少构建所需资产」 | 上游资产命名变更 | 人工确认后调整 Dockerfile 的下载 URL 模式与 tracker 校验列表 |
| tracker 报错「没有任何文件被改动」 | 版本引用模式失效 | 检查 Dockerfile / README 两处版本字符串形态是否被重构 |
| tracker 报 403（覆盖范围：派发失败） | `permissions` 缺少 `actions: write` | 已在 2026-10-21 修复；若再出现，检查 workflow 顶部 permissions |
| tracker 报「ghcr 缺少 X.Y.Z 镜像」 | 版本已 bump 但镜像没发出去 | tracker 每天会自动重试补齐；连续失败看 docker-publish 的 run 日志 |
| tracker 显示「查询 ghcr 失败（unknown）」 | 网络 / ghcr 临时故障 | 本轮跳过判定，次日自动重试；持续出现再看 ghcr 状态页 |
| 镜像构建失败，日志停在 `wget`/`mv` 步骤 | 上游改了包内顶层目录名或资产命名 | Dockerfile 已改为自动探测顶层目录；若仍失败，看构建日志里 `ls -la /iventoy` 输出确认新结构 |
| 镜像构建失败 | 上游新版本的运行依赖变化 | 看 docker-publish 日志，修 Dockerfile 后手动派发重建 |

## 上游包结构变更记录

- `<= 1.0.45`：tarball 顶层目录为 `iventoy-<版本>/`（如 `iventoy-1.0.45/`）
- `>= 1.0.46`：顶层目录改为固定的 `iventoy/`；`lib/iventoy` 与 `lib/lin64/*.so` 布局未变

Dockerfile 已按「解包到临时目录 → 探测唯一顶层目录 → mv 成 /iventoy」处理，
不依赖具体目录名；并校验 `lib/iventoy` 存在，缺失时立即报错并打印目录清单。
