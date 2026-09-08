# SPU — Symbolic Processing Unit

The goal of this project is to implement an AI accelerator for training and inference of **symbolic (Symbolism) models**: the SPU (Symbolic Processing Unit). It consists of a synthesizable hardware acceleration unit (SystemVerilog RTL) and the matching compiler / functional simulator / runtime (Python). The RTL is verified bit-for-bit against the Python golden model by Verilator co-simulation; `hw/fpga/` additionally provides integration scripts that use the AMD Versal HBM **VHK158** (`xcvh1582-vsva3697-2MP-e-S`) as a reference platform.

Symbols are physically represented with a **Vector Symbolic Architecture (VSA / hyperdimensional computing)**: a symbol is an 8192-bit random hypervector; binding (XOR) expresses role-filler pairs and relation tuples, bundling (majority vote) expresses sets / records / superposed knowledge, permutation (cyclic shift) expresses order, unbinding + associative memory performs inference, and accumulation + binarization performs learning. On this algebra, classifier training, knowledge-base pattern queries and Horn-rule forward chaining (Datalog-style fixpoint) all compile into **a single autonomously running SPU program**.

```
  Python frontends (HDC classifier / knowledge base + rules / custom DSL)
        │  spuc.frontends, spuc.dsl
        ▼
  SPU program (64-bit ISA) + data image + manifest     ──▶  spuc.sim   (golden model, cycle estimate)
        │  spuc.runtime.Session                        ──▶  Verilator  (RTL co-simulation, bit-exact)
        ▼                                              ──▶  VHK158     (UIO / XDMA driver)
  spu_top: scalar core + 512-bit chunked hypervector datapath + accumulators + associative search + AXI DMA
```

## Layout

| Path | Contents |
|------|----------|
| `sw/spuc/isa.py` | Single source of truth for the ISA / register map (generates `spu_pkg.sv` and `spu_regs.h`) |
| `sw/spuc/{hv,asm,sim}.py` | Hypervector arithmetic, assembler / disassembler, functional simulator (golden model) |
| `sw/spuc/dsl.py` | Compiler: symbol table, buffer layout, scoped register allocation, structured control flow, linking |
| `sw/spuc/frontends/hdc.py` | HDC classification: record / n-gram encoding, on-device training (incl. retraining epochs) and inference |
| `sw/spuc/frontends/logic.py` | Knowledge base: fact encoding, pattern queries, Horn-rule forward chaining, deduplication, fixpoint |
| `sw/spuc/runtime.py` | `SimDevice / VerilatorDevice / XdmaDevice / UioDevice` + `Session` |
| `sw/spuc/cosim.py`, `sw/tests/` | RTL co-simulation and all tests |
| `sw/examples/` | Analogy reasoning, HDC training / inference, knowledge-base rule inference, cycle benchmark, resonator structure decomposition |
| `hw/rtl/` | Synthesizable RTL (`spu_top` and submodules); `hw/tb/` Verilator testbench |
| `hw/fpga/vhk158/` | IP packaging, block design, build scripts, PCIe integration notes |
| `hw/host/` | Device-tree overlay (UIO + u-dma-buf), C register header |
| `docs/` | **00 chip architecture and design rationale**, 01 micro-architecture, 02 ISA, 03 compiler, 04 mapping of symbolic models, 05 FPGA reference flow (documents are written in Chinese) |

## Quick start

```bash
# Requirements: python3 + numpy (pytest optional); RTL simulation needs verilator 5.x
sh scripts/run_tests.sh                         # golden model / compiler / frontends / (if verilator is present) RTL cosim
python3 sw/examples/01_analogy.py               # "dollar of Mexico" -> PESO
python3 sw/examples/02_hdc_classify.py sim      # 3 epochs of on-device training + inference, bit-exact vs. host reference
python3 sw/examples/03_kb_rules.py sim          # forward-chain 6 rules to a fixpoint, matches a Datalog evaluator
python3 sw/examples/05_resonator.py sim 24      # resonator decomposition S = X⊗Y⊗Z (13824 combinations)
python3 sw/examples/03_kb_rules.py rtl          # the same program running on the Verilator RTL
python3 -m spuc asm prog.s -o prog.hex          # assemble / disassemble / single-step
```

Optional FPGA reference implementation (Vivado 2023.1+): `cd hw/fpga/vhk158 && vivado -mode batch -source build.tcl`, then follow `docs/05_fpga_vhk158.md` for PetaLinux (PS path) or XDMA (PCIe path).

## Status

| Component | Status |
|-----------|--------|
| ISA, assembler, functional simulator | Done, `tests/test_sim_basic.py` |
| Compiler DSL (register allocation, control flow, linking) | Done, `tests/test_compiler.py` |
| HDC classification frontend (training / retraining / inference all on device) | Done, device results bit-exact vs. host reference |
| Knowledge base + Horn-rule forward chaining | Done, results match a naive Datalog evaluator |
| RTL (`hw/rtl`, 10 modules, Verilator `-Wall` lint clean) | Done; `tests/test_cosim.py` runs 10 programs (incl. 3 randomized differential tests) bit-exact against the golden model; all five examples match the host reference on the RTL; measured cycles in `hw/README.md` |
| VHK158 Vivado scripts / device tree / driver path | Written, **not verified on real Vivado or hardware** (no Vivado on the development machine); items marked `VERIFY` need to be confirmed once in the GUI |

Measured on the RTL (Verilator, scaled to 250 MHz): analogy reasoning 46 instructions / 2,154 cycles; forward chaining 6 rules deriving 27 facts 620 k cycles ≈ 2.5 ms; HDC classification with 32 features 7.6 k cycles per training sample, 31 µs per inference.
