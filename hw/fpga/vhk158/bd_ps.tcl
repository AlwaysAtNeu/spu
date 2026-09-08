# Block design: SPU driven from the Versal PS (Cortex-A72 Linux).
#
#   CIPS (PS, PMC, PL clock) --M_AXI_FPD--> smartconnect --> spu_top.s_axi (CSR + program memory)
#   spu_top.m_axi (512b) --> axi_noc S_AXI (PL NMU) --> DDR4 memory controller (or HBM, see below)
#   spu_top.irq --> CIPS pl_ps_irq0
#
# This is the lowest-risk bring-up path on VHK158: no PCIe, the Python runtime
# (spuc.runtime.UioDevice) runs on the A72 under PetaLinux and talks to the SPU
# through UIO + a physically contiguous DMA buffer.  Sourced by build.tcl.
#
# Lines marked VERIFY depend on the Vivado version / board file revision and
# must be checked in the Vivado GUI once (Address Editor / Validate Design).

set bd_name spu_ps
create_bd_design $bd_name

# ---------------------------------------------------------------- CIPS
set cips [create_bd_cell -type ip -vlnv xilinx.com:ip:versal_cips:3.4 versal_cips_0]   ;# VERIFY: IP version
apply_bd_automation -rule xilinx.com:bd_rule:cips -config { \
    board_preset {Yes} boot_config {Custom} configure_noc {Add new AXI NoC} \
    debug_config {JTAG} design_flow {Full System} mc_type {DDR} num_mc_ddr {1} num_mc_lpddr {None} \
    pl_clocks {1} pl_resets {1} } $cips
set_property -dict [list \
    CONFIG.PS_PMC_CONFIG { \
        PS_USE_M_AXI_FPD {1} PS_M_AXI_FPD_DATA_WIDTH {128} \
        PS_USE_PMCPL_CLK0 {1} PMC_CRP_PL0_REF_CTRL_FREQMHZ {100} \
        PS_USE_IRQ_0 {1} PS_USE_PL_RESET {1} \
        PS_NUM_FABRIC_RESETS {1} \
    } ] $cips                                                                       ;# VERIFY: merges with the preset

# NoC created by the automation (DDR4 DIMM preset from the board file)
set noc [get_bd_cells axi_noc_0]

# ------------------------------------------------------- clock and reset
set clkwiz [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wizard:1.0 clk_wizard_0]
set_property -dict [list CONFIG.CLKOUT_DRIVES {BUFG} CONFIG.CLKOUT_REQUESTED_OUT_FREQUENCY {250.000} \
    CONFIG.CLKOUT_USED {true} CONFIG.PRIM_SOURCE {No_buffer} CONFIG.USE_LOCKED {true} CONFIG.USE_RESET {false}] $clkwiz
connect_bd_net [get_bd_pins $cips/pl0_ref_clk] [get_bd_pins $clkwiz/clk_in1]

set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_0]
connect_bd_net [get_bd_pins $clkwiz/clk_out1] [get_bd_pins $rst/slowest_sync_clk]
connect_bd_net [get_bd_pins $cips/pl0_resetn] [get_bd_pins $rst/ext_reset_in]
connect_bd_net [get_bd_pins $clkwiz/locked] [get_bd_pins $rst/dcm_locked]

# ------------------------------------------------------------------ SPU
set spu [create_bd_cell -type ip -vlnv xilinx.com:user:spu_top:1.0 spu_top_0]
connect_bd_net [get_bd_pins $clkwiz/clk_out1] [get_bd_pins $spu/clk]
connect_bd_net [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $spu/rst_n]
connect_bd_net [get_bd_pins $spu/irq] [get_bd_pins $cips/pl_ps_irq0]

# PS -> SPU control path (128-bit FPD master -> 32-bit AXI-Lite)
set sc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 smartconnect_0]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {2}] $sc
connect_bd_intf_net [get_bd_intf_pins $cips/M_AXI_FPD] [get_bd_intf_pins $sc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $sc/M00_AXI] [get_bd_intf_pins $spu/s_axi]
connect_bd_net [get_bd_pins $cips/pl0_ref_clk] [get_bd_pins $sc/aclk]        ;# VERIFY: M_AXI_FPD clock pin name (fpd_axi_noc_axi0_clk / pl0_ref_clk)
connect_bd_net [get_bd_pins $clkwiz/clk_out1] [get_bd_pins $sc/aclk1]
connect_bd_net [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $sc/aresetn]
connect_bd_net [get_bd_pins $cips/pl0_ref_clk] [get_bd_pins $cips/m_axi_fpd_aclk]

# SPU -> memory data path: add one PL AXI slave port (512-bit) to the NoC
set n_si [get_property CONFIG.NUM_SI $noc]
set n_clk [get_property CONFIG.NUM_CLKS $noc]
set_property -dict [list CONFIG.NUM_SI [expr {$n_si + 1}] CONFIG.NUM_CLKS [expr {$n_clk + 1}]] $noc
set si  [format "S%02d_AXI" $n_si]
set clk [format "aclk%d" $n_clk]
set_property -dict [list CONFIG.DATA_WIDTH {512} CONFIG.CATEGORY {pl} \
    CONFIG.CONNECTIONS {MC_0 {read_bw {10000} write_bw {10000} read_avg_burst {16} write_avg_burst {16}}}] \
    [get_bd_intf_pins $noc/$si]                                                 ;# VERIFY: MC_0 = DDR4 controller port name
set_property -dict [list CONFIG.ASSOCIATED_BUSIF $si] [get_bd_pins $noc/$clk]
connect_bd_intf_net [get_bd_intf_pins $spu/m_axi] [get_bd_intf_pins $noc/$si]
connect_bd_net [get_bd_pins $clkwiz/clk_out1] [get_bd_pins $noc/$clk]

# --------------------------------------------------------- address map
assign_bd_address
# SPU registers at 0xA400_0000 in the PL address region (256 KB) - matches hw/host/spu-uio.dtsi
set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces $cips/FPD_AXI_NOC_0] -filter {NAME =~ *spu_top_0*}]   ;# VERIFY: master space name
if {[llength $seg]} { set_property offset 0xA4000000 $seg; set_property range 256K $seg }
# SPU master sees the whole DDR (MEM_BASE register selects the window at run time)

validate_bd_design
save_bd_design
make_wrapper -files [get_files $bd_name.bd] -top -import
set_property top ${bd_name}_wrapper [current_fileset]
