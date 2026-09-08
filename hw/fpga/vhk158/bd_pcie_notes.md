# PCIe (CPM5 + HBM) integration notes for VHK158

The PS path (`bd_ps.tcl`) is the recommended first bring-up.  For the PCIe host
path the CPM5 configuration is large and board-revision specific, so start from
the example design that AMD ships with Vivado instead of writing it by hand:

1. `Tools -> Open Example Design` or, in the IP catalog, open *Versal CPM5 QDMA*
   (or *CPM5 PCIe DMA*) and choose the **VHK158** board.  This yields a validated
   block design with CIPS (PMC/PS + CPM5 Gen4/Gen5 x8 endpoint), an `axi_noc`
   with the **HBM** controllers enabled and the DMA's AXI-MM master routed to HBM.
2. Add the SPU IP repo (`hw/fpga/vhk158/ip_repo`) under *Settings -> IP -> Repository*
   and drop `spu_top` into the diagram.
3. Connect
   * DMA **AXI-Lite master** (`M_AXI_LITE`, the "user" BAR) -> `spu_top/s_axi`
     (through a SmartConnect; give it 256 KB).  With XDMA this is `/dev/xdma0_user`
     offset 0; with QDMA it is the user BAR reachable through `/dev/qdma<bdf>-MM-...`
     mmap - `spuc.runtime.XdmaDevice` only needs the char-device paths.
   * `spu_top/m_axi` -> a new **PL** slave port of the `axi_noc` (512-bit, category
     `pl`) with a connection to the same HBM port(s) the DMA uses (e.g.
     `HBM0_PORT0`).  Then SPU addresses = `MEM_BASE + offset`, where `MEM_BASE` is
     the HBM base shown in the Address Editor for that port.
   * `spu_top/irq` -> the DMA user interrupt (`usr_irq_req[0]`) or leave unconnected
     and poll `STATUS.DONE` (the runtime polls).
   * clock: the SPU can run on the DMA `axi_aclk` (250 MHz for Gen4 x8) or on its own
     `clk_wizard` output; use a `proc_sys_reset` for `rst_n`.
4. `Validate Design`, generate the wrapper, run `build.tcl` (synth/impl/PDI).
5. On the host: load the XDMA/QDMA driver (`dma_ip_drivers` from AMD's GitHub), then

       python3 -c "from spuc.runtime import XdmaDevice; d = XdmaDevice(mem_base=0x40_0000_0000); print(hex(d.reg_read(0x20)))"

   must print `0x53505531` ("SPU1").  `mem_base` is the HBM address from step 3.

Bandwidth note: one SPU instance streams 64 B/cycle from memory during HSEARCH
(16 GB/s at 250 MHz); HBM2e on VH1582 provides ~820 GB/s, so up to ~32 SPU cores
(or wider search units) can be fed before memory becomes the limit.
