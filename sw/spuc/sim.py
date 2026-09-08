"""Functional simulator / golden model for the SPU ISA.

The simulator is bit-exact with the RTL for every architectural side effect
(registers, accumulators, memory, CSRs) and additionally estimates cycles with a
cost model calibrated against the RTL (hw/README.md, memory latency = Config.MEM_LAT).
"""
import numpy as np
from .isa import (Config, DEFAULT_CONFIG, OP, OP_NAME, ALU_FN, BR_FN, ACC_FN, CSR, NCSR, SPU_ID,
                  SEARCH_WRDIST, ERR_NONE, ERR_ILLEGAL_OP, ERR_ACC_INDEX, ERR_PC_RANGE, Instr)
from . import hv as H
from .asm import disassemble_one

M32 = 0xFFFFFFFF


def s32(x):
    x &= M32
    return x - (1 << 32) if x & 0x80000000 else x


def s16(x):
    x &= 0xFFFF
    return x - (1 << 16) if x & 0x8000 else x


class Memory:
    """Sparse, page based byte memory (little endian)."""
    PAGE_BITS = 16
    PAGE = 1 << PAGE_BITS

    def __init__(self):
        self.pages = {}

    def _page(self, n, create):
        p = self.pages.get(n)
        if p is None and create:
            p = self.pages[n] = bytearray(self.PAGE)
        return p

    def read(self, addr, n):
        out = bytearray()
        while n > 0:
            pn, off = addr >> self.PAGE_BITS, addr & (self.PAGE - 1)
            take = min(n, self.PAGE - off)
            p = self._page(pn, False)
            out += p[off:off + take] if p is not None else bytes(take)
            addr += take
            n -= take
        return bytes(out)

    def write(self, addr, data):
        data = bytes(data)
        pos = 0
        while pos < len(data):
            pn, off = addr >> self.PAGE_BITS, addr & (self.PAGE - 1)
            take = min(len(data) - pos, self.PAGE - off)
            self._page(pn, True)[off:off + take] = data[pos:pos + take]
            addr += take
            pos += take

    def read_u32(self, addr):
        return int.from_bytes(self.read(addr & ~3 & M32, 4), "little")

    def write_u32(self, addr, v):
        self.write(addr & ~3 & M32, (v & M32).to_bytes(4, "little"))

    def read_u8(self, addr):
        return self.read(addr & M32, 1)[0]

    def write_u8(self, addr, v):
        self.write(addr & M32, bytes([v & 0xFF]))

    def touched_ranges(self):
        """Sorted list of (addr, nbytes) for every allocated page."""
        return [(n << self.PAGE_BITS, self.PAGE) for n in sorted(self.pages)]

    def dump_hex(self, ranges=None):
        """Verilog $readmemh compatible dump (64-byte words, @address in words)."""
        L = []
        for base, n in (ranges or self.touched_ranges()):
            data = self.read(base, n)
            L.append("@%x" % (base // 64))
            for i in range(0, n, 64):
                L.append(data[i:i + 64][::-1].hex())
        return "\n".join(L) + "\n"


class SPUSim:
    def __init__(self, cfg: Config = DEFAULT_CONFIG, mem: Memory = None, trace=False):
        self.cfg = cfg
        self.mem = mem if mem is not None else Memory()
        self.trace = trace
        self.prog = []
        self.reset()

    # ---------------------------------------------------------------- state
    def reset(self):
        c = self.cfg
        self.h = [0] * c.NHREG
        self.s = [0] * c.NSREG
        self.acc = np.zeros((c.NACC, c.D), dtype=np.int16)
        self.csr = [0] * NCSR
        self.csr[CSR["ID"]] = SPU_ID
        self.csr[CSR["CFG_D"]] = c.D
        self.csr[CSR["CFG_NACC"]] = c.NACC
        self.csr[CSR["SR_IDX"]] = M32
        self.csr[CSR["SR_DIST"]] = 0xFFFF
        self.csr[CSR["SR_IDX2"]] = M32
        self.csr[CSR["SR_DIST2"]] = 0xFFFF
        self.pc = 0
        self.cycles = 0
        self.instret = 0
        self.halted = False
        self.err = ERR_NONE
        self.op_cycles = {}

    def load_program(self, words):
        if len(words) > self.cfg.PROG_DEPTH:
            raise ValueError("program too large: %d > %d" % (len(words), self.cfg.PROG_DEPTH))
        self.prog = list(words)

    # ------------------------------------------------------------- helpers
    def _hv_read(self, addr):
        return H.from_bytes(self.mem.read(addr & ~63 & M32, self.cfg.HV_BYTES))

    def _hv_write(self, addr, x):
        self.mem.write(addr & ~63 & M32, H.to_bytes(x, self.cfg.D))

    def _val(self, i):        # s[rc] + imm (mod 2^32)
        return (self.s[i.rc] + i.imm) & M32

    def _addr(self, i):       # s[ra] + imm (mod 2^32)
        return (self.s[i.ra] + i.imm) & M32

    def _set_s(self, r, v):
        if r != 0:
            self.s[r] = v & M32

    def _cost(self, name, n):
        self.cycles += n
        self.op_cycles[name] = self.op_cycles.get(name, 0) + n

    # ---------------------------------------------------------------- run
    def step(self):
        c = self.cfg
        if self.halted:
            return False
        if not 0 <= self.pc < len(self.prog):
            self.err, self.halted = ERR_PC_RANGE, True
            self.csr[CSR["ERR"]] = self.err
            return False
        word = self.prog[self.pc]
        i = Instr.decode(word)
        name = OP_NAME.get(i.op)
        if self.trace:
            print("%5d: %s" % (self.pc, disassemble_one(word, self.pc)))
        next_pc = self.pc + 1
        D, C = c.D, c.C
        if name == "NOP":
            self._cost(name, 2)
        elif name == "HALT":
            self.halted = True
            self._cost(name, 2)
        elif name == "SOP" or name == "SOPI":
            a = self.s[i.ra]
            b = self.s[i.rb] if name == "SOP" else (i.imm & M32)
            fn = i.fn
            if fn == ALU_FN["ADD"]:
                r = a + b
            elif fn == ALU_FN["SUB"]:
                r = a - b
            elif fn == ALU_FN["AND"]:
                r = a & b
            elif fn == ALU_FN["OR"]:
                r = a | b
            elif fn == ALU_FN["XOR"]:
                r = a ^ b
            elif fn == ALU_FN["SHL"]:
                r = a << (b & 31)
            elif fn == ALU_FN["SHR"]:
                r = a >> (b & 31)
            elif fn == ALU_FN["SRA"]:
                r = s32(a) >> (b & 31)
            elif fn == ALU_FN["MUL"]:
                r = a * b
            elif fn == ALU_FN["SLT"]:
                r = 1 if s32(a) < s32(b) else 0
            elif fn == ALU_FN["SLTU"]:
                r = 1 if a < b else 0
            else:
                return self._illegal()
            self._set_s(i.rd, r)
            self._cost(name, 3 if fn == ALU_FN["MUL"] else 2)
        elif name == "LW":
            self._set_s(i.rd, self.mem.read_u32(self._addr(i)))
            self._cost(name, 7 + c.MEM_LAT)
        elif name == "LB":
            self._set_s(i.rd, self.mem.read_u8(self._addr(i)))
            self._cost(name, 7 + c.MEM_LAT)
        elif name == "SW":
            self.mem.write_u32(self._addr(i), self.s[i.rb])
            self._cost(name, 8)
        elif name == "SB":
            self.mem.write_u8(self._addr(i), self.s[i.rb])
            self._cost(name, 8)
        elif name == "BR":
            a, b, fn = self.s[i.ra], self.s[i.rb], i.fn
            if fn == BR_FN["BEQ"]:
                t = a == b
            elif fn == BR_FN["BNE"]:
                t = a != b
            elif fn == BR_FN["BLT"]:
                t = s32(a) < s32(b)
            elif fn == BR_FN["BGE"]:
                t = s32(a) >= s32(b)
            elif fn == BR_FN["BLTU"]:
                t = a < b
            elif fn == BR_FN["BGEU"]:
                t = a >= b
            else:
                return self._illegal()
            if t:
                next_pc = self.pc + i.imm
            self._cost(name, 3 if t else 2)
        elif name == "JAL":
            self._set_s(i.rd, self.pc + 1)
            next_pc = self.pc + i.imm
            self._cost(name, 3)
        elif name == "JALR":
            self._set_s(i.rd, self.pc + 1)
            next_pc = self._addr(i)
            self._cost(name, 3)
        elif name == "CSRR":
            idx = i.imm & M32
            self._set_s(i.rd, self.csr[idx] if idx < NCSR else 0)
            self._cost(name, 2)
        elif name == "CSRW":
            idx = i.imm & M32
            if idx < NCSR and idx not in (CSR["ID"], CSR["SR_IDX"], CSR["SR_DIST"], CSR["SR_IDX2"], CSR["SR_DIST2"],
                                          CSR["CYCLES_LO"], CSR["CYCLES_HI"], CSR["INSTRET_LO"], CSR["INSTRET_HI"],
                                          CSR["CFG_D"], CSR["CFG_NACC"], CSR["ERR"]):
                self.csr[idx] = self.s[i.ra]
            self._cost(name, 2)
        elif name == "HXOR":
            self.h[i.rd] = self.h[i.ra] ^ self.h[i.rb]
            self._cost(name, C + 6)
        elif name == "HAND":
            self.h[i.rd] = self.h[i.ra] & self.h[i.rb]
            self._cost(name, C + 6)
        elif name == "HOR":
            self.h[i.rd] = self.h[i.ra] | self.h[i.rb]
            self._cost(name, C + 6)
        elif name == "HNOT":
            self.h[i.rd] = (~self.h[i.ra]) & H.mask(D)
            self._cost(name, C + 6)
        elif name == "HROT":
            self.h[i.rd] = H.rotl(self.h[i.ra], self._val(i) % D, D)
            self._cost(name, (2 * C + 7) if i.rd == i.ra else (C + 6))   # in-place rotate goes through hbuf
        elif name == "HGEN":
            self.h[i.rd] = H.hgen(self._val(i), D)
            self._cost(name, C + 12)
        elif name == "HMASK":
            self.h[i.rd] = H.hmask(s32(self._val(i)), D)
            self._cost(name, C + 6)
        elif name == "HLD":
            self.h[i.rd] = self._hv_read(self._addr(i))
            self._cost(name, C + 6 + c.MEM_LAT)
        elif name == "HST":
            self._hv_write(self._addr(i), self.h[i.rd])
            self._cost(name, 2 * C + 8)
        elif name == "HDIST":
            self._set_s(i.rd, H.hamming(self.h[i.ra], self.h[i.rb]))
            self._cost(name, C + 8)
        elif name == "HSEARCH":
            self._search(i)
        elif name == "HACC":
            if not self._acc(i):
                return False
        else:
            return self._illegal()
        self.instret += 1
        self.pc = next_pc
        return not self.halted

    def _illegal(self):
        self.err, self.halted = ERR_ILLEGAL_OP, True
        self.csr[CSR["ERR"]] = self.err
        return False

    def _search(self, i):
        c = self.cfg
        q = self.h[i.ra]
        base, count = self.s[i.rb], self.s[i.rc]
        wrdist = bool(i.fn & SEARCH_WRDIST)
        dout = self.csr[CSR["DOUT"]]
        best, bidx, best2, bidx2 = 0xFFFF, M32, 0xFFFF, M32
        for k in range(count):
            cand = self._hv_read((base + k * c.HV_BYTES) & M32)
            d = H.hamming(q, cand)
            if wrdist:
                self.mem.write_u32((dout + 4 * k) & M32, d)
            if d < best:
                best2, bidx2 = best, bidx
                best, bidx = d, k
            elif d < best2:
                best2, bidx2 = d, k
        self.csr[CSR["SR_IDX"]], self.csr[CSR["SR_DIST"]] = bidx, best
        self.csr[CSR["SR_IDX2"]], self.csr[CSR["SR_DIST2"]] = bidx2, best2
        self._set_s(i.rd, bidx)
        self._cost("HSEARCH", count * c.C + 60 + c.MEM_LAT + ((count + 15) // 16 * 6 if wrdist else 0))

    def _acc(self, i):
        c = self.cfg
        k = i.rb
        if k >= c.NACC:
            self.err, self.halted = ERR_ACC_INDEX, True
            self.csr[CSR["ERR"]] = self.err
            return False
        fn = i.fn
        if fn == ACC_FN["CLR"]:
            self.acc[k] = 0
            self._cost("HACC.CLR", c.ACC_STEPS + 7)
        elif fn == ACC_FN["ADD"] or fn == ACC_FN["SUB"]:
            w = s16(self._val(i))
            if fn == ACC_FN["SUB"]:
                w = -w
            self.acc[k] = H.sat16(self.acc[k].astype(np.int32) + w * H.to_pm1(self.h[i.ra], c.D).astype(np.int32))
            self._cost("HACC.ADD", c.ACC_STEPS + 27)
        elif fn == ACC_FN["THR"]:
            t = s16(self._val(i))
            self.h[i.rd] = H.from_bits(self.acc[k] > t)
            self._cost("HACC.THR", c.ACC_STEPS + 9)
        elif fn == ACC_FN["LD"]:
            raw = self.mem.read(self._addr(i) & ~63 & M32, c.D * 2)
            self.acc[k] = np.frombuffer(raw, dtype="<i2").copy()
            self._cost("HACC.LD", 3 * c.ACC_STEPS + 84 + c.MEM_LAT)
        elif fn == ACC_FN["ST"]:
            self.mem.write(self._addr(i) & ~63 & M32, self.acc[k].astype("<i2").tobytes())
            self._cost("HACC.ST", 6 * c.ACC_STEPS + 76)
        else:
            return self._illegal()
        return True

    def run(self, max_instr=None):
        """Run until HALT, error or max_instr.  Returns a status string."""
        n = 0
        while not self.halted:
            if max_instr is not None and n >= max_instr:
                return "timeout"
            self.step()
            n += 1
        self.csr[CSR["CYCLES_LO"]] = self.cycles & M32
        self.csr[CSR["CYCLES_HI"]] = (self.cycles >> 32) & M32
        self.csr[CSR["INSTRET_LO"]] = self.instret & M32
        self.csr[CSR["INSTRET_HI"]] = (self.instret >> 32) & M32
        return "error(%d)" % self.err if self.err else "halt"
