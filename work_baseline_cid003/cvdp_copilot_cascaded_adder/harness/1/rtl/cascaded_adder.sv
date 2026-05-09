module cascaded_adder #(
    parameter int unsigned IN_DATA_WIDTH = 16,
    parameter int unsigned IN_DATA_NS    = 4
)(
    input  wire                                  clk,
    input  wire                                  rst_n,
    input  wire                                  i_valid,
    input  wire  [IN_DATA_WIDTH * IN_DATA_NS - 1:0] i_data,
    output wire                                  o_valid,
    output wire  [IN_DATA_WIDTH + $clog2(IN_DATA_NS) - 1:0] o_data
);

    localparam int unsigned INPUT_WIDTH  = IN_DATA_WIDTH * IN_DATA_NS;
    localparam int unsigned OUTPUT_WIDTH = IN_DATA_WIDTH + $clog2(IN_DATA_NS);

    logic [INPUT_WIDTH-1:0]    i_data_reg;
    logic                      i_valid_reg;

    // Cycle 1: Register input data and valid signal
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            i_data_reg    <= '0;
            i_valid_reg   <= 1'b0;
        end else if (i_valid) begin
            i_data_reg    <= i_data;
            i_valid_reg   <= 1'b1;
        end else begin
            i_valid_reg   <= 1'b0;
        end
    end

    // Combinational cascaded summation
    logic [OUTPUT_WIDTH-1:0] sum;
    always_comb begin
        sum = '0;
        for (int unsigned i = 0; i < IN_DATA_NS; i++) begin
            // Extract each IN_DATA_WIDTH bit element and add progressively
            sum += i_data_reg[i * IN_DATA_WIDTH +: IN_DATA_WIDTH];
        end
    end

    // Cycle 2: Register output sum and valid signal
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_data  <= '0;
            o_valid <= 1'b0;
        end else if (i_valid_reg) begin
            o_data  <= sum;
            o_valid <= 1'b1;
        end else begin
            o_valid <= 1'b0;
        end
    end

endmodule