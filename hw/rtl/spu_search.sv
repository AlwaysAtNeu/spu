// Streaming associative search: Hamming distance of a query HV against `count`
// candidates stored back to back in memory; keeps best and second best (strict <,
// earliest index wins) and optionally writes every distance as a packed 32-bit word.
module spu_search import spu_pkg::*; #(
  parameter int MAX_INFLIGHT = 4
) (
  input  logic            clk,
  input  logic            rst_n,
  input  logic            start,
  input  logic [31:0]     base,
  input  logic [31:0]     count,
  input  logic            wrdist,
  input  logic [31:0]     dout,
  input  logic [D-1:0]    query,
  // DMA read
  output logic            rq_valid,
  input  logic            rq_ready,
  output logic [31:0]     rq_addr,
  output logic [15:0]     rq_beats,
  input  logic            rd_valid,
  input  logic [W-1:0]    rd_data,
  // DMA write (distances)
  output logic            wq_valid,
  input  logic            wq_ready,
  output logic [31:0]     wq_addr,
  output logic [15:0]     wq_beats,
  output logic            wd_valid,
  input  logic            wd_ready,
  output logic [W-1:0]    wd_data,
  output logic [W/8-1:0]  wd_strb,
  output logic            done,
  output logic [31:0]     best_idx,
  output logic [31:0]     best_dist,
  output logic [31:0]     best2_idx,
  output logic [31:0]     best2_dist
);
  localparam int PCW    = $clog2(W + 1);   // popcount width
  localparam int DW     = $clog2(D + 1);   // distance width
  localparam int LANES  = W / 32;          // distances per beat (16)
  localparam int LW     = $clog2(LANES);
  localparam int FIFO_D = 16;
  localparam int PIPE   = 5;               // xor register + 4 popcount stages
  localparam int BS     = $clog2(W / 8);   // beat address shift

  logic        busy;
  logic [31:0] base_q, count_q, dout_q;
  logic        wrdist_q;
  logic [31:0] k_issue, k_done;
  logic [CH_W-1:0] bc;                     // beat within candidate

  // ---------------- issue ----------------
  logic [$clog2(FIFO_D+1)-1:0] fifo_cnt;
  logic fifo_room;
  assign fifo_room = (fifo_cnt < ($clog2(FIFO_D+1))'(FIFO_D - MAX_INFLIGHT - 1));
  assign rq_valid  = busy && (k_issue < count_q) && ((k_issue - k_done) < 32'(MAX_INFLIGHT)) && fifo_room;
  assign rq_addr   = base_q + (k_issue << $clog2(HV_BYTES));
  assign rq_beats  = 16'(C);

  // ---------------- distance pipeline ----------------
  logic [W-1:0]   xr;
  logic [PIPE-1:0] pv, plast, pfirst;
  logic [PCW-1:0] pc;
  logic [DW-1:0]  acc;
  logic           dist_valid;
  logic [DW-1:0]  dist_r;

  spu_popcount #(.N(W)) u_pc (.clk(clk), .din(xr), .dout(pc));

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      pv <= '0; plast <= '0; pfirst <= '0; bc <= '0; dist_valid <= 1'b0;
    end else begin
      xr     <= rd_data ^ query[bc * W +: W];
      pv     <= {pv[PIPE-2:0],     busy && rd_valid};
      plast  <= {plast[PIPE-2:0],  bc == CH_W'(C - 1)};
      pfirst <= {pfirst[PIPE-2:0], bc == '0};
      if (busy && rd_valid) bc <= bc + 1'b1;
      if (start) bc <= '0;
      dist_valid <= 1'b0;
      if (pv[PIPE-1]) begin
        if (pfirst[PIPE-1]) acc <= DW'(pc);
        else                acc <= acc + DW'(pc);
        if (plast[PIPE-1]) begin
          dist_valid <= 1'b1;
          dist_r       <= (pfirst[PIPE-1] ? DW'(0) : acc) + DW'(pc);
        end
      end
    end
  end

  // ---------------- argmin + distance FIFO ----------------
  logic [15:0] fifo [FIFO_D];
  logic [$clog2(FIFO_D)-1:0] f_wp, f_rp;
  logic f_push, f_pop;
  assign f_push = dist_valid && wrdist_q;

  // packer
  typedef enum logic [1:0] {PK_IDLE, PK_REQ, PK_DATA} pk_t;
  pk_t pk;
  logic [31:0]      kp;                 // next distance index to pack
  logic [W-1:0]     dbuf;
  logic [W/8-1:0]   dstrb;
  logic [LW-1:0]    lane;
  assign lane   = LW'((dout_q >> 2) + kp);
  assign f_pop  = (pk == PK_IDLE) && (fifo_cnt != '0);
  assign wq_valid = (pk == PK_REQ);
  assign wq_beats = 16'd1;
  assign wd_valid = (pk == PK_DATA);
  assign wd_data  = dbuf;
  assign wd_strb  = dstrb;

  logic [31:0] dline;
  assign dline = dout_q + (kp << 2);
  logic unused_dline;
  assign unused_dline = ^dline[BS-1:0];
  logic pk_idle_all;
  assign pk_idle_all = (pk == PK_IDLE) && (fifo_cnt == '0);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      busy <= 1'b0; done <= 1'b0; k_issue <= '0; k_done <= '0;
      best_idx <= 32'hFFFFFFFF; best_dist <= 32'hFFFF; best2_idx <= 32'hFFFFFFFF; best2_dist <= 32'hFFFF;
      fifo_cnt <= '0; f_wp <= '0; f_rp <= '0; pk <= PK_IDLE; kp <= '0; dbuf <= '0; dstrb <= '0;
      base_q <= '0; count_q <= '0; dout_q <= '0; wrdist_q <= 1'b0; wq_addr <= '0;
    end else begin
      done <= 1'b0;
      if (start) begin
        busy <= 1'b1; base_q <= base; count_q <= count; dout_q <= dout; wrdist_q <= wrdist;
        k_issue <= '0; k_done <= '0; kp <= '0; dbuf <= '0; dstrb <= '0;
        best_idx <= 32'hFFFFFFFF; best_dist <= 32'hFFFF; best2_idx <= 32'hFFFFFFFF; best2_dist <= 32'hFFFF;
      end
      if (rq_valid && rq_ready) k_issue <= k_issue + 32'd1;
      if (dist_valid) begin
        if (32'(dist_r) < best_dist) begin
          best2_dist <= best_dist; best2_idx <= best_idx;
          best_dist  <= 32'(dist_r);  best_idx  <= k_done;
        end else if (32'(dist_r) < best2_dist) begin
          best2_dist <= 32'(dist_r);  best2_idx <= k_done;
        end
        k_done <= k_done + 32'd1;
      end
      // fifo
      if (f_push) begin
        fifo[f_wp] <= 16'(dist_r);
        f_wp <= f_wp + 1'b1;
      end
      if (f_pop) f_rp <= f_rp + 1'b1;
      fifo_cnt <= fifo_cnt + ($clog2(FIFO_D+1))'(f_push) - ($clog2(FIFO_D+1))'(f_pop);
      // packer
      case (pk)
        PK_IDLE: if (f_pop) begin
          dbuf[lane * 32 +: 32]  <= {16'b0, fifo[f_rp]};
          dstrb[lane * 4 +: 4]   <= 4'hF;
          kp <= kp + 32'd1;
          if ((lane == LW'(LANES - 1)) || (kp + 32'd1 == count_q)) begin
            wq_addr <= {dline[31:BS], {BS{1'b0}}};
            pk <= PK_REQ;
          end
        end
        PK_REQ: if (wq_ready) pk <= PK_DATA;
        PK_DATA: if (wd_ready) begin
          pk    <= PK_IDLE;
          dbuf  <= '0;
          dstrb <= '0;
        end
        default: pk <= PK_IDLE;
      endcase
      // completion
      if (busy && (k_done == count_q) && pk_idle_all && !dist_valid && (pv == '0)) begin
        busy <= 1'b0;
        done <= 1'b1;
      end
    end
  end
endmodule
