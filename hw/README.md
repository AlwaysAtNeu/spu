# SPU hardware (RTL)

Synthesizable SystemVerilog implementation of the Symbolic Processing Unit ISA defined in
`sw/spuc/isa.py`.  The Python simulator `sw/spuc/sim.py` is the golden model; the RTL is
verified against it bit-for-bit with the Verilator co-simulation in `sw/tests/test_cosim.py`.

## Files

| file | contents |
|---|---|
| `rtl/spu_pkg.sv` | **generated** from `isa.py` (`python3 -c "from spuc.isa import emit_sv_package; print(emit_sv_package(), end='')"`): parameters, opcodes, CSR / host register map |
| `rtl/spu_top.sv` | top level: AXI4-Lite slave `s_axi_*` (18-bit address, 32-bit data) + AXI4 master `m_axi_*` (64-bit address, 512-bit data, 4-bit id), `clk`, `rst_n`, `irq` |
| `rtl/spu_csr.sv` | host registers (CTRL/STATUS/PC/MEM_BASE/IRQ), CSR + scalar + hypervector debug readback, 4096 x 64-bit instruction BRAM written as 32-bit lo/hi halves |
| `rtl/spu_core.sv` | instruction sequencer, scalar unit, hypervector micro-sequencer, CSR file |
| `rtl/spu_hvrf.sv` | HV register file: 32 regs x 16 chunks x 512 bit as two duplicated dual-port BRAM copies (2 reads + 1 write per cycle) |
| `rtl/spu_popcount.sv` | 4-stage pipelined 512 -> 10 bit population count |
| `rtl/spu_gen.sv` | splitmix64 hypervector generator, 8 lanes x 64 bit per cycle, multiplies split into pipelined 32x32 partial products |
| `rtl/spu_acc.sv` | 16 bundling accumulators x 8192 lanes x 16-bit saturating counters, 128 lanes per cycle, ADD/SUB/THR/CLR/LD/ST |
| `rtl/spu_dma.sv` | AXI4 read/write engines: 64-byte beats, bursts split at 4 KiB, up to 8 outstanding read bursts, write fence |
| `rtl/spu_search.sv` | streaming associative search (Hamming argmin + second best, optional packed distance write-out) |
| `rtl/files.f` | compile order |
| `tb/tb_spu.sv` | self-checking-free driver testbench: loads `+prog=`/`+mem=`, runs, dumps registers/CSRs/memory |
| `tb/axi_mem_model.sv` | behavioural 16 MiB AXI4 slave with random back-pressure and latency |
| `tb/Makefile`, `tb/run.sh` | Verilator build (`--binary --timing`) and run helpers |
| `tb/measure_cycles.py` | per-instruction cycle measurement on the RTL |
| `tb/verilator.vlt` | lint waivers (benign only) |

## Micro-architecture

```
                 +-----------------------------------------------------------------+
 AXI4-Lite  ---> | spu_csr : CTRL/STATUS, PC_START, MEM_BASE, CSR/SREG/HREG        |
 (host)          |           readback, instruction BRAM 4096 x 64                  |
                 +------------------------------+----------------------------------+
                                                | prog_addr / prog_rdata (1 cycle)
                 +------------------------------v----------------------------------+
                 | spu_core                                                        |
                 |  FETCH -> DECODE -> EXEC   (next instruction prefetched in EXEC)|
                 |  scalar unit: 32 x 32-bit regs, ALU, MUL, branches, CSRs        |
                 |  HV pipeline: RD -> EX1 -> EX2 -> WB, one 512-bit chunk / cycle |
                 |     spu_hvrf (2 copies)   hbuf (8192-bit operand buffer)       |
                 |     barrel rotate (2 stages), mask, xor/and/or/not              |
                 |  spu_gen ---> RF        spu_popcount (HDIST)                    |
                 |  spu_acc  <-> hbuf/RF   spu_search <-> hbuf                     |
                 |  spu_dma  <-- HLD/HST/LW/LB/SW/SB/ACC.LD/ACC.ST/search streams   |
                 +------------------------------+----------------------------------+
                                                | AXI4 master, 512-bit data
                                            memory (HBM / DDR via NoC)
```

* **Instruction flow.** `S_FETCH` presents `pc`, `S_DECODE` latches the 64-bit word,
  `S_EXEC` executes scalar ops in one cycle and retires; the sequential successor is
  prefetched during `EXEC`, so straight-line scalar code runs at **2 cycles/instruction**,
  taken branches at 3.  HV ops start a micro-sequence and retire when it drains.
* **HV pipeline.** Each chunk (512 bit) is read from the two RF copies, transformed
  (EX1/EX2) and written back three cycles later; a full HV op is `C + 3 + 3` cycles.
  `HROT` with `rd == ra` first copies the source to `hbuf` (WAR hazard on the chunk
  loop), all other ops read and write the register file directly.
* **hbuf.** A 16 x 512-bit register buffer holds the operand for `HST`, `HSEARCH`
  (query), `ACC.ADD/SUB` and buffered rotates.
* **Memory ordering.** Stores are posted; any read request waits until every write has
  been acknowledged (`wr_idle` fence), so program order is preserved for
  store -> load / `HST` -> `HSEARCH` sequences.  `HALT` waits for all writes before
  raising `DONE`/`irq`.
* **Search.** Up to 4 candidates in flight (`MAX_INFLIGHT`), one 64-byte beat per cycle
  through XOR + pipelined popcount; strict `<` keeps the earliest index on ties, second
  best tracked exactly like the golden model.  Distances are packed 16 per beat and
  written with byte strobes (any 4-byte aligned `DOUT`).
* **Accumulators.** 2 Mbit memory (16 x 64 rows x 2048 bit), 2-cycle read, 128 lanes of
  18-bit saturating adders per cycle; `THR` produces 128 bits per step which the core
  packs into RF chunks.
* **Addresses.** 32-bit SPU offsets + 64-bit `MEM_BASE`.  HV / accumulator accesses ignore
  the low 6 address bits, `LW/SW` the low 2.  Bursts never cross 4 KiB boundaries.
* **Errors.** Illegal opcode / function, accumulator index >= NACC, `pc >= PROG_DEPTH`
  and AXI SLVERR/DECERR set `CSR[ERR]` and `STATUS.ERR` and halt.
* **Host debug.** Writing `HREG_BASE` selects `{reg, chunk}`; the 16 words at
  `HREG_BASE+4..` return that chunk while the core is idle (used by the testbench to dump
  all hypervector registers through the real host interface).

## Measured cycles per instruction (Verilator, memory latency 4 cycles, no back-pressure)

| instruction | RTL cycles | golden-model estimate |
|---|---:|---:|
| scalar ALU (`ADDI`, `AND`, ...) | 2 | 3 |
| `CSRR` / `CSRW` | 2 | 3 |
| branch not taken / taken | 2 / 3 | 3 / 3 |
| `MUL` | 3 | 3 |
| `LW` / `LB` | 11 (+ memory latency) | 43 |
| `SW` / `SB` (posted) | 8 | 4 |
| `HXOR` / `HAND` / `HOR` / `HNOT` / `HMASK` | 22 | 21 |
| `HROT` (`rd != ra`) | 22 | 23 |
| `HROT` (`rd == ra`, buffered) | 39 | 23 |
| `HGEN` | 28 | 25 |
| `HLD` | 26 (+ memory latency) | 61 |
| `HST` | 40 | 23 |
| `HDIST` | 24 | 24 |
| `HSEARCH`, 8 candidates | 192 | 179 |
| `HSEARCH.WD`, 8 candidates | 198 | 183 |
| `ACC.CLR` / `ACC.THR` | 71 / 73 | 70 |
| `ACC.ADD` / `ACC.SUB` | 91 | 70 |
| `ACC.LD` | 280 | 363 |
| `ACC.ST` | 460 | 323 |

(`measure_cycles.py` reproduces the table; the golden model only estimates cycles and
is not required to match.)

## Lint / simulation

```
cd hw/tb
make lint                 # verilator --lint-only -Wall (clean, see verilator.vlt for the 3 waived benign rules)
make                      # builds obj_dir/tb_spu  (verilator --binary --timing)
./run.sh prog.hex mem.hex [+timeout=N +backpressure=pct +max_lat=cycles +vcd]
```

`prog.hex` is one 64-bit instruction per line (`spuc.asm.to_hex`), `mem.hex` is the
`$readmemh` image written by `spuc.sim.Memory.dump_hex` (`@addr` in 64-byte words).  The
run writes `mem_out.hex`, `sregs.txt`, `hregs.txt`, `csrs.txt`, `cycles.txt`.

Co-simulation against the golden model (builds the model automatically):

```
cd sw
python3 -m pytest -q tests/test_cosim.py      # 10 programs incl. 3 randomized differential runs
python3 -c "from spuc.cosim import cosim; cosim('HGEN h1, 7\nHROT h2, h1, 5\nHALT')"
```

`spuc.cosim.compare` checks all 32 scalar registers, all 32 hypervector registers (via the
host debug readback), all CSRs except the cycle/instruction counters, every memory page
the model touched and that the RTL wrote nothing elsewhere.

## Resource expectations (xcvh1582, default configuration)

* HV register file: 2 x 256 Kbit BRAM (~16 BRAM36 or 2 URAM stacks)
* accumulators: 2 Mbit (URAM or ~58 BRAM36), 128 x 18-bit adders
* generator: 8 lanes x 6 32x32 multipliers ~ 150-200 DSP58 (reduce `LANES` in `spu_gen`
  if DSPs are scarce)
* barrel rotate: two 1024-bit shift stages (~10 k LUT), popcount trees ~ 2 x 2 k LUT
* hbuf / search query / distance buffers: ~10 k flip-flops
* overall on the order of 40-60 k LUTs, well below 5 % of the device

## Known limitations / next steps

* Timing has not been run through Vivado in this session; the pipelines are designed for
  250-300 MHz on Versal but the 64-bit generator multiplies and the accumulator adders
  are the paths to watch (add pipeline registers or reduce `LANES`/`ACC_LANES`).
* `HST` copies the register into `hbuf` before streaming (16 extra cycles); `ACC.ST`
  serialises row read and 4-beat write (could be pipelined 3x).
* Reads are fenced behind *all* outstanding writes rather than only conflicting ones.
* `pc` range check uses `PROG_DEPTH` (the model checks against the loaded program length).
* Single-issue, no overlap between consecutive HV instructions.
