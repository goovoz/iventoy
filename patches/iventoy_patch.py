#!/usr/bin/env python3
"""iVentoy Linux unlock patcher - symbol driven, version tolerant.

Hard-coded addresses break on every upstream release, so this script anchors on
the ELF symbol table and on instruction signatures inside the named functions:

  * toy_get_free_max_client          -> replaced by a constant-return stub
  * g_aucRecordMac (+6-byte records) -> relocated into fresh .bss pages and
                                        every instruction referencing it rewritten
  * dhcp_read_callback_normal/_proxy -> ARM64 / LoongArch reject branches removed
  * main_start / http_init /
    http_master_thread_main          -> self-checksum handling neutralised

Each site is verified before it is written. If a signature is not found the
script prints the function, the expected pattern and the first bytes of the
function, then exits 2 - no partial patch is ever written in that case.

Usage:
    iventoy_patch.py [--max-clients N] [--check] [--report FILE] BINARY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

EM_X86_64, EM_AARCH64 = 62, 183
# 6 bytes/record: 1e6 records = 6 MB of .bss, far beyond any DHCP pool
MAX_CLIENTS_LIMIT = 1_000_000
RECORD_SIZE = 6
NOP_X86 = b"\x90"
NOP_ARM = (0xD503201F).to_bytes(4, "little")
RET_ARM = 0xD65F03C0


# --------------------------------------------------------------------------- ELF
class ELF:
    def __init__(self, data: bytes):
        if data[:4] != b"\x7fELF" or data[4] != 2:
            raise SystemExit("not a 64-bit ELF")
        self.d = data
        self.machine = struct.unpack_from("<H", data, 18)[0]
        self.e_phoff = struct.unpack_from("<Q", data, 32)[0]
        self.e_phentsize = struct.unpack_from("<H", data, 54)[0]
        self.e_phnum = struct.unpack_from("<H", data, 56)[0]
        self.e_shoff = struct.unpack_from("<Q", data, 40)[0]
        self.e_shentsize = struct.unpack_from("<H", data, 58)[0]
        self.e_shnum = struct.unpack_from("<H", data, 60)[0]
        self.e_shstrndx = struct.unpack_from("<H", data, 62)[0]
        self.loads = []
        self._read_phdrs()
        self.sections = self._read_sections()
        self.syms = self._read_syms()
        self.text = next((s for s in self.sections if s[0] == ".text"), None)

    def _read_phdrs(self):
        for i in range(self.e_phnum):
            off = self.e_phoff + i * self.e_phentsize
            (p_type, p_flags, p_offset, p_vaddr, _pa, p_filesz, p_memsz, _al) = \
                struct.unpack_from("<IIQQQQQQ", self.d, off)
            if p_type == 1:
                self.loads.append((p_offset, p_vaddr, p_filesz, p_memsz, p_flags, off))

    def _read_sections(self):
        raw = []
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            raw.append(struct.unpack_from("<IIQQQQIIQQ", self.d, off))
        strtab_off = raw[self.e_shstrndx][4] if self.e_shstrndx < len(raw) else 0
        out = []
        for name, sh_type, _flags, addr, offset, size, link, _info, _align, entsize in raw:
            end = self.d.find(b"\0", strtab_off + name)
            out.append((self.d[strtab_off + name:end].decode("ascii", "replace"),
                        sh_type, addr, offset, size, link, entsize))
        return out

    def _read_syms(self):
        sec = next((s for s in self.sections if s[0] == ".symtab"), None)
        if not sec:
            return {}
        _, _t, _a, off, size, _link, entsize = sec
        strtab = next((s for s in self.sections if s[0] == ".strtab"), None)
        if not strtab:
            raise SystemExit(".symtab without .strtab")
        soff = strtab[3]
        syms = {}
        step = entsize or 24
        for i in range(size // step):
            p = off + i * step
            st_name, _info, _other, _shndx, st_value, st_size = struct.unpack_from("<IBBHQQ", self.d, p)
            if not st_name:
                continue
            end = self.d.find(b"\0", soff + st_name)
            syms[self.d[soff + st_name:end].decode("ascii", "replace")] = (st_value, st_size)
        return syms

    def off(self, va: int) -> int:
        for p_offset, p_vaddr, p_filesz, _m, _f, _o in self.loads:
            if p_vaddr <= va < p_vaddr + p_filesz:
                return p_offset + (va - p_vaddr)
        raise SystemExit(f"vaddr 0x{va:X} is not file backed")

    def sym(self, name: str):
        if name not in self.syms:
            raise SystemExit(f"symbol not found: {name} (is the upstream binary stripped?)")
        return self.syms[name]

    def last_rw_load(self):
        rw = [l for l in self.loads if l[4] == 6]
        if not rw:
            raise SystemExit("no RW PT_LOAD")
        return max(rw, key=lambda l: l[1])

    def plt(self, name: str):
        """Address of the PLT stub of an imported symbol, or None."""
        if getattr(self, "_pltmap", None) is None:
            self._pltmap = {}
            rela = next((s for s in self.sections if s[0] == ".rela.plt"), None)
            plt = next((s for s in self.sections if s[0] == ".plt"), None)
            dynsym = next((s for s in self.sections if s[0] == ".dynsym"), None)
            dynstr = next((s for s in self.sections if s[0] == ".dynstr"), None)
            if rela and plt and dynsym and dynstr:
                _n, _t, _a, roff, rsize, _lk, rentsize = rela
                plt_va, plt_entsize = plt[2], (plt[6] or 16)
                # x86-64 reserves one 16-byte PLT entry, aarch64 two (32 bytes)
                header = 16 if self.machine == EM_X86_64 else 32
                step = rentsize or 24
                for i in range(rsize // step):
                    _off, r_info, _add = struct.unpack_from("<QQq", self.d, roff + i * step)
                    so = dynsym[3] + (r_info >> 32) * (dynsym[6] or 24)
                    st_name = struct.unpack_from("<I", self.d, so)[0]
                    end = self.d.find(b"\0", dynstr[3] + st_name)
                    self._pltmap[self.d[dynstr[3] + st_name:end].decode("ascii", "replace")] = \
                        plt_va + header + i * plt_entsize
        return self._pltmap.get(name)


class Patcher:
    def __init__(self, data: bytearray, max_clients: int):
        self.data = data
        self.elf = ELF(bytes(data))
        self.x86 = self.elf.machine == EM_X86_64
        self.max_clients = max_clients
        self.applied = []
        self.notes = []
        self.ok = True

    # ------------------------------------------------------------------ helpers
    def fail(self, where: str, msg: str, va: int | None = None):
        self.ok = False
        extra = ""
        if va is not None:
            o = self.elf.off(va)
            extra = "\n        head: " + self.data[o:o + 32].hex(" ")
        print(f"  [FAIL] {where}: {msg}{extra}", file=sys.stderr)

    def write(self, va: int, old: bytes, new: bytes, where: str, why: str) -> bool:
        if len(old) != len(new):
            raise SystemExit("internal: length mismatch")
        o = self.elf.off(va)
        cur = bytes(self.data[o:o + len(old)])
        if cur != old:
            self.fail(where, f"0x{va:X}: expected {old.hex()} found {cur.hex()}", va)
            return False
        self.data[o:o + len(new)] = new
        self.applied.append({"site": where, "va": f"0x{va:X}", "old": old.hex(),
                             "new": new.hex(), "why": why})
        return True

    def body(self, name: str):
        va, size = self.elf.sym(name)
        o = self.elf.off(va)
        return va, size, bytes(self.data[o:o + size])

    # --------------------------------------------------------------- 1. limit
    def patch_limit(self):
        va, size, _ = self.body("toy_get_free_max_client")
        if self.x86:
            stub = b"\xb8" + struct.pack("<I", self.max_clients) + b"\xc3"
            pad = NOP_X86
        else:
            n = self.max_clients
            words = [0x52800000 | ((n & 0xFFFF) << 5)]
            if n > 0xFFFF:
                words.append(0xF2A00000 | (((n >> 16) & 0xFFFF) << 5))
            words.append(RET_ARM)
            stub = b"".join(w.to_bytes(4, "little") for w in words)
            pad = NOP_ARM
        if len(stub) > size:
            self.fail("toy_get_free_max_client", f"body {size}B too small for stub {len(stub)}B", va)
            return
        stub += pad * ((size - len(stub)) // len(pad))
        o = self.elf.off(va)
        self.write(va, bytes(self.data[o:o + len(stub)]), stub,
                   "toy_get_free_max_client", f"return {self.max_clients}")

    # --------------------------------------------------------------- 2. array
    def patch_array(self):
        a_va, a_size = self.elf.sym("g_aucRecordMac")
        cap = a_size // RECORD_SIZE
        if cap < 1:
            self.fail("g_aucRecordMac", f"symbol size {a_size} unusable")
            return
        _po, seg_va, _pf, seg_memsz, _fl, phdr = self.elf.last_rw_load()
        new_base = (seg_va + seg_memsz + 0xFFFF) & ~0xFFFF
        new_memsz = (new_base + self.max_clients * RECORD_SIZE + 0x1000) - seg_va

        refs = self._array_refs(a_va, new_base)
        if not refs:
            self.fail("g_aucRecordMac", "no instruction referencing it was found")
            return
        for va, old, new, desc in refs:
            if new is None:
                self.fail("g_aucRecordMac", f"0x{va:X}: unsupported addressing form ({desc})", va)
                continue
            self.write(va, old, new, "g_aucRecordMac", desc)

        # anything still pointing at the old array means an addressing form this
        # script does not rewrite - fail instead of shipping a half-moved array
        leftover = self._array_refs(a_va, new_base)
        if leftover:
            self.fail("g_aucRecordMac", "%d reference(s) still point at 0x%X: %s"
                      % (len(leftover), a_va,
                         ", ".join("0x%X" % v for v, *_ in leftover[:8])))

        struct.pack_into("<Q", self.data, phdr + 0x28, new_memsz)
        self.applied.append({"site": "PT_LOAD.p_memsz", "va": f"0x{seg_va:X}",
                             "old": f"0x{seg_memsz:X}", "new": f"0x{new_memsz:X}",
                             "why": "map zero pages for the relocated array"})
        self.notes.append(f"array 0x{a_va:X} -> 0x{new_base:X}, capacity {cap} -> "
                          f"{self.max_clients}, segment now ends 0x{seg_va + new_memsz:X}")

    def _array_refs(self, a_va: int, new_base: int):
        out = []
        if self.x86:
            _n, _t, t_va, t_off, t_size, _l, _e = self.elf.text
            d = self.data
            for i in range(t_size - 10):
                p = t_off + i
                # mov r32, imm32
                if 0xB8 <= d[p] <= 0xBF:
                    v = struct.unpack_from("<I", d, p + 1)[0]
                    if v in (a_va, a_va + 4):
                        out.append((t_va + i, bytes(d[p:p + 5]),
                                    bytes([d[p]]) + struct.pack("<I", new_base + v - a_va),
                                    "mov r32,imm32"))
                        continue
                # mov/lea with modrm: 8b/89/8d (+ 48/66 prefixes)
                for pre_len, prefix in ((0, b""), (1, b"\x48"), (1, b"\x66")):
                    if prefix and d[p] != prefix[0]:
                        continue
                    op = d[p + pre_len]
                    if op not in (0x8B, 0x89, 0x8D):
                        continue
                    modrm = d[p + pre_len + 1]
                    mod, rm = modrm & 0xC0, modrm & 7
                    if mod == 0x80 and rm != 4:            # [reg+disp32]
                        disp_at = p + pre_len + 2
                    elif mod == 0x00 and rm == 5:          # [rip+disp32]
                        disp_at = p + pre_len + 2
                    else:
                        continue
                    v = struct.unpack_from("<i", d, disp_at)[0]
                    if v in (a_va, a_va + 4):
                        n = pre_len + 6
                        out.append((t_va + i, bytes(d[p:p + n]),
                                    d[p:p + pre_len + 2] + struct.pack("<I", new_base + v - a_va),
                                    "mov/lea [.]"))
                        break
        else:
            _n, _t, t_va, t_off, t_size, _l, _e = self.elf.text
            d = self.data
            words = {}
            for i in range(0, t_size - 4, 4):
                words[i] = struct.unpack_from("<I", d, t_off + i)[0]
            for i in range(0, t_size - 8, 4):
                w = words[i]
                # movz + movk pair that materialises the address (non-PIE build, so
                # the absolute address fits in two 16-bit halves)
                if (w & 0xFF800000) == 0xD2800000:
                    rd = w & 31
                    nxt = words.get(i + 4)
                    if nxt is not None and (nxt & 0xFF800000) == 0xF2800000 and (nxt & 31) == rd:
                        val = ((w >> 5) & 0xFFFF) | (((nxt >> 5) & 0xFFFF) << 16)
                        if val == a_va:
                            nw1 = 0xD2800000 | ((new_base & 0xFFFF) << 5) | rd
                            nw2 = 0xF2800000 | (((new_base >> 16) & 0xFFFF) << 5) | rd
                            out.append((t_va + i, d[t_off + i:t_off + i + 4],
                                        struct.pack("<I", nw1), "movz -> new base"))
                            out.append((t_va + i + 4, d[t_off + i + 4:t_off + i + 8],
                                        struct.pack("<I", nw2), "movk -> new base"))
                    continue
                if (w & 0x9F000000) != 0x90000000:         # adrp
                    continue
                rd = w & 31
                page = self._adrp_page(t_va + i, w)
                for j in range(i + 4, i + 20, 4):
                    if j not in words:
                        continue
                    nxt = words[j]
                    if (nxt & 0xFF800000) == 0x91000000 and ((nxt >> 5) & 31) == rd:
                        imm = (nxt >> 10) & 0xFFF
                        if page + imm == a_va:
                            # rewrite adrp -> new page, add -> #0
                            nw_adrp = self._adrp_encode(t_va + i, new_base, rd)
                            nw_add = 0x91000000 | (rd << 5) | (nxt & 31)   # add xD, xN, #0
                            out.append((t_va + i, d[t_off + i:t_off + i + 4],
                                        struct.pack("<I", nw_adrp), "adrp -> new page"))
                            out.append((t_va + j, d[t_off + j:t_off + j + 4],
                                        struct.pack("<I", nw_add), "add #0"))
                        break
                    # adrp + ldr: the address would come from a literal slot, which
                    # this script cannot retarget - report it instead of missing it
                    if (nxt & 0xFFC00000) in (0xF9400000, 0xB9400000) and ((nxt >> 5) & 31) == rd:
                        scale = 8 if (nxt >> 30) & 1 else 4
                        if page + ((nxt >> 10) & 0xFFF) * scale == a_va:
                            out.append((t_va + i, d[t_off + i:t_off + i + 4], None,
                                        "adrp+ldr literal load"))
                        break
        # a 66/48 prefixed instruction also matches the unprefixed pattern one byte
        # later - keep the earliest (longest) match and drop the overlapping one
        out.sort(key=lambda x: x[0])
        dedup, end = [], -1
        for cand in out:
            # strict '<': an arm64 adrp at i and its add at i+4 are adjacent, and the
            # add must survive; an x86 66/48-prefixed instruction overlaps a candidate
            # starting mid-instruction, which must be dropped.
            if cand[0] < end:
                continue
            dedup.append(cand)
            end = cand[0] + len(cand[1])
        return dedup

    @staticmethod
    def _adrp_page(pc: int, w: int) -> int:
        imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 3)
        if imm & (1 << 20):
            imm -= 1 << 21
        return (pc & ~0xFFF) + (imm << 12)

    @staticmethod
    def _adrp_encode(pc: int, target: int, rd: int) -> int:
        imm = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
        imm &= 0x1FFFFF
        return 0x90000000 | ((imm & 3) << 29) | (((imm >> 2) & 0x7FFFF) << 5) | rd

    # ---------------------------------------------------------- 3. arch gates
    def patch_arch_gates(self):
        for fn in ("dhcp_read_callback_normal", "dhcp_read_callback_proxy"):
            if fn not in self.elf.syms:
                self.fail(fn, "symbol missing")
                continue
            va, _size, body = self.body(fn)
            hits = 0
            for glob in ("g_test_arm_mode", "g_test_loongarch_mode"):
                if glob not in self.elf.syms:
                    self.fail(fn, f"symbol {glob} missing")
                    continue
                g_va, _ = self.elf.sym(glob)
                for site_va, old, new in self._flag_branches(body, va, g_va):
                    if self.write(site_va, old, new, f"{fn}:{glob}", "reject gate disabled"):
                        hits += 1
            if hits == 0:
                self.fail(fn, "no compare/branch on the test-mode flags found", va)

    def _flag_branches(self, body: bytes, base: int, g_va: int):
        out = []
        if self.x86:
            for i in range(len(body) - 13):
                if body[i:i + 2] != b"\x83\x3d":                 # cmp dword [rip+d],0
                    continue
                disp = struct.unpack_from("<i", body, i + 2)[0]
                if base + i + 7 + disp != g_va:
                    continue
                j = i + 7
                if body[j] == 0x0F and body[j + 1] in (0x84, 0x85):
                    out.append((base + j, body[j:j + 6], NOP_X86 * 6))
            # mov r32,[rip+disp] / test r,r / jcc  (the other shape gcc emits)
            for i in range(len(body) - 16):
                if body[i] not in (0x8B, 0x3B) or (body[i + 1] & 0xC7) != 0x05:
                    continue
                disp = struct.unpack_from("<i", body, i + 2)[0]
                if base + i + 6 + disp != g_va:
                    continue
                for j in range(i + 6, min(i + 18, len(body) - 6)):
                    if body[j] == 0x0F and body[j + 1] in (0x84, 0x85):
                        out.append((base + j, body[j:j + 6], NOP_X86 * 6))
                        break
        else:
            for i in range(0, len(body) - 8, 4):
                w = struct.unpack_from("<I", body, i)[0]
                if (w & 0xFFC00000) != 0xB9400000:               # ldr wT,[xN,#imm]
                    continue
                rn = (w >> 5) & 31
                page = 0
                for back in range(1, 4):
                    k = i - back * 4
                    if k < 0:
                        continue
                    pw = struct.unpack_from("<I", body, k)[0]
                    if (pw & 0x9F000000) == 0x90000000 and (pw & 31) == rn:
                        page = self._adrp_page(base + k, pw)
                        break
                if not page:
                    continue
                if page + ((w >> 10) & 0xFFF) * 4 != g_va:
                    continue
                j = i + 4
                nxt = struct.unpack_from("<I", body, j)[0]
                if (nxt & 0x7E000000) in (0x34000000, 0x35000000):
                    out.append((base + j, body[j:j + 4], NOP_ARM))
        return out

    # ------------------------------------------------------------ 4. self-checks
    def patch_checks(self):
        self._patch_main_start()
        self._patch_http_init()
        self._patch_master_thread()

    def _patch_main_start(self):
        va, _s, body = self.body("main_start")
        if self.x86:
            want = self.elf.plt("g_ascii_strncasecmp")
            for i in range(len(body) - 10):
                if body[i] != 0xE8:
                    continue
                rel = struct.unpack_from("<i", body, i + 1)[0]
                if want is not None and va + i + 5 + rel != want:
                    continue
                if body[i + 5:i + 9] == b"\x85\xc0\x89\xc3":    # test eax,eax; mov ebx,eax
                    self.write(va + i + 5, b"\x85\xc0", b"\x31\xc0",
                               "main_start", "compare forced equal")
                    return
            self.fail("main_start", "call g_ascii_strncasecmp + compare not found", va)
        else:
            want = self.elf.plt("g_ascii_strncasecmp")
            for i in range(0, len(body) - 8, 4):
                w = struct.unpack_from("<I", body, i)[0]
                if (w & 0xFC000000) != 0x94000000:               # bl
                    continue
                imm = w & 0x3FFFFFF
                if imm & (1 << 25):
                    imm -= 1 << 26
                if want is None or va + i + (imm << 2) != want:
                    continue                                     # not the strncasecmp call
                nxt = struct.unpack_from("<I", body, i + 4)[0]
                if (nxt & 0x7E000000) in (0x34000000, 0x35000000):
                    rt = nxt & 31
                    self.write(va + i + 4, body[i + 4:i + 8],
                               struct.pack("<I", 0x52800000 | rt),
                               "main_start", f"cbnz -> mov w{rt},#0")
                    return
            self.fail("main_start", "bl g_ascii_strncasecmp + cbz not found", va)

    def _patch_http_init(self):
        va, _s, body = self.body("http_init")
        if self.x86:
            cands = [i for i in range(len(body) - 4) if body[i:i + 3] == b"\x48\x0f\x45"]
            for i in reversed(cands):
                # the real site is the one whose result is tested right afte
                # (test r64, r64 -> "if (matched == 0)"), so a future build with
                # several cmovne cannot be patched at the wrong one silently
                if b"\x48\x85" in body[i + 4:i + 24]:
                    self.write(va + i, body[i:i + 4], NOP_X86 * 4, "http_init",
                               "check result pinned to 1")
                    return
            self.fail("http_init",
                      f"{len(cands)} cmovne found, none followed by test/je", va)
        else:
            for i in range(0, len(body) - 20, 4):
                w = struct.unpack_from("<I", body, i)[0]
                if not ((w & 0xFFE0F000) == 0x9A800000 and ((w >> 16) & 31) == 31
                        and ((w >> 12) & 15) == 0):              # csel xD, xD, xzr, eq
                    continue
                rd = w & 31
                guarded = False
                for j in range(i + 4, i + 20, 4):                # must be followed by cbz xD
                    nxt = struct.unpack_from("<I", body, j)[0]
                    if (nxt & 0x7F000000) == 0x34000000 and (nxt & 31) == rd:
                        guarded = True
                        break
                if not guarded:
                    continue
                self.write(va + i, body[i:i + 4],
                           struct.pack("<I", 0xD2800000 | (1 << 5) | rd),
                           "http_init", f"csel -> mov x{rd},#1")
                return
            self.fail("http_init", "csel xD,xD,xzr,eq not found", va)

    def _patch_master_thread(self):
        va, _s, body = self.body("http_master_thread_main")
        if self.x86:
            k = body.find(b"\x41\xbf\x65\x90\xff\xff")           # mov r15d,0xffff9065 (26000)
            if k < 0:
                self.fail("http_master_thread_main", "API port constant not found", va)
                return
            for j in range(max(k - 32, 0), min(k + 48, len(body) - 3)):
                if body[j:j + 3] == b"\x38\x45\x0c":             # cmp [rbp+0xc], al
                    self.write(va + j, body[j:j + 3], b"\x38\xc0\x90",
                               "http_master_thread_main", "cfg compare forced equal")
                    return
            self.fail("http_master_thread_main", "cfg byte compare not found", va)
        else:
            for i in range(0, len(body) - 20, 4):
                w1 = struct.unpack_from("<I", body, i)[0]
                if (w1 & 0xFFC00000) != 0x39400000 or ((w1 >> 10) & 0xFFF) != 12:
                    continue                                     # ldrb w?, [x?, #12]
                rn = (w1 >> 5) & 31
                w2 = struct.unpack_from("<I", body, i + 4)[0]
                if (w2 & 0xFFC00000) != 0x39400000 or ((w2 >> 10) & 0xFFF) != 76 \
                        or ((w2 >> 5) & 31) != rn:
                    continue                                     # ldrb w?, [x?, #76]
                for j in range(i + 8, i + 24, 4):
                    wj = struct.unpack_from("<I", body, j)[0]
                    if (wj & 0xFF00000F) == 0x54000001:          # b.ne
                        self.write(va + j, body[j:j + 4], NOP_ARM,
                                   "http_master_thread_main", "b.ne -> nop (force 26000)")
                        return
            self.fail("http_master_thread_main", "cfg byte compare not found", va)


def verify_patched(binary: str, report_path: str) -> int:
    """Re-check a shipped binary against the report produced when it was patched.

    This is what CI runs on the artifact pulled out of the built image: it proves
    the image really carries the patched binary, not just that a report exists.
    """
    rep = json.loads(Path(report_path).read_text(encoding="utf-8"))
    data = Path(binary).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != rep.get("output_sha256"):
        print(f"FAIL: {binary} sha256 {sha} != report output_sha256 "
              f"{rep.get('output_sha256')}", file=sys.stderr)
        return 2
    if not rep.get("ok"):
        print("FAIL: report says ok=false", file=sys.stderr)
        return 2

    elf = ELF(data)
    bad = 0
    for site in rep.get("applied", []):
        name = site["site"]
        if name == "PT_LOAD.p_memsz":
            continue
        va = int(site["va"], 16)
        want = bytes.fromhex(site["new"])
        try:
            off = elf.off(va)
        except SystemExit:
            print(f"FAIL {name} 0x{va:X}: not file backed", file=sys.stderr)
            bad += 1
            continue
        got = data[off:off + len(want)]
        if got != want:
            print(f"FAIL {name} 0x{va:X}: {got.hex()} != {want.hex()}", file=sys.stderr)
            bad += 1

    _po, seg_va, _pf, seg_memsz, _fl, _ph = elf.last_rw_load()
    declared = [s for s in rep.get("applied", []) if s["site"] == "PT_LOAD.p_memsz"]
    if declared and int(declared[0]["new"], 16) != seg_memsz:
        print(f"FAIL PT_LOAD.p_memsz: 0x{seg_memsz:X} != {declared[0]['new']}", file=sys.stderr)
        bad += 1

    if bad:
        print(f"FAILED: {bad} site(s) do not match the patch report", file=sys.stderr)
        return 2
    print(f"OK: {len(rep.get('applied', []))} sites match the report, "
          f"max_clients={rep.get('max_clients')}, arch={rep.get('arch')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--max-clients", type=int, default=65535)
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--report", help="write a JSON report to this path")
    ap.add_argument("--verify", metavar="REPORT",
                    help="verify an already patched binary against this report")
    args = ap.parse_args()

    if args.verify:
        return verify_patched(args.binary, args.verify)

    if not 1 <= args.max_clients <= MAX_CLIENTS_LIMIT:
        ap.error(f"--max-clients must be between 1 and {MAX_CLIENTS_LIMIT} "
                 f"(got {args.max_clients})")

    raw = Path(args.binary).read_bytes()
    data = bytearray(raw)
    p = Patcher(data, args.max_clients)
    print(f"== {args.binary} ({'amd64' if p.x86 else 'arm64'}) "
          f"sha256={hashlib.sha256(raw).hexdigest()}")

    p.patch_limit()
    p.patch_array()
    p.patch_arch_gates()
    p.patch_checks()

    report = {
        "binary": str(args.binary),
        "arch": "amd64" if p.x86 else "arm64",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "max_clients": args.max_clients,
        "ok": p.ok,
        "sites": len(p.applied),
        "applied": p.applied,
        "notes": p.notes,
    }
    if not p.ok:
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print("\nFAILED: signature(s) missing - upstream changed the code layout.", file=sys.stderr)
        print("Fix the failing site(s) (see patches/README.md) before building.", file=sys.stderr)
        return 2

    out = bytes(data)
    report["output_sha256"] = hashlib.sha256(out).hexdigest()
    if args.check:
        print(f"   check only: {len(p.applied)} sites matched, nothing written")
    else:
        Path(args.binary).write_bytes(out)
        print(f"   patched: sha256={report['output_sha256']} sites={len(p.applied)}")
    for n in p.notes:
        print(f"   note: {n}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
