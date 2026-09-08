.PHONY: test cosim examples headers lint clean
test:            ## software tests (+ RTL cosim when verilator is available)
	sh scripts/run_tests.sh
cosim:           ## RTL co-simulation only
	cd sw && python3 -m pytest -q tests/test_cosim.py
examples:
	python3 sw/examples/01_analogy.py && python3 sw/examples/02_hdc_classify.py sim && python3 sw/examples/03_kb_rules.py sim
headers:         ## regenerate spu_pkg.sv / spu_regs.h from isa.py
	python3 scripts/gen_headers.py
lint:
	$(MAKE) -C hw/tb lint
clean:
	rm -rf hw/tb/obj_dir hw/fpga/vhk158/build sw/.pytest_cache; find . -name __pycache__ -exec rm -rf {} +
