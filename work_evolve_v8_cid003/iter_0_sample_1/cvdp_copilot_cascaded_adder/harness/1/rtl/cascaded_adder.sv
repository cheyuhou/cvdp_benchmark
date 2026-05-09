module cascaded_adder #(
    parameter int IN_DATA_WIDTH = 16,
    parameter int IN_DATA_NS    = 4
) (
    input  logic                                  clk,
    input  logic                                  rst_n,
    input  logic                                  i_valid,
    input  logic [IN_DATA_WIDTH * IN_DATA_NS - 1 : 0] i_data,
    output logic                                  o_valid,
    output logic [IN_DATA_WIDTH + $clog2(IN_DATA_NS) - 1 : 0] o_data
);

    localparam int OUT_WIDTH = IN_DATA_WIDTH + $clog2(IN_DATA_NS);

    logic [IN_DATA_WIDTH * IN_DATA_NS - 1 : 0] i_data_reg;
    logic i_valid_reg;

    // Register input data and valid signal on positive clock edge
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            i_data_reg <= '0;
            i_valid_reg <= 1'b0;
        end else begin
            i_data_reg <= i_valid ? i_data : i_data_reg;
            i_valid_reg <= i_valid;
        end
    end

    // Cascaded combinational addition of all input elements
    logic [OUT_WIDTH - 1 : 0] sum_comb;
    always_comb begin
        sum_comb = '0;
        for (int k = 0; k < IN_DATA_NS; k++) begin
            sum_comb += i_data_reg[k * IN_DATA_WIDTH +: IN_DATA_WIDTH];
        end
    end

    // Register output sum and valid signal
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_data <= '0;
            o_valid <= 1'b0;
        end else begin
            o_data <= sum_comb;
            o_valid <= i_valid_reg;
        end
    end

endmodule
