// Pipelined population count, fixed latency 4.
// N must be a multiple of 128: N/8 4-bit counts -> N/32 6-bit -> N/128 8-bit -> final.
module spu_popcount #(
  parameter int N = 512,
  localparam int OW = $clog2(N + 1)
) (
  input  logic          clk,
  input  logic [N-1:0]  din,
  output logic [OW-1:0] dout
);
  localparam int G1 = N / 8;     // groups of 8 bits
  localparam int G2 = N / 32;
  localparam int G3 = N / 128;

  function automatic logic [3:0] pc8(input logic [7:0] b);
    pc8 = 4'(b[0]) + 4'(b[1]) + 4'(b[2]) + 4'(b[3]) + 4'(b[4]) + 4'(b[5]) + 4'(b[6]) + 4'(b[7]);
  endfunction

  logic [G1-1:0][3:0] s1;
  logic [G2-1:0][5:0] s2;
  logic [G3-1:0][7:0] s3;
  logic [OW-1:0]      s4;

  always_ff @(posedge clk) begin
    for (int i = 0; i < G1; i++) s1[i] <= pc8(din[i*8 +: 8]);
    for (int i = 0; i < G2; i++) s2[i] <= 6'(s1[4*i]) + 6'(s1[4*i+1]) + 6'(s1[4*i+2]) + 6'(s1[4*i+3]);
    for (int i = 0; i < G3; i++) s3[i] <= 8'(s2[4*i]) + 8'(s2[4*i+1]) + 8'(s2[4*i+2]) + 8'(s2[4*i+3]);
    begin
      logic [OW-1:0] t;
      t = '0;
      for (int i = 0; i < G3; i++) t = t + OW'(s3[i]);
      s4 <= t;
    end
  end
  assign dout = s4;
endmodule
