// SPU top-level testbench (Verilator --binary --timing).
// plusargs: +prog=<hex> +mem=<hex> +mem_out=<hex> +sregs=<txt> +hregs=<txt> +csrs=<txt> +cycles=<txt>
//           +timeout=<cycles> +backpressure=<pct> +max_lat=<cycles>
module tb_spu import spu_pkg::*; ();
  logic clk = 1'b0;
  logic rst_n = 1'b0;
  always #5 clk = ~clk;

  // ---------------- AXI-Lite (host) ----------------
  logic [HOST_AW-1:0] s_axi_awaddr = '0;
  logic               s_axi_awvalid = 1'b0, s_axi_awready;
  logic [31:0]        s_axi_wdata = '0;
  logic [3:0]         s_axi_wstrb = '0;
  logic               s_axi_wvalid = 1'b0, s_axi_wready;
  logic [1:0]         s_axi_bresp;
  logic               s_axi_bvalid, s_axi_bready = 1'b0;
  logic [HOST_AW-1:0] s_axi_araddr = '0;
  logic               s_axi_arvalid = 1'b0, s_axi_arready;
  logic [31:0]        s_axi_rdata;
  logic [1:0]         s_axi_rresp;
  logic               s_axi_rvalid, s_axi_rready = 1'b0;
  // ---------------- AXI4 (memory) ----------------
  logic [3:0]   m_axi_awid, m_axi_bid, m_axi_arid, m_axi_rid;
  logic [63:0]  m_axi_awaddr, m_axi_araddr;
  logic [7:0]   m_axi_awlen, m_axi_arlen;
  logic [2:0]   m_axi_awsize, m_axi_arsize;
  logic [1:0]   m_axi_awburst, m_axi_arburst, m_axi_bresp, m_axi_rresp;
  logic         m_axi_awvalid, m_axi_awready, m_axi_wlast, m_axi_wvalid, m_axi_wready;
  logic         m_axi_bvalid, m_axi_bready, m_axi_arvalid, m_axi_arready, m_axi_rlast, m_axi_rvalid, m_axi_rready;
  logic [W-1:0] m_axi_wdata, m_axi_rdata;
  logic [W/8-1:0] m_axi_wstrb;
  logic irq;
  logic dump_req = 1'b0, dump_done;
  logic unused_tb;
  assign unused_tb = ^{s_axi_bresp, s_axi_rresp};

  spu_top dut (.*);
  axi_mem_model u_mem (
    .clk(clk), .rst_n(rst_n), .dump_req(dump_req), .dump_done(dump_done),
    .s_awid(m_axi_awid), .s_awaddr(m_axi_awaddr), .s_awlen(m_axi_awlen), .s_awsize(m_axi_awsize),
    .s_awburst(m_axi_awburst), .s_awvalid(m_axi_awvalid), .s_awready(m_axi_awready),
    .s_wdata(m_axi_wdata), .s_wstrb(m_axi_wstrb), .s_wlast(m_axi_wlast), .s_wvalid(m_axi_wvalid), .s_wready(m_axi_wready),
    .s_bid(m_axi_bid), .s_bresp(m_axi_bresp), .s_bvalid(m_axi_bvalid), .s_bready(m_axi_bready),
    .s_arid(m_axi_arid), .s_araddr(m_axi_araddr), .s_arlen(m_axi_arlen), .s_arsize(m_axi_arsize),
    .s_arburst(m_axi_arburst), .s_arvalid(m_axi_arvalid), .s_arready(m_axi_arready),
    .s_rid(m_axi_rid), .s_rdata(m_axi_rdata), .s_rresp(m_axi_rresp), .s_rlast(m_axi_rlast), .s_rvalid(m_axi_rvalid),
    .s_rready(m_axi_rready));

  // ---------------- AXI-Lite driver ----------------
  // Convention for every testbench process: outputs are driven (blocking) at negedge,
  // handshakes are sampled SETUP time units after that negedge (i.e. just before the
  // posedge), then the process advances to the next negedge.  Race free under any
  // simulator region ordering.
  localparam int SETUP = 4;

  task automatic axil_write(input logic [HOST_AW-1:0] addr, input logic [31:0] data);
    bit aw_done = 0, w_done = 0, aw_hs, w_hs, b_hs;
    @(negedge clk);
    s_axi_awaddr = addr; s_axi_awvalid = 1'b1;
    s_axi_wdata = data; s_axi_wstrb = 4'hF; s_axi_wvalid = 1'b1;
    s_axi_bready = 1'b1;
    while (!(aw_done && w_done)) begin
      #SETUP;
      aw_hs = s_axi_awvalid && s_axi_awready;
      w_hs  = s_axi_wvalid && s_axi_wready;
      @(negedge clk);
      if (aw_hs) begin aw_done = 1; s_axi_awvalid = 1'b0; end
      if (w_hs)  begin w_done = 1;  s_axi_wvalid = 1'b0; end
    end
    do begin
      #SETUP;
      b_hs = s_axi_bvalid && s_axi_bready;
      @(negedge clk);
    end while (!b_hs);
    s_axi_bready = 1'b0;
  endtask

  task automatic axil_read(input logic [HOST_AW-1:0] addr, output logic [31:0] data);
    bit hs;
    @(negedge clk);
    s_axi_araddr = addr; s_axi_arvalid = 1'b1;
    do begin
      #SETUP;
      hs = s_axi_arvalid && s_axi_arready;
      @(negedge clk);
    end while (!hs);
    s_axi_arvalid = 1'b0; s_axi_rready = 1'b1;
    do begin
      #SETUP;
      hs = s_axi_rvalid && s_axi_rready;
      data = s_axi_rdata;
      @(negedge clk);
    end while (!hs);
    s_axi_rready = 1'b0;
  endtask

  // ---------------- main ----------------
  logic [63:0] prog_img [0:PROG_DEPTH-1];
  string prog_file, sregs_file, hregs_file, csrs_file, cycles_file;
  int timeout_cycles = 2_000_000;
  int nprog = 0;

  initial begin
    logic [31:0] v, st;
    logic [31:0] cyc_lo, cyc_hi, ins_lo, ins_hi;
    int f, waited;
    if ($test$plusargs("vcd")) begin $dumpfile("tb.vcd"); $dumpvars(0, tb_spu); end
    // reset
    repeat (5) @(posedge clk);
    rst_n = 1'b1;
    repeat (3) @(posedge clk);
    $display("TB: reset released"); $fflush();

    // program
    if (!$value$plusargs("prog=%s", prog_file)) prog_file = "prog.hex";
    if ($value$plusargs("timeout=%d", timeout_cycles)) ;
    for (int i = 0; i < PROG_DEPTH; i++) prog_img[i] = 64'hFFFF_FFFF_FFFF_FFFF;   // sentinel (illegal op)
    $readmemh(prog_file, prog_img);
    nprog = 0;
    for (int i = 0; i < PROG_DEPTH; i++) if (prog_img[i] != 64'hFFFF_FFFF_FFFF_FFFF) nprog = i + 1;
    for (int i = 0; i < nprog; i++) begin
      axil_write(HOST_PROG_BASE + HOST_AW'(8 * i),     prog_img[i][31:0]);
      axil_write(HOST_PROG_BASE + HOST_AW'(8 * i + 4), prog_img[i][63:32]);
    end
    $display("TB: loaded %0d instructions", nprog); $fflush();
    // readback check of the last instruction
    if (nprog > 0) begin
      axil_read(HOST_PROG_BASE + HOST_AW'(8 * (nprog - 1)), v);
      if (v !== prog_img[nprog-1][31:0]) $error("program readback mismatch: %h vs %h", v, prog_img[nprog-1][31:0]);
    end
    axil_read(HOST_ID, v);
    if (v !== SPU_ID) $error("bad ID register %h", v);

    // run
    axil_write(HOST_MEM_BASE_LO, 32'd0);
    axil_write(HOST_MEM_BASE_HI, 32'd0);
    axil_write(HOST_PC_START, 32'd0);
    axil_write(HOST_CTRL, 32'(CTRL_START_BIT + 1) | 32'd4);   // START | IRQ_EN
    $display("TB: started"); $fflush();
    waited = 0;
    st = 0;
    do begin
      repeat (50) @(posedge clk);
      waited += 50;
      axil_read(HOST_STATUS, st);
    end while (!st[STATUS_DONE_BIT] && waited < timeout_cycles);
    if (!st[STATUS_DONE_BIT]) begin
      axil_read(HOST_PC, v);
      $display("TB: TIMEOUT after %0d cycles (status=%h pc=%0d core_state=%0d)", waited, st, v, dut.u_core.state);
    end
    else $display("TB: DONE status=%h irq=%0d", st, irq);

    // dump state
    if (!$value$plusargs("sregs=%s", sregs_file)) sregs_file = "sregs.txt";
    if (!$value$plusargs("hregs=%s", hregs_file)) hregs_file = "hregs.txt";
    if (!$value$plusargs("csrs=%s", csrs_file)) csrs_file = "csrs.txt";
    if (!$value$plusargs("cycles=%s", cycles_file)) cycles_file = "cycles.txt";

    f = $fopen(sregs_file, "w");
    for (int i = 0; i < NSREG; i++) begin axil_read(HOST_SREG_BASE + HOST_AW'(4 * i), v); $fwrite(f, "%08x\n", v); end
    $fclose(f);

    f = $fopen(csrs_file, "w");
    for (int i = 0; i < NCSR; i++) begin axil_read(HOST_CSR_BASE + HOST_AW'(4 * i), v); $fwrite(f, "%08x\n", v); end
    $fclose(f);

    f = $fopen(hregs_file, "w");
    for (int r = 0; r < NHREG; r++) begin
      for (int c = C - 1; c >= 0; c--) begin
        axil_write(HOST_HREG_BASE, 32'((r << CH_W) | c));
        for (int w = W / 32 - 1; w >= 0; w--) begin
          axil_read(HOST_HREG_BASE + HOST_AW'(4 + 4 * w), v);
          $fwrite(f, "%08x", v);
        end
      end
      $fwrite(f, "\n");
    end
    $fclose(f);

    axil_read(HOST_CSR_BASE + HOST_AW'(4 * CSR_CYCLES_LO), cyc_lo);
    axil_read(HOST_CSR_BASE + HOST_AW'(4 * CSR_CYCLES_HI), cyc_hi);
    axil_read(HOST_CSR_BASE + HOST_AW'(4 * CSR_INSTRET_LO), ins_lo);
    axil_read(HOST_CSR_BASE + HOST_AW'(4 * CSR_INSTRET_HI), ins_hi);
    f = $fopen(cycles_file, "w");
    $fwrite(f, "cycles %0d\ninstret %0d\nstatus %0d\nirq %0d\n", {cyc_hi, cyc_lo}, {ins_hi, ins_lo}, st, irq);
    $fclose(f);

    // memory
    dump_req = 1'b1;
    @(posedge dump_done);
    $display("TB: cycles=%0d instret=%0d", {cyc_hi, cyc_lo}, {ins_hi, ins_lo});
    $finish;
  end
endmodule
