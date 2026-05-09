module axis_upscale (
    input  wire        clk,
    input  wire        resetn,
    input  wire        dfmt_enable,
    input  wire        dfmt_type,
    input  wire        dfmt_se,
    input  wire        s_axis_valid,
    input  wire [23:0] s_axis_data,
    input  wire        m_axis_ready,
    output wire        s_axis_ready,
    output reg         m_axis_valid,
    output reg [31:0]  m_axis_data
);

    // AXI-Stream ready handshake: pass through m_axis_ready to s_axis_ready
    assign s_axis_ready = m_axis_ready;

    // Determine the base extension bit based on sign extension enable
    wire base_ext = dfmt_se ? s_axis_data[23] : 1'b0;
    
    // Apply inversion if dfmt_type is asserted
    wire ext_bit  = base_ext ^ dfmt_type;

    // Construct the 32-bit output data
    // When dfmt_enable is high: extend ext_bit to the upper 8 bits [31:24]
    // When dfmt_enable is low: zero-extend the input data (upper 8 bits are 0)
    wire [31:0] data_out = dfmt_enable ? { {8{ext_bit}}, s_axis_data } : { 8'b0, s_axis_data };

    // Single pipeline register stage for output data and valid signal
    always_ff @(posedge clk or negedge resetn) begin
        if (!resetn) begin
            m_axis_data  <= 32'b0;
            m_axis_valid <= 1'b0;
        end else begin
            if (s_axis_valid && m_axis_ready) begin
                m_axis_data  <= data_out;
                m_axis_valid <= 1'b1;
            end else begin
                m_axis_valid <= 1'b0;
            end
        end
    end

endmodule
