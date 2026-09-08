# Full build for the VHK158 (Versal HBM VH1582): package the SPU IP, create the
# PS block design, synthesise, implement and write the PDI + XSA.
#
#   cd hw/fpga/vhk158 && vivado -mode batch -source build.tcl [-tclargs <jobs>]
#
set here  [file dirname [file normalize [info script]]]
set jobs  [expr {[llength $argv] > 0 ? [lindex $argv 0] : 8}]
set part  "xcvh1582-vsva3697-2MP-e-S"
set board "xilinx.com:vhk158:part0:1.1"     ;# VERIFY with: get_board_parts *vhk158*
set proj  "$here/build/spu_vhk158"

source "$here/package_ip.tcl"

create_project -force spu_vhk158 $proj -part $part
if {[llength [get_board_parts $board]]} { set_property board_part $board [current_project] }
set_property ip_repo_paths [list "$here/ip_repo"] [current_project]
update_ip_catalog

source "$here/bd_ps.tcl"
add_files -fileset constrs_1 "$here/constraints.xdc"

# Versal timing: SPU at 250 MHz; retiming helps the pipelined multipliers/popcount
set_property STEPS.SYNTH_DESIGN.ARGS.RETIMING true [get_runs synth_1]
set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]

launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
launch_runs impl_1 -to_step write_device_image -jobs $jobs
wait_on_run impl_1
open_run impl_1
report_utilization -hierarchical -file "$proj/utilization.rpt"
report_timing_summary -file "$proj/timing.rpt"
write_hw_platform -fixed -include_bit -force "$proj/spu_vhk158.xsa"
puts "PDI: [glob -nocomplain $proj/spu_vhk158.runs/impl_1/*.pdi]"
puts "XSA: $proj/spu_vhk158.xsa  (use with PetaLinux / Vitis)"
