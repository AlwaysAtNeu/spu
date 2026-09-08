# Build the Verilator model of the SPU testbench:  make        (binary in obj_dir/tb_spu)
# Run a program:                                    make run PROG=prog.hex MEM=mem.hex
VERILATOR ?= verilator
RTL_DIR   := ../rtl
RTL       := $(addprefix $(RTL_DIR)/,$(shell cat $(RTL_DIR)/files.f))
TB        := axi_mem_model.sv tb_spu.sv
VFLAGS    := --binary --timing -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT -Wno-INITIALDLY --top-module tb_spu -j 0 \
             -O2 --x-assign fast --x-initial fast --trace verilator.vlt
BIN       := obj_dir/tb_spu

all: $(BIN)

$(BIN): $(RTL) $(TB) verilator.vlt Makefile
	$(VERILATOR) $(VFLAGS) $(RTL) $(TB) -o tb_spu

lint:
	$(VERILATOR) --lint-only -Wall verilator.vlt $(RTL) --top-module spu_top

run: $(BIN)
	./$(BIN) +prog=$(PROG) +mem=$(MEM) +mem_out=mem_out.hex

clean:
	rm -rf obj_dir

.PHONY: all lint run clean
