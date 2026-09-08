# SPU ISA 参考（v1）

单一事实来源：`sw/spuc/isa.py`（`scripts/gen_headers.py` 由它生成 `hw/rtl/spu_pkg.sv` 与 `hw/host/spu_regs.h`）。

## 机器模型

| 资源 | 说明 |
|------|------|
| `h0..h31` | 32 个超向量寄存器，每个 D=8192 bit（可配置，须为 W 的 2 的幂倍数） |
| `s0..s31` | 32 个 32 bit 标量寄存器，`s0` 恒为 0 |
| `acc[0..15]` | 16 个捆绑累加器，每个 D 个 16 bit 有符号饱和计数器 |
| CSR | 32 个控制/状态寄存器（见下） |
| 程序存储 | 4096 条 64 bit 指令（片内 BRAM，由主机经 AXI-Lite 写入） |
| 数据存储 | 字节寻址、小端；SPU 使用 32 bit 偏移，硬件加上 64 bit `MEM_BASE` 后访问 AXI 内存（HBM/DDR） |

超向量在内存中为 D/8 字节小端整数（bit i 在第 i/8 字节的第 i%8 位）；HV 访问忽略地址低 6 位（64 B 对齐），`LW/SW` 忽略低 2 位。

## 指令编码（64 bit）

```
63:58 op(6) | 57:52 fn(6) | 51:47 rd(5) | 46:42 ra(5) | 41:37 rb(5) | 36:32 rc(5) | 31:0 imm(32)
```

许多指令用 **`s[rc] + imm`** 构成一个值（`rc = s0` 时即立即数），用 **`s[ra] + imm`** 构成地址。

## 指令列表

### 标量 / 控制

| 助记符 | 语义 |
|--------|------|
| `NOP` / `HALT` | 空操作 / 停机（置 DONE，可触发中断） |
| `ADD SUB AND OR XOR SHL SHR SRA MUL SLT SLTU sd, sa, sb` | `sd = sa op sb`（SOP，fn 选功能；移位量取低 5 位） |
| `ADDI SUBI ... SLTIU sd, sa, imm` | 立即数形式（SOPI）；`LI sd, imm`、`MV sd, sa` 为别名 |
| `LW sd, off(sa)` / `LB` | 32 bit 字 / 零扩展字节加载 |
| `SW sb, off(sa)` / `SB` | 存储 |
| `BEQ BNE BLT BGE BLTU BGEU sa, sb, target` | 条件跳转（imm 为相对指令数，汇编器接受绝对标号） |
| `JAL sd, target` / `J target` | `sd = pc+1; pc += imm` |
| `JALR sd, sa, imm` | `sd = pc+1; pc = sa + imm` |
| `CSRR sd, CSR` / `CSRW CSR, sa` | CSR 读写 |

### 超向量

| 助记符 | 语义 |
|--------|------|
| `HXOR/HAND/HOR hd, ha, hb` | 逐位运算（XOR = 绑定 / 解绑） |
| `HNOT hd, ha` | 取反；`HMOV` 为 `HOR hd, ha, ha` 别名 |
| `HROT hd, ha, [sc,] imm` | 循环左移 `(s[rc]+imm) mod D`（置换 ρ） |
| `HGEN hd, [sc,] imm` | 由 32 bit 种子 `(s[rc]+imm)` 确定性生成伪随机 HV（符号/项目记忆） |
| `HMASK hd, [sc,] imm` | 低 n 位为 1（n 截断到 [0,D]），用于等级编码、加噪等 |
| `HLD hd, off(sa)` / `HST hd, off(sa)` | HV 加载 / 存储（16 拍 512 bit AXI 突发） |
| `HDIST sd, ha, hb` | 汉明距离 |
| `HSEARCH[.WD] sd, ha, sb, sc` | 关联搜索：对 `mem[s[sb] + i*HV_BYTES]`, `i < s[sc]` 计算与 `ha` 的汉明距离；`sd`=最近索引；CSR `SR_IDX/SR_DIST/SR_IDX2/SR_DIST2`；`.WD` 时把每个距离写为 u32 到 `csr[DOUT] + 4i`。严格小于才更新，故相同距离取最小索引；count=0 时 idx=0xFFFFFFFF, dist=0xFFFF |

### 累加器（捆绑 / 训练）

| 助记符 | 语义 |
|--------|------|
| `ACC.CLR k` | `acc[k] = 0` |
| `ACC.ADD k, ha, [sc,] imm` | `acc[k][i] += bit_i(ha) ? +w : -w`，w = `(s[rc]+imm)` 的低 16 bit（有符号），饱和 |
| `ACC.SUB k, ha, [sc,] imm` | 同上取负（用于再训练 / 误分类修正） |
| `ACC.THR hd, k, [sc,] imm` | `hd[i] = acc[k][i] > t`（t 有符号 16 bit；相等取 0，偶数捆绑由编译器追加 tie-break 符号） |
| `ACC.LD k, off(sa)` / `ACC.ST` | 原始 int16 小端读写（保存 / 续训练模型） |

HGEN 生成规则（主机、模拟器、RTL 三者比特一致）：第 w 个 64 bit 字 = `smix64(((seed<<32)|w) ^ 0x9E3779B97F4A7C15)`，`smix64` 为 splitmix64 终结函数。

## CSR

| 索引 | 名称 | 说明 |
|------|------|------|
| 0 | ID | 0x53505531 "SPU1" |
| 1 | DOUT | HSEARCH.WD 距离输出地址 |
| 2-5 | SR_IDX / SR_DIST / SR_IDX2 / SR_DIST2 | 最近一次搜索结果 |
| 6-9 | CYCLES / INSTRET (lo/hi) | 性能计数 |
| 10-11 | CFG_D / CFG_NACC | 配置 |
| 12 | ERR | 0 无错，1 非法指令，2 累加器索引越界，3 PC 越界，4 AXI 错误 |
| 13-16 | SCRATCH0-3 | 主机 ↔ SPU 信箱 |

## 主机寄存器映射（AXI4-Lite，18 bit 地址）

| 偏移 | 寄存器 |
|------|--------|
| 0x000 | CTRL：bit0 START，bit1 SOFT_RESET，bit2 IRQ_EN |
| 0x004 | STATUS：bit0 BUSY，bit1 DONE，bit2 ERR，bit3 IRQ |
| 0x008 / 0x00C | PC_START / PC |
| 0x010 / 0x014 | MEM_BASE_LO / HI |
| 0x018 | IRQ_ACK（写 1 清 DONE/IRQ） |
| 0x020 / 0x024 / 0x028 / 0x02C | ID / VERSION / CFG0 / CFG1 |
| 0x100 + 4i | CSR i |
| 0x200 + 4i | 标量寄存器 i（只读调试） |
| 0x20000 + 8i | 程序存储第 i 条指令（低字在前） |

## 汇编示例

```
        LI      s1, 0x10000          ; 原型表基址
        LI      s2, 10               ; 10 个原型
        HGEN    h1, 0x1000           ; 符号 A
        HGEN    h2, 0x1001           ; 符号 B
        HXOR    h3, h1, h2           ; A*B
        ACC.CLR 0
        ACC.ADD 0, h1, 1
        ACC.ADD 0, h2, 1
        ACC.ADD 0, h3, 1
        ACC.THR h4, 0, 0             ; maj(A, B, A*B)
        HSEARCH s3, h4, s1, s2       ; 最近原型
        CSRR    s4, SR_DIST
        HALT
```

`python3 -m spuc asm prog.s -o prog.hex`、`python3 -m spuc dis prog.hex`、`python3 -m spuc run prog.s --trace`。
