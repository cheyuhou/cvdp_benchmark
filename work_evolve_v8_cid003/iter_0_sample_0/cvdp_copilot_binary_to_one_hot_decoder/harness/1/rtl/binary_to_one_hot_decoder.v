module binary_to_one_hot_decoder #(
    parameter integer BINARY_WIDTH = 5,
    parameter integer OUTPUT_WIDTH = 32
)(
    input  wire [BINARY_WIDTH-1:0] binary_in,
    output wire [OUTPUT_WIDTH-1:0] one_hot_out
);

    always_comb begin
        one_hot_out = '0;
        if (binary_in < OUTPUT_WIDTH) begin
            one_hot_out[binary_in] = 1'b1;
        end
    end

endmodule
