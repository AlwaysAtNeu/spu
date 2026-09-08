// AXI4 master read/write engines shared by the core, the search unit and the accumulator.
//  * requests are (32-bit SPU offset, number of 64-byte beats); MEM_BASE is added
//  * bursts never cross a 4 KiB boundary (split automatically)
//  * reads are fenced behind all outstanding writes (program-order memory consistency)
//  * the read data stream has no backpressure: consumers must always accept
module spu_dma import spu_pkg::*; #(
  parameter int MAX_RD_BURSTS = 8
) (
  input  logic              clk,
  input  logic              rst_n,
  input  logic [63:0]       mem_base,
  // read request queue
  input  logic              rq_valid,
  output logic              rq_ready,
  input  logic [31:0]       rq_addr,
  input  logic [15:0]       rq_beats,
  output logic              rd_valid,
  output logic [W-1:0]      rd_data,
  output logic              rd_idle,
  // write request queue + data stream
  input  logic              wq_valid,
  output logic              wq_ready,
  input  logic [31:0]       wq_addr,
  input  logic [15:0]       wq_beats,
  input  logic              wd_valid,
  output logic              wd_ready,
  input  logic [W-1:0]      wd_data,
  input  logic [W/8-1:0]    wd_strb,
  output logic              wr_idle,
  output logic              axi_err,
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
  localparam int BEAT_BYTES = W / 8;           // 64
  localparam int BEAT_SHIFT = $clog2(BEAT_BYTES);
  localparam int BEATS_4K   = 4096 / BEAT_BYTES; // 64
  localparam int RQ_DEPTH   = 4;
  localparam int WQ_DEPTH   = 2;

  assign m_axi_awid    = 4'd0;
  assign m_axi_arid    = 4'd0;
  assign m_axi_awsize  = 3'(BEAT_SHIFT);
  assign m_axi_arsize  = 3'(BEAT_SHIFT);
  assign m_axi_awburst = 2'b01;
  assign m_axi_arburst = 2'b01;
  assign m_axi_rready  = 1'b1;
  assign m_axi_bready  = 1'b1;

  // ------------------------------------------------------------ write engine
  logic [31:0] wq_a [WQ_DEPTH];
  logic [15:0] wq_n [WQ_DEPTH];
  logic [$clog2(WQ_DEPTH):0] wq_cnt;
  logic [$clog2(WQ_DEPTH)-1:0] wq_rp, wq_wp;
  logic        wq_pop;
  assign wq_ready = (wq_cnt != ($clog2(WQ_DEPTH)+1)'(WQ_DEPTH));

  typedef enum logic [1:0] {W_IDLE, W_AW, W_DATA} wstate_t;
  wstate_t wstate;
  logic [31:0] w_addr;
  logic [15:0] w_beats;      // beats left in the request
  logic [7:0]  w_burst_left; // beats left in the current burst
  logic [7:0]  w_burst_len;
  logic [7:0]  wr_pending;   // AWs issued minus Bs received

  logic [15:0] w_to_boundary;
  assign w_to_boundary = 16'(BEATS_4K) - 16'(w_addr[11:BEAT_SHIFT]);

  always_comb begin
    logic [15:0] l;
    l = (w_beats < w_to_boundary) ? w_beats : w_to_boundary;
    w_burst_len = (l > 16'd256) ? 8'd255 : 8'(l - 16'd1);   // AXI len = beats - 1
  end

  assign m_axi_awvalid = (wstate == W_AW);
  assign m_axi_awaddr  = mem_base + {32'b0, w_addr};
  assign m_axi_awlen   = w_burst_len;
  assign m_axi_wvalid  = (wstate == W_DATA) && wd_valid;
  assign wd_ready      = (wstate == W_DATA) && m_axi_wready;
  assign m_axi_wdata   = wd_data;
  assign m_axi_wstrb   = wd_strb;
  assign m_axi_wlast   = (w_burst_left == 8'd1);
  assign wq_pop        = (wstate == W_IDLE) && (wq_cnt != '0);
  assign wr_idle       = (wstate == W_IDLE) && (wq_cnt == '0) && (wr_pending == '0);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      wq_cnt <= '0; wq_rp <= '0; wq_wp <= '0;
      wstate <= W_IDLE; wr_pending <= '0; axi_err <= 1'b0;
      w_addr <= '0; w_beats <= '0; w_burst_left <= '0;
    end else begin
      // queue
      if (wq_valid && wq_ready) begin
        wq_a[wq_wp] <= wq_addr;
        wq_n[wq_wp] <= wq_beats;
        wq_wp <= wq_wp + 1'b1;
      end
      if (wq_pop) wq_rp <= wq_rp + 1'b1;
      wq_cnt <= wq_cnt + ($clog2(WQ_DEPTH)+1)'(wq_valid && wq_ready) - ($clog2(WQ_DEPTH)+1)'(wq_pop);
      // engine
      case (wstate)
        W_IDLE: if (wq_pop) begin
          w_addr  <= {wq_a[wq_rp][31:BEAT_SHIFT], {BEAT_SHIFT{1'b0}}};
          w_beats <= wq_n[wq_rp];
          wstate  <= (wq_n[wq_rp] == '0) ? W_IDLE : W_AW;
        end
        W_AW: if (m_axi_awready) begin
          w_burst_left <= w_burst_len + 8'd1;
          w_beats      <= w_beats - 16'(w_burst_len) - 16'd1;
          w_addr       <= w_addr + ((32'(w_burst_len) + 32'd1) << BEAT_SHIFT);
          wstate       <= W_DATA;
        end
        W_DATA: if (m_axi_wvalid && m_axi_wready) begin
          w_burst_left <= w_burst_left - 8'd1;
          if (w_burst_left == 8'd1) wstate <= (w_beats == '0) ? W_IDLE : W_AW;
        end
        default: wstate <= W_IDLE;
      endcase
      wr_pending <= wr_pending + 8'(m_axi_awvalid && m_axi_awready) - 8'(m_axi_bvalid);
      if (m_axi_bvalid && m_axi_bresp[1]) axi_err <= 1'b1;
      if (m_axi_rvalid && m_axi_rresp[1]) axi_err <= 1'b1;
    end
  end

  // ------------------------------------------------------------ read engine
  logic [31:0] rq_a [RQ_DEPTH];
  logic [15:0] rq_n [RQ_DEPTH];
  logic [$clog2(RQ_DEPTH):0] rq_cnt;
  logic [$clog2(RQ_DEPTH)-1:0] rq_rp, rq_wp;
  logic        rq_pop;
  assign rq_ready = (rq_cnt != ($clog2(RQ_DEPTH)+1)'(RQ_DEPTH));

  logic        r_active;
  logic [31:0] r_addr;
  logic [15:0] r_beats;
  logic [7:0]  r_burst_len;
  logic [$clog2(MAX_RD_BURSTS+1)-1:0] r_outstanding;
  logic [15:0] r_to_boundary;
  assign r_to_boundary = 16'(BEATS_4K) - 16'(r_addr[11:BEAT_SHIFT]);
  always_comb begin
    logic [15:0] l;
    l = (r_beats < r_to_boundary) ? r_beats : r_to_boundary;
    r_burst_len = (l > 16'd256) ? 8'd255 : 8'(l - 16'd1);
  end

  // fence: a new request is only started when every write has been acknowledged
  assign rq_pop        = !r_active && (rq_cnt != '0) && wr_idle && !(wq_valid);
  assign m_axi_arvalid = r_active && (r_outstanding != ($clog2(MAX_RD_BURSTS+1))'(MAX_RD_BURSTS));
  assign m_axi_araddr  = mem_base + {32'b0, r_addr};
  assign m_axi_arlen   = r_burst_len;
  assign rd_valid      = m_axi_rvalid;
  assign rd_data       = m_axi_rdata;
  assign rd_idle       = !r_active && (rq_cnt == '0) && (r_outstanding == '0);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      rq_cnt <= '0; rq_rp <= '0; rq_wp <= '0;
      r_active <= 1'b0; r_addr <= '0; r_beats <= '0; r_outstanding <= '0;
    end else begin
      if (rq_valid && rq_ready) begin
        rq_a[rq_wp] <= rq_addr;
        rq_n[rq_wp] <= rq_beats;
        rq_wp <= rq_wp + 1'b1;
      end
      if (rq_pop) rq_rp <= rq_rp + 1'b1;
      rq_cnt <= rq_cnt + ($clog2(RQ_DEPTH)+1)'(rq_valid && rq_ready) - ($clog2(RQ_DEPTH)+1)'(rq_pop);
      if (rq_pop) begin
        r_addr   <= {rq_a[rq_rp][31:BEAT_SHIFT], {BEAT_SHIFT{1'b0}}};
        r_beats  <= rq_n[rq_rp];
        r_active <= (rq_n[rq_rp] != '0);
      end else if (r_active && m_axi_arvalid && m_axi_arready) begin
        r_beats <= r_beats - 16'(r_burst_len) - 16'd1;
        r_addr  <= r_addr + ((32'(r_burst_len) + 32'd1) << BEAT_SHIFT);
        if (r_beats == 16'(r_burst_len) + 16'd1) r_active <= 1'b0;
      end
      r_outstanding <= r_outstanding + ($clog2(MAX_RD_BURSTS+1))'(m_axi_arvalid && m_axi_arready)
                                     - ($clog2(MAX_RD_BURSTS+1))'(m_axi_rvalid && m_axi_rlast);
    end
  end

  logic unused;
  assign unused = ^{m_axi_bid, m_axi_rid, m_axi_bresp[0], m_axi_rresp[0]};
endmodule
