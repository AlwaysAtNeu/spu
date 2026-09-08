"""Command line front door.

  python3 -m spuc asm  prog.s  [-o prog.hex]    assemble text -> hex
  python3 -m spuc dis  prog.hex                 disassemble
  python3 -m spuc run  prog.s|prog.hex [--trace] run on the simulator and dump registers
  python3 -m spuc pkg                           print spu_pkg.sv
"""
import sys
from .asm import assemble, disassemble, to_hex
from .isa import emit_sv_package


def _load_words(path):
    if path.endswith(".hex"):
        return [int(l, 16) for l in open(path).read().split() if l.strip()]
    return assemble(open(path).read())


def main(argv):
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "pkg":
        sys.stdout.write(emit_sv_package())
    elif cmd == "asm":
        words = _load_words(argv[1])
        out = argv[argv.index("-o") + 1] if "-o" in argv else None
        if out:
            open(out, "w").write(to_hex(words))
        else:
            sys.stdout.write(to_hex(words))
    elif cmd == "dis":
        print(disassemble(_load_words(argv[1])))
    elif cmd == "run":
        from .sim import SPUSim
        sim = SPUSim(trace="--trace" in argv)
        sim.load_program(_load_words(argv[1]))
        st = sim.run()
        print("status:", st, "pc:", sim.pc, "cycles:", sim.cycles, "instret:", sim.instret)
        for i in range(0, 32, 8):
            print("  " + "  ".join("s%-2d=%08x" % (k, sim.s[k]) for k in range(i, i + 8)))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
