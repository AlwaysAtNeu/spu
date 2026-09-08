"""spuc - compiler, simulator and runtime for the SPU (Symbolic Processing Unit).

Layout
------
isa.py       single source of truth for the ISA / register maps (also generates spu_pkg.sv)
hv.py        hypervector arithmetic shared by simulator, compiler and host runtime
asm.py       assembler / disassembler for SPU assembly text
sim.py       cycle-estimating functional simulator (golden model for the RTL)
ir.py        compiler IR (structured program of HV / scalar statements)
dsl.py       Python DSL used to author symbolic programs
codegen.py   IR -> machine code (register allocation, lowering, emission)
runtime.py   Device abstraction: SimDevice / VerilatorDevice / XdmaDevice / UioDevice
frontends/   hdc.py (HDC classification train/infer), logic.py (VSA knowledge base + Horn rules)
"""
from .isa import Config, DEFAULT_CONFIG  # noqa: F401
__version__ = "0.1.0"
