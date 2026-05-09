module cascaded_adder #(
    parameter int unsigned IN_DATA_WIDTH = 16,
    parameter int unsigned IN_DATA_NS    = 4
)(
    input  logic                        clk,
    input  logic                        rst_n,
    input  logic                        i_valid,
    input  logic [IN_DATA_WIDTH*IN_DATA_NS - 1 : 0] i_data,
    output logic                        o_valid,
    output logic [IN_DATA_WIDTH + $clog2(IN_DATA_NS) - 1 : 0] o_data
);

    localparam int unsigned OUT_DATA_WIDTH = IN_DATA_WIDTH + $clog2(IN_DATA_NS);

    // Internal registers for the input data pipeline
    logic [IN_DATA_WIDTH - 1 : 0] reg_inputs [0:IN_DATA_NS-1];
    
    // Cascaded sum chain for combinational processing
    logic [OUT_DATA_WIDTH - 1 : 0] chain_sum [0:IN_DATA_NS-1];
    
    // Output pipeline registers
    logic [OUT_DATA_WIDTH - 1 : 0] o_data_reg;
    logic                        o_valid_reg;

    // Register input data on the positive edge of clk
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int i = 0; i < IN_DATA_NS; i++) begin
                reg_inputs[i] <= '0;
            end
        end else if (i_valid) begin
            for (int i = 0; i < IN_DATA_NS; i++) begin
                reg_inputs[i] <= i_data[i * IN_DATA_WIDTH +: IN_DATA_WIDTH];
            end
        end
    end

    // Combinational cascaded addition
    assign chain_sum[0] = {OUT_DATA_WIDTH{1'b0}} + reg_inputs[0];
    
    genvar i;
    generate
        for (i = 1; i < IN_DATA_NS; i++) begin : add_chain
            assign chain_sum[i] = chain_sum[i-1] + reg_inputs[i];
        end
    endgenerate

    // Register output sum and valid signal
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            o_valid_reg <= 1'b0;
            o_data_reg  <= {(OUT_DATA_WIDTH){1'b0}};
        end else begin
            o_valid_reg <= i_valid;
            o_data_reg  <= chain_sum[IN_DATA_NS - 1];
        end
    end

    assign o_valid = o_valid_reg;
    assign o_data  = o_data_reg;

endmodule
