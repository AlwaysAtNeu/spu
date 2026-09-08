# 在 VHK158 上运行

参考板卡：AMD Versal HBM 系列 VHK158 评估套件，器件 `xcvh1582-vsva3697-2MP-e-S`（32 GB HBM2e，CPM5 PCIe Gen5，双核 A72 PS）。需要 Vivado 2023.1 或更新（VHK158 板卡文件）。**本仓库的 FPGA 脚本尚未在真实 Vivado / 板卡上运行**（开发环境无 Vivado），RTL 已通过 Verilator 与黄金模型的联合仿真；`VERIFY` 标记处是需要在 GUI 中确认一次的属性名 / 版本号。

## 集成拓扑

SPU 只需要两个接口：AXI4-Lite 从（寄存器 + 程序存储，256 KB）与 AXI4 主（512 bit，64 bit 地址）。

* **PS 路径（推荐先做）** `hw/fpga/vhk158/bd_ps.tcl`：CIPS PS `M_AXI_FPD` → SmartConnect → `spu_top/s_axi`；`spu_top/m_axi` → `axi_noc` 新增 PL 从口（512 bit）→ DDR4 控制器；`irq` → `pl_ps_irq0`；`clk_wizard` 250 MHz。Linux（PetaLinux）上用 UIO 访问寄存器、u-dma-buf 提供物理连续数据窗口，`spuc.runtime.UioDevice`。
* **PCIe 路径** `hw/fpga/vhk158/bd_pcie_notes.md`：以 Vivado 自带的 CPM5 QDMA/XDMA 示例设计为基础，把 DMA 的 AXI-Lite 用户 BAR 接到 `s_axi`，`m_axi` 接到 NoC 的 HBM 端口；主机用 `spuc.runtime.XdmaDevice(mem_base=<HBM 基址>)`。

## 构建

```bash
cd hw/fpga/vhk158
vivado -mode batch -source build.tcl -tclargs 8      # 打包 IP → 块设计 → 综合 / 实现 → PDI + XSA
```

产物：`build/spu_vhk158/spu_vhk158.runs/impl_1/*.pdi`、`spu_vhk158.xsa`（给 PetaLinux / Vitis）。

## PetaLinux（PS 路径）

1. `petalinux-create -t project --template versal`，`petalinux-config --get-hw-description=<xsa>`。
2. 把 `hw/host/spu-uio.dtsi` 合并到 `system-user.dtsi`；内核启用 `UIO_PDRV_GENIRQ`，bootargs 加 `uio_pdrv_genirq.of_id=generic-uio`；编译 u-dma-buf 模块。
3. 板上 `pip install numpy`，拷贝 `sw/`，运行：

```bash
python3 -c "from spuc.runtime import get_device; d=get_device('uio'); print(hex(d.reg_read(0x20)))"   # 0x53505531
python3 sw/examples/03_kb_rules.py uio
```

## 检查清单

* `bd_ps.tcl` 中 `versal_cips` 版本、`M_AXI_FPD` 时钟引脚、NoC 从口到 `MC_0` 的连接名、SPU 寄存器段 0xA400_0000 —— 在 Address Editor 中核对，与 `spu-uio.dtsi` 一致。
* 时序：`timing.rpt` 中 SPU 时钟 250 MHz 应无违例；如 HGEN 乘法链或 popcount 报负 slack，把 `clk_wizard` 降到 200 MHz 或在 `spu_gen` 中增加流水级。
* 资源：`utilization.rpt`；累加器阵列应映射到 URAM（见 `constraints.xdc` 注释）。
* 内存窗口：`MEM_BASE` 必须与运行时使用的物理地址一致（UIO 路径由 u-dma-buf 的 `phys_addr` 自动读取；PCIe 路径手动传入 HBM 基址）。
* 首次上电用 `SCRATCH0` 回环和 `01_analogy.py`（48 条指令、无内存突发依赖以外的复杂路径）做冒烟测试，再跑 HDC / 规则示例。
