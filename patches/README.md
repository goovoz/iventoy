# iVentoy 解限补丁

镜像里的 `lib/iventoy` 是**打过补丁**的上游二进制。这个目录是补丁的唯一来源，构建时会自动应用，不产出「半补丁」的镜像。

## 改了什么

| # | 站点 | 上游行为 | 补丁后 |
|---|---|---|---|
| 1 | `toy_get_free_max_client` | 返回 20（Free 版）/ 1（test mode） | 整个函数替换成 `return MAX_CLIENTS`（默认 65535） |
| 2 | `g_aucRecordMac` | 20 条 × 6 字节静态数组，客户端记录表 | 数组搬到新的 .bss 页（`p_memsz` 扩出来的零页），容量 = MAX_CLIENTS |
| 3 | `dhcp_read_callback_normal` / `_proxy` | 架构是 ARM64 / LoongArch 时进拒绝分支 | 拒绝分支的跳转改成 NOP，直接进放行路径 |
| 4 | `main_start` | 自校验：把 exe 的 SHA-256 与 tarfs 里的 `sha256/<type>.txt` 比对，两个哈希都写进工作配置 | **把 tarfs 里的期望值改写成实算哈希**（复用原本要调 `strncasecmp` 的那条 call，参数与 `memcpy(dst, src, 0x40)` 完全一致），于是后面所有比对都自然成立 |

### 第 4 条为什么是「改写期望值」而不是「让比对结果等于 0」

早期版本把比对结果按相等处理（`test eax,eax` → `xor eax,eax`），再补两处把连锁判断也钉死：

1. `http_init` 复用同一个比对结果选 API（26000）还是 PXE（16000）模式 → 结果钉成 1；
2. `http_master_thread_main` 在 `cfg[0xc] != cfg[0x4c]` 时把 API 端口改成 `60000+time%1000` → 比较强制相等。

这三处一起钉住，服务确实起来了、26000 也确实在听 —— **但 WebUI 是空白的**：`GET /` 返回 200 和 1 个字节，
浏览器一片白。原因在于 iVentoy 不只是「看比对结果」，它把两个哈希值本身留在工作配置里当运行时一致性判据
（`cfg+0xc` 实算值 / `cfg+0x4c` 期望值 / `cfg+0xa0` 指向期望值缓冲区，前 8 个十六进制字符还会转成两个整数存到
`g_stUtilLinuxBootInfo-0x1fc/-0x1f8`）。两份值不相等时它不报错、不退出，只是 tarfs 里的 `www` 资源读出来是空的。

所以正确的做法是**让期望值变成真的**：把实算哈希抄进 tarfs 的期望值缓冲区。之后
`cfg+0xc == cfg+0x4c`、`http_init` 的重算比对、端口反篡改比对全部自然成立，一处都不用伪造，
`http_init` 与 `http_master_thread_main` 也不再需要补丁（站点数从 6 类降到 4 类）。

代价：改写期望值是**运行期写内存**，写的是 tarfs 解密后的堆缓冲区（可写）。若哪天上游把它改成只读映射，
表现会是启动即段错误 —— 冒烟会当场红。

## 分机独启（matrix boot）：后补的通路

免费版把这条功能裁掉了，但不是「隐藏开关」，而是**编译成桩**：

| 位置 | 免费版行为 |
|---|---|
| `pxe_api_add_matrix_boot` | 不解析请求，直接 `snprintf` 回 `{"result":"notsupport"}` |
| `toy_matrix_boot_walk` | 启动恢复回调：只打一行 `Recover matrix boot policy` 日志，**不往链表里放东西** → 重启后条目全丢 |
| `toy_matrix_boot_add` | `mov eax, -22 ; ret`（没人调用它，接口层根本没走到这里） |

其余 `list_json` / `del` / `save` / `recovery` / `proc` / `init` 都是完整实现 ——
记录链表、TLV 持久化、启动流程都在，缺的只是「登记一条」和「恢复时放回链表」两个入口。
所以补法是接管这两个桩：

| 文件 | 作用 |
|---|---|
| `matrix_boot_add.c` | 两个入口：`matrix_boot_add_api`（解析 `bios`/`timeout`/`mac`/`path`，校验、去重、建记录、返回 `success`/`exist`/`failed`）和 `matrix_boot_walk_hook`（启动恢复时把记录放回链表） |
| `matrix_boot_hook.py` | 把上面的 C 编成机器码块，找可执行落点（RX 段尾部的**全零填充**优先，放不下就追加到文件尾并延长 `p_filesz`），再把两个桩改成 `jmp`；带 `--verify` 与 `--check` |

记录布局由 `toy_matrix_boot_list_json` 反推：`+0x00 bios(u8)` / `+0x04 timeout(u32)` /
`+0x08 mac[18]` / `+0x1a path`，整条 `0x101c` 字节。地址全部现场从符号表 / PLT 解析，
桩的形态先核对（大小、是否已被注入），对不上直接失败。

**恢复回调的参数只有一个**：上游自己的 `toy_matrix_boot_walk(byte *entry)` 就是单参数形态，
多读第二个参数会拿到调用方没设的寄存器 —— 实测直接段错误（exit 139）。这个坑已经踩过，
C 里现在签名就是单参数。

构建期在 `$TARGETPLATFORM` 阶段用本机 `gcc` 编译（arm64 在 qemu 下跑，约多一分钟），
不需要交叉工具链；两份报告都进镜像：`/usr/share/iventoy/patch.json`、`patch-mb.json`。

### 怎么验

CI 的冒烟不是「服务起来了」就算，每次都打真接口并**重启一次容器**：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"method":"add_matrix_boot","bios":1,"timeout":30,"mac":"11-22-33-44-55-66","path":"smoke.iso"}' \
  http://127.0.0.1:26000/iventoy/json                 # 期望 { "result" : "success" }
curl -s -X POST -d '{"method":"get_matrix_boot"}' \
  http://127.0.0.1:26000/iventoy/json                 # 期望列表里有刚加的 MAC
docker restart <容器>                                  # 关键：恢复回调走的就是这段
curl -s -X POST -d '{"method":"get_matrix_boot"}' \
  http://127.0.0.1:26000/iventoy/json                 # 条目还在才算过
```

写错会得到什么：`exist`（重复）或 `failed`（MAC 形态不对）；恢复回调没接上则重启后列表变空。
列表/删除用的都是上游自己的实现，不是另写一套。

## 为什么上限是 65535 而不是「无限制」

上限就是记录表的容量。`toy_pxe_record_mac_add` 用客户端计数直接做下标写 `g_aucRecordMac[count]`，**上限只要大于数组容量就是越界写**，第 N+1 台客户端会直接写坏相邻内存。

- 原始数组 `0x78` 字节 = 20 条 × 6 字节
- 不扩段的情况下，`_end` 到页边界只剩 2648 字节 ≈ 441 条
- 现在把最后一个 RW `PT_LOAD` 的 `p_memsz` 放大，把数组放进新映射的零页：65535 条 = 393,210 字节

65535 也正好是一个 /16 网段的最大主机数；再大要改 `--max-clients`，脚本会自动算 `p_memsz`（arm64 的 `MOVZ` 只有 16 位立即数，超过 65535 会编成 `movz`+`movk` 两条）。

## 为什么不用 `.lic` 文件

`data/iventoy.lic` 只影响 Web UI 的 `lic_exist` 字段（`fun_140005a10` 里等价逻辑），不放行任何受限功能。出版本相关的能力全部是编译期常量 + 上面那三条自校验。

## 版本更新会怎么坏，以及怎么修

补丁**不写死地址**，靠三样东西定位：ELF 符号表（`toy_get_free_max_client` / `g_aucRecordMac` / `dhcp_read_callback_*` …）、指令字节签名、以及符号引用（所有引用 `g_aucRecordMac` 的指令都被改写）。

上游发布新版本时，可能踩到这些点：

| 变更 | 影响 | 表现 | 处理 |
|---|---|---|---|
| 编译器/优化级别变了，指令序列变形 | 第 1、3、4 条的签名匹配不到 | `[FAIL] <函数名>: ... not found`，退出码 2，**构建失败** | 按下面步骤重新锚定 |
| 函数被内联 | `toy_get_free_max_client` 调用点消失 | 同上（函数还在但没人调用） | 检查 `dhcp_read_callback_*` 里是否出现内联的常量比较 |
| 结构体/数组尺寸变了 | 容量换算错 | 脚本打印 `capacity 20 -> 65535`，看数字是否与 `0x78/6` 相符 | 一般自愈（按符号 size 算） |
| 新增第 4 层校验 | 未覆盖 | **容器能起、但某个功能消失** | 必须做冒烟：`26000` 有应答 **且返回的是一份真页面（≥20 KB、含 `<html>`）** |
| 二进制被 strip | 符号表没了 | `symbol not found: ... (is the upstream binary stripped?)` | 退化为纯签名匹配，需要重写 `ELF.syms` 的兜底 |

**重新锚定**（新版本对不上时）：

```bash
# 1. 拿到失败函数的反汇编，确认指令形状
objdump -d --no-show-raw-insn -M intel iventoy | sed -n '/<main_start>:/,/^$/p' | grep -B4 -A8 'strncasecmp'
# arm64: python3 scripts/arm_scan.py（或任意 aarch64 反汇编器），找 bl g_ascii_strncasecmp

# 2. 改 patches/iventoy_patch.py 里对应函数的签名字节
# 3. 本地验证（不写文件，只报告命中多少处）
python3 patches/iventoy_patch.py --check iventoy

# 4. 确认 13（amd64）/ 14（arm64）处全部命中后再跑真实构建
```

`--report` 会输出一份 JSON，记录每个站点的 VA、改前/改后字节和原因，镜像里也留了一份（`/usr/share/iventoy/patch.json`），出问题可以直接对账。

## CI 里的闸门

1. `ci-validate.yml` 的 `patch` 作业：每次 push/PR 都对**当前固定版本**跑 `--check`，签名失配立即红。
2. `track-iventoy-release.yml`：自动跟新版本时，**先对新版本跑 `--check`**；不通过就中止，不 bump 版本、不打 tag、不派发构建——不会产出坏镜像。
3. `docker-publish.yml`：发布后按架构起容器验证 `26000` 返回 200，并断言镜像里 `patch.json` 的 `ok` 为 true。
4. `docker-publish.yml` 的「校验镜像内二进制与补丁报告一致」：把镜像里的 `lib/iventoy` 和构建期写的 `patch.json` 一起取出来，跑
   `python3 patches/iventoy_patch.py --verify <patch.json> <bin>` 逐站点对字节。冒烟只能证明「容器起得来」，
   这一步才能证明「镜像里那份二进制真是打过补丁的」——补丁被跳过、被回滚、只改了一半，都会红。
5. `track-iventoy-release.yml` 的源码漂移对账：`patches/` 的改动如果没进发布镜像，就按 `main` 重发一次发布构建，
   不用等上游发新版本（2026-11-11 那次的根因就是「合并进 main ≠ 发布了镜像」）。

## 已知边界

- 只处理 Linux amd64 / arm64；Windows 的 `iVentoy_64.exe` 是另一套地址，没纳入。
- 65535 是**并发出租**上限，不是 DHCP 池大小；池子仍在 WebUI 里配。
- 补丁改的是「客户端准入」，不改变 PXE/TFTP/HTTP 的实际带宽与并发能力。
- arm64 上游包是 **trial** 版（官方只发布 arm64 试用版），补丁把它的 1 客户端限制也抬到 65535，但试用版是否还有别的功能限制未逐项验证。

## 调整上限

镜像默认 65535。两种改法都不用动代码：

- **仓库变量（推荐，下次 CI 构建生效）**：Settings → Secrets and variables → Actions → **Variables** → New repository variable，
  名字 `IVENTOY_MAX_CLIENTS`，值填数字。`docker-publish.yml` 会把它当 `--build-arg` 传下去。
- **本地构建**：`docker build --build-arg IVENTOY_MAX_CLIENTS=1024 .`

两个约束：数组是 6 字节/条、放在扩出来的 .bss 零页里（脚本按这个数字算 `p_memsz`），
以及 arm64 的 `MOVZ` 只有 16 位立即数（超过 65535 脚本会编成 `movz`+`movk`，能用但没必要）。
取值必须在 **1..1000000** 之间，越界脚本直接报错退出（不写文件）。
