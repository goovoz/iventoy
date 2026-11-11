# iVentoy 解限补丁

镜像里的 `lib/iventoy` 是**打过补丁**的上游二进制。这个目录是补丁的唯一来源，构建时会自动应用，不产出「半补丁」的镜像。

## 改了什么

| # | 站点 | 上游行为 | 补丁后 |
|---|---|---|---|
| 1 | `toy_get_free_max_client` | 返回 20（Free 版）/ 1（test mode） | 整个函数替换成 `return MAX_CLIENTS`（默认 65535） |
| 2 | `g_aucRecordMac` | 20 条 × 6 字节静态数组，客户端记录表 | 数组搬到新的 .bss 页（`p_memsz` 扩出来的零页），容量 = MAX_CLIENTS |
| 3 | `dhcp_read_callback_normal` / `_proxy` | 架构是 ARM64 / LoongArch 时进拒绝分支 | 拒绝分支的跳转改成 NOP，直接进放行路径 |
| 4 | `main_start` | 自校验：exe 的 SHA-256 与 tarfs 里的 `sha256/<type>.txt` 比对，不等就退出 | 比对强制相等 |
| 5 | `http_init` | **同一个比对结果**决定主线程跑 API 模式（26000）还是 PXE 模式（16000） | 结果固定为 1 → 永远 API 模式 |
| 6 | `http_master_thread_main` | 反篡改：`cfg[0xc] != cfg[0x4c]` 时把 API 端口改成 `60000+time%1000` 随机端口 | 比较强制相等 → 固定 26000 |

第 4/5/6 条是连锁的：**只绕过第 4 条不够**——第 5 条会让 WebUI 消失（容器在跑但没有 26000），第 6 条会让 26000 变成随机端口（日志仍打印 26000，实际监听另一个端口）。三处必须一起处理。

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
| 编译器/优化级别变了，指令序列变形 | 第 1、4、5、6 条的签名匹配不到 | `[FAIL] <函数名>: ... not found`，退出码 2，**构建失败** | 按下面步骤重新锚定 |
| 函数被内联 | `toy_get_free_max_client` 调用点消失 | 同上（函数还在但没人调用） | 检查 `dhcp_read_callback_*` 里是否出现内联的常量比较 |
| 结构体/数组尺寸变了 | 容量换算错 | 脚本打印 `capacity 20 -> 65535`，看数字是否与 `0x78/6` 相符 | 一般自愈（按符号 size 算） |
| 新增第 4 层校验 | 未覆盖 | **容器能起、但某个功能消失** | 必须做冒烟：`26000` 有应答 + 能启动 PXE 服务 |
| 二进制被 strip | 符号表没了 | `symbol not found: ... (is the upstream binary stripped?)` | 退化为纯签名匹配，需要重写 `ELF.syms` 的兜底 |

**重新锚定**（新版本对不上时）：

```bash
# 1. 拿到失败函数的反汇编，确认指令形状
objdump -d --no-show-rom-insn -M intel iventoy | sed -n '/<http_init>:/,/^$/p' | grep -A20 'strncasecmp'
# arm64: aarch64-linux-gnu-objdump -d iventoy | sed -n '/<http_init>:/,/^$/p'

# 2. 改 patches/iventoy_patch.py 里对应函数的签名字节
# 3. 本地验证（不写文件，只报告命中多少处）
python3 patches/iventoy_patch.py --check iventoy

# 4. 确认 13（amd64）/ 12（arm64）处全部命中后再跑真实构建
```

`--report` 会输出一份 JSON，记录每个站点的 VA、改前/改后字节和原因，镜像里也留了一份（`/usr/share/iventoy/patch.json`），出问题可以直接对账。

## CI 里的三道闸

1. `ci-validate.yml` 的 `patch` 作业：每次 push/PR 都对**当前固定版本**跑 `--check`，签名失配立即红。
2. `track-iventoy-release.yml`：自动跟新版本时，**先对新版本跑 `--check`**；不通过就中止，不 bump 版本、不打 tag、不派发构建——不会产出坏镜像。
3. `docker-publish.yml`：发布后按架构起容器验证 `26000` 返回 200，并断言镜像里 `patch.json` 的 `ok` 为 true。

## 已知边界

- 只处理 Linux amd64 / arm64；Windows 的 `iVentoy_64.exe` 是另一套地址，没纳入。
- 65535 是**并发出租**上限，不是 DHCP 池大小；池子仍在 WebUI 里配。
- 补丁改的是「客户端准入」，不改变 PXE/TFTP/HTTP 的实际带宽与并发能力。
- arm64 上游包是 **trial** 版（官方只发布 arm64 试用版），补丁把它的 1 客户端限制也抬到 65535，但试用版是否还有别的功能限制未逐项验证。
