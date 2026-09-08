// Behavioural AXI4 slave memory (W-bit data) for simulation only.
//  * MEM_WORDS x 64-byte words, loaded from +mem=<file> ($readmemh, @addr in 64-byte words)
//  * random ready back-pressure and read latency, in-order responses, bursts, byte strobes
//  * dump_req -> writes every touched / non-zero word to +mem_out=<file> in the same format
module axi_mem_model import spu_pkg::*; #(
  parameter int MEM_WORDS = 1 << 18          // 16 MiB
) (
  input  logic              clk,
  input  logic              rst_n,
  input  logic              dump_req,
  output logic              dump_done,
  input  logic [3:0]        s_awid,
  input  logic [63:0]       s_awaddr,
  input  logic [7:0]        s_awlen,
  input  logic [2:0]        s_awsize,
  input  logic [1:0]        s_awburst,
  input  logic              s_awvalid,
  output logic              s_awready,
  input  logic [W-1:0]      s_wdata,
  input  logic [W/8-1:0]    s_wstrb,
  input  logic              s_wlast,
  input  logic              s_wvalid,
  output logic              s_wready,
  output logic [3:0]        s_bid,
  output logic [1:0]        s_bresp,
  output logic              s_bvalid,
  input  logic              s_bready,
  input  logic [3:0]        s_arid,
  input  logic [63:0]       s_araddr,
  input  logic [7:0]        s_arlen,
  input  logic [2:0]        s_arsize,
  input  logic [1:0]        s_arburst,
  input  logic              s_arvalid,
  output logic              s_arready,
  output logic [3:0]        s_rid,
  output logic [W-1:0]      s_rdata,
  output logic [1:0]        s_rresp,
  output logic              s_rlast,
  output logic              s_rvalid,
  input  logic              s_rready
);
  localparam int BS = $clog2(W / 8);

  logic [W-1:0] mem [0:MEM_WORDS-1];
  bit           touched [0:MEM_WORDS-1];

  int backpressure = 30;   // percent of cycles a ready is withheld
  int min_lat = 4, max_lat = 24;

  // ------------------------------------------------------------ load / dump
  string mem_file, mem_out_file;
  initial begin
    for (int i = 0; i < MEM_WORDS; i++) begin mem[i] = '0; touched[i] = 1'b0; end
    if ($value$plusargs("mem=%s", mem_file)) begin
      $readmemh(mem_file, mem);
      for (int i = 0; i < MEM_WORDS; i++) if (mem[i] != '0) touched[i] = 1'b1;
    end
    if ($value$plusargs("backpressure=%d", backpressure)) ;
    if ($value$plusargs("max_lat=%d", max_lat)) ;
  end

  initial begin
    int f;
    dump_done = 1'b0;
    @(posedge dump_req);
    if ($value$plusargs("mem_out=%s", mem_out_file)) begin
      f = $fopen(mem_out_file, "w");
      for (int i = 0; i < MEM_WORDS; i++) begin
        if (touched[i] || mem[i] != '0) begin
          $fwrite(f, "@%x\n%h\n", i, mem[i]);
        end
      end
      $fclose(f);
    end
    dump_done = 1'b1;
  end

  function automatic bit rnd_ready();
    rnd_ready = ($urandom_range(0, 99) >= backpressure);
  endfunction

  // ------------------------------------------------------------ write channel
  // Every process drives its outputs (blocking) at negedge and samples handshakes
  // SETUP time units later (just before the posedge): race free under any region ordering.
  localparam int SETUP = 4;
  typedef struct { logic [63:0] addr; int len; } req_t;
  req_t wq[$];
  req_t rq[$];

  initial begin
    bit hs;
    req_t r;
    s_awready = 1'b0;
    @(negedge clk);
    forever begin
      #SETUP;
      hs = s_awvalid && s_awready;
      r.addr = s_awaddr; r.len = int'(s_awlen) + 1;
      if (hs && s_awsize != 3'(BS)) $error("axi_mem_model: unsupported awsize %0d", s_awsize);
      if (hs && s_awburst != 2'b01) $error("axi_mem_model: unsupported awburst %0d", s_awburst);
      @(negedge clk);
      if (hs) wq.push_back(r);
      s_awready = rnd_ready() && (wq.size() < 4);
    end
  end

  initial begin
    bit hs, last;
    logic [W-1:0] wdata;
    logic [W/8-1:0] wstrb;
    s_wready = 1'b0; s_bvalid = 1'b0; s_bid = '0; s_bresp = 2'b00;
    @(negedge clk);
    forever begin
      req_t r;
      int beats;
      while (wq.size() == 0) @(negedge clk);
      r = wq.pop_front();
      beats = 0;
      s_wready = rnd_ready();
      while (beats < r.len) begin
        #SETUP;
        hs = s_wvalid && s_wready; wdata = s_wdata; wstrb = s_wstrb; last = s_wlast;
        @(negedge clk);
        if (hs) begin
          logic [63:0] a;
          int idx;
          a   = r.addr + 64'(beats) * 64'(W / 8);
          idx = int'(a >> BS);
          if (idx >= MEM_WORDS) $error("axi_mem_model: write address 0x%h out of range", a);
          else begin
            for (int b = 0; b < W / 8; b++) if (wstrb[b]) mem[idx][b*8 +: 8] = wdata[b*8 +: 8];
            touched[idx] = 1'b1;
          end
          beats++;
          if (beats == r.len && !last) $error("axi_mem_model: wlast missing");
        end
        s_wready = rnd_ready() && (beats < r.len);
      end
      s_wready = 1'b0;
      repeat ($urandom_range(1, 6)) @(negedge clk);
      s_bvalid = 1'b1;
      do begin
        #SETUP;
        hs = s_bvalid && s_bready;
        @(negedge clk);
      end while (!hs);
      s_bvalid = 1'b0;
    end
  end

  // ------------------------------------------------------------ read channel
  initial begin
    bit hs;
    req_t r;
    s_arready = 1'b0;
    @(negedge clk);
    forever begin
      #SETUP;
      hs = s_arvalid && s_arready;
      r.addr = s_araddr; r.len = int'(s_arlen) + 1;
      if (hs && s_arsize != 3'(BS)) $error("axi_mem_model: unsupported arsize %0d", s_arsize);
      if (hs && s_arburst != 2'b01) $error("axi_mem_model: unsupported arburst %0d", s_arburst);
      @(negedge clk);
      if (hs) rq.push_back(r);
      s_arready = rnd_ready() && (rq.size() < 8);
    end
  end

  initial begin
    bit hs;
    s_rvalid = 1'b0; s_rlast = 1'b0; s_rdata = '0; s_rid = '0; s_rresp = 2'b00;
    @(negedge clk);
    forever begin
      req_t r;
      while (rq.size() == 0) @(negedge clk);
      r = rq.pop_front();
      repeat ($urandom_range(min_lat, max_lat)) @(negedge clk);
      for (int beats = 0; beats < r.len; beats++) begin
        logic [63:0] a;
        int idx;
        a   = r.addr + 64'(beats) * 64'(W / 8);
        idx = int'(a >> BS);
        if (idx >= MEM_WORDS) begin
          $error("axi_mem_model: read address 0x%h out of range", a);
          s_rdata = '0;
        end else s_rdata = mem[idx];
        s_rlast  = (beats == r.len - 1);
        s_rvalid = 1'b1;
        do begin
          #SETUP;
          hs = s_rvalid && s_rready;
          @(negedge clk);
        end while (!hs);
        s_rvalid = 1'b0;
        if (rnd_ready() == 1'b0) @(negedge clk);   // occasional bubble between beats
      end
    end
  end

  logic unused;
  assign unused = ^{s_awid, s_arid, rst_n};
endmodule
