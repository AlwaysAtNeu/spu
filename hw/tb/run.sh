#!/bin/sh
# Build (if needed) and run the SPU testbench on a program/memory image.
# usage: ./run.sh prog.hex mem.hex [extra plusargs...]
set -e
cd "$(dirname "$0")"
make -s
PROG=$1; MEM=$2; shift 2
./obj_dir/tb_spu +prog="$PROG" +mem="$MEM" +mem_out=mem_out.hex "$@"
