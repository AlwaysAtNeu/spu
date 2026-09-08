// SPU top level: AXI4-Lite control slave + AXI4 memory master.
// Port naming follows the Vivado convention so `package_project` infers the interfaces.
module spu_top import spu_pkg::*; (
  input  logic                    clk,
  input  logic                    rst_n,
  output logic                    irq,
  // AXI4-Lite slave (control)
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
  // AXI4 master (memory)
  output logic [3:0]              m_axi_awid,
  output logic [63:0]             m_axi_awaddr,
  output logic [7:0]              m_axi_awlen,
  output logic [2:0]              m_axi_awsize,
  output logic [1:0]              m_axi_awburst,
  output logic                    m_axi_awvalid,
  input  logic                    m_axi_awready,
  output logic [W-1:0]            m_axi_wdata,
  output logic [W/8-1:0]          m_axi_wstrb,
  output logic                    m_axi_wlast,
  output logic                    m_axi_wvalid,
  input  logic                    m_axi_wready,
  input  logic [3:0]              m_axi_bid,
  input  logic [1:0]              m_axi_bresp,
  input  logic                    m_axi_bvalid,
  output logic                    m_axi_bready,
  output logic [3:0]              m_axi_arid,
  output logic [63:0]             m_axi_araddr,
  output logic [7:0]              m_axi_arlen,
  output logic [2:0]              m_axi_arsize,
  output logic [1:0]              m_axi_arburst,
  output logic                    m_axi_arvalid,
  input  logic                    m_axi_arready,
  input  logic [3:0]              m_axi_rid,
  input  logic [W-1:0]            m_axi_rdata,
  input  logic [1:0]              m_axi_rresp,
  input  logic                    m_axi_rlast,
  input  logic                    m_axi_rvalid,
  output logic                    m_axi_rready
);
  logic        start, soft_reset, busy, done_pulse, err_flag, host_csr_we;
  logic [31:0] pc_start, pc, host_csr_wdata;
  logic [63:0] mem_base;
  logic [4:0]  host_csr_idx;
  logic [NCSR-1:0][31:0]  csr_v;
  logic [NSREG-1:0][31:0] sreg_v;
  logic [RF_AW-1:0] dbg_addr;
  logic [W-1:0]     dbg_data;
  logic [$clog2(PROG_DEPTH)-1:0] prog_addr;
  logic [63:0]      prog_rdata;
  logic             core_rst_n;

  always_ff @(posedge clk) core_rst_n <= rst_n && !soft_reset;

  spu_csr u_csr (
    .clk(clk), .rst_n(rst_n),
    .s_axi_awaddr(s_axi_awaddr), .s_axi_awvalid(s_axi_awvalid), .s_axi_awready(s_axi_awready),
    .s_axi_wdata(s_axi_wdata), .s_axi_wstrb(s_axi_wstrb), .s_axi_wvalid(s_axi_wvalid), .s_axi_wready(s_axi_wready),
    .s_axi_bresp(s_axi_bresp), .s_axi_bvalid(s_axi_bvalid), .s_axi_bready(s_axi_bready),
    .s_axi_araddr(s_axi_araddr), .s_axi_arvalid(s_axi_arvalid), .s_axi_arready(s_axi_arready),
    .s_axi_rdata(s_axi_rdata), .s_axi_rresp(s_axi_rresp), .s_axi_rvalid(s_axi_rvalid), .s_axi_rready(s_axi_rready),
    .start(start), .soft_reset(soft_reset), .pc_start(pc_start), .mem_base(mem_base),
    .busy(busy), .done_pulse(done_pulse), .err_flag(err_flag), .pc(pc),
    .host_csr_we(host_csr_we), .host_csr_idx(host_csr_idx), .host_csr_wdata(host_csr_wdata),
    .csr_i(csr_v), .sreg_i(sreg_v), .dbg_addr(dbg_addr), .dbg_data(dbg_data),
    .prog_addr(prog_addr), .prog_rdata(prog_rdata), .irq(irq));

  spu_core u_core (
    .clk(clk), .rst_n(core_rst_n),
    .start(start), .pc_start(pc_start), .mem_base(mem_base),
    .busy(busy), .done_pulse(done_pulse), .err_flag(err_flag), .pc_out(pc),
    .host_csr_we(host_csr_we), .host_csr_idx(host_csr_idx), .host_csr_wdata(host_csr_wdata),
    .csr_o(csr_v), .sreg_o(sreg_v), .dbg_addr(dbg_addr), .dbg_data(dbg_data),
    .prog_addr(prog_addr), .prog_rdata(prog_rdata),
    .m_axi_awid(m_axi_awid), .m_axi_awaddr(m_axi_awaddr), .m_axi_awlen(m_axi_awlen), .m_axi_awsize(m_axi_awsize),
    .m_axi_awburst(m_axi_awburst), .m_axi_awvalid(m_axi_awvalid), .m_axi_awready(m_axi_awready),
    .m_axi_wdata(m_axi_wdata), .m_axi_wstrb(m_axi_wstrb), .m_axi_wlast(m_axi_wlast), .m_axi_wvalid(m_axi_wvalid),
    .m_axi_wready(m_axi_wready), .m_axi_bid(m_axi_bid), .m_axi_bresp(m_axi_bresp), .m_axi_bvalid(m_axi_bvalid),
    .m_axi_bready(m_axi_bready), .m_axi_arid(m_axi_arid), .m_axi_araddr(m_axi_araddr), .m_axi_arlen(m_axi_arlen),
    .m_axi_arsize(m_axi_arsize), .m_axi_arburst(m_axi_arburst), .m_axi_arvalid(m_axi_arvalid),
    .m_axi_arready(m_axi_arready), .m_axi_rid(m_axi_rid), .m_axi_rdata(m_axi_rdata), .m_axi_rresp(m_axi_rresp),
    .m_axi_rlast(m_axi_rlast), .m_axi_rvalid(m_axi_rvalid), .m_axi_rready(m_axi_rready));
endmodule
