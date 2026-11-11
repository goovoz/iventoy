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
6. **源码漂移对账**（版本未变且镜像已发布时才判）：拿镜像 config blob 里的
   `org.opencontainers.image.revision`（构建它的提交号）与 `main` 的 HEAD 比较，
   只看**影响镜像内容**的路径 —— `Dockerfile` / `.dockerignore` / `files/` / `patches/`。
   - 有差异 = 发布标签落后源码（补丁改了几轮、PR 全合并，但镜像还是旧的），
     按 `ref=main` 重新派发一次发布构建
   - 无差异 = 空跑；`docs/`、`README`、workflow 这类提交不会触发重建，
     避免每天白跑一次多架构构建
   - 读不到 label / token 失败 → 记 `unknown`，本轮跳过；镜像记录的提交不在仓库历史里
     → 按「有漂移」处理。宁可多建一次，也不漏掉补丁改动
   - 这一条是补 2026-11-11 那类故障：代码进了 `main`、镜像没动，属于「合并 ≠ 发布」的盲区
7. **动作收敛**：把上面 5/6 与版本变更收敛成一个 `action` ∈
   `release-tag` / `backfill` / `refresh` / `none`，每个动作只对应一个派发 ref
   （新 tag / 当前 tag / `main`），避免多个 `if` 条件互相打架、同一天重复派发两次构建

## 镜像发布链路（docker-publish.yml）

- 触发：`vX.Y.Z` 标签推送（其他 tag 不触发）、`workflow_dispatch`（tracker 自动派发或手动）、
  **`main` 分支推送**（只出 `:edge` 开发通道，见下）
- 版本来源：tag 事件取 tag；其余触发取 `Dockerfile` 当前默认版本。**解析失败直接中止**，
  不再静默回落到默认值（那会产出 tag 与内容不符的镜像）
- 发布目标：`ghcr.io/goovoz/iventoy`；配置了 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`
  secrets 时同时发布到 Docker Hub（fork 不继承上游 secrets，未配置时自动跳过 Docker Hub）
- 标签策略：`latest` + 精确版本号（如 `1.0.47`），不产 major/minor 浮动标签。
  `latest` 只在「tag 版本 == Dockerfile 默认版本」或 dispatch 时刷新，推一个旧 tag
  不会把 `latest` 回退到旧版本
- **`:edge` 开发通道**：`main` 的每次推送都构建一次多架构镜像并只打 `:edge`（仅 ghcr，
  不动 Docker Hub）。发布标签（`latest` / `<version>`）完全不参与 —— 一次坏提交也不会
  污染发布；同时它让「代码是否真的变成了镜像」当场可见，补 2026-11-11 那类盲区。
  `main` 上的 edge 构建允许被新提交取代（`cancel-in-progress`），tag / dispatch 的发布
  构建绝不取消（半途取消会在 registry 上留下不完整的多架构索引）
- tracker 的版本 bump 提交（`chore: auto-update iVentoy ...`）自己会让出 edge 构建：
  同一次运行里紧跟的 tag 推送才是真正的发布，重复构建没有意义
- 发布前先跑 `tests/test-container-entrypoint.sh`（`needs: test`），不通过不构建
- gha 缓存显式指定 `scope=iventoy`：默认按 ref 隔离，否则每个新 tag 都是冷启动
- 每次构建结尾写 run summary：通道、上游版本、源码提交、客户端上限、所有 tag ——
  看 run 列表就能判断「这份镜像对应哪份代码」

## 底包选型（为什么 final 必须是 debian 系，以及为什么是 trixie）

iVentoy 官方预编译二进制与 `lib/lin64/` 下的捆绑库全部是 glibc 链接（实证：`ldd` 只依赖
`libc.so.6` / `libpthread.so.0` / `libdl.so.2`，其余全部解析到捆绑库）。Alpine 使用 musl libc，
没有 glibc，二进制加载即失败——**final 镜像没有选择，必须 glibc 系，不能用 Alpine 瘦身**。

当前结构：

| 阶段 | 底包 | 职责 |
|---|---|---|
| builder | `alpine:3.24`（3.5MB） | 仅用 busybox wget/tar 下载解压，不执行二进制 |
| final | `debian:trixie-slim`（28.5MB，按索引 digest 钉死） | 只承载 `/iventoy` 运行时，零自装 apt 包 |

- debian 优于 ubuntu：更小、包更少、支持期更长（Debian 13 到 2030+，Ubuntu 22.04 标准支持 2027-04 截止）
- **2026-10-25 从 bookworm 换到 trixie**：Docker Hub 的镜像扫描对 bookworm 版报 High 8 / Medium 4 /
  Low 38，全部落在 Debian 12 的基础包（util-linux 家族占大头），iVentoy 自身零命中。
  trivy 实测这些漏洞在 Debian 12 **没有任何可用修复版本**（affected 169 / fix_deferred 41 /
  will_not_fix 17），所以对 bookworm 做 apt upgrade 一条都修不掉，只有换底包是真解。
  换 trixie 后 227 条降到 161 条（HIGH 56 → 43，CRITICAL 1 → 0），体积 64.7MB → 66.9MB（+3.4%）。
- final 底包**按索引 digest 钉死**（`debian:trixie-slim@sha256:62bd423a…`）而不是用滚动 tag：
  可复现、不受官方重推影响。注意必须钉**索引（manifest list / OCI index）digest**——
  Docker Hub 镜像页右边那栏 `MANIFEST DIGEST` 是当前选中架构的单平台清单，
  钉它会在另一个架构上静默用错底包（实测钉 386 的清单能在 amd64 上「构建成功」但产物是 i386）。
  刷新：`docker buildx imagetools inspect debian:trixie-slim`。
  钉底包不冻结安全更新：final 阶段的 `apt-get upgrade` 仍会装当前最新的包。
- 回退方式：Dockerfile 里那行 `FROM` 改回 `debian:bookworm-slim`，其余不用动。
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
| 代码已合并进 `main`，但 `latest` / 版本镜像还是旧的 | 「合并 ≠ 发布」：`main` 推送只出 `:edge` | tracker 每天做源码漂移对账并自动按 `main` 重发；要立刻生效就手动 dispatch `docker-publish.yml` |
| `:edge` 与 `latest` 内容不一致 | 正常：edge 跟着 `main` 走，`latest` 只在发布/漂移对账时刷新 | 想尽快把 `main` 的改动升成发布版：等当天的 tracker，或手动 dispatch |
| tracker 报「源码漂移 unknown」 | 取 ghcr token / 读 config label 失败 | 本轮跳过，次日重试；持续出现看 ghcr 状态页 |
| tracker 报「镜像记录的提交不在仓库历史里」 | 镜像来自已删除的分支 / force push | 按有漂移处理，会重发一次；确认 `main` 上代码是期望的即可 |

## 上游包结构变更记录

- `<= 1.0.45`：tarball 顶层目录为 `iventoy-<版本>/`（如 `iventoy-1.0.45/`）
- `>= 1.0.46`：顶层目录改为固定的 `iventoy/`；`lib/iventoy` 与 `lib/lin64/*.so` 布局未变

Dockerfile 已按「解包到临时目录 → 探测唯一顶层目录 → mv 成 /iventoy」处理，
不依赖具体目录名；并校验 `lib/iventoy` 存在，缺失时立即报错并打印目录清单。

## Docker Hub 的 tag 清理（cleanup-dockerhub.yml）

ghcr 的版本由 `cleanup-package.yml` 管；Docker Hub 是另一套 API（hub.docker.com 的 REST + JWT，
凭据用 `DOCKERHUB_TOKEN`），所以单独一个 workflow：`cleanup-dockerhub.yml`
（每天 04:47 + 手动，`dry_run` 默认 true，也支持 `keep_tags` 显式指定）。

保留策略与 ghcr 一致：**latest + 当前版本 + 次新版本**（留一档回滚）。
当前版本从 Dockerfile 读，次新版本按语义化版本号挑。

能力边界（Hub API 限制，不是偷懒）：**Hub 只开放「删 tag」的接口，没有删 manifest 的公开接口**。
删掉 tag 后底层镜像变成未标记状态，Hub 什么时候回收不由我们控制。
所以这个 workflow 保证的是「tag 列表干净」，不是「底层字节一定消失」。

## 二进制补丁（patches/）

镜像里的 `lib/iventoy` 是打过补丁的，补丁在构建期应用，**签名失配就让构建失败**，不会产出「能起但功能残缺」的镜像。

原因：补丁改的是编译期常量与三处自校验分支，上游只要换编译器 / 改函数形状，地址和指令就全变。所以补丁不写死地址，靠符号表 + 指令签名定位，并且有三道闸：

1. `ci-validate.yml` 的 `patch` 作业 —— 每次 push/PR 对当前固定版本跑 `--check`，失配立即红。
2. `track-iventoy-release.yml` —— 自动跟版前先对新版本跑 `--check`；不通过就停在原地，不 bump、不打 tag、不派发构建。
3. `docker-publish.yml` —— 发布后按架构起容器验 26000 返回 200，并断言镜像内 `/usr/share/iventoy/patch.json` 的 `ok` 为 true。

签名失配的典型表现、重新锚定步骤、以及「为什么上限是 65535 而不是无限制」，见 [patches/README.md](../patches/README.md)。
