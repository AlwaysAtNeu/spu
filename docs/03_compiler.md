# 编译器（`sw/spuc`）

```
 符号模型前端                DSL / 程序构建               链接 / 输出
 ─────────────              ───────────────             ─────────────
 frontends/hdc.py   ──▶  dsl.Program: 符号表、缓冲区、  ──▶  Binary: 指令字 + 数据镜像 +
 frontends/logic.py      作用域寄存器分配、结构化控制流、       manifest(缓冲区偏移 / 符号种子)
 (自定义 Python DSL)      高层算子降级 (bundle/cleanup/...)        │
                                                                 ▼
                                             runtime.Session → SimDevice / VerilatorDevice / XdmaDevice / UioDevice
```

## 分层

1. **前端**：把一个符号模型（HDC 分类器、知识库 + 规则）翻译成 DSL 调用。前端同时提供 **比特精确的主机参考实现**（`train_ref`、`evaluate_ref` 等），测试用它验证设备结果。
2. **DSL（`dsl.py`）**：
   * 符号：`symbol()`、`symbol_family(n)`（连续种子，支持设备端 `HGEN(s+base)` 循环）、`tie_break()`。
   * 值：`hv()` / `s()` 寄存器句柄；标量表达式（`i * 3 + j`, `<<`, `&`）自动降级为 SOP/SOPI 序列。
   * 内存：`buffer(name, size, kind=input|output|table|internal, init=...)`；地址在链接时确定，指令里的引用通过 fixup 修补；`addr(buf, index, stride)` 生成地址计算。
   * HV 算子：`gen bind bind_all permute mask load store load_idx store_idx dist search cleanup bundle acc_*`。
   * 控制流：`loop(n)`（含 `break_/continue_`）、`while_`、`if_`、`if_else`；标号 / 相对跳转由链接器回填。
   * 寄存器分配：**作用域式**（`with p.scope():` 结束时释放），确定、无溢出；超出 31 标量 / 32 HV 会报错，这是前端要遵守的约束。句柄在其作用域释放后再被使用会在编译期报错（use-after-free 检测）。
3. **链接（`Program.compile`）**：数据段布局（64 B 对齐，`DATA_BASE=0x10000`）、fixup 回填、编码为 64 bit 指令字、生成 `Binary`（`words`、`image()`、`manifest()`、`disassembly()`）。
4. **运行时（`runtime.py`）**：`Session(device, binary)` 写入程序与初始镜像，`set_input` / `run` / `get_output`，`report()` 返回周期数与估计时间。所有设备共享同一接口，因此同一 `Binary` 在模拟器、RTL 仿真和 FPGA 上运行的结果应完全一致。

## 一个最小例子

```python
from spuc.dsl import Program
from spuc.runtime import SimDevice, Session

p = Program()
a, b = p.symbol("a"), p.symbol("b")
items = p.symbol_table("ITEMS", [a, b] + [p.symbol("x%d" % i) for i in range(30)])
out = p.buffer("OUT", 64, kind="output")
with p.scope():
    ab = p.bind(p.gen(a), p.gen(b))        # 绑定
    noisy = p.bind(ab, p.gen(b))           # 解绑 -> a
    idx = p.cleanup(noisy, items, 32)      # 关联记忆
    p.sw(idx, out, 0)
binary = p.compile()
s = Session(SimDevice(), binary).run()
print(s.get_u32s("OUT", 1))                # [0]
```

## 汇编器

`asm.py` 提供文本汇编 / 反汇编（`python3 -m spuc asm|dis|run`），语法见 `docs/02_isa.md`。反汇编输出可重新汇编（测试 `test_disassemble_roundtrip`）。

## 与硬件的一致性

`isa.py` 是唯一事实来源：`scripts/gen_headers.py` 生成 `spu_pkg.sv` 和 C 头文件。`sim.py` 是 RTL 的黄金模型；`cosim.py` / `tests/test_cosim.py` 在 Verilator 上跑同一批程序并逐字节比较内存、寄存器、CSR。
