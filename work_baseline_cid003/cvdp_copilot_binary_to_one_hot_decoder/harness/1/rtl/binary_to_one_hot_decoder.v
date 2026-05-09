`timescale 1ns / 1ps

module binary_to_one_hot_decoder (
    input  wire [BINARY_WIDTH-1:0] binary_in,
    output wire [OUTPUT_WIDTH-1:0] one_hot_out
);
    parameter BINARY_WIDTH  = 5;
    parameter OUTPUT_WIDTH  = 32;

    // Combinational binary to one-hot decoder
    // If binary_in is within OUTPUT_WIDTH bounds, the corresponding bit is set to 1.
    // If binary_in is out of range (>= OUTPUT_WIDTH), the output is all zeros.
    assign one_hot_out = (binary_in < OUTPUT_WIDTH) ? (1 << binary_in) : '0;

endmodule