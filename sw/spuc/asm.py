"""SPU assembler / disassembler.

Syntax (one instruction per line, `;` or `#` starts a comment, `label:` defines a label):

    ADDI  s1, s0, 100         SOPI forms: ADDI SUBI ANDI ORI XORI SHLI SHRI SRAI MULI SLTI SLTIU
    ADD   s1, s2, s3          SOP  forms: ADD SUB AND OR XOR SHL SHR SRA MUL SLT SLTU
    LI    s1, imm             alias for ADDI s1, s0, imm
    MV    s1, s2              alias for ADDI s1, s2, 0
    LW    s1, 8(s2)  |  LW s1, s2, 8        (also LB, SW, SB: SW s_data, off(s_base))
    BEQ   s1, s2, label       (BNE BLT BGE BLTU BGEU)     J label  |  JAL s1, label
    JALR  s1, s2, 0           CSRR s1, DOUT     CSRW DOUT, s1
    HXOR  h1, h2, h3          HAND HOR ; HNOT h1, h2 ; HMOV h1, h2
    HROT  h1, h2, s3, imm  |  HROT h1, h2, imm
    HGEN  h1, s2, imm      |  HGEN h1, imm
    HMASK h1, s2, imm      |  HMASK h1, imm
    HLD   h1, 64(s2)       |  HLD h1, s2, 64      ; HST h1, 64(s2)
    HDIST s1, h2, h3
    HSEARCH s1, h2, s3, s4    HSEARCH.WD s1, h2, s3, s4
    ACC.CLR 3                 ACC.ADD 3, h2, s4, imm | ACC.ADD 3, h2, imm      (ACC.SUB likewise)
    ACC.THR h1, 3, s4, imm |  ACC.THR h1, 3, imm
    ACC.LD 3, 0(s2)        |  ACC.ST 3, 0(s2)
    HALT  NOP
"""
import re
from .isa import (OP, OP_NAME, ALU_FN, ALU_FN_NAME, BR_FN, BR_FN_NAME, ACC_FN, ACC_FN_NAME,
                  CSR, CSR_NAME, SEARCH_WRDIST, Instr)

_SOPI_ALIAS = {"ADDI": "ADD", "SUBI": "SUB", "ANDI": "AND", "ORI": "OR", "XORI": "XOR", "SHLI": "SHL",
               "SHRI": "SHR", "SRAI": "SRA", "MULI": "MUL", "SLTI": "SLT", "SLTIU": "SLTU"}
_SOPI_REV = {v: k for k, v in _SOPI_ALIAS.items()}


class AsmError(Exception):
    pass


def _reg(tok, kind):
    tok = tok.strip().lower()
    m = re.fullmatch(r"([hs])(\d+)", tok)
    if not m or m.group(1) != kind or int(m.group(2)) > 31:
        raise AsmError("expected %s register, got %r" % ("hypervector" if kind == "h" else "scalar", tok))
    return int(m.group(2))


def _int(tok, symbols=None):
    tok = tok.strip()
    if symbols and tok in symbols:
        return symbols[tok]
    try:
        return int(tok, 0)
    except ValueError:
        raise AsmError("bad integer/label %r" % tok)


def _memop(toks, symbols):
    """Parse ["off(sN)"] or ["sN", "off"] -> (reg, off)."""
    if len(toks) == 1:
        m = re.fullmatch(r"\s*(-?[\w]+)\s*\(\s*(s\d+)\s*\)\s*", toks[0])
        if not m:
            raise AsmError("bad memory operand %r" % toks[0])
        return _reg(m.group(2), "s"), _int(m.group(1), symbols)
    if len(toks) == 2:
        return _reg(toks[0], "s"), _int(toks[1], symbols)
    raise AsmError("bad memory operand %r" % toks)


def _val(toks, symbols):
    """Parse [sN, imm] or [imm] -> (rc, imm)."""
    if len(toks) == 1:
        return 0, _int(toks[0], symbols)
    if len(toks) == 2:
        return _reg(toks[0], "s"), _int(toks[1], symbols)
    raise AsmError("bad value operand %r" % toks)


def parse_line(line):
    line = line.split(";")[0].split("#")[0].strip()
    if not line:
        return None, None
    label = None
    m = re.match(r"^([A-Za-z_.$][\w.$]*)\s*:\s*(.*)$", line)
    if m:
        label, line = m.group(1), m.group(2).strip()
    if not line:
        return label, None
    parts = line.split(None, 1)
    mnem = parts[0].upper()
    ops = [o.strip() for o in parts[1].split(",")] if len(parts) > 1 else []
    return label, (mnem, ops)


def assemble(text, symbols=None):
    """Assemble text -> list of 64-bit instruction words."""
    symbols = dict(symbols or {})
    lines = text.splitlines()
    # pass 1: labels
    pc = 0
    items = []
    for ln, raw in enumerate(lines, 1):
        try:
            label, ins = parse_line(raw)
        except AsmError as e:
            raise AsmError("line %d: %s" % (ln, e))
        if label:
            if label in symbols:
                raise AsmError("line %d: duplicate label %s" % (ln, label))
            symbols[label] = pc
        if ins:
            items.append((ln, pc, ins))
            pc += 1
    words = []
    for ln, pc, (mnem, ops) in items:
        try:
            words.append(encode_one(mnem, ops, pc, symbols).encode())
        except AsmError as e:
            raise AsmError("line %d: %s" % (ln, e))
    return words


def encode_one(mnem, ops, pc, symbols):
    I = Instr
    if mnem in ("NOP", "HALT"):
        return I(OP[mnem])
    if mnem == "LI":
        return I(OP["SOPI"], ALU_FN["ADD"], rd=_reg(ops[0], "s"), ra=0, imm=_int(ops[1], symbols))
    if mnem == "MV":
        return I(OP["SOPI"], ALU_FN["ADD"], rd=_reg(ops[0], "s"), ra=_reg(ops[1], "s"), imm=0)
    if mnem in ALU_FN:
        return I(OP["SOP"], ALU_FN[mnem], rd=_reg(ops[0], "s"), ra=_reg(ops[1], "s"), rb=_reg(ops[2], "s"))
    if mnem in _SOPI_ALIAS:
        return I(OP["SOPI"], ALU_FN[_SOPI_ALIAS[mnem]], rd=_reg(ops[0], "s"), ra=_reg(ops[1], "s"),
                 imm=_int(ops[2], symbols))
    if mnem in ("LW", "LB"):
        ra, off = _memop(ops[1:], symbols)
        return I(OP[mnem], rd=_reg(ops[0], "s"), ra=ra, imm=off)
    if mnem in ("SW", "SB"):
        ra, off = _memop(ops[1:], symbols)
        return I(OP[mnem], ra=ra, rb=_reg(ops[0], "s"), imm=off)
    if mnem in BR_FN:
        target = _int(ops[2], symbols)
        return I(OP["BR"], BR_FN[mnem], ra=_reg(ops[0], "s"), rb=_reg(ops[1], "s"), imm=target - pc)
    if mnem == "J":
        return I(OP["JAL"], rd=0, imm=_int(ops[0], symbols) - pc)
    if mnem == "JAL":
        return I(OP["JAL"], rd=_reg(ops[0], "s"), imm=_int(ops[1], symbols) - pc)
    if mnem == "JALR":
        return I(OP["JALR"], rd=_reg(ops[0], "s"), ra=_reg(ops[1], "s"), imm=_int(ops[2], symbols) if len(ops) > 2 else 0)
    if mnem == "CSRR":
        return I(OP["CSRR"], rd=_reg(ops[0], "s"), imm=CSR.get(ops[1].upper(), None) if ops[1].upper() in CSR else _int(ops[1], symbols))
    if mnem == "CSRW":
        return I(OP["CSRW"], ra=_reg(ops[1], "s"), imm=CSR.get(ops[0].upper(), None) if ops[0].upper() in CSR else _int(ops[0], symbols))
    if mnem in ("HXOR", "HAND", "HOR"):
        return I(OP[mnem], rd=_reg(ops[0], "h"), ra=_reg(ops[1], "h"), rb=_reg(ops[2], "h"))
    if mnem == "HNOT":
        return I(OP["HNOT"], rd=_reg(ops[0], "h"), ra=_reg(ops[1], "h"))
    if mnem == "HMOV":
        return I(OP["HOR"], rd=_reg(ops[0], "h"), ra=_reg(ops[1], "h"), rb=_reg(ops[1], "h"))
    if mnem == "HROT":
        rc, imm = _val(ops[2:], symbols)
        return I(OP["HROT"], rd=_reg(ops[0], "h"), ra=_reg(ops[1], "h"), rc=rc, imm=imm)
    if mnem in ("HGEN", "HMASK"):
        rc, imm = _val(ops[1:], symbols)
        return I(OP[mnem], rd=_reg(ops[0], "h"), rc=rc, imm=imm)
    if mnem in ("HLD", "HST"):
        ra, off = _memop(ops[1:], symbols)
        return I(OP[mnem], rd=_reg(ops[0], "h"), ra=ra, imm=off)
    if mnem == "HDIST":
        return I(OP["HDIST"], rd=_reg(ops[0], "s"), ra=_reg(ops[1], "h"), rb=_reg(ops[2], "h"))
    if mnem in ("HSEARCH", "HSEARCH.WD"):
        return I(OP["HSEARCH"], SEARCH_WRDIST if mnem.endswith(".WD") else 0, rd=_reg(ops[0], "s"),
                 ra=_reg(ops[1], "h"), rb=_reg(ops[2], "s"), rc=_reg(ops[3], "s"))
    if mnem.startswith("ACC."):
        fn = mnem[4:]
        if fn not in ACC_FN:
            raise AsmError("unknown accumulator op %s" % mnem)
        if fn == "CLR":
            return I(OP["HACC"], ACC_FN[fn], rb=_int(ops[0], symbols))
        if fn in ("ADD", "SUB"):
            rc, imm = _val(ops[2:], symbols)
            return I(OP["HACC"], ACC_FN[fn], ra=_reg(ops[1], "h"), rb=_int(ops[0], symbols), rc=rc, imm=imm)
        if fn == "THR":
            rc, imm = _val(ops[2:], symbols)
            return I(OP["HACC"], ACC_FN[fn], rd=_reg(ops[0], "h"), rb=_int(ops[1], symbols), rc=rc, imm=imm)
        if fn in ("LD", "ST"):
            ra, off = _memop(ops[1:], symbols)
            return I(OP["HACC"], ACC_FN[fn], ra=ra, rb=_int(ops[0], symbols), imm=off)
    raise AsmError("unknown mnemonic %r" % mnem)


def disassemble_one(word, pc=None):
    i = Instr.decode(word)
    name = OP_NAME.get(i.op, "OP%02X" % i.op)
    h, s = (lambda r: "h%d" % r), (lambda r: "s%d" % r)
    if name in ("NOP", "HALT"):
        return name
    if name == "SOP":
        return "%s %s, %s, %s" % (ALU_FN_NAME.get(i.fn, "?"), s(i.rd), s(i.ra), s(i.rb))
    if name == "SOPI":
        return "%s %s, %s, %d" % (_SOPI_REV.get(ALU_FN_NAME.get(i.fn, "?"), "?"), s(i.rd), s(i.ra), i.imm)
    if name in ("LW", "LB"):
        return "%s %s, %d(%s)" % (name, s(i.rd), i.imm, s(i.ra))
    if name in ("SW", "SB"):
        return "%s %s, %d(%s)" % (name, s(i.rb), i.imm, s(i.ra))
    if name == "BR":   # absolute target when pc is known (re-assemblable), else relative
        tgt = str(pc + i.imm) if pc is not None else "%+d" % i.imm
        return "%s %s, %s, %s" % (BR_FN_NAME.get(i.fn, "?"), s(i.ra), s(i.rb), tgt)
    if name == "JAL":
        tgt = str(pc + i.imm) if pc is not None else "%+d" % i.imm
        return "JAL %s, %s" % (s(i.rd), tgt)
    if name == "JALR":
        return "JALR %s, %s, %d" % (s(i.rd), s(i.ra), i.imm)
    if name == "CSRR":
        return "CSRR %s, %s" % (s(i.rd), CSR_NAME.get(i.imm, str(i.imm)))
    if name == "CSRW":
        return "CSRW %s, %s" % (CSR_NAME.get(i.imm, str(i.imm)), s(i.ra))
    if name in ("HXOR", "HAND", "HOR"):
        return "%s %s, %s, %s" % (name, h(i.rd), h(i.ra), h(i.rb))
    if name == "HNOT":
        return "HNOT %s, %s" % (h(i.rd), h(i.ra))
    if name == "HROT":
        return "HROT %s, %s, %s, %d" % (h(i.rd), h(i.ra), s(i.rc), i.imm)
    if name in ("HGEN", "HMASK"):
        return "%s %s, %s, %d" % (name, h(i.rd), s(i.rc), i.imm)
    if name in ("HLD", "HST"):
        return "%s %s, %d(%s)" % (name, h(i.rd), i.imm, s(i.ra))
    if name == "HDIST":
        return "HDIST %s, %s, %s" % (s(i.rd), h(i.ra), h(i.rb))
    if name == "HSEARCH":
        return "HSEARCH%s %s, %s, %s, %s" % (".WD" if i.fn & SEARCH_WRDIST else "", s(i.rd), h(i.ra), s(i.rb), s(i.rc))
    if name == "HACC":
        fn = ACC_FN_NAME.get(i.fn, "?")
        if fn == "CLR":
            return "ACC.CLR %d" % i.rb
        if fn in ("ADD", "SUB"):
            return "ACC.%s %d, %s, %s, %d" % (fn, i.rb, h(i.ra), s(i.rc), i.imm)
        if fn == "THR":
            return "ACC.THR %s, %d, %s, %d" % (h(i.rd), i.rb, s(i.rc), i.imm)
        if fn in ("LD", "ST"):
            return "ACC.%s %d, %d(%s)" % (fn, i.rb, i.imm, s(i.ra))
    return "%s fn=%d rd=%d ra=%d rb=%d rc=%d imm=%d" % (name, i.fn, i.rd, i.ra, i.rb, i.rc, i.imm)


def disassemble(words, with_addr=True):
    if with_addr:
        return "\n".join("%5d: %016x  %s" % (pc, w, disassemble_one(w, pc)) for pc, w in enumerate(words))
    return "\n".join(disassemble_one(w, pc) for pc, w in enumerate(words))


def to_hex(words):
    """Verilog $readmemh format, one 64-bit word per line."""
    return "\n".join("%016x" % w for w in words) + "\n"
