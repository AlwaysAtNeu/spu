// Deterministic pseudo random hypervector generator.
// word w of the HV = smix64( ((seed32 << 32) | w) ^ GEN_GOLDEN ), smix64 = splitmix64 finaliser.
// LANES words are produced per cycle and packed into W-bit chunks.  Each 64x64 multiply
// by a constant is split into three 32x32 partial products (only the low 64 bits are needed)
// and registered, so every pipeline stage is a single 32x32 multiply or an adder.
module spu_gen import spu_pkg::*; #(
  parameter int LANES = 8
) (
  input  logic            clk,
  input  logic            rst_n,
  input  logic            start,
  input  logic [31:0]     seed,
  output logic            out_valid,
  output logic [W-1:0]    out_data,
  output logic [CH_W-1:0] out_chunk,
  output logic            done
);
  localparam int NW    = D / 64;              // words per HV
  localparam int WPC   = W / 64;              // words per chunk
  localparam int SUBS  = WPC / LANES;         // cycles per chunk
  localparam int NCYC  = NW / LANES;          // issue cycles per HV
  localparam int CNTW  = $clog2(NCYC + 1);
  localparam int STAGES = 8;

  // ---------------- issue ----------------
  logic            active;
  logic [CNTW-1:0] issue_cnt;
  logic [31:0]     seed_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      active    <= 1'b0;
      issue_cnt <= '0;
      seed_q    <= '0;
    end else begin
      if (start) begin
        active    <= 1'b1;
        issue_cnt <= '0;
        seed_q    <= seed;
      end else if (active) begin
        if (issue_cnt == CNTW'(NCYC - 1)) active <= 1'b0;
        issue_cnt <= issue_cnt + 1'b1;
      end
    end
  end

  // ---------------- pipeline ----------------
  // stage 0 : z0 = ((seed<<32)|w) ^ GOLDEN ; z0 ^= z0 >> 30
  // stage 1 : partial products of z0 * MUL1
  // stage 2 : z1 = sum
  // stage 3 : z1 ^= z1 >> 27
  // stage 4 : partial products of z1 * MUL2
  // stage 5 : z2 = sum
  // stage 6 : out = z2 ^ (z2 >> 31)
  // stage 7 : pack
  logic [STAGES-1:0] v;
  logic [CNTW-1:0]   cnt_pipe [STAGES];
  logic [LANES-1:0][63:0] a0, a1, a2, a3, a4, a5, a6;
  logic [LANES-1:0][63:0] p0_1, p0_2;
  logic [LANES-1:0][31:0] p1_1, p2_1, p1_2, p2_2;

  logic issue;
  assign issue = active;

  always_ff @(posedge clk) begin
    v <= {v[STAGES-2:0], issue};
    cnt_pipe[0] <= issue_cnt;
    for (int s = 1; s < STAGES; s++) cnt_pipe[s] <= cnt_pipe[s-1];
    for (int l = 0; l < LANES; l++) begin
      logic [63:0] z0, z1, z2, z3;
      logic [31:0] w;
      w  = 32'(issue_cnt) * 32'(LANES) + 32'(l);
      z0 = ({seed_q, w}) ^ GEN_GOLDEN;
      a0[l] <= z0 ^ (z0 >> 30);
      // multiply by MUL1 (mod 2^64)
      p0_1[l] <= {32'b0, a0[l][31:0]} * {32'b0, GEN_MUL1[31:0]};
      p1_1[l] <= a0[l][63:32] * GEN_MUL1[31:0];
      p2_1[l] <= a0[l][31:0]  * GEN_MUL1[63:32];
      a1[l] <= p0_1[l] + {p1_1[l] + p2_1[l], 32'b0};
      z1 = a1[l];
      a2[l] <= z1 ^ (z1 >> 27);
      // multiply by MUL2 (mod 2^64)
      p0_2[l] <= {32'b0, a2[l][31:0]} * {32'b0, GEN_MUL2[31:0]};
      p1_2[l] <= a2[l][63:32] * GEN_MUL2[31:0];
      p2_2[l] <= a2[l][31:0]  * GEN_MUL2[63:32];
      a3[l] <= p0_2[l] + {p1_2[l] + p2_2[l], 32'b0};
      z2 = a3[l];
      a4[l] <= z2 ^ (z2 >> 31);
      z3 = a4[l];
      a5[l] <= z3;
      a6[l] <= a5[l];
    end
  end

  // ---------------- pack into chunks ----------------
  // a4 is the finished word set at stage index 6 (v[6]); use v[6]/cnt_pipe[6].
  logic [CNTW-1:0]  cnt5;
  logic             v5;
  assign v5   = v[6];
  assign cnt5 = cnt_pipe[6];

  generate
    if (SUBS == 1) begin : g_direct
      always_ff @(posedge clk) begin
        out_valid <= v5;
        out_data  <= a4;
        out_chunk <= CH_W'(cnt5);
      end
    end else begin : g_pack
      logic [W-1:0] pack;
      always_ff @(posedge clk) begin
        out_valid <= 1'b0;
        if (v5) begin
          pack[(cnt5 % SUBS) * LANES * 64 +: LANES * 64] <= a4;
          if ((cnt5 % SUBS) == CNTW'(SUBS - 1)) begin
            out_valid <= 1'b1;
            out_data  <= {a4, pack[(SUBS-1)*LANES*64-1:0]};
            out_chunk <= CH_W'(cnt5 / SUBS);
          end
        end
      end
    end
  endgenerate

  // done pulses one cycle after the last chunk has been presented
  logic last_chunk_q;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      last_chunk_q <= 1'b0;
      done <= 1'b0;
    end else begin
      last_chunk_q <= v5 && (cnt5 == CNTW'(NCYC - 1));
      done <= last_chunk_q;
    end
  end

  // unused pipeline stages kept for latency documentation
  logic unused;
  assign unused = ^{a6, v[STAGES-1:7], v[5:0], cnt_pipe[7], cnt_pipe[0], cnt_pipe[1], cnt_pipe[2], cnt_pipe[3], cnt_pipe[4], cnt_pipe[5]};
endmodule
