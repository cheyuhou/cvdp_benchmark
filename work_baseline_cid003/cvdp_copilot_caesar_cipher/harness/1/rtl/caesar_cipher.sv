module caesar_cipher (
    input  wire [7:0] input_char,
    input  wire [3:0] key,
    output wire [7:0] output_char
);

    localparam [7:0] A = 8'h41;
    localparam [7:0] Z = 8'h5A;
    localparam [7:0] a = 8'h61;
    localparam [7:0] z = 8'h7A;

    logic [7:0] shifted_upper;
    logic [7:0] shifted_lower;
    logic is_upper;
    logic is_lower;

    assign is_upper = (input_char >= A) && (input_char <= Z);
    assign is_lower = (input_char >= a) && (input_char <= z);

    assign shifted_upper = ((input_char - A + key) % 26) + A;
    assign shifted_lower = ((input_char - a + key) % 26) + a;

    assign output_char = is_upper ? shifted_upper : 
                         is_lower ? shifted_lower : 
                         input_char;

endmodule
