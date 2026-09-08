"""Host runtime: run a compiled `Binary` on a device.

Devices
-------
SimDevice        Python golden model (always available)
VerilatorDevice  RTL simulation through spuc.cosim (needs verilator + built tb)
XdmaDevice       VHK158 over PCIe with the AMD XDMA/QDMA kernel driver
                 (user BAR via mmap, DMA via the MM char devices)
UioDevice        VHK158 driven from the on-chip Versal PS (Linux UIO + a
                 physically contiguous DMA buffer such as u-dma-buf / reserved memory)

All devices share the same tiny interface (write/read memory, load program, start,
wait, read CSR) so the same compiled Binary runs on all of them.
"""
import mmap
import os
import struct
import time
from .isa import (DEFAULT_CONFIG, HOST, CSR, NCSR, CTRL_START, CTRL_SOFT_RESET, CTRL_IRQ_EN,
                  STATUS_BUSY, STATUS_DONE, STATUS_ERR, SPU_ID)
from .sim import SPUSim, Memory


class DeviceError(Exception):
    pass


class Device:
    cfg = DEFAULT_CONFIG

    def write(self, offset, data):      raise NotImplementedError
    def read(self, offset, n):          raise NotImplementedError
    def load_program(self, words):      raise NotImplementedError
    def run(self, timeout=30.0):        raise NotImplementedError   # returns "halt" | "error(n)" | "timeout"
    def csr(self, name):                raise NotImplementedError
    def cycles(self):
        return self.csr("CYCLES_LO") | (self.csr("CYCLES_HI") << 32)
    def instret(self):
        return self.csr("INSTRET_LO") | (self.csr("INSTRET_HI") << 32)
    def close(self):
        pass

    # convenience
    def read_u32(self, offset):
        return struct.unpack("<I", self.read(offset, 4))[0]

    def read_u32s(self, offset, n):
        return list(struct.unpack("<%dI" % n, self.read(offset, 4 * n)))

    def write_u32s(self, offset, vals):
        self.write(offset, struct.pack("<%dI" % len(vals), *vals))


class SimDevice(Device):
    def __init__(self, cfg=DEFAULT_CONFIG, trace=False):
        self.cfg = cfg
        self.sim = SPUSim(cfg, trace=trace)

    def write(self, offset, data):
        self.sim.mem.write(offset, data)

    def read(self, offset, n):
        return self.sim.mem.read(offset, n)

    def load_program(self, words):
        self.sim.load_program(words)

    def run(self, timeout=30.0, max_instr=200_000_000):
        mem = self.sim.mem
        self.sim.reset()
        self.sim.mem = mem
        return self.sim.run(max_instr=max_instr)

    def csr(self, name):
        return self.sim.csr[CSR[name]]


class VerilatorDevice(Device):
    """Runs the whole program on the Verilator RTL model (batch: the image is written
    to a memory model, the simulation runs to HALT, results are read back).
    Extra keyword arguments go to spuc.cosim.run_rtl (timeout_cycles, seed,
    backpressure, max_lat, keep, workdir)."""

    def __init__(self, cfg=DEFAULT_CONFIG, timeout_cycles=500_000_000, **kw):
        from . import cosim
        self.cfg = cfg
        self.cosim = cosim
        self.kw = dict(kw, timeout_cycles=timeout_cycles)
        self.mem = Memory()
        self.words = []
        self.result = None

    def write(self, offset, data):
        self.mem.write(offset, data)

    def read(self, offset, n):
        return self.mem.read(offset, n)

    def load_program(self, words):
        self.words = list(words)

    def run(self, timeout=3600.0):
        r = self.cosim.run_rtl(self.words, self.mem, **self.kw)
        self.result = r
        self.mem = r.mem
        if r.timed_out:
            return "timeout"
        if r.err:
            return "error(%d)" % r.csrs[CSR["ERR"]]
        return "halt" if r.done else "unknown"

    def csr(self, name):
        return self.result.csrs[CSR[name]]

    def cycles(self):
        return self.result.cycles

    def instret(self):
        return self.result.instret


class _MmioBase(Device):
    """Common MMIO register access over an mmap'ed user BAR / UIO region."""

    def __init__(self, cfg, mm, mem_base=0):
        self.cfg = cfg
        self.mm = mm
        self.mem_base = mem_base
        if self.reg_read(HOST["ID"]) != SPU_ID:
            raise DeviceError("SPU ID mismatch at %s: 0x%08x" % (self, self.reg_read(HOST["ID"])))
        self.reg_write(HOST["CTRL"], CTRL_SOFT_RESET)
        self.reg_write(HOST["MEM_BASE_LO"], mem_base & 0xFFFFFFFF)
        self.reg_write(HOST["MEM_BASE_HI"], (mem_base >> 32) & 0xFFFFFFFF)

    def reg_read(self, off):
        return struct.unpack("<I", self.mm[off:off + 4])[0]

    def reg_write(self, off, v):
        self.mm[off:off + 4] = struct.pack("<I", v & 0xFFFFFFFF)

    def load_program(self, words):
        base = HOST["PROG_BASE"]
        for i, w in enumerate(words):
            self.reg_write(base + 8 * i, w & 0xFFFFFFFF)
            self.reg_write(base + 8 * i + 4, (w >> 32) & 0xFFFFFFFF)

    def run(self, timeout=30.0):
        self.reg_write(HOST["IRQ_ACK"], 1)
        self.reg_write(HOST["PC_START"], 0)
        self.reg_write(HOST["CTRL"], CTRL_START)
        t0 = time.time()
        while True:
            st = self.reg_read(HOST["STATUS"])
            if st & STATUS_DONE or st & STATUS_ERR:
                break
            if time.time() - t0 > timeout:
                return "timeout"
            time.sleep(0.0005)
        if st & STATUS_ERR:
            return "error(%d)" % self.csr("ERR")
        return "halt"

    def csr(self, name):
        return self.reg_read(HOST["CSR_BASE"] + 4 * CSR[name])


class XdmaDevice(_MmioBase):
    """PCIe host access through the AMD XDMA (or QDMA MM) character devices."""

    def __init__(self, cfg=DEFAULT_CONFIG, user="/dev/xdma0_user", h2c="/dev/xdma0_h2c_0",
                 c2h="/dev/xdma0_c2h_0", mem_base=0, bar_size=1 << 20):
        self.fd_user = os.open(user, os.O_RDWR | os.O_SYNC)
        self.fd_h2c = os.open(h2c, os.O_RDWR)
        self.fd_c2h = os.open(c2h, os.O_RDWR)
        mm = mmap.mmap(self.fd_user, bar_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        super().__init__(cfg, mm, mem_base)

    def write(self, offset, data):
        os.pwrite(self.fd_h2c, bytes(data), self.mem_base + offset)

    def read(self, offset, n):
        return os.pread(self.fd_c2h, n, self.mem_base + offset)

    def close(self):
        self.mm.close()
        for fd in (self.fd_user, self.fd_h2c, self.fd_c2h):
            os.close(fd)


class UioDevice(_MmioBase):
    """Versal PS (Linux) access: SPU registers through a UIO device, SPU memory
    through a physically contiguous buffer exposed as a char device (u-dma-buf)
    or /dev/mem at a reserved-memory physical address."""

    def __init__(self, cfg=DEFAULT_CONFIG, uio="/dev/uio0", reg_size=1 << 18, dma_dev="/dev/udmabuf0",
                 dma_phys=None, dma_size=64 << 20):
        self.fd_uio = os.open(uio, os.O_RDWR | os.O_SYNC)
        mm = mmap.mmap(self.fd_uio, reg_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
        if dma_phys is None:
            with open("/sys/class/u-dma-buf/%s/phys_addr" % os.path.basename(dma_dev)) as f:
                dma_phys = int(f.read().strip(), 16)
        self.fd_dma = os.open(dma_dev, os.O_RDWR | os.O_SYNC)
        self.dma = mmap.mmap(self.fd_dma, dma_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.dma_size = dma_size
        super().__init__(cfg, mm, mem_base=dma_phys)

    def write(self, offset, data):
        self.dma[offset:offset + len(data)] = bytes(data)

    def read(self, offset, n):
        return bytes(self.dma[offset:offset + n])

    def close(self):
        self.dma.close()
        self.mm.close()
        os.close(self.fd_dma)
        os.close(self.fd_uio)


def get_device(name="sim", **kw):
    name = name.lower()
    if name == "sim":
        return SimDevice(**kw)
    if name in ("rtl", "verilator"):
        return VerilatorDevice(**kw)
    if name == "xdma":
        return XdmaDevice(**kw)
    if name == "uio":
        return UioDevice(**kw)
    raise DeviceError("unknown device %s" % name)


class Session:
    """Load a Binary on a device, feed inputs, run, collect outputs."""

    def __init__(self, device, binary):
        self.dev, self.bin = device, binary
        self.dev.load_program(binary.words)
        for off, data in binary.image():
            self.dev.write(off, data)
        self.status = None
        self.wall = 0.0

    def set_input(self, name, data):
        b = self.bin.buffers[name]
        data = bytes(data)
        if len(data) > b.size:
            raise DeviceError("input %s too large (%d > %d)" % (name, len(data), b.size))
        self.dev.write(b.offset, data)

    def run(self, timeout=600.0):
        t0 = time.time()
        self.status = self.dev.run(timeout=timeout)
        self.wall = time.time() - t0
        if self.status != "halt":
            raise DeviceError("SPU run finished with status %s" % self.status)
        return self

    def get_output(self, name, n=None):
        b = self.bin.buffers[name]
        return self.dev.read(b.offset, b.size if n is None else n)

    def get_u32s(self, name, n=None):
        b = self.bin.buffers[name]
        n = b.size // 4 if n is None else n
        return self.dev.read_u32s(b.offset, n)

    def report(self, clock_hz=250e6):
        cyc = self.dev.cycles()
        return {"status": self.status, "cycles": cyc, "instret": self.dev.instret(),
                "est_time_at_%dMHz_ms" % int(clock_hz / 1e6): 1e3 * cyc / clock_hz,
                "host_wall_s": self.wall}
