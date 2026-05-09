module binary_to_one_hot_decoder # (
    parameter BINARY_WIDTH = 5,
    parameter OUTPUT_WIDTH = 32
) (
    input wire [BINARY_WIDTH-1:0] binary_in,
    output reg [OUTPUT_WIDTH-1:0] one_hot_out
);

    // Combinational logic for binary to one-hot decoding
    always @* begin
        // Default to all zeros
        one_hot_out = {OUTPUT_WIDTH{1'b0}};
        
        // Set the corresponding bit to 1 if within valid range
        if (binary_in < OUTPUT_WIDTH) begin
            one_hot_out[binary_in] = 1'b1;
        end
    end

endmodule
