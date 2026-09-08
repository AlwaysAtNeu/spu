// Bundling accumulators: NACC accumulators x D lanes of ACC_BITS-bit signed saturating counters.
// ACC_LANES lanes are processed per cycle; a row of the accumulator memory holds one step.
//   ADD/SUB : acc[k][i] += (hv[i] ? +w : -w)   (SUB negates w)
//   THR     : bits[i] = acc[k][i] > t           (signed compare)
//   CLR     : acc[k] = 0
//   LD/ST   : raw little-endian int16 image, ACC_BEATS beats of W bits
module spu_acc import spu_pkg::*; (
  input  logic                    clk,
  input  logic                    rst_n,
  input  logic                    start,
  input  logic [5:0]              fn,
  input  logic [$clog2(NACC)-1:0] k,
  input  logic signed [15:0]      val,
  input  logic [D-1:0]            hv_in,
  output logic                    thr_valid,
  output logic [ACC_LANES-1:0]    thr_bits,
  output logic [STEP_W-1:0]       thr_step,
  input  logic                    ld_valid,
  input  logic [W-1:0]            ld_data,
  output logic                    st_valid,
  output logic [W-1:0]            st_data,
  input  logic                    st_ready,
  output logic                    done
);
  localparam int ROWW  = ACC_LANES * ACC_BITS;
  localparam int ROWS  = NACC * ACC_STEPS;
  localparam int AW    = $clog2(ROWS);
  localparam int BPR   = ROWW / W;               // beats per row (LD/ST)
  localparam int BPRW  = $clog2(BPR);
  initial if (BPR < 2) $error("spu_acc: ACC_LANES*ACC_BITS must be at least 2*W");

  // Wide, shallow memory: Vivado maps it to BRAM/URAM (2 Mbit for the default config).
  logic [ROWW-1:0] mem [0:ROWS-1];
  logic            rd_en;
  logic [AW-1:0]   rd_addr;
  logic [ROWW-1:0] rd_q1, rd_q2;
  logic            we;
  logic [AW-1:0]   w_addr;
  logic [ROWW-1:0] w_data;

  always_ff @(posedge clk) begin
    if (we) mem[w_addr] <= w_data;
    if (rd_en) rd_q1 <= mem[rd_addr];
    rd_q2 <= rd_q1;
  end

  typedef enum logic [2:0] {S_IDLE, S_RUN, S_DRAIN, S_LD, S_ST_READ, S_ST_WAIT, S_ST_OUT, S_DONE} state_t;
  state_t state;

  logic [5:0]                 fn_q;
  logic [$clog2(NACC)-1:0]    k_q;
  logic signed [15:0]         val_q;
  logic [STEP_W-1:0]          step;         // issue step
  // read pipeline tags (2 cycle memory latency + 1 compute)
  logic [2:0]                 pv;           // valid per stage
  logic [STEP_W-1:0]          pstep [3];
  logic [ACC_LANES-1:0]       pbits [3];
  // LD assembly
  logic [BPRW-1:0]            ld_beat;
  logic [STEP_W-1:0]          ld_row;
  logic [ROWW-W-1:0]          ld_shift;
  // ST
  logic [STEP_W-1:0]          st_row;
  logic [BPRW-1:0]            st_beat;
  logic [1:0]                 st_wait;
  logic [ROWW-1:0]            st_rowdata;

  logic [ACC_LANES-1:0] lanes_now;
  assign lanes_now = hv_in[step * ACC_LANES +: ACC_LANES];

  // ---------------- lane arithmetic ----------------
  function automatic logic [ROWW-1:0] add_row(input logic [ROWW-1:0] row, input logic [ACC_LANES-1:0] bits,
                                              input logic signed [15:0] wv, input logic sub);
    logic signed [17:0] a, d, r, w;
    w = sub ? -18'(wv) : 18'(wv);          // exact negation (no 16-bit wrap for -32768)
    for (int l = 0; l < ACC_LANES; l++) begin
      a = 18'(signed'(row[l*16 +: 16]));
      d = bits[l] ? w : -w;
      r = a + d;
      if (r > 18'sd32767)       add_row[l*16 +: 16] = 16'h7FFF;
      else if (r < -18'sd32768) add_row[l*16 +: 16] = 16'h8000;
      else                      add_row[l*16 +: 16] = r[15:0];
    end
  endfunction

  function automatic logic [ACC_LANES-1:0] thr_row(input logic [ROWW-1:0] row, input logic signed [15:0] t);
    for (int l = 0; l < ACC_LANES; l++) thr_row[l] = signed'(row[l*16 +: 16]) > t;
  endfunction


  // ---------------- control ----------------
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state     <= S_IDLE;
      done      <= 1'b0;
      pv        <= '0;
      we        <= 1'b0;
      rd_en     <= 1'b0;
      thr_valid <= 1'b0;
      st_valid  <= 1'b0;
      step      <= '0;
      fn_q      <= '0;
      k_q       <= '0;
      val_q     <= '0;
      ld_beat   <= '0;
      ld_row    <= '0;
      st_row    <= '0;
      st_beat   <= '0;
      st_wait   <= '0;
    end else begin
      done      <= 1'b0;
      we        <= 1'b0;
      rd_en     <= 1'b0;
      thr_valid <= 1'b0;
      pv        <= {pv[1:0], 1'b0};
      pstep[1]  <= pstep[0];
      pstep[2]  <= pstep[1];
      pbits[1]  <= pbits[0];
      pbits[2]  <= pbits[1];
      case (state)
        S_IDLE: begin
          if (start) begin
            fn_q    <= fn;
            k_q     <= k;
            val_q   <= val;
            step    <= '0;
            ld_beat <= '0;
            ld_row  <= '0;
            st_row  <= '0;
            st_beat <= '0;
            case (fn)
              ACC_CLR, ACC_ADD, ACC_SUB, ACC_THR: state <= S_RUN;
              ACC_LD: state <= S_LD;
              ACC_ST: state <= S_ST_READ;
              default: state <= S_DONE;
            endcase
          end
        end
        S_RUN: begin
          // issue one step per cycle
          if (fn_q == ACC_CLR) begin
            we     <= 1'b1;
            w_addr <= AW'({k_q, step});
            w_data <= '0;
          end else begin
            rd_en    <= 1'b1;
            rd_addr  <= AW'({k_q, step});
            pv[0]    <= 1'b1;
            pstep[0] <= step;
            pbits[0] <= lanes_now;
          end
          step <= step + 1'b1;
          if (step == STEP_W'(ACC_STEPS - 1)) state <= S_DRAIN;
        end
        S_DRAIN: begin
          if (pv == '0 && !we) state <= S_DONE;
        end
        S_LD: begin
          if (ld_valid) begin
            ld_shift <= {ld_data, ld_shift[ROWW-W-1:W]};
            if (ld_beat == BPRW'(BPR - 1)) begin
              ld_beat <= '0;
              we      <= 1'b1;
              w_addr  <= AW'({k_q, ld_row});
              w_data  <= {ld_data, ld_shift};
              ld_row  <= ld_row + 1'b1;
              if (ld_row == STEP_W'(ACC_STEPS - 1)) state <= S_DONE;
            end else begin
              ld_beat <= ld_beat + 1'b1;
            end
          end
        end
        S_ST_READ: begin
          rd_en   <= 1'b1;
          rd_addr <= AW'({k_q, st_row});
          st_wait <= 2'd2;
          state   <= S_ST_WAIT;
        end
        S_ST_WAIT: begin
          st_wait <= st_wait - 1'b1;
          if (st_wait == 2'd1) begin
            st_rowdata <= rd_q1;      // rd_q1 holds the row 1 cycle after rd_en; take it here
            st_valid   <= 1'b1;
            st_beat    <= '0;
            state      <= S_ST_OUT;
          end
        end
        S_ST_OUT: begin
          if (st_ready) begin
            st_rowdata <= {{W{1'b0}}, st_rowdata[ROWW-1:W]};
            if (st_beat == BPRW'(BPR - 1)) begin
              st_valid <= 1'b0;
              st_row   <= st_row + 1'b1;
              if (st_row == STEP_W'(ACC_STEPS - 1)) state <= S_DONE;
              else state <= S_ST_READ;
            end else begin
              st_beat <= st_beat + 1'b1;
            end
          end
        end
        S_DONE: begin
          done  <= 1'b1;
          state <= S_IDLE;
        end
        default: state <= S_IDLE;
      endcase

      // read-pipeline completion (2 cycles after rd_en the row is in rd_q2)
      if (pv[2]) begin
        if (fn_q == ACC_THR) begin
          thr_valid <= 1'b1;
          thr_bits  <= thr_row(rd_q2, val_q);
          thr_step  <= pstep[2];
        end else begin
          we     <= 1'b1;
          w_addr <= AW'({k_q, pstep[2]});
          w_data <= add_row(rd_q2, pbits[2], val_q, fn_q == ACC_SUB);
        end
      end
    end
  end
  assign st_data = st_rowdata[W-1:0];
endmodule
