# Package hw/rtl as an IP-XACT core (xilinx.com:user:spu_top) so it can be
# instantiated in a Versal block design.  AXI interfaces are inferred from the
# s_axi_* / m_axi_* port names of spu_top.
#
#   vivado -mode batch -source package_ip.tcl
#
set here      [file dirname [file normalize [info script]]]
set rtl_dir   [file normalize "$here/../../rtl"]
set ip_repo   [file normalize "$here/ip_repo"]
set part      "xcvh1582-vsva3697-2MP-e-S"
set tmp_proj  [file normalize "$here/build/ip_pkg"]

file mkdir $ip_repo
create_project -force spu_ip $tmp_proj -part $part
set_property target_language Verilog [current_project]
add_files -norecurse [glob $rtl_dir/*.sv]
# spu_pkg.sv must be compiled first
set_property library work [get_files *.sv]
update_compile_order -fileset sources_1
set_property top spu_top [current_fileset]

ipx::package_project -root_dir $ip_repo/spu_top -vendor xilinx.com -library user -taxonomy /UserIP -import_files -set_current true -force
set core [ipx::current_core]
set_property name        spu_top   $core
set_property display_name "SPU Symbolic Processing Unit" $core
set_property description "Vector-symbolic accelerator: hypervector datapath, accumulators, associative search, scalar control" $core
set_property version 1.0 $core
set_property core_revision 1 $core
set_property supported_families {versal Production} $core

# clock / reset association (interfaces themselves are auto-inferred)
catch { ipx::infer_bus_interfaces xilinx.com:interface:aximm_rtl:1.0 $core }
ipx::associate_bus_interfaces -busif s_axi -clock clk $core
ipx::associate_bus_interfaces -busif m_axi -clock clk $core
set_property value ACTIVE_LOW [ipx::get_bus_parameters POLARITY -of_objects [ipx::get_bus_interfaces rst_n -of_objects $core]]
# interrupt
ipx::add_bus_interface irq $core
set_property abstraction_type_vlnv xilinx.com:signal:interrupt_rtl:1.0 [ipx::get_bus_interfaces irq -of_objects $core]
set_property bus_type_vlnv xilinx.com:signal:interrupt:1.0 [ipx::get_bus_interfaces irq -of_objects $core]
set_property interface_mode master [ipx::get_bus_interfaces irq -of_objects $core]
ipx::add_port_map INTERRUPT [ipx::get_bus_interfaces irq -of_objects $core]
set_property physical_name irq [ipx::get_port_maps INTERRUPT -of_objects [ipx::get_bus_interfaces irq -of_objects $core]]
# register map: 256 KB window (18-bit address)
ipx::add_memory_map s_axi $core
set_property slave_memory_map_ref s_axi [ipx::get_bus_interfaces s_axi -of_objects $core]
set ab [ipx::add_address_block reg0 [ipx::get_memory_maps s_axi -of_objects $core]]
set_property range 262144 $ab
set_property width 32 $ab
# master address space: 64-bit
ipx::add_address_space m_axi $core
set_property master_address_space_ref m_axi [ipx::get_bus_interfaces m_axi -of_objects $core]
set_property width 512 [ipx::get_address_spaces m_axi -of_objects $core]
set_property range 18446744073709551616 [ipx::get_address_spaces m_axi -of_objects $core]   ;# 2^64 (VERIFY: GUI shows 16E)

ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::check_integrity $core
ipx::save_core $core
close_project -delete
puts "SPU IP packaged into $ip_repo"
