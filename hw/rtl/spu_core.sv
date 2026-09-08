// SPU core: instruction sequencer, scalar unit, hypervector micro-sequencer.
//
// Instruction flow: FETCH (present pc) -> DECODE (latch word) -> EXEC (scalar ops retire
// here; HV ops start a multi-cycle micro-sequence).  The next sequential instruction is
// prefetched during EXEC so straight-line scalar code runs at 2 cycles / instruction;
// taken branches cost 3.
//
// HV ALU pipeline (per chunk):  RD (RF address) -> EX1 -> EX2 -> WB, one chunk per cycle,
// i.e. C + 3 cycles per HV op plus the 2 issue cycles.
module spu_core import spu_pkg::*; (
  input  logic                    clk,
  input  logic                    rst_n,
  // host control
  input  logic                    start,
  input  logic [31:0]             pc_start,
  input  logic [63:0]             mem_base,
  output logic                    busy,
  output logic                    done_pulse,
  output logic                    err_flag,
  output logic [31:0]             pc_out,
  // host CSR write / readback
  input  logic                    host_csr_we,
  input  logic [4:0]              host_csr_idx,
  input  logic [31:0]             host_csr_wdata,
  output logic [NCSR-1:0][31:0]   csr_o,
  output logic [NSREG-1:0][31:0]  sreg_o,
  // debug hypervector readback (valid while idle)
  input  logic [RF_AW-1:0]        dbg_addr,
  output logic [W-1:0]            dbg_data,
  // program memory (1 cycle read latency)
  output logic [$clog2(PROG_DEPTH)-1:0] prog_addr,
  input  logic [63:0]             prog_rdata,
  // AXI4 master
  output logic [3:0]        m_axi_awid,
  output logic [63:0]       m_axi_awaddr,
  output logic [7:0]        m_axi_awlen,
  output logic [2:0]        m_axi_awsize,
  output logic [1:0]        m_axi_awburst,
  output logic              m_axi_awvalid,
  input  logic              m_axi_awready,
  output logic [W-1:0]      m_axi_wdata,
  output logic [W/8-1:0]    m_axi_wstrb,
  output logic              m_axi_wlast,
  output logic              m_axi_wvalid,
  input  logic              m_axi_wready,
  input  logic [3:0]        m_axi_bid,
  input  logic [1:0]        m_axi_bresp,
  input  logic              m_axi_bvalid,
  output logic              m_axi_bready,
  output logic [3:0]        m_axi_arid,
  output logic [63:0]       m_axi_araddr,
  output logic [7:0]        m_axi_arlen,
  output logic [2:0]        m_axi_arsize,
  output logic [1:0]        m_axi_arburst,
  output logic              m_axi_arvalid,
  input  logic              m_axi_arready,
  input  logic [3:0]        m_axi_rid,
  input  logic [W-1:0]      m_axi_rdata,
  input  logic [1:0]        m_axi_rresp,
  input  logic              m_axi_rlast,
  input  logic              m_axi_rvalid,
  output logic              m_axi_rready
);
  localparam int PW   = $clog2(PROG_DEPTH);
  localparam int DW   = $clog2(D + 1);       // distance width
  localparam int PCW  = $clog2(W + 1);
  localparam int LOGW = $clog2(W);           // bits of a rotation inside a chunk
  localparam int LOGD = $clog2(D);
  localparam int BS   = $clog2(W / 8);       // beat address shift
  localparam int WPB  = W / 32;              // 32-bit words per beat
  localparam int NPIPE = 5;                  // xor register + popcount stages

  // ------------------------------------------------------------------ state
  typedef enum logic [4:0] {
    S_IDLE, S_FETCH, S_DECODE, S_EXEC, S_MULWB,
    S_LDREQ, S_LDW, S_STREQ, S_STDATA,
    S_HBUF, S_HV, S_GEN, S_HLDREQ, S_HLD, S_HSTREQ, S_HSTDATA, S_HDIST,
    S_SEARCH, S_ACC, S_ACCLDREQ, S_ACCLD, S_ACCSTREQ, S_ACCST,
    S_HALT_WAIT, S_DONE
  } state_t;
  state_t state, hb_ret;

  instr_t      ir;
  logic [31:0] pc;
  logic [63:0] cycles, instret;
  logic [7:0]  err_code;
  logic        halt_err;

  logic [31:0] sreg [NSREG];
  logic [31:0] csr  [NCSR];
  logic [C-1:0][W-1:0] hbuf;

  // decoded operands (valid from EXEC on)
  logic [31:0] sa, sb, sc;               // s[ra], s[rb], s[rc]
  logic [31:0] val_ci;                   // s[rc] + imm
  logic [31:0] addr_ai;                  // s[ra] + imm
  assign sa      = sreg[ir.ra];
  assign sb      = sreg[ir.rb];
  assign sc      = sreg[ir.rc];
  assign val_ci  = sc + ir.imm;
  assign addr_ai = sa + ir.imm;

  // ------------------------------------------------------------------ RF
  logic             rf_ra_en, rf_rb_en, rf_we;
  logic [RF_AW-1:0] rf_ra_addr, rf_rb_addr, rf_waddr;
  logic [W-1:0]     rf_ra_data, rf_rb_data, rf_wdata;
  spu_hvrf u_rf (.clk(clk), .ra_en(rf_ra_en), .ra_addr(rf_ra_addr), .ra_data(rf_ra_data),
                 .rb_en(rf_rb_en), .rb_addr(rf_rb_addr), .rb_data(rf_rb_data),
                 .we(rf_we), .w_addr(rf_waddr), .w_data(rf_wdata));
  assign dbg_data = rf_ra_data;

  // ------------------------------------------------------------------ DMA
  logic        rq_valid, rq_ready, rd_valid, rd_idle;
  logic [31:0] rq_addr;
  logic [15:0] rq_beats;
  logic [W-1:0] rd_data;
  logic        wq_valid, wq_ready, wd_valid, wd_ready, wr_idle, axi_err;
  logic [31:0] wq_addr;
  logic [15:0] wq_beats;
  logic [W-1:0] wd_data;
  logic [W/8-1:0] wd_strb;
  // core-side requests
  logic        c_rq_valid, c_wq_valid, c_wd_valid;
  logic [31:0] c_rq_addr, c_wq_addr;
  logic [15:0] c_rq_beats, c_wq_beats;
  logic [W-1:0] c_wd_data;
  logic [W/8-1:0] c_wd_strb;
  // search-side requests
  logic        s_rq_valid, s_wq_valid, s_wd_valid;
  logic [31:0] s_rq_addr, s_wq_addr;
  logic [15:0] s_rq_beats, s_wq_beats;
  logic [W-1:0] s_wd_data;
  logic [W/8-1:0] s_wd_strb;
  logic        in_search;
  assign in_search = (state == S_SEARCH);
  assign rq_valid = in_search ? s_rq_valid : c_rq_valid;
  assign rq_addr  = in_search ? s_rq_addr  : c_rq_addr;
  assign rq_beats = in_search ? s_rq_beats : c_rq_beats;
  assign wq_valid = in_search ? s_wq_valid : c_wq_valid;
  assign wq_addr  = in_search ? s_wq_addr  : c_wq_addr;
  assign wq_beats = in_search ? s_wq_beats : c_wq_beats;
  assign wd_valid = in_search ? s_wd_valid : c_wd_valid;
  assign wd_data  = in_search ? s_wd_data  : c_wd_data;
  assign wd_strb  = in_search ? s_wd_strb  : c_wd_strb;

  spu_dma u_dma (
    .clk(clk), .rst_n(rst_n), .mem_base(mem_base),
    .rq_valid(rq_valid), .rq_ready(rq_ready), .rq_addr(rq_addr), .rq_beats(rq_beats),
    .rd_valid(rd_valid), .rd_data(rd_data), .rd_idle(rd_idle),
    .wq_valid(wq_valid), .wq_ready(wq_ready), .wq_addr(wq_addr), .wq_beats(wq_beats),
    .wd_valid(wd_valid), .wd_ready(wd_ready), .wd_data(wd_data), .wd_strb(wd_strb),
    .wr_idle(wr_idle), .axi_err(axi_err),
    .m_axi_awid(m_axi_awid), .m_axi_awaddr(m_axi_awaddr), .m_axi_awlen(m_axi_awlen), .m_axi_awsize(m_axi_awsize),
    .m_axi_awburst(m_axi_awburst), .m_axi_awvalid(m_axi_awvalid), .m_axi_awready(m_axi_awready),
    .m_axi_wdata(m_axi_wdata), .m_axi_wstrb(m_axi_wstrb), .m_axi_wlast(m_axi_wlast), .m_axi_wvalid(m_axi_wvalid),
    .m_axi_wready(m_axi_wready), .m_axi_bid(m_axi_bid), .m_axi_bresp(m_axi_bresp), .m_axi_bvalid(m_axi_bvalid),
    .m_axi_bready(m_axi_bready), .m_axi_arid(m_axi_arid), .m_axi_araddr(m_axi_araddr), .m_axi_arlen(m_axi_arlen),
    .m_axi_arsize(m_axi_arsize), .m_axi_arburst(m_axi_arburst), .m_axi_arvalid(m_axi_arvalid),
    .m_axi_arready(m_axi_arready), .m_axi_rid(m_axi_rid), .m_axi_rdata(m_axi_rdata), .m_axi_rresp(m_axi_rresp),
    .m_axi_rlast(m_axi_rlast), .m_axi_rvalid(m_axi_rvalid), .m_axi_rready(m_axi_rready));

  // ------------------------------------------------------------------ search
  logic        srch_start, srch_done;
  logic [31:0] srch_best_idx, srch_best_dist, srch_best2_idx, srch_best2_dist;
  spu_search u_search (
    .clk(clk), .rst_n(rst_n), .start(srch_start), .base(sb), .count(sc), .wrdist(ir.fn[SEARCH_WRDIST_BIT]),
    .dout(csr[CSR_DOUT]), .query(hbuf),
    .rq_valid(s_rq_valid), .rq_ready(rq_ready), .rq_addr(s_rq_addr), .rq_beats(s_rq_beats),
    .rd_valid(rd_valid && in_search), .rd_data(rd_data),
    .wq_valid(s_wq_valid), .wq_ready(wq_ready), .wq_addr(s_wq_addr), .wq_beats(s_wq_beats),
    .wd_valid(s_wd_valid), .wd_ready(wd_ready), .wd_data(s_wd_data), .wd_strb(s_wd_strb),
    .done(srch_done), .best_idx(srch_best_idx), .best_dist(srch_best_dist),
    .best2_idx(srch_best2_idx), .best2_dist(srch_best2_dist));

  // ------------------------------------------------------------------ accumulator
  logic        acc_start, acc_done, acc_thr_valid, acc_st_valid;
  logic [ACC_LANES-1:0] acc_thr_bits;
  logic [STEP_W-1:0]    acc_thr_step;
  logic [W-1:0]         acc_st_data;
  spu_acc u_acc (
    .clk(clk), .rst_n(rst_n), .start(acc_start), .fn(ir.fn), .k(ir.rb[$clog2(NACC)-1:0]),
    .val(val_ci[15:0]), .hv_in(hbuf),
    .thr_valid(acc_thr_valid), .thr_bits(acc_thr_bits), .thr_step(acc_thr_step),
    .ld_valid(rd_valid && (state == S_ACCLD)), .ld_data(rd_data),
    .st_valid(acc_st_valid), .st_data(acc_st_data), .st_ready(wd_ready && (state == S_ACCST)),
    .done(acc_done));

  // ------------------------------------------------------------------ generator
  logic        gen_start, gen_valid, gen_done;
  logic [W-1:0] gen_data;
  logic [CH_W-1:0] gen_chunk;
  spu_gen u_gen (.clk(clk), .rst_n(rst_n), .start(gen_start), .seed(val_ci),
                 .out_valid(gen_valid), .out_data(gen_data), .out_chunk(gen_chunk), .done(gen_done));

  // ------------------------------------------------------------------ HV pipeline
  typedef enum logic [2:0] {HV_XOR, HV_AND, HV_OR, HV_NOT, HV_MASK, HV_ROT} hvop_t;
  hvop_t          hv_op;
  logic           hv_buf_src;             // ROT reads its source from hbuf
  logic [CH_W-1:0] rot_q;                 // chunk offset
  logic [LOGW-1:0] rot_r;                 // bit offset inside a chunk
  logic [LOGD:0]   mask_n;                // clamped mask length (0..D)
  logic [4:0]      hb_reg;                // register being copied into hbuf
  logic            iss_active;
  logic [CH_W-1:0] c_iss;
  logic            p1_v, p2_v, p3_v;
  logic [CH_W-1:0] p1_c, p2_c, p3_c;
  logic [2*W-1:0]  p2_d;
  logic [W-1:0]    p3_d;
  logic [CH_W-1:0] ld_c;                  // HLD / HST beat counter
  logic [4:0]      hv_rd;                 // destination register of the running HV op

  // ---- read address generation
  logic [CH_W-1:0] rot_c0, rot_c1;
  assign rot_c0 = c_iss - rot_q;
  assign rot_c1 = c_iss - rot_q - 1'b1;
  always_comb begin
    rf_ra_en   = 1'b0;
    rf_rb_en   = 1'b0;
    rf_ra_addr = dbg_addr;
    rf_rb_addr = '0;
    case (state)
      S_IDLE: rf_ra_en = 1'b1;   // debug readback
      S_HBUF: begin
        rf_ra_en   = iss_active;
        rf_ra_addr = {hb_reg, c_iss};
      end
      S_HV: begin
        if (hv_op == HV_ROT) begin
          rf_ra_en   = iss_active && !hv_buf_src;
          rf_rb_en   = iss_active && !hv_buf_src;
          rf_ra_addr = {ir.ra, rot_c0};
          rf_rb_addr = {ir.ra, rot_c1};
        end else begin
          rf_ra_en   = iss_active;
          rf_rb_en   = iss_active;
          rf_ra_addr = {ir.ra, c_iss};
          rf_rb_addr = {ir.rb, c_iss};
        end
      end
      S_HDIST: begin
        rf_ra_en   = iss_active;
        rf_rb_en   = iss_active;
        rf_ra_addr = {ir.ra, c_iss};
        rf_rb_addr = {ir.rb, c_iss};
      end
      default: ;
    endcase
  end

  // ---- EX1: chunk operation on the RF outputs (tag p1_c)
  logic [W-1:0] ex1_res;
  logic [W-1:0] mask_chunk;
  logic [CH_W-1:0] p1_c0, p1_c1;
  logic [W-1:0] d0, d1;
  assign p1_c0 = p1_c - rot_q;
  assign p1_c1 = p1_c - rot_q - 1'b1;
  assign d0 = hv_buf_src ? hbuf[p1_c0] : rf_ra_data;
  assign d1 = hv_buf_src ? hbuf[p1_c1] : rf_rb_data;
  always_comb begin
    logic [LOGD:0] lo, hi;
    lo = {1'b0, p1_c, {LOGW{1'b0}}};   // first bit index of chunk
    hi = lo + (LOGD+1)'(W);            // one past the last
    if (mask_n >= hi)      mask_chunk = '1;
    else if (mask_n <= lo) mask_chunk = '0;
    else                   mask_chunk = (W'(1) << (mask_n - lo)) - 1'b1;
  end
  always_comb begin
    case (hv_op)
      HV_XOR:  ex1_res = rf_ra_data ^ rf_rb_data;
      HV_AND:  ex1_res = rf_ra_data & rf_rb_data;
      HV_OR:   ex1_res = rf_ra_data | rf_rb_data;
      HV_NOT:  ex1_res = ~rf_ra_data;
      HV_MASK: ex1_res = mask_chunk;
      default: ex1_res = '0;
    endcase
  end
  logic [2*W-1:0] rot_stage1, rot_stage2;
  assign rot_stage1 = {d0, d1} << rot_r[4:0];
  assign rot_stage2 = p2_d << {rot_r[LOGW-1:5], 5'b0};

  // ---- HDIST popcount pipeline
  logic [W-1:0]     xr;
  logic [NPIPE-1:0] xv, xlast;
  logic [PCW-1:0]   pc_cnt;
  logic [DW-1:0]    dist_acc;
  spu_popcount #(.N(W)) u_pc (.clk(clk), .din(xr), .dout(pc_cnt));

  // ---- THR packing
  logic [W-ACC_LANES-1:0] thr_shift;

  // ------------------------------------------------------------------ RF write mux
  always_comb begin
    rf_we    = 1'b0;
    rf_waddr = '0;
    rf_wdata = '0;
    case (state)
      S_HV: begin
        rf_we    = p3_v;
        rf_waddr = {hv_rd, p3_c};
        rf_wdata = p3_d;
      end
      S_HLD: begin
        rf_we    = rd_valid;
        rf_waddr = {hv_rd, ld_c};
        rf_wdata = rd_data;
      end
      S_GEN: begin
        rf_we    = gen_valid;
        rf_waddr = {hv_rd, gen_chunk};
        rf_wdata = gen_data;
      end
      S_ACC: begin
        rf_we    = acc_thr_valid && (ir.fn == ACC_THR) && (acc_thr_step[$clog2(ACC_SPC)-1:0] == ($clog2(ACC_SPC))'(ACC_SPC - 1));
        rf_waddr = {hv_rd, acc_thr_step[STEP_W-1:$clog2(ACC_SPC)]};
        rf_wdata = {acc_thr_bits, thr_shift};
      end
      default: ;
    endcase
  end

  // ------------------------------------------------------------------ core DMA request logic
  logic [31:0] ld_word;
  logic [7:0]  ld_byte;
  assign ld_word = rd_data[addr_ai[BS-1:2] * 32 +: 32];
  assign ld_byte = rd_data[addr_ai[BS-1:0] * 8 +: 8];
  always_comb begin
    c_rq_valid = 1'b0; c_rq_addr = addr_ai; c_rq_beats = 16'd1;
    c_wq_valid = 1'b0; c_wq_addr = addr_ai; c_wq_beats = 16'd1;
    c_wd_valid = 1'b0; c_wd_data = '0;      c_wd_strb  = '0;
    case (state)
      S_LDREQ:    c_rq_valid = 1'b1;
      S_HLDREQ:   begin c_rq_valid = 1'b1; c_rq_beats = 16'(C); end
      S_ACCLDREQ: begin c_rq_valid = 1'b1; c_rq_beats = 16'(ACC_BEATS); end
      S_STREQ:    c_wq_valid = 1'b1;
      S_HSTREQ:   begin c_wq_valid = 1'b1; c_wq_beats = 16'(C); end
      S_ACCSTREQ: begin c_wq_valid = 1'b1; c_wq_beats = 16'(ACC_BEATS); end
      S_STDATA: begin
        c_wd_valid = 1'b1;
        if (ir.op == OP_SW) begin
          c_wd_data = {WPB{sb}};
          c_wd_strb[addr_ai[BS-1:2] * 4 +: 4] = 4'hF;
        end else begin
          c_wd_data = {(W/8){sb[7:0]}};
          c_wd_strb[addr_ai[BS-1:0]] = 1'b1;
        end
      end
      S_HSTDATA: begin
        c_wd_valid = 1'b1;
        c_wd_data  = hbuf[ld_c];
        c_wd_strb  = '1;
      end
      S_ACCST: begin
        c_wd_valid = acc_st_valid;
        c_wd_data  = acc_st_data;
        c_wd_strb  = '1;
      end
      default: ;
    endcase
  end

  // ------------------------------------------------------------------ scalar ALU
  logic [31:0] alu_b, alu_res, mul_p;
  logic        br_taken;
  assign alu_b = (ir.op == OP_SOPI) ? ir.imm : sb;
  always_comb begin
    case (ir.fn)
      ALU_ADD:  alu_res = sa + alu_b;
      ALU_SUB:  alu_res = sa - alu_b;
      ALU_AND:  alu_res = sa & alu_b;
      ALU_OR:   alu_res = sa | alu_b;
      ALU_XOR:  alu_res = sa ^ alu_b;
      ALU_SHL:  alu_res = sa << alu_b[4:0];
      ALU_SHR:  alu_res = sa >> alu_b[4:0];
      ALU_SRA:  alu_res = 32'(signed'(sa) >>> alu_b[4:0]);
      ALU_SLT:  alu_res = {31'b0, signed'(sa) < signed'(alu_b)};
      ALU_SLTU: alu_res = {31'b0, sa < alu_b};
      default:  alu_res = '0;
    endcase
  end
  always_comb begin
    case (ir.fn)
      BR_BEQ:  br_taken = sa == sb;
      BR_BNE:  br_taken = sa != sb;
      BR_BLT:  br_taken = signed'(sa) < signed'(sb);
      BR_BGE:  br_taken = signed'(sa) >= signed'(sb);
      BR_BLTU: br_taken = sa < sb;
      BR_BGEU: br_taken = sa >= sb;
      default: br_taken = 1'b0;
    endcase
  end

  // ------------------------------------------------------------------ CSR read view
  logic [31:0] csr_rd [NCSR];
  always_comb begin
    for (int i = 0; i < NCSR; i++) csr_rd[i] = csr[i];
    csr_rd[CSR_CYCLES_LO]  = cycles[31:0];
    csr_rd[CSR_CYCLES_HI]  = cycles[63:32];
    csr_rd[CSR_INSTRET_LO] = instret[31:0];
    csr_rd[CSR_INSTRET_HI] = instret[63:32];
    csr_rd[CSR_ID]         = SPU_ID;
    csr_rd[CSR_CFG_D]      = 32'(D);
    csr_rd[CSR_CFG_NACC]   = 32'(NACC);
    csr_rd[CSR_ERR]        = {24'b0, err_code};
  end
  always_comb begin
    for (int i = 0; i < NCSR; i++) csr_o[i] = csr_rd[i];
    for (int i = 0; i < NSREG; i++) sreg_o[i] = sreg[i];
  end
  function automatic logic csr_writable(input logic [31:0] idx);
    csr_writable = (idx < 32'(NCSR)) && !(idx == 32'(CSR_ID) || idx == 32'(CSR_SR_IDX) || idx == 32'(CSR_SR_DIST) ||
                   idx == 32'(CSR_SR_IDX2) || idx == 32'(CSR_SR_DIST2) || idx == 32'(CSR_CYCLES_LO) ||
                   idx == 32'(CSR_CYCLES_HI) || idx == 32'(CSR_INSTRET_LO) || idx == 32'(CSR_INSTRET_HI) ||
                   idx == 32'(CSR_CFG_D) || idx == 32'(CSR_CFG_NACC) || idx == 32'(CSR_ERR));
  endfunction

  // ------------------------------------------------------------------ prefetch address
  always_comb begin
    prog_addr = PW'(pc + 32'd1);
    if (state == S_FETCH || state == S_IDLE) prog_addr = PW'(pc);
  end
  assign pc_out   = pc;
  assign busy     = (state != S_IDLE);
  assign err_flag = halt_err;

  // ------------------------------------------------------------------ main sequencer
  logic retire;                     // instruction completes this cycle -> pc+1, DECODE
  logic core_csr_we;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state <= S_IDLE; hb_ret <= S_IDLE; pc <= '0; cycles <= '0; instret <= '0; err_code <= '0; halt_err <= 1'b0;
      done_pulse <= 1'b0; ir <= '0;
      iss_active <= 1'b0; c_iss <= '0; p1_v <= 1'b0; p2_v <= 1'b0; p3_v <= 1'b0; p1_c <= '0; p2_c <= '0; p3_c <= '0;
      ld_c <= '0; hv_op <= HV_XOR; hv_buf_src <= 1'b0; rot_q <= '0; rot_r <= '0; mask_n <= '0; hb_reg <= '0; hv_rd <= '0;
      xv <= '0; xlast <= '0; dist_acc <= '0; srch_start <= 1'b0; acc_start <= 1'b0; gen_start <= 1'b0;
      for (int i = 0; i < NSREG; i++) sreg[i] <= '0;
      for (int i = 0; i < NCSR; i++) csr[i] <= '0;
      csr[CSR_SR_IDX] <= 32'hFFFFFFFF; csr[CSR_SR_DIST] <= 32'hFFFF;
      csr[CSR_SR_IDX2] <= 32'hFFFFFFFF; csr[CSR_SR_DIST2] <= 32'hFFFF;
    end else begin
      done_pulse <= 1'b0;
      srch_start <= 1'b0;
      acc_start  <= 1'b0;
      gen_start  <= 1'b0;
      retire     = 1'b0;
      if (state != S_IDLE) cycles <= cycles + 64'd1;

      // host CSR writes (core writes below take priority)
      if (host_csr_we && csr_writable({27'b0, host_csr_idx})) csr[host_csr_idx] <= host_csr_wdata;

      // ---- HV pipeline advance (RD -> EX1 -> EX2 -> WB)
      p1_v <= 1'b0;
      if (iss_active) begin
        p1_v  <= 1'b1;
        p1_c  <= c_iss;
        c_iss <= c_iss + 1'b1;
        if (c_iss == CH_W'(C - 1)) iss_active <= 1'b0;
      end
      p2_v <= p1_v; p2_c <= p1_c;
      p3_v <= p2_v; p3_c <= p2_c;
      if (p1_v) begin
        if (hv_op == HV_ROT) p2_d <= rot_stage1;
        else                 p2_d <= {ex1_res, {W{1'b0}}};
      end
      if (p2_v) begin
        if (hv_op == HV_ROT) begin
          p3_d <= rot_stage2[2*W-1:W];
        end else begin
          p3_d <= p2_d[2*W-1:W];
        end
      end
      // hbuf capture (S_HBUF uses port A with tag p1_c)
      if (state == S_HBUF && p1_v) hbuf[p1_c] <= rf_ra_data;

      // ---- HDIST pipeline
      xv    <= {xv[NPIPE-2:0], (state == S_HDIST) && p1_v};
      xlast <= {xlast[NPIPE-2:0], p1_c == CH_W'(C - 1)};
      if (p1_v) xr <= rf_ra_data ^ rf_rb_data;
      if (xv[NPIPE-1]) dist_acc <= dist_acc + DW'(pc_cnt);

      // ---- THR packing
      if (acc_thr_valid) thr_shift <= {acc_thr_bits, thr_shift[W-ACC_LANES-1:ACC_LANES]};

      case (state)
        // ------------------------------------------------------------
        S_IDLE: begin
          if (start) begin
            pc       <= pc_start;
            cycles   <= '0;
            instret  <= '0;
            err_code <= '0;
            halt_err <= 1'b0;
            state    <= S_FETCH;
          end
        end
        S_FETCH: state <= S_DECODE;
        S_DECODE: begin
          ir    <= prog_rdata;
          state <= S_EXEC;
          if (pc >= 32'(PROG_DEPTH)) begin
            err_code <= ERR_PC_RANGE; halt_err <= 1'b1; state <= S_HALT_WAIT;
          end
        end
        // ------------------------------------------------------------
        S_EXEC: begin
          hv_rd <= ir.rd;
          case (ir.op)
            OP_NOP: retire = 1'b1;
            OP_HALT: state <= S_HALT_WAIT;
            OP_SOP, OP_SOPI: begin
              if (ir.fn == ALU_MUL) begin
                mul_p <= sa * alu_b;
                state <= S_MULWB;
              end else if (ir.fn > ALU_SLTU) begin
                err_code <= ERR_ILLEGAL_OP; halt_err <= 1'b1; state <= S_HALT_WAIT;
              end else begin
                if (ir.rd != 5'd0) sreg[ir.rd] <= alu_res;
                retire = 1'b1;
              end
            end
            OP_LW, OP_LB: state <= S_LDREQ;
            OP_SW, OP_SB: state <= S_STREQ;
            OP_BR: begin
              if (ir.fn > BR_BGEU) begin
                err_code <= ERR_ILLEGAL_OP; halt_err <= 1'b1; state <= S_HALT_WAIT;
              end else if (br_taken) begin
                pc <= pc + ir.imm; instret <= instret + 64'd1; state <= S_FETCH;
              end else retire = 1'b1;
            end
            OP_JAL: begin
              if (ir.rd != 5'd0) sreg[ir.rd] <= pc + 32'd1;
              pc <= pc + ir.imm; instret <= instret + 64'd1; state <= S_FETCH;
            end
            OP_JALR: begin
              if (ir.rd != 5'd0) sreg[ir.rd] <= pc + 32'd1;
              pc <= addr_ai; instret <= instret + 64'd1; state <= S_FETCH;
            end
            OP_CSRR: begin
              if (ir.rd != 5'd0) sreg[ir.rd] <= (ir.imm < 32'(NCSR)) ? csr_rd[ir.imm[4:0]] : 32'd0;
              retire = 1'b1;
            end
            OP_CSRW: begin
              if (csr_writable(ir.imm)) csr[ir.imm[4:0]] <= sa;
              retire = 1'b1;
            end
            // ---- hypervector ALU ops
            OP_HXOR, OP_HAND, OP_HOR, OP_HNOT, OP_HMASK: begin
              case (ir.op)
                OP_HXOR: hv_op <= HV_XOR;
                OP_HAND: hv_op <= HV_AND;
                OP_HOR:  hv_op <= HV_OR;
                OP_HNOT: hv_op <= HV_NOT;
                default: hv_op <= HV_MASK;
              endcase
              mask_n     <= (signed'(val_ci) <= 0) ? '0 : (val_ci >= 32'(D)) ? (LOGD+1)'(D) : (LOGD+1)'(val_ci);
              hv_buf_src <= 1'b0;
              iss_active <= 1'b1; c_iss <= '0;
              state      <= S_HV;
            end
            OP_HROT: begin
              hv_op <= HV_ROT;
              rot_q <= val_ci[LOGD-1:LOGW];
              rot_r <= val_ci[LOGW-1:0];
              if (ir.rd == ir.ra) begin        // source is overwritten chunk by chunk: buffer it first
                hv_buf_src <= 1'b1; hb_reg <= ir.ra; hb_ret <= S_HV;
                iss_active <= 1'b1; c_iss <= '0; state <= S_HBUF;
              end else begin
                hv_buf_src <= 1'b0;
                iss_active <= 1'b1; c_iss <= '0; state <= S_HV;
              end
            end
            OP_HGEN: begin gen_start <= 1'b1; state <= S_GEN; end
            OP_HLD:  begin ld_c <= '0; state <= S_HLDREQ; end
            OP_HST:  begin hb_reg <= ir.rd; hb_ret <= S_HSTREQ; iss_active <= 1'b1; c_iss <= '0; state <= S_HBUF; end
            OP_HDIST: begin
              dist_acc <= '0; iss_active <= 1'b1; c_iss <= '0; state <= S_HDIST;
            end
            OP_HSEARCH: begin
              hb_reg <= ir.ra; hb_ret <= S_SEARCH; iss_active <= 1'b1; c_iss <= '0; state <= S_HBUF;
            end
            OP_HACC: begin
              if (ir.rb >= 5'(NACC)) begin
                err_code <= ERR_ACC_INDEX; halt_err <= 1'b1; state <= S_HALT_WAIT;
              end else case (ir.fn)
                ACC_CLR, ACC_THR: begin acc_start <= 1'b1; state <= S_ACC; end
                ACC_ADD, ACC_SUB: begin hb_reg <= ir.ra; hb_ret <= S_ACC; iss_active <= 1'b1; c_iss <= '0; state <= S_HBUF; end
                ACC_LD: begin acc_start <= 1'b1; state <= S_ACCLDREQ; end
                ACC_ST: begin acc_start <= 1'b1; state <= S_ACCSTREQ; end
                default: begin err_code <= ERR_ILLEGAL_OP; halt_err <= 1'b1; state <= S_HALT_WAIT; end
              endcase
            end
            default: begin err_code <= ERR_ILLEGAL_OP; halt_err <= 1'b1; state <= S_HALT_WAIT; end
          endcase
        end
        S_MULWB: begin
          if (ir.rd != 5'd0) sreg[ir.rd] <= mul_p;
          retire = 1'b1;
        end
        // ---- scalar memory
        S_LDREQ: if (rq_ready) state <= S_LDW;
        S_LDW: if (rd_valid) begin
          if (ir.rd != 5'd0) sreg[ir.rd] <= (ir.op == OP_LW) ? ld_word : {24'b0, ld_byte};
          retire = 1'b1;
        end
        S_STREQ: if (wq_ready) state <= S_STDATA;
        S_STDATA: if (wd_ready) retire = 1'b1;
        // ---- hypervector
        S_HBUF: begin
          // last chunk lands in hbuf when p1_v carries tag C-1
          if (p1_v && p1_c == CH_W'(C - 1)) begin
            state <= hb_ret;
            case (hb_ret)
              S_HV:     begin iss_active <= 1'b1; c_iss <= '0; end
              S_SEARCH: srch_start <= 1'b1;
              S_ACC:    acc_start <= 1'b1;
              default: ;
            endcase
          end
        end
        S_HV: begin
          if (!iss_active && !p1_v && !p2_v && !p3_v) retire = 1'b1;
        end
        S_GEN: if (gen_done) retire = 1'b1;
        S_HLDREQ: if (rq_ready) state <= S_HLD;
        S_HLD: if (rd_valid) begin
          ld_c <= ld_c + 1'b1;
          if (ld_c == CH_W'(C - 1)) retire = 1'b1;
        end
        S_HSTREQ: if (wq_ready) begin ld_c <= '0; state <= S_HSTDATA; end
        S_HSTDATA: if (wd_ready) begin
          ld_c <= ld_c + 1'b1;
          if (ld_c == CH_W'(C - 1)) retire = 1'b1;
        end
        S_HDIST: begin
          if (xv[NPIPE-1] && xlast[NPIPE-1]) begin
            if (ir.rd != 5'd0) sreg[ir.rd] <= 32'(dist_acc + DW'(pc_cnt));
            retire = 1'b1;
          end
        end
        S_SEARCH: if (srch_done) begin
          csr[CSR_SR_IDX]   <= srch_best_idx;
          csr[CSR_SR_DIST]  <= srch_best_dist;
          csr[CSR_SR_IDX2]  <= srch_best2_idx;
          csr[CSR_SR_DIST2] <= srch_best2_dist;
          if (ir.rd != 5'd0) sreg[ir.rd] <= srch_best_idx;
          retire = 1'b1;
        end
        S_ACC: if (acc_done) retire = 1'b1;
        S_ACCLDREQ: if (rq_ready) state <= S_ACCLD;
        S_ACCLD: if (acc_done) retire = 1'b1;
        S_ACCSTREQ: if (wq_ready) state <= S_ACCST;
        S_ACCST: if (acc_done) retire = 1'b1;
        // ---- halt
        S_HALT_WAIT: if (wr_idle && rd_idle) begin
          if (axi_err && err_code == ERR_NONE) begin err_code <= ERR_AXI; halt_err <= 1'b1; end
          state <= S_DONE;
        end
        S_DONE: begin
          done_pulse <= 1'b1;
          if (!halt_err) instret <= instret + 64'd1;   // HALT itself retires
          state <= S_IDLE;
        end
        default: state <= S_IDLE;
      endcase

      if (retire) begin
        pc      <= pc + 32'd1;
        instret <= instret + 64'd1;
        state   <= S_DECODE;
      end
      sreg[0] <= '0;
    end
  end

  logic unused;
  assign unused = ^{core_csr_we, rd_idle, m_axi_rid, rot_stage2[W-1:0]};
  assign core_csr_we = 1'b0;
endmodule
