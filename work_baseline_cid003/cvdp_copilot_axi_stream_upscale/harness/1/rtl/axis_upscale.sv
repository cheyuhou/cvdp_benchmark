module axis_upscale (
    input  logic        clk,
    input  logic        resetn,
    input  logic        dfmt_enable,
    input  logic        dfmt_type,
    input  logic        dfmt_se,
    input  logic        s_axis_valid,
    input  logic [23:0] s_axis_data,
    input  logic        m_axis_ready,
    output logic        s_axis_ready,
    output logic        m_axis_valid,
    output logic [31:0] m_axis_data
);

    logic [7:0] ext_bits;
    logic       sel_bit;

    // Determine the extension bit based on data format control signals
    always_comb begin
        if (!dfmt_enable) begin
            sel_bit = 1'b0;
        end else if (dfmt_se) begin
            sel_bit = s_axis_data[23]; // Sign extension (23rd bit)
        end else if (dfmt_type) begin
            sel_bit = ~s_axis_data[23]; // Inverted MSB carry forward
        end else begin
            sel_bit = 1'b0; // Zero extension
        end
        ext_bits = {8{sel_bit}};
    end

    // AXI Stream handshaking signals
    assign s_axis_ready = m_axis_ready;
    assign m_axis_valid = s_axis_valid;

    // Single pipeline register stage for output data with active-low synchronous reset
    always_ff @(posedge clk) begin
        if (!resetn) begin
            m_axis_data <= 32'b0;
        end else begin
            m_axis_data <= {ext_bits, s_axis_data};
        end
    end

endmodule