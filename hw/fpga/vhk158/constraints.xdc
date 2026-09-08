# SPU on VHK158: all clocks come from CIPS/clk_wizard inside the block design, so
# no pin constraints are needed here.  Add board I/O constraints only if you
# bring SPU signals to pins (not required).
#
# The generated HV register file and accumulator memories are inferred RAM; if
# Vivado maps the 512-wide accumulator array to BRAM instead of URAM, force it:
# set_property RAM_STYLE ULTRA [get_cells -hier -filter {NAME =~ *spu_acc*mem*}]
