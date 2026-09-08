// Host interface: AXI4-Lite slave with control/status registers, CSR / scalar / HV
// debug readback and the 64-bit x PROG_DEPTH instruction memory (written as 32-bit
// lo/hi halves at HOST_PROG_BASE + 8*i).
module spu_csr import spu_pkg::*; (
  input  logic                    clk,
  input  logic                    rst_n,
  // AXI4-Lite slave
  input  logic [HOST_AW-1:0]      s_axi_awaddr,
  input  logic                    s_axi_awvalid,
  output logic                    s_axi_awready,
  input  logic [31:0]             s_axi_wdata,
  input  logic [3:0]              s_axi_wstrb,
  input  logic                    s_axi_wvalid,
  output logic                    s_axi_wready,
  output logic [1:0]              s_axi_bresp,
  output logic                    s_axi_bvalid,
  input  logic                    s_axi_bready,
  input  logic [HOST_AW-1:0]      s_axi_araddr,
  input  logic                    s_axi_arvalid,
  output logic                    s_axi_arready,
  output logic [31:0]             s_axi_rdata,
  output logic [1:0]              s_axi_rresp,
  output logic                    s_axi_rvalid,
  input  logic                    s_axi_rready,
  // core
  output logic                    start,
  output logic                    soft_reset,
  output logic [31:0]             pc_start,
  output logic [63:0]             mem_base,
  input  logic                    busy,
  input  logic                    done_pulse,
  input  logic                    err_flag,
  input  logic [31:0]             pc,
  output logic                    host_csr_we,
  output logic [4:0]              host_csr_idx,
  output logic [31:0]             host_csr_wdata,
  input  logic [NCSR-1:0][31:0]   csr_i,
  input  logic [NSREG-1:0][31:0]  sreg_i,
  output logic [RF_AW-1:0]        dbg_addr,
  input  logic [W-1:0]            dbg_data,
  input  logic [$clog2(PROG_DEPTH)-1:0] prog_addr,
  output logic [63:0]             prog_rdata,
  output logic                    irq
);
  localparam int PW = $clog2(PROG_DEPTH);

  // ------------------------------------------------------------ program memory
  logic [31:0] prog_lo [PROG_DEPTH];
  logic [31:0] prog_hi [PROG_DEPTH];
  logic          hp_we_lo, hp_we_hi;
  logic [PW-1:0] hp_addr;
  logic [31:0]   hp_wdata, hp_q_lo, hp_q_hi;
  logic [3:0]    hp_strb;

  always_ff @(posedge clk) begin
    // core read port
    prog_rdata <= {prog_hi[prog_addr], prog_lo[prog_addr]};
    // host port (read-first)
    hp_q_lo <= prog_lo[hp_addr];
    hp_q_hi <= prog_hi[hp_addr];
    if (hp_we_lo) begin
      for (int b = 0; b < 4; b++) if (hp_strb[b]) prog_lo[hp_addr][b*8 +: 8] <= hp_wdata[b*8 +: 8];
    end
    if (hp_we_hi) begin
      for (int b = 0; b < 4; b++) if (hp_strb[b]) prog_hi[hp_addr][b*8 +: 8] <= hp_wdata[b*8 +: 8];
    end
  end

  // ------------------------------------------------------------ registers
  logic        irq_en, done_q, irq_q;
  logic [8:0]  hreg_sel;                        // {reg, chunk} (RF_AW bits)

  // ------------------------------------------------------------ AXI-Lite write
  logic                aw_have, w_have;
  logic [HOST_AW-1:0]  aw_q;
  logic [31:0]         w_q;
  logic [3:0]          wstrb_q;
  logic                do_write;
  assign s_axi_awready = !aw_have;
  assign s_axi_wready  = !w_have;
  assign s_axi_bresp   = 2'b00;
  assign do_write      = aw_have && w_have && !s_axi_bvalid;

  logic is_prog_w, is_prog_r;
  assign is_prog_w = (aw_q >= HOST_PROG_BASE) && (aw_q < HOST_PROG_BASE + HOST_AW'(PROG_DEPTH * 8));
  assign hp_we_lo  = do_write && is_prog_w && !aw_q[2];
  assign hp_we_hi  = do_write && is_prog_w &&  aw_q[2];
  assign hp_wdata  = w_q;
  assign hp_strb   = wstrb_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      aw_have <= 1'b0; w_have <= 1'b0; s_axi_bvalid <= 1'b0; aw_q <= '0; w_q <= '0; wstrb_q <= '0;
      start <= 1'b0; soft_reset <= 1'b0; pc_start <= '0; mem_base <= '0; irq_en <= 1'b0;
      done_q <= 1'b0; irq_q <= 1'b0; host_csr_we <= 1'b0; host_csr_idx <= '0; host_csr_wdata <= '0; hreg_sel <= '0;
    end else begin
      start       <= 1'b0;
      soft_reset  <= 1'b0;
      host_csr_we <= 1'b0;
      if (s_axi_awvalid && s_axi_awready) begin aw_have <= 1'b1; aw_q <= s_axi_awaddr; end
      if (s_axi_wvalid  && s_axi_wready)  begin w_have  <= 1'b1; w_q  <= s_axi_wdata; wstrb_q <= s_axi_wstrb; end
      if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 1'b0;
      if (do_write) begin
        aw_have <= 1'b0; w_have <= 1'b0; s_axi_bvalid <= 1'b1;
        if (!is_prog_w) begin
          case (aw_q)
            HOST_CTRL: begin
              if (w_q[CTRL_START_BIT] && !busy) begin start <= 1'b1; done_q <= 1'b0; irq_q <= 1'b0; end
              if (w_q[CTRL_SOFT_RESET_BIT]) soft_reset <= 1'b1;
              irq_en <= w_q[CTRL_IRQ_EN_BIT];
            end
            HOST_PC_START:    pc_start <= w_q;
            HOST_MEM_BASE_LO: mem_base[31:0]  <= w_q;
            HOST_MEM_BASE_HI: mem_base[63:32] <= w_q;
            HOST_IRQ_ACK:     if (w_q[0]) begin irq_q <= 1'b0; done_q <= 1'b0; end
            HOST_HREG_BASE:   hreg_sel <= w_q[8:0];
            default: begin
              if (aw_q >= HOST_CSR_BASE && aw_q < HOST_CSR_BASE + HOST_AW'(NCSR * 4)) begin
                host_csr_we    <= 1'b1;
                host_csr_idx   <= aw_q[6:2];
                host_csr_wdata <= w_q;
              end
            end
          endcase
        end
      end
      if (done_pulse) begin
        done_q <= 1'b1;
        if (irq_en) irq_q <= 1'b1;
      end
    end
  end
  assign irq      = irq_q;
  assign dbg_addr = RF_AW'(hreg_sel);

  // ------------------------------------------------------------ AXI-Lite read (2 cycles)
  logic               ar_have;
  logic [HOST_AW-1:0] ar_q;
  logic               rd_stage;
  assign s_axi_arready = !ar_have && !s_axi_rvalid;
  assign s_axi_rresp   = 2'b00;
  assign is_prog_r     = (ar_q >= HOST_PROG_BASE) && (ar_q < HOST_PROG_BASE + HOST_AW'(PROG_DEPTH * 8));
  assign hp_addr       = ar_have ? ar_q[PW+2:3] : aw_q[PW+2:3];

  logic [31:0] rd_mux;
  always_comb begin
    rd_mux = 32'd0;
    if (is_prog_r) rd_mux = ar_q[2] ? hp_q_hi : hp_q_lo;
    else if (ar_q >= HOST_CSR_BASE && ar_q < HOST_CSR_BASE + HOST_AW'(NCSR * 4)) rd_mux = csr_i[ar_q[6:2]];
    else if (ar_q >= HOST_SREG_BASE && ar_q < HOST_SREG_BASE + HOST_AW'(NSREG * 4)) rd_mux = sreg_i[ar_q[6:2]];
    else if (ar_q > HOST_HREG_BASE && ar_q < HOST_HREG_BASE + HOST_AW'(4 + W / 8)) rd_mux = dbg_data[(ar_q[6:2] - 5'd1) * 32 +: 32];
    else case (ar_q)
      HOST_CTRL:        rd_mux = {29'b0, irq_en, 2'b0};
      HOST_STATUS:      rd_mux = {28'b0, irq_q, err_flag, done_q, busy};
      HOST_PC_START:    rd_mux = pc_start;
      HOST_PC:          rd_mux = pc;
      HOST_MEM_BASE_LO: rd_mux = mem_base[31:0];
      HOST_MEM_BASE_HI: rd_mux = mem_base[63:32];
      HOST_ID:          rd_mux = SPU_ID;
      HOST_VERSION:     rd_mux = SPU_VERSION;
      HOST_CFG0:        rd_mux = {16'(W), 16'(D)};
      HOST_CFG1:        rd_mux = {16'(ACC_LANES), 8'(NACC), 8'(NHREG)};
      HOST_HREG_BASE:   rd_mux = {23'b0, hreg_sel};
      default:          rd_mux = 32'd0;
    endcase
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      ar_have <= 1'b0; ar_q <= '0; rd_stage <= 1'b0; s_axi_rvalid <= 1'b0; s_axi_rdata <= '0;
    end else begin
      if (s_axi_arvalid && s_axi_arready) begin ar_have <= 1'b1; ar_q <= s_axi_araddr; rd_stage <= 1'b0; end
      if (ar_have && !rd_stage) rd_stage <= 1'b1;          // cycle 1: memory / register read
      if (ar_have && rd_stage) begin                        // cycle 2: present data
        s_axi_rdata  <= rd_mux;
        s_axi_rvalid <= 1'b1;
        ar_have      <= 1'b0;
        rd_stage     <= 1'b0;
      end
      if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 1'b0;
    end
  end

  logic unused;
  assign unused = ^{ar_q[1:0], aw_q[1:0]};
endmodule
