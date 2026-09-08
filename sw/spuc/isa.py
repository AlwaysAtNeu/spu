"""SPU ISA definition (single source of truth).

Everything that must agree between the compiler, the Python golden model and the
SystemVerilog RTL lives here.  `python3 -m spuc.isa --sv` prints spu_pkg.sv so the
hardware package can never drift from the software definition.

Instruction word (64 bit):

    63:58 op   (6)   opcode
    57:52 fn   (6)   function / flags
    51:47 rd   (5)   destination (HV reg for H* ops, scalar reg for S* ops, HV source for HST)
    46:42 ra   (5)   operand A
    41:37 rb   (5)   operand B
    36:32 rc   (5)   operand C (scalar; usually "s[rc] + imm" forms a value)
    31:0  imm  (32)  immediate (sign-extended where used as a value / offset)

Registers: h0..h31 hypervector registers (D bits each), s0..s31 scalar (32 bit, s0 == 0).
Memory:    byte addressed, little endian, 32-bit offsets relative to MEM_BASE.
"""
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Hardware configuration (must match spu_pkg.sv parameters)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    D: int = 8192           # hypervector width in bits
    W: int = 512            # datapath chunk / AXI data width in bits
    NHREG: int = 32         # hypervector registers
    NSREG: int = 32         # scalar registers
    NACC: int = 16          # bundling accumulators
    ACC_LANES: int = 128    # accumulator lanes processed per cycle
    ACC_BITS: int = 16      # accumulator counter width (signed, saturating)
    PROG_DEPTH: int = 4096  # instruction memory depth (64-bit words)
    MEM_LAT: int = 4        # memory latency (cycles) assumed by the simulator's cost model (tb default); HBM adds ~100+

    @property
    def C(self):            # chunks per hypervector
        return self.D // self.W

    @property
    def HV_BYTES(self):
        return self.D // 8

    @property
    def ACC_STEPS(self):    # cycles per accumulator pass
        return self.D // self.ACC_LANES

    def validate(self):
        assert self.D % self.W == 0 and (self.D & (self.D - 1)) == 0, "D must be a power of two multiple of W"
        assert (self.W & (self.W - 1)) == 0 and self.W % 64 == 0
        assert self.D % self.ACC_LANES == 0 and self.W % self.ACC_LANES == 0 or self.ACC_LANES % self.W == 0
        assert self.NHREG <= 32 and self.NSREG <= 32 and self.NACC <= 32
        return self

DEFAULT_CONFIG = Config().validate()

# ---------------------------------------------------------------------------
# Opcodes
# ---------------------------------------------------------------------------
OP = {
    "NOP":     0x00,
    "HALT":    0x01,
    "SOP":     0x02,   # rd = ra <fn> rb
    "SOPI":    0x03,   # rd = ra <fn> imm
    "LW":      0x04,   # rd = mem32[s[ra] + imm]
    "LB":      0x05,   # rd = mem8 [s[ra] + imm]  (zero extended)
    "SW":      0x06,   # mem32[s[ra] + imm] = s[rb]
    "SB":      0x07,   # mem8 [s[ra] + imm] = s[rb][7:0]
    "BR":      0x08,   # if (s[ra] <fn> s[rb]) pc += imm      (imm in instructions)
    "JAL":     0x09,   # s[rd] = pc + 1 ; pc += imm
    "JALR":    0x0A,   # s[rd] = pc + 1 ; pc  = s[ra] + imm
    "CSRR":    0x0B,   # s[rd] = csr[imm]
    "CSRW":    0x0C,   # csr[imm] = s[ra]
    "HXOR":    0x10,   # h[rd] = h[ra] ^ h[rb]
    "HAND":    0x11,
    "HOR":     0x12,
    "HNOT":    0x13,   # h[rd] = ~h[ra]
    "HROT":    0x14,   # h[rd] = rotl(h[ra], (s[rc] + imm) mod D)
    "HGEN":    0x15,   # h[rd] = gen((s[rc] + imm) & 0xFFFFFFFF)
    "HMASK":   0x16,   # h[rd] = (1 << clamp(s[rc] + imm, 0, D)) - 1
    "HLD":     0x18,   # h[rd] = mem_hv[s[ra] + imm]
    "HST":     0x19,   # mem_hv[s[ra] + imm] = h[rd]
    "HDIST":   0x1A,   # s[rd] = popcount(h[ra] ^ h[rb])
    "HSEARCH": 0x1B,   # s[rd] = argmin_i hamming(h[ra], mem_hv[s[rb] + i*HV_BYTES]), i < s[rc]
    "HACC":    0x1C,   # accumulator ops, see ACC_FN
}
OP_NAME = {v: k for k, v in OP.items()}

# scalar ALU functions (SOP / SOPI)
ALU_FN = {"ADD": 0, "SUB": 1, "AND": 2, "OR": 3, "XOR": 4, "SHL": 5, "SHR": 6, "SRA": 7,
          "MUL": 8, "SLT": 9, "SLTU": 10}
ALU_FN_NAME = {v: k for k, v in ALU_FN.items()}

# branch conditions (BR)
BR_FN = {"BEQ": 0, "BNE": 1, "BLT": 2, "BGE": 3, "BLTU": 4, "BGEU": 5}
BR_FN_NAME = {v: k for k, v in BR_FN.items()}

# HSEARCH flags (fn bits)
SEARCH_WRDIST = 0x1      # also write every distance as a 32-bit word to csr[DOUT] + 4*i

# HACC functions.  k (accumulator index) is always in the rb field.
ACC_FN = {
    "CLR": 0,   # acc[k] = 0
    "ADD": 1,   # acc[k][i] += (h[ra][i] ? +w : -w),  w = (s[rc] + imm)[15:0] signed
    "SUB": 2,   # acc[k][i] -= (h[ra][i] ? +w : -w)
    "THR": 3,   # h[rd][i] = acc[k][i] > t,          t = (s[rc] + imm)[15:0] signed
    "LD":  4,   # acc[k] = mem_i16[s[ra] + imm ...]  (D * 2 bytes, little endian)
    "ST":  5,   # mem_i16[s[ra] + imm ...] = acc[k]
}
ACC_FN_NAME = {v: k for k, v in ACC_FN.items()}

# ---------------------------------------------------------------------------
# CSRs (CSRR/CSRW index and host register map 0x100 + 4*idx)
# ---------------------------------------------------------------------------
CSR = {
    "ID": 0,          # RO 0x53505531 "SPU1"
    "DOUT": 1,        # RW distance output base address for HSEARCH.WD
    "SR_IDX": 2,      # RO last search: best index   (0xFFFFFFFF if count == 0)
    "SR_DIST": 3,     # RO last search: best distance(0xFFFF if count == 0)
    "SR_IDX2": 4,     # RO second best index
    "SR_DIST2": 5,    # RO second best distance
    "CYCLES_LO": 6,   # RO cycles since START
    "CYCLES_HI": 7,
    "INSTRET_LO": 8,  # RO instructions retired since START
    "INSTRET_HI": 9,
    "CFG_D": 10,      # RO hypervector width
    "CFG_NACC": 11,   # RO number of accumulators
    "ERR": 12,        # RO error code (see ERR_*)
    "SCRATCH0": 13,   # RW host <-> SPU mailbox
    "SCRATCH1": 14,
    "SCRATCH2": 15,
    "SCRATCH3": 16,
}
CSR_NAME = {v: k for k, v in CSR.items()}
NCSR = 32
SPU_ID = 0x53505531
SPU_VERSION = 0x00010000

ERR_NONE = 0
ERR_ILLEGAL_OP = 1
ERR_ACC_INDEX = 2
ERR_PC_RANGE = 3
ERR_AXI = 4

# ---------------------------------------------------------------------------
# Host (AXI4-Lite) register map, byte offsets.  Address width 18 bits.
# ---------------------------------------------------------------------------
HOST = {
    "CTRL": 0x000,        # W: bit0 START, bit1 SOFT_RESET, bit2 IRQ_EN
    "STATUS": 0x004,      # R: bit0 BUSY, bit1 DONE, bit2 ERR, bit3 IRQ
    "PC_START": 0x008,    # RW
    "PC": 0x00C,          # R current pc
    "MEM_BASE_LO": 0x010, # RW 64-bit base of the SPU memory window
    "MEM_BASE_HI": 0x014,
    "IRQ_ACK": 0x018,     # W1C clears DONE / IRQ
    "ID": 0x020,          # R SPU_ID
    "VERSION": 0x024,     # R SPU_VERSION
    "CFG0": 0x028,        # R  D[15:0] | W[31:16]
    "CFG1": 0x02C,        # R  NHREG[7:0] | NACC[15:8] | ACC_LANES[31:16]
    "CSR_BASE": 0x100,    # CSR i at CSR_BASE + 4*i
    "SREG_BASE": 0x200,   # scalar reg i (RO) at SREG_BASE + 4*i
    "HREG_BASE": 0x400,   # debug: HV register readback: HREG_SEL at 0x400, data words at 0x404.. (32 words = 1 chunk of 512 bits per HREG_SEL chunk)
    "PROG_BASE": 0x20000, # instruction i: lo word at PROG_BASE + 8*i, hi word at +4
}
HOST_ADDR_WIDTH = 18
CTRL_START = 1 << 0
CTRL_SOFT_RESET = 1 << 1
CTRL_IRQ_EN = 1 << 2
STATUS_BUSY = 1 << 0
STATUS_DONE = 1 << 1
STATUS_ERR = 1 << 2
STATUS_IRQ = 1 << 3

# ---------------------------------------------------------------------------
# Instruction encode / decode
# ---------------------------------------------------------------------------
@dataclass
class Instr:
    op: int
    fn: int = 0
    rd: int = 0
    ra: int = 0
    rb: int = 0
    rc: int = 0
    imm: int = 0          # python int, may be negative; encoded as 32-bit two's complement

    def encode(self) -> int:
        for name, v, bits in (("op", self.op, 6), ("fn", self.fn, 6), ("rd", self.rd, 5),
                              ("ra", self.ra, 5), ("rb", self.rb, 5), ("rc", self.rc, 5)):
            if not 0 <= v < (1 << bits):
                raise ValueError("field %s=%d out of range" % (name, v))
        if not -(1 << 31) <= self.imm < (1 << 32):
            raise ValueError("imm out of range: %d" % self.imm)
        return ((self.op << 58) | (self.fn << 52) | (self.rd << 47) | (self.ra << 42) |
                (self.rb << 37) | (self.rc << 32) | (self.imm & 0xFFFFFFFF))

    @staticmethod
    def decode(word: int) -> "Instr":
        imm = word & 0xFFFFFFFF
        if imm & 0x80000000:
            imm -= 1 << 32
        return Instr(op=(word >> 58) & 0x3F, fn=(word >> 52) & 0x3F, rd=(word >> 47) & 0x1F,
                     ra=(word >> 42) & 0x1F, rb=(word >> 37) & 0x1F, rc=(word >> 32) & 0x1F, imm=imm)

    @property
    def imm_u(self):
        return self.imm & 0xFFFFFFFF


def emit_sv_package(cfg: Config = DEFAULT_CONFIG) -> str:
    """Render spu_pkg.sv from the tables above."""
    L = []
    L.append("// AUTO-GENERATED by `python3 -m spuc.isa --sv` -- do not edit by hand.")
    L.append("package spu_pkg;")
    L.append("  // ---- configuration ----")
    for k in ("D", "W", "NHREG", "NSREG", "NACC", "ACC_LANES", "ACC_BITS", "PROG_DEPTH"):
        L.append("  localparam int %-10s = %d;" % (k, getattr(cfg, k)))
    L.append("  localparam int C          = D / W;          // chunks per hypervector")
    L.append("  localparam int HV_BYTES   = D / 8;")
    L.append("  localparam int ACC_STEPS  = D / ACC_LANES;")
    L.append("  localparam int CH_W       = $clog2(C);      // chunk index width")
    L.append("  localparam int RF_AW      = $clog2(NHREG) + CH_W;")
    L.append("  localparam int ACC_SPC    = W / ACC_LANES;  // accumulator steps per chunk")
    L.append("  localparam int ACC_BEATS  = D * ACC_BITS / W; // 64B beats in an accumulator image")
    L.append("  localparam int STEP_W     = $clog2(ACC_STEPS); // accumulator step index width")
    L.append("  localparam int HOST_AW    = %d;" % HOST_ADDR_WIDTH)
    L.append("  localparam logic [31:0] SPU_ID      = 32'h%08X;" % SPU_ID)
    L.append("  localparam logic [31:0] SPU_VERSION = 32'h%08X;" % SPU_VERSION)
    L.append("  // ---- opcodes ----")
    for name, v in OP.items():
        L.append("  localparam logic [5:0] OP_%-8s = 6'h%02X;" % (name, v))
    L.append("  // ---- scalar ALU functions ----")
    for name, v in ALU_FN.items():
        L.append("  localparam logic [5:0] ALU_%-5s = 6'd%d;" % (name, v))
    L.append("  // ---- branch conditions ----")
    for name, v in BR_FN.items():
        L.append("  localparam logic [5:0] BR_%-5s = 6'd%d;" % (name, v))
    L.append("  // ---- search flags ----")
    L.append("  localparam int SEARCH_WRDIST_BIT = 0;")
    L.append("  // ---- accumulator functions ----")
    for name, v in ACC_FN.items():
        L.append("  localparam logic [5:0] ACC_%-4s = 6'd%d;" % (name, v))
    L.append("  // ---- CSR indices ----")
    for name, v in CSR.items():
        L.append("  localparam int CSR_%-11s = %d;" % (name, v))
    L.append("  localparam int NCSR = %d;" % NCSR)
    L.append("  // ---- error codes ----")
    for name, v in (("NONE", ERR_NONE), ("ILLEGAL_OP", ERR_ILLEGAL_OP), ("ACC_INDEX", ERR_ACC_INDEX),
                    ("PC_RANGE", ERR_PC_RANGE), ("AXI", ERR_AXI)):
        L.append("  localparam logic [7:0] ERR_%-11s = 8'd%d;" % (name, v))
    L.append("  // ---- host register map (byte offsets) ----")
    for name, v in HOST.items():
        L.append("  localparam logic [HOST_AW-1:0] HOST_%-12s = 'h%05X;" % (name, v))
    L.append("  localparam int CTRL_START_BIT = 0, CTRL_SOFT_RESET_BIT = 1, CTRL_IRQ_EN_BIT = 2;")
    L.append("  localparam int STATUS_BUSY_BIT = 0, STATUS_DONE_BIT = 1, STATUS_ERR_BIT = 2, STATUS_IRQ_BIT = 3;")
    L.append("  // ---- instruction word ----")
    L.append("  typedef struct packed {")
    L.append("    logic [5:0]  op;")
    L.append("    logic [5:0]  fn;")
    L.append("    logic [4:0]  rd;")
    L.append("    logic [4:0]  ra;")
    L.append("    logic [4:0]  rb;")
    L.append("    logic [4:0]  rc;")
    L.append("    logic [31:0] imm;")
    L.append("  } instr_t;")
    L.append("  // ---- pseudo random generator constants (splitmix64 finaliser) ----")
    L.append("  localparam logic [63:0] GEN_GOLDEN = 64'h9E3779B97F4A7C15;")
    L.append("  localparam logic [63:0] GEN_MUL1   = 64'hBF58476D1CE4E5B9;")
    L.append("  localparam logic [63:0] GEN_MUL2   = 64'h94D049BB133111EB;")
    L.append("endpackage")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    import sys
    if "--sv" in sys.argv:
        sys.stdout.write(emit_sv_package())
    else:
        print(__doc__)
