module axis_upscale (
    input  logic clk,
    input  logic resetn,
    input  logic dfmt_enable,
    input  logic dfmt_type,
    input  logic dfmt_se,
    input  logic s_axis_valid,
    input  logic [23:0] s_axis_data,
    input  logic m_axis_ready,
    output logic s_axis_ready,
    output logic m_axis_valid,
    output logic [31:0] m_axis_data
);

    logic fill_bit;
    logic [31:0] m_axis_data_d;
    logic m_axis_valid_d;

    // AXI Stream Handshake: Pass ready signal upstream
    assign s_axis_ready = m_axis_ready;

    // Determine padding bit based on data format signals
    always_comb begin
        if (!dfmt_enable) begin
            // When data format is disabled, pad with zeros
            fill_bit = 1'b0;
        end else if (dfmt_se) begin
            // Sign extension enabled: carry forward MSB or its inverted version
            fill_bit = dfmt_type ? ~s_axis_data[23] : s_axis_data[23];
        end else begin
            // Sign extension disabled: pad with zeros
            fill_bit = 1'b0;
        end
    end

    // Upscale 24-bit input to 32-bit output by concatenating 8 padding bits
    always_comb begin
        m_axis_data_d = {fill_bit, fill_bit, fill_bit, fill_bit, fill_bit, fill_bit, fill_bit, fill_bit, s_axis_data};
    end

    // Valid signal passes through combinational path
    always_comb begin
        m_axis_valid_d = s_axis_valid;
    end

    // Single pipeline register stage with active-low synchronous reset
    always @(posedge clk) begin
        if (!resetn) begin
            m_axis_data <= 32'b0;
            m_axis_valid <= 1'b0;
        end else begin
            m_axis_data <= m_axis_data_d;
            m_axis_valid <= m_axis_valid_d;
        end
    end

endmodule