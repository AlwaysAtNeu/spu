# SPU — Symbolic Processing Unit

本项目的目标是实现一个面向 **符号主义（Symbolism）模型** 训练 / 推理的 AI 加速器：SPU（Symbolic Processing Unit），包括可综合的硬件加速单元（SystemVerilog RTL）和配套的编译器 / 功能模拟器 / 运行时（Python）。RTL 已与 Python 黄金模型在 Verilator 上逐比特联合仿真；`hw/fpga/` 另附以 AMD Versal HBM **VHK158**（`xcvh1582-vsva3697-2MP-e-S`）为参考平台的集成脚本。

符号计算的物理载体是 **向量符号架构（VSA / 超维计算）**：符号 = 8192 bit 随机超向量；绑定（XOR）表达角色-填充与关系元组，捆绑（多数表决）表达集合 / 记录 / 知识叠加，置换（循环移位）表达顺序，解绑 + 关联记忆完成推理，累加 + 二值化完成学习。在这一代数上，分类模型训练、知识库模式查询、Horn 规则前向链接（Datalog 式不动点）都能被编译成 **单个自主运行的 SPU 程序**。

```
  Python 前端 (HDC 分类器 / 知识库+规则 / 自定义 DSL)
        │  spuc.frontends, spuc.dsl
        ▼
  SPU 程序 (64-bit ISA) + 数据镜像 + manifest        ──▶  spuc.sim   (黄金模型, 周期估计)
        │  spuc.runtime.Session                        ──▶  Verilator  (RTL 联合仿真, 比特一致)
        ▼                                              ──▶  VHK158     (UIO / XDMA 驱动)
  spu_top: 标量核 + 512b 分块超向量数据通路 + 累加器 + 关联搜索 + AXI DMA
```

## 目录

| 路径 | 内容 |
|------|------|
| `sw/spuc/isa.py` | ISA / 寄存器映射的唯一事实来源（生成 `spu_pkg.sv`、`spu_regs.h`） |
| `sw/spuc/{hv,asm,sim}.py` | 超向量算术、汇编器 / 反汇编器、功能模拟器（黄金模型） |
| `sw/spuc/dsl.py` | 编译器：符号表、缓冲区布局、作用域寄存器分配、结构化控制流、链接 |
| `sw/spuc/frontends/hdc.py` | HDC 分类：记录 / n-gram 编码、设备端训练（含再训练 epoch）与推理 |
| `sw/spuc/frontends/logic.py` | 知识库：事实编码、模式查询、Horn 规则前向链接、去重、不动点 |
| `sw/spuc/runtime.py` | `SimDevice / VerilatorDevice / XdmaDevice / UioDevice` + `Session` |
| `sw/spuc/cosim.py`, `sw/tests/` | RTL 联合仿真与全部测试 |
| `sw/examples/` | 类比推理、HDC 训练 / 推理、知识库规则推理、周期基准、共振器结构分解 |
| `hw/rtl/` | 可综合 RTL（`spu_top` 及子模块），`hw/tb/` Verilator 测试平台 |
| `hw/fpga/vhk158/` | IP 打包、块设计、构建脚本、PCIe 集成说明 |
| `hw/host/` | 设备树 overlay（UIO + u-dma-buf）、C 寄存器头文件 |
| `docs/` | **00 芯片架构与设计理由**、01 微架构、02 ISA、03 编译器、04 符号模型映射、05 FPGA 上板 |

## 快速开始

```bash
# 依赖：python3 + numpy（pytest 可选），RTL 仿真需要 verilator 5.x
sh scripts/run_tests.sh                         # 黄金模型 / 编译器 / 前端 / (若有 verilator) RTL cosim
python3 sw/examples/01_analogy.py               # "dollar of Mexico" -> PESO
python3 sw/examples/02_hdc_classify.py sim      # 设备端训练 3 epoch + 推理，与主机参考比特一致
python3 sw/examples/03_kb_rules.py sim          # 6 条规则前向链接到不动点，与 Datalog 求值器一致
python3 sw/examples/05_resonator.py sim 24      # 共振器分解 S = X⊗Y⊗Z（13824 种组合）
python3 sw/examples/03_kb_rules.py rtl          # 同一程序在 Verilator RTL 上运行
python3 -m spuc asm prog.s -o prog.hex          # 汇编 / 反汇编 / 单步运行
```

可选的 FPGA 参考实现（Vivado 2023.1+）：`cd hw/fpga/vhk158 && vivado -mode batch -source build.tcl`，然后按 `docs/05_fpga_vhk158.md` 做 PetaLinux（PS 路径）或 XDMA（PCIe 路径）。

## 状态

| 部分 | 状态 |
|------|------|
| ISA、汇编器、功能模拟器 | 完成，`tests/test_sim_basic.py` |
| 编译器 DSL（寄存器分配、控制流、链接） | 完成，`tests/test_compiler.py` |
| HDC 分类前端（训练 / 再训练 / 推理全部在设备上） | 完成，设备结果与主机参考比特一致 |
| 知识库 + Horn 规则前向链接 | 完成，与朴素 Datalog 求值器结果一致 |
| RTL（`hw/rtl`，10 个模块，Verilator `-Wall` lint 通过） | 完成；`tests/test_cosim.py` 10 个程序（含 3 组随机差分测试）与黄金模型逐比特一致；五个示例在 RTL 上结果与主机参考一致；实测周期见 `hw/README.md` |
| VHK158 Vivado 脚本 / 设备树 / 驱动路径 | 已编写，**未在真实 Vivado 与板卡上验证**（本机无 Vivado）；`VERIFY` 标记处需在 GUI 中确认 |

为什么这样设计（表示选择、每个单元的取舍、性能与扩展）见 `docs/00_design_rationale.md`；符号模型如何映射见 `docs/04_symbolic_models.md`。

RTL 实测（Verilator，250 MHz 换算）：类比推理 46 条指令 / 2 154 拍；6 条规则前向链接推出 27 条事实 620 k 拍 ≈ 2.5 ms；HDC 分类 32 特征每样本训练 7.6 k 拍、推理 31 µs。
