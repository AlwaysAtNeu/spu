// Hypervector register file: NHREG registers x C chunks of W bits.
// Two identical BRAM copies give two independent read ports; both copies are
// written together.  Read latency: 1 cycle (address registered in the BRAM).
module spu_hvrf import spu_pkg::*; (
  input  logic              clk,
  input  logic              ra_en,
  input  logic [RF_AW-1:0]  ra_addr,
  output logic [W-1:0]      ra_data,
  input  logic              rb_en,
  input  logic [RF_AW-1:0]  rb_addr,
  output logic [W-1:0]      rb_data,
  input  logic              we,
  input  logic [RF_AW-1:0]  w_addr,
  input  logic [W-1:0]      w_data
);
  (* ram_style = "block" *) logic [W-1:0] mem0 [0:NHREG*C-1];
  (* ram_style = "block" *) logic [W-1:0] mem1 [0:NHREG*C-1];

  always_ff @(posedge clk) begin
    if (we) begin
      mem0[w_addr] <= w_data;
      mem1[w_addr] <= w_data;
    end
    if (ra_en) ra_data <= mem0[ra_addr];
    if (rb_en) rb_data <= mem1[rb_addr];
  end
endmodule
