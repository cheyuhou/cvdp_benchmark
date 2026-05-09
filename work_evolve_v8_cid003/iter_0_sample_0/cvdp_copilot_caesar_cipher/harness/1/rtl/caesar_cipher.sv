module caesar_cipher (
    input  logic [7:0] input_char,
    input  logic [3:0] key,
    output logic [7:0] output_char
);
    always_comb begin
        if (input_char >= 8'h41 && input_char <= 8'h5A) begin
            output_char = (input_char - 8'h41 + key) % 26 + 8'h41;
        end else if (input_char >= 8'h61 && input_char <= 8'h7A) begin
            output_char = (input_char - 8'h61 + key) % 26 + 8'h61;
        end else begin
            output_char = input_char;
        end
    end
endmodule
