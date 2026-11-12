#!/usr/bin/env python3
"""补上免费版被裁掉的两条「分机独启」(matrix boot) 通路，并把配置落盘改成串行写。

免费版把这两个函数编译成了桩：

    pxe_api_add_matrix_boot  -> 不解析请求，直接回 {"result":"notsupport"}
    toy_matrix_boot_walk     -> 启动恢复时只打一行日志，不往链表里放东西
                                （所以重启后条目全丢）

而 list / del / save / recovery / proc / init 都是完整实现，缺的只是这两个入口。

第三个入口不是桩，是加固：`toy_util_buf2file` 用 open(O_TRUNC) + 一次 write 落盘，
非原子。配置保存会在「HTTP API 线程」和「关闭流程」两个线程上同时发生，撞在一起就
写出半截 config.dat，加载侧校验不过会整份丢弃（config data corrupted, ignore it.）
—— 所有设置回默认、分机独启列表空掉，且不报错。这里把它换成加 flock 串行的等价实现。

做法：

1. 把 `matrix_boot_add.c` 编成位置无关的机器码块（x86-64 / aarch64，编译器按宿主与
   目标架构自动选：同架构用本机 gcc，否则用 aarch64-linux-gnu-* 交叉工具链）；
2. 找可执行落点：优先 RX 段尾部到下一个 LOAD 之间的**全零填充**，
   放不下就追加到文件尾并延长 RX 段的 p_filesz/p_memsz；
3. 把三个入口改成跳进代码块里对应的函数。

地址一律通过 ELF 符号表 / PLT 现场解析；桩的形态要先核对（大小、以及它是否已经
被注入过），对不上直接失败 —— 宁可构建红，也不产出半残的镜像。

用法：
    matrix_boot_hook.py BINARY --report FILE      # 注入
    matrix_boot_hook.py BINARY --verify FILE      # 校验产物与报告
    matrix_boot_hook.py BINARY --check            # 只做可行性检查，不写文件、不编译
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from iventoy_patch import ELF  # noqa: E402

EM_X86_64, EM_AARCH64 = 62, 183

CFLAGS = ("-Os -ffreestanding -fno-builtin -fno-stack-protector -fno-pic -fno-pie "
          "-fno-asynchronous-unwind-tables -fno-unwind-tables -Wall -Wextra").split()

TOOLCHAIN = {
    EM_X86_64: ("gcc", "ld", "objcopy", ["-fcf-protection=none", "-mno-red-zone"]),
    EM_AARCH64: ("aarch64-linux-gnu-gcc", "aarch64-linux-gnu-ld",
                 "aarch64-linux-gnu-objcopy", []),
}
CROSS_PREFIX = {EM_X86_64: "x86_64-linux-gnu-", EM_AARCH64: "aarch64-linux-gnu-"}
ARCH_NAME = {EM_X86_64: "amd64", EM_AARCH64: "arm64"}

# 要接管的上游函数 -> (代码块里的实现, 允许的最大长度)
# None 表示「不是桩，是整体替换」，长度不设上限。
ENTRIES = [
    ("pxe_api_add_matrix_boot", "matrix_boot_add_api", 96),
    ("pxe_api_clr_matrix_boot", "matrix_boot_clr_api", 96),
    ("toy_matrix_boot_walk", "matrix_boot_walk_hook", 96),
    ("toy_util_buf2file", "buf2file_hook", None),
]
STUB_MAX_LEN = 96          # 桩类入口超过这个长度说明上游可能已经实现了，别乱覆盖

# 调用点重定向：把 (上游函数) 的所有 call 改成调到 (代码块里的实现)。
# 用于「不能替换函数本身、但需要在其调用前后插一段逻辑」的场合 —— 这里是用它
# 在 tarfs 查找静态文件时改内存里的 WebUI 页面（免费版把分机独启按钮的前台闸门
# 写死在 index.html 的 g_current_edition = 0 上）。
CALL_REDIRECTS = [
    ("toy_tarfs_find_file", "tarfs_find_hook"),
]

# 代码块要引用的外部符号
FUNC_SYMS = ["toy_json_get_int", "toy_json_get_uint", "toy_json_get_string",
             "toy_json_get_string_ex", "toy_log", "toy_tarfs_find_file",
             "toy_matrix_boot_clear_all"]
FUNC_PLTS = ["g_list_first", "g_list_append", "g_mutex_lock", "g_mutex_unlock",
             "g_malloc0", "g_free", "g_ascii_toupper", "g_regex_match_simple",
             "g_snprintf", "strcmp", "memcpy", "g_strlcpy",
             "open64", "write", "close", "flock", "memcmp"]
DATA_SYMS = ["g_pstEntryList", "g_stMbLock", "g_pucTarfsBuf", "g_iTarfsLen"]


def toolchain(machine: int) -> tuple[str, str, str, list[str]]:
    """同架构用本机 gcc，跨架构用交叉前缀（Dockerfile 里跑在 $TARGETPLATFORM 阶段）。"""
    cc, ld, objcopy, extra = TOOLCHAIN[machine]
    host = platform.machine().lower()
    same = host in {EM_X86_64: ("x86_64", "amd64"), EM_AARCH64: ("aarch64", "arm64")}[machine]
    if same:
        cc, ld, objcopy = "gcc", "ld", "objcopy"
        if machine == EM_AARCH64:
            extra = []
    elif shutil.which(cc) is None:
        raise SystemExit("需要交叉工具链 %s（宿主 %s）" % (cc, host))
    return cc, ld, objcopy, extra


def resolve(elf: ELF) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in FUNC_SYMS + DATA_SYMS:
        va, _sz = elf.sym(name)
        out[name] = va
    for name in FUNC_PLTS:
        va = elf.plt(name)
        if not va:
            raise SystemExit("PLT 里找不到 %s" % name)
        out[name] = va
    return out


def phdrs(data: bytes):
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    out = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        out.append({"off": o,
                    "type": struct.unpack_from("<I", data, o)[0],
                    "flags": struct.unpack_from("<I", data, o + 4)[0],
                    "offset": struct.unpack_from("<Q", data, o + 8)[0],
                    "vaddr": struct.unpack_from("<Q", data, o + 16)[0],
                    "filesz": struct.unpack_from("<Q", data, o + 32)[0],
                    "memsz": struct.unpack_from("<Q", data, o + 40)[0]})
    return out


def compile_blob(machine: int, text_va: int, syms: dict[str, int], src: Path):
    """返回 (raw bytes, blob ELF 里的符号表)。"""
    cc, ld, objcopy, extra = toolchain(machine)
    work = Path(tempfile.mkdtemp(prefix="mbhook-"))
    try:
        obj, elf_path, blob_path = work / "mb.o", work / "mb.elf", work / "mb.bin"
        subprocess.run([cc, *CFLAGS, *extra, "-c", str(src), "-o", str(obj)], check=True)
        defs = ["--defsym=%s=0x%X" % (k, v) for k, v in syms.items()]
        r = subprocess.run([ld, "-Ttext=0x%X" % text_va, *defs, "-e", "0",
                            "-o", str(elf_path), str(obj)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("ld 失败:\n%s%s" % (r.stdout, r.stderr))
        subprocess.run([objcopy, "-O", "binary", "-j", ".text", "-j", ".rodata",
                        str(elf_path), str(blob_path)], check=True)
        blob = blob_path.read_bytes()
        blob_elf = ELF(elf_path.read_bytes())
        return blob, blob_elf
    finally:
        shutil.rmtree(work, ignore_errors=True)


def plan_placement(data: bytes, rx, nxt, blob_len: int):
    """决定代码块落在哪：RX 段尾部的全零填充，或追加到文件尾。"""
    rx_end_file = rx["offset"] + rx["filesz"]
    rx_end_va = rx["vaddr"] + (rx_end_file - rx["offset"])
    gap_file = (nxt["offset"] - rx_end_file) if nxt else 0
    gap_va = (nxt["vaddr"] - rx_end_va) if nxt else 0x1000000
    if blob_len <= gap_file and blob_len <= gap_va:
        if any(data[rx_end_file:rx_end_file + blob_len]):
            return None, "RX 段尾部填充不是全零，不能直接覆盖"
        return ("segment-gap", rx_end_file, rx_end_va, rx["filesz"] + blob_len), None
    cave_off = (len(data) + 0xFFF) & ~0xFFF
    cave_va = rx["vaddr"] + (cave_off - rx["offset"])
    if nxt and cave_va + blob_len > nxt["vaddr"]:
        return None, ("尾部落点 0x%X..0x%X 会压到下一个段 0x%X"
                      % (cave_va, cave_va + blob_len, nxt["vaddr"]))
    return ("file-append", cave_off, cave_va,
            cave_off + blob_len - rx["offset"]), None


def make_jump(machine: int, from_va: int, to_va: int, length: int) -> bytes:
    if machine == EM_X86_64:
        delta = to_va - (from_va + 5)
        if not -(1 << 31) <= delta < (1 << 31):
            raise SystemExit("跳转距离超出 rel32（0x%X -> 0x%X）" % (from_va, to_va))
        jmp = b"\xe9" + struct.pack("<i", delta)
    else:
        delta = to_va - from_va
        if not -(1 << 27) <= delta < (1 << 27):
            raise SystemExit("跳转距离超出 b 指令范围（0x%X -> 0x%X）" % (from_va, to_va))
        jmp = struct.pack("<I", 0x14000000 | ((delta >> 2) & 0x3FFFFFF))
    if len(jmp) > length:
        raise SystemExit("桩只有 %d 字节，放不下 %d 字节的跳转" % (length, len(jmp)))
    return jmp + b"\x90" * (length - len(jmp))


def injected(machine: int, va: int, size: int, old: bytes) -> bool:
    """判断这个入口是不是已经被注入过。

    arm64 上不能靠「第一条指令是 b」判断：编译器会把几个内容相同的桩折叠成
    `b <另一个桩>`（实测 pxe_api_clr_matrix_boot 就是 `b pxe_api_add_matrix_boot`），
    这是上游的正常形态。x86 只在 jmp rel32 跳到本函数体之外时才认定是注入痕迹，
    arm64 直接放行（重复注入的后果只是多一份代码块，钩子本身幂等）。
    """
    if machine == EM_X86_64:
        if old[:1] == b"\xe9":
            rel = struct.unpack_from("<i", old, 1)[0]
            target = va + 5 + rel
            return not (va <= target < va + size)
    return False


def redirect_calls(data: bytearray, machine: int, rx, hi: int,
                   target_va: int, hook_va: int) -> list[dict]:
    """把 RX 段内所有「调用 target_va」的指令改成调用 hook_va。

    x86-64 认 `e8 rel32`，aarch64 认 `bl imm26`。目标地址一律按 VA 现算现比，
    所以上游版本挪了位置也不用改脚本；距离越界直接失败。
    """
    lo = rx["offset"]
    base = rx["vaddr"] - rx["offset"]
    out: list[dict] = []
    if machine == EM_X86_64:
        i = lo
        while i < hi - 5:
            if data[i] == 0xE8:
                rel = struct.unpack_from("<i", data, i + 1)[0]
                va = i + base
                if va + 5 + rel == target_va:
                    new_rel = hook_va - (va + 5)
                    if not -(1 << 31) <= new_rel < (1 << 31):
                        raise SystemExit("调用点重定向超出 rel32（0x%X）" % va)
                    old = bytes(data[i:i + 5])
                    struct.pack_into("<i", data, i + 1, new_rel)
                    out.append({"va": "0x%X" % va, "off": i, "old": old.hex(),
                                "new": bytes(data[i:i + 5]).hex()})
            i += 1
    else:
        i = lo
        while i < hi - 4:
            w = struct.unpack_from("<I", data, i)[0]
            if (w >> 26) == 0x25:                       # bl
                imm = w & 0x03FFFFFF
                if imm & 0x02000000:
                    imm -= 0x04000000
                va = i + base
                if va + imm * 4 == target_va:
                    delta = (hook_va - va) >> 2
                    if not -(1 << 25) <= delta < (1 << 25):
                        raise SystemExit("调用点重定向超出 bl 范围（0x%X）" % va)
                    old = bytes(data[i:i + 4])
                    struct.pack_into("<I", data, i, 0x94000000 | (delta & 0x03FFFFFF))
                    out.append({"va": "0x%X" % va, "off": i, "old": old.hex(),
                                "new": bytes(data[i:i + 4]).hex()})
            i += 4
    return out


def verify(binary: str, report_path: str) -> int:
    rep = json.loads(Path(report_path).read_text())
    data = Path(binary).read_bytes()
    if hashlib.sha256(data).hexdigest() != rep["output_sha256"]:
        print("FAIL: 二进制 sha256 与报告不符", file=sys.stderr)
        return 1
    cave = data[rep["cave_off"]:rep["cave_off"] + rep["blob_len"]]
    if hashlib.sha256(cave).hexdigest() != rep["blob_sha256"]:
        print("FAIL: 代码块内容与报告不符", file=sys.stderr)
        return 1
    for stub in rep["stubs"]:
        got = data[stub["off"]:stub["off"] + stub["len"]]
        if got.hex() != stub["new"]:
            print("FAIL: %s 处的字节与报告不符" % stub["symbol"], file=sys.stderr)
            return 1
    for site in rep.get("callsites", []):
        off = site["off"]
        got = data[off:off + len(site["new"]) // 2]
        if got.hex() != site["new"]:
            print("FAIL: %s 的调用点 0x%s 与报告不符" % (site["target"], site["va"]),
                  file=sys.stderr)
            return 1
    for p in phdrs(data):
        if p["off"] == rep["phdr_off"]:
            if p["filesz"] != rep["rx_filesz"] or p["memsz"] != rep["rx_memsz"]:
                print("FAIL: RX 段长度与报告不符", file=sys.stderr)
                return 1
            break
    else:
        raise SystemExit("FAIL: 找不到记录的 program header")
    print("OK: 分机独启钩子在位（cave 0x%X，%d 字节，%d 个入口，%d 个调用点）"
          % (rep["cave_va"], rep["blob_len"], len(rep["stubs"]),
             len(rep.get("callsites", []))))
    return 0


def check(binary: str) -> int:
    """不做任何修改的可行性检查：符号、桩形态、落点容量。"""
    data = Path(binary).read_bytes()
    machine = struct.unpack_from("<H", data, 0x12)[0]
    if machine not in TOOLCHAIN:
        raise SystemExit("不支持的架构 e_machine=%d" % machine)
    elf = ELF(data)
    resolve(elf)                                   # 缺符号会在这里直接退出
    plist = phdrs(data)
    rx = next((p for p in plist if p["type"] == 1 and (p["flags"] & 0x5) == 0x5), None)
    if rx is None:
        raise SystemExit("找不到 RX LOAD 段")
    nxt = min((p for p in plist if p["type"] == 1 and p["vaddr"] > rx["vaddr"]),
              key=lambda p: p["vaddr"], default=None)
    print("%s: 符号齐全" % ARCH_NAME[machine])
    for sym, fn, maxlen in ENTRIES:
        va, size = elf.sym(sym)
        head = data[elf.off(va):elf.off(va) + min(size, 8)].hex()
        kind = "桩" if maxlen else "整体替换"
        print("  %-26s va=0x%-7X size=%-3d head=%s  -> %s（%s）"
              % (sym, va, size, head, fn, kind))
        if maxlen and size > maxlen:
            print("  [警告] 该函数 %d 字节，可能上游已经实现，注入会覆盖真实逻辑" % size)
    probe = bytearray(data)
    for target_sym, hook_fn in CALL_REDIRECTS:
        target_va, _sz = elf.sym(target_sym)
        n = len(redirect_calls(probe, machine, rx, rx["offset"] + rx["filesz"],
                              target_va, target_va))
        mark = "" if n else "  [警告] 找不到调用点，注入会直接失败"
        print("  调用点 %-24s %d 处 -> %s%s" % (target_sym, n, hook_fn, mark))
    placement, err = plan_placement(data, rx, nxt, 8192)      # 按 8KB 上限估容量
    gap = (nxt["offset"] - (rx["offset"] + rx["filesz"])) if nxt else 0
    if placement is not None:
        print("  落点可用：%s（RX 尾部全零填充 %d 字节）" % (placement[0], gap))
    else:
        print("  RX 尾部填充 %d 字节；8KB 上限下 %s —— 实际由编译后的代码块大小决定，"
              "太小就追加到文件尾" % (gap, err))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--report")
    ap.add_argument("--verify", metavar="REPORT")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--c-source", default=str(Path(__file__).with_name("matrix_boot_add.c")))
    args = ap.parse_args()

    if args.verify:
        return verify(args.binary, args.verify)
    if args.check:
        return check(args.binary)
    if not args.report:
        ap.error("需要 --report / --verify REPORT / --check")

    path = Path(args.binary)
    raw = path.read_bytes()
    data = bytearray(raw)
    machine = struct.unpack_from("<H", data, 0x12)[0]
    if machine not in TOOLCHAIN:
        raise SystemExit("不支持的架构 e_machine=%d" % machine)
    elf = ELF(bytes(data))
    syms = resolve(elf)

    plist = phdrs(data)
    rx = next((p for p in plist if p["type"] == 1 and (p["flags"] & 0x5) == 0x5), None)
    if rx is None:
        raise SystemExit("找不到 RX LOAD 段")
    nxt = min((p for p in plist if p["type"] == 1 and p["vaddr"] > rx["vaddr"]),
              key=lambda p: p["vaddr"], default=None)

    # 入口形态先核对：太短 / 桩过长 / 已经是跳转（重复注入）都拒绝
    stubs = []
    for sym, fn, maxlen in ENTRIES:
        va, size = elf.sym(sym)
        if size < 4:
            raise SystemExit("%s 大小 %d 不合理，拒绝注入" % (sym, size))
        if maxlen and size > maxlen:
            raise SystemExit("%s 大小 %d > %d（可能上游已实现），拒绝注入"
                             % (sym, size, maxlen))
        off = elf.off(va)
        old = bytes(data[off:off + size])
        if injected(machine, va, size, old):
            raise SystemExit("%s 已经是被改过的跳转，看起来注入过，拒绝重复注入" % sym)
        stubs.append({"symbol": sym, "target": fn, "va": va, "off": off,
                      "len": size, "old": old})

    src = Path(args.c_source)
    rx_end_va = rx["vaddr"] + (rx["offset"] + rx["filesz"] - rx["offset"])
    blob, blob_elf = compile_blob(machine, rx_end_va, syms, src)
    placement, err = plan_placement(bytes(data), rx, nxt, len(blob))
    if placement is None:
        if "不是全零" not in (err or ""):
            raise SystemExit(err)
        placement = None
    if placement is None or placement[0] == "file-append":
        cave_off = (len(data) + 0xFFF) & ~0xFFF
        cave_va = rx["vaddr"] + (cave_off - rx["offset"])
        blob, blob_elf = compile_blob(machine, cave_va, syms, src)
        placement, err = plan_placement(bytes(data), rx, nxt, len(blob))
        if placement is None:
            raise SystemExit(err)

    kind, cave_off, cave_va, new_filesz = placement
    rx_hi_orig = rx["offset"] + rx["filesz"]          # 重定向只扫原始代码，别扫到代码块
    struct.pack_into("<Q", data, rx["off"] + 32, new_filesz)     # p_filesz
    struct.pack_into("<Q", data, rx["off"] + 40, new_filesz)     # p_memsz
    if cave_off > len(data):
        data.extend(b"\x00" * (cave_off - len(data)))
    data[cave_off:cave_off + len(blob)] = blob

    patched = []
    for stub in stubs:
        target_va, _sz = blob_elf.sym(stub["target"])
        new = make_jump(machine, stub["va"], target_va, stub["len"])
        data[stub["off"]:stub["off"] + stub["len"]] = new
        patched.append({"symbol": stub["symbol"], "target": stub["target"],
                        "va": "0x%X" % stub["va"], "off": stub["off"],
                        "len": stub["len"], "old": stub["old"].hex(),
                        "new": new.hex(), "target_va": "0x%X" % target_va})

    callsites = []
    for target_sym, hook_fn in CALL_REDIRECTS:
        target_va, _sz = elf.sym(target_sym)
        hook_va, _sz = blob_elf.sym(hook_fn)
        sites = redirect_calls(data, machine, rx, rx_hi_orig, target_va, hook_va)
        if not sites:
            raise SystemExit("%s 没有找到任何调用点，拒绝产出（上游可能改名/内联了）"
                             % target_sym)
        for s in sites:
            s["target"] = target_sym
            s["hook"] = hook_fn
        callsites.extend(sites)
        print("  调用点重定向：%s x%d -> %s @0x%X"
              % (target_sym, len(sites), hook_fn, hook_va))

    out = bytes(data)
    path.write_bytes(out)
    report = {
        "binary": str(path),
        "arch": ARCH_NAME[machine],
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "output_sha256": hashlib.sha256(out).hexdigest(),
        "placement": kind,
        "cave_va": cave_va,
        "cave_off": cave_off,
        "blob_len": len(blob),
        "blob_sha256": hashlib.sha256(blob).hexdigest(),
        "stubs": patched,
        "callsites": callsites,
        "phdr_off": rx["off"],
        "rx_filesz": new_filesz,
        "rx_memsz": new_filesz,
        "resolved": {k: "0x%X" % v for k, v in syms.items()},
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print("分机独启钩子: %s %s，代码块 %d 字节 @0x%X，接管 %d 个入口"
          % (report["arch"], kind, len(blob), cave_va, len(patched)))
    for p in patched:
        print("  %-26s 0x%X (%d 字节) -> %s @0x%X"
              % (p["symbol"], int(p["va"], 16), p["len"], p["target"], int(p["target_va"], 16)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
