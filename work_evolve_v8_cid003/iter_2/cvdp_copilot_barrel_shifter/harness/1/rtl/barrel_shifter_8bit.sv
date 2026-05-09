// rtl/barrel_shifter_8bit.sv
module barrel_shifter_8bit (
    input  logic [7:0] data_in,
    input  logic [2:0] shift_bits,
    input  logic       left_right,
    output logic [7:0] data_out
);

    // Combinational logic implementation for the 8-bit barrel shifter
    // Completes within one clock cycle as requested
    always_comb begin
        if (left_right) begin
            // Shift left: zeros fill LSB positions
            data_out = data_in << shift_bits;
        end else begin
            // Shift right: zeros fill MSB positions
            data_out = data_in >> shift_bits;
        end
    end

endmodule