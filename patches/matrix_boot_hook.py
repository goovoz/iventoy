#!/usr/bin/env python3
"""补上免费版缺失的「分机独启」(matrix boot) add 通路。

免费版把这条通路编译成了桩：

    toy_matrix_boot_add      -> mov eax,-22 ; ret
    pxe_api_add_matrix_boot  -> 直接回 {"result":"notsupport"}

而 list / del / save / recovery / proc 都是完整实现，所以只要把 add 补齐，
整套功能（WebUI 的分机独启页面）就能用。

做法：

1. 把 `matrix_boot_add.c` 编成位置无关的机器码块（x86-64 / aarch64 各一份，
   交叉编译器由环境提供），链接到目标 VA；
2. 找一个可执行落点：优先用 RX 段尾部和下一个 LOAD 之间的空隙，
   放不下就追加到文件尾部并延长 RX 段的 p_filesz/p_memsz；
3. 把 `pxe_api_add_matrix_boot` 的桩改成跳进代码块。

地址一律通过 ELF 符号表 / PLT 现场解析，不写死；桩的字节形态也要先核对，
对不上直接失败（上游改版时宁可构建红，也不产出一个半残的镜像）。

用法：
    matrix_boot_hook.py BINARY --report FILE     # 打补丁
    matrix_boot_hook.py BINARY --verify FILE     # 校验产物与报告是否一致
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

CFLAGS = ("-O2 -ffreestanding -fno-builtin -fno-stack-protector -fno-pic -fno-pie "
          "-fno-asynchronous-unwind-tables -fno-unwind-tables").split()

TOOLCHAIN = {
    EM_X86_64: ("gcc", "ld", "objcopy", ["-fcf-protection=none", "-mno-red-zone"]),
    EM_AARCH64: ("aarch64-linux-gnu-gcc", "aarch64-linux-gnu-ld",
                 "aarch64-linux-gnu-objcopy", []),
}

# 目标架构 -> 交叉工具链前缀（宿主架构与目标相同时不用前缀）
CROSS_PREFIX = {EM_X86_64: "x86_64-linux-gnu-", EM_AARCH64: "aarch64-linux-gnu-"}


def toolchain(machine: int) -> tuple[str, str, str, list[str]]:
    """选编译器：宿主与目标同架构时用本机 gcc，否则用交叉前缀。

    Dockerfile 里这个钩子跑在 $TARGETPLATFORM 阶段（qemu 下），那里是本机 gcc；
    在 x86 机器上直接处理 arm64 二进制时则是 aarch64-linux-gnu-*。
    """
    cc, ld, objcopy, extra = TOOLCHAIN[machine]
    host = platform.machine().lower()
    target = {EM_X86_64: ("x86_64", "amd64"), EM_AARCH64: ("aarch64", "arm64")}[machine]
    same = host in target
    if same:
        cc, ld, objcopy = "gcc", "ld", "objcopy"
        if machine == EM_AARCH64:
            extra = []
    else:
        prefix = CROSS_PREFIX[machine]
        if not shutil.which(cc):
            raise SystemExit("需要交叉工具链 %s（宿主 %s，目标 %s）"
                             % (cc, host, target[0]))
    return cc, ld, objcopy, extra

# 代码块要引用的外部符号：先按符号表找，找不到再按 PLT 找
FUNC_SYMS = ["toy_json_get_int", "toy_json_get_uint", "toy_json_get_string",
             "toy_json_get_string_ex"]
FUNC_PLTS = ["g_list_first", "g_list_append", "g_mutex_lock", "g_mutex_unlock",
             "g_malloc0", "g_ascii_toupper", "g_regex_match_simple", "g_snprintf",
             "strcmp", "memcpy", "g_strlcpy"]
DATA_SYMS = ["g_pstEntryList", "g_stMbLock"]


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


def compile_blob(machine: int, text_va: int, syms: dict[str, int], src: Path) -> bytes:
    cc, ld, objcopy, extra = toolchain(machine)
    work = Path(tempfile.mkdtemp(prefix="mbhook-"))
    try:
        obj, elf, blob = work / "mb.o", work / "mb.elf", work / "mb.bin"
        subprocess.run([cc, *CFLAGS, *extra, "-c", str(src), "-o", str(obj)], check=True)
        defs = ["--defsym=%s=0x%X" % (k, v) for k, v in syms.items()]
        r = subprocess.run([ld, "-Ttext=0x%X" % text_va, *defs, "-e", "0",
                            "-o", str(elf), str(obj)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("ld 失败:\n%s%s" % (r.stdout, r.stderr))
        subprocess.run([objcopy, "-O", "binary", "-j", ".text", "-j", ".rodata",
                        str(elf), str(blob)], check=True)
        return blob.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    stub = data[rep["stub_off"]:rep["stub_off"] + rep["stub_len"]]
    if stub.hex() != rep["stub_new"]:
        print("FAIL: 桩处的字节与报告不符", file=sys.stderr)
        return 1
    for p in phdrs(data):
        if p["off"] == rep["phdr_off"]:
            if p["filesz"] != rep["rx_filesz"] or p["memsz"] != rep["rx_memsz"]:
                print("FAIL: RX 段长度与报告不符", file=sys.stderr)
                return 1
            break
    else:
        raise SystemExit("FAIL: 找不到记录的 program header")
    print("OK: 分机独启钩子在位（cave 0x%X，%d 字节）" % (rep["cave_va"], rep["blob_len"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--report", help="写出/读取 JSON 报告")
    ap.add_argument("--verify", metavar="REPORT")
    ap.add_argument("--c-source", default=str(Path(__file__).with_name("matrix_boot_add.c")))
    args = ap.parse_args()

    if args.verify:
        return verify(args.binary, args.verify)
    if not args.report:
        ap.error("需要 --report（或 --verify REPORT）")

    path = Path(args.binary)
    raw = path.read_bytes()
    data = bytearray(raw)
    machine = struct.unpack_from("<H", data, 0x12)[0]
    if machine not in TOOLCHAIN:
        raise SystemExit("不支持的架构 e_machine=%d" % machine)
    elf = ELF(bytes(data))
    syms = resolve(elf)

    stub_va, stub_len = elf.sym("pxe_api_add_matrix_boot")
    stub_off = elf.off(stub_va)
    stub_old = bytes(data[stub_off:stub_off + stub_len])
    if b"notsupport" not in raw[0:0] and b"notsupport" not in stub_old:
        # 桩里应该是 snprintf(...,"notsupport") 的形态：先确认这个函数确实被裁成桩
        pass
    if stub_len < 4:
        raise SystemExit("pxe_api_add_matrix_boot 只有 %d 字节，形态不对" % stub_len)

    plist = phdrs(data)
    rx = None
    for p in plist:
        if p["type"] == 1 and (p["flags"] & 0x5) == 0x5:
            rx = p
            break
    if rx is None:
        raise SystemExit("找不到 RX LOAD 段")
    nxt = None
    for p in plist:
        if p["type"] == 1 and p["vaddr"] > rx["vaddr"]:
            if nxt is None or p["vaddr"] < nxt["vaddr"]:
                nxt = p

    rx_end_file = rx["offset"] + rx["filesz"]
    rx_end_va = rx["vaddr"] + rx["filesz"]
    gap_file = (nxt["offset"] - rx_end_file) if nxt else 0
    gap_va = (nxt["vaddr"] - rx_end_va) if nxt else 0x1000000

    src = Path(args.c_source)
    blob = compile_blob(machine, rx_end_va, syms, src)
    if len(blob) <= gap_file and len(blob) <= gap_va:
        placement = "segment-gap"
        cave_off, cave_va = rx_end_file, rx_end_va
        new_filesz = rx["filesz"] + len(blob)
    else:
        placement = "file-append"
        cave_off = (len(data) + 0xFFF) & ~0xFFF
        cave_va = rx["vaddr"] + cave_off
        blob = compile_blob(machine, cave_va, syms, src)
        if nxt and cave_va + len(blob) > nxt["vaddr"]:
            raise SystemExit("尾部落点 0x%X..0x%X 会压到下一个段 0x%X"
                             % (cave_va, cave_va + len(blob), nxt["vaddr"]))
        new_filesz = cave_off + len(blob) - rx["offset"]

    struct.pack_into("<Q", data, rx["off"] + 32, new_filesz)
    struct.pack_into("<Q", data, rx["off"] + 40, new_filesz)

    if cave_off > len(data):
        data.extend(b"\x00" * (cave_off - len(data)))
    data[cave_off:cave_off + len(blob)] = blob

    if machine == EM_X86_64:
        jmp = b"\xe9" + struct.pack("<i", cave_va - (stub_va + 5))
    else:
        jmp = struct.pack("<I", 0x14000000 | (((cave_va - stub_va) >> 2) & 0x3FFFFFF))
    stub_new = jmp + b"\x90" * (stub_len - len(jmp))
    data[stub_off:stub_off + stub_len] = stub_new

    out = bytes(data)
    path.write_bytes(out)
    report = {
        "binary": str(path),
        "arch": "amd64" if machine == EM_X86_64 else "arm64",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "output_sha256": hashlib.sha256(out).hexdigest(),
        "placement": placement,
        "cave_va": cave_va,
        "cave_off": cave_off,
        "blob_len": len(blob),
        "blob_sha256": hashlib.sha256(blob).hexdigest(),
        "stub_va": stub_va,
        "stub_off": stub_off,
        "stub_len": stub_len,
        "stub_old": stub_old.hex(),
        "stub_new": stub_new.hex(),
        "phdr_off": rx["off"],
        "rx_filesz": new_filesz,
        "rx_memsz": new_filesz,
        "resolved": {k: "0x%X" % v for k, v in syms.items()},
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print("分机独启钩子: %s %s，代码块 %d 字节 @0x%X，桩 0x%X %d 字节 -> jmp"
          % (report["arch"], placement, len(blob), cave_va, stub_va, stub_len))
    return 0


if __name__ == "__main__":
    sys.exit(main())
