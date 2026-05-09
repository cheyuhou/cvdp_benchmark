module axis_joiner (
    input  logic                    clk,
    input  logic                    rst,
    // Stream 1
    input  logic [7:0]              s_axis_tdata_1,
    input  logic                    s_axis_tvalid_1,
    output logic                    s_axis_tready_1,
    input  logic                    s_axis_tlast_1,
    // Stream 2
    input  logic [7:0]              s_axis_tdata_2,
    input  logic                    s_axis_tvalid_2,
    output logic                    s_axis_tready_2,
    input  logic                    s_axis_tlast_2,
    // Stream 3
    input  logic [7:0]              s_axis_tdata_3,
    input  logic                    s_axis_tvalid_3,
    output logic                    s_axis_tready_3,
    input  logic                    s_axis_tlast_3,
    // Merged Output
    output logic [7:0]              m_axis_tdata,
    output logic                    m_axis_tvalid,
    input  logic                    m_axis_tready,
    output logic                    m_axis_tlast,
    output logic [1:0]              m_axis_tuser,
    // Status
    output logic                    busy
);

    // FSM State Definitions
    typedef enum logic [1:0] {
        STATE_IDLE = 2'b00,
        STATE_1    = 2'b01,
        STATE_2    = 2'b10,
        STATE_3    = 2'b11
    } state_t;

    state_t state, next_state;

    // Internal output registers for seamless buffering
    logic [7:0] m_tdata_reg;
    logic       m_tlast_reg;
    logic [1:0] m_tuser_reg;
    logic       temp; // Stall/hold indicator flag

    // Combinational MUX outputs based on current state
    logic [7:0] muxed_data;
    logic       muxed_last;
    logic [1:0] muxed_user;

    // Data Selection MUX
    always_comb begin
        case (state)
            STATE_IDLE: begin
                muxed_data  = 8'b0;
                muxed_last  = 1'b0;
                muxed_user  = 2'b00;
            end
            STATE_1: begin
                muxed_data  = s_axis_tdata_1;
                muxed_last  = s_axis_tlast_1;
                muxed_user  = 2'b01; // TAG_ID_1 = 0x1
            end
            STATE_2: begin
                muxed_data  = s_axis_tdata_2;
                muxed_last  = s_axis_tlast_2;
                muxed_user  = 2'b10; // TAG_ID_2 = 0x2
            end
            STATE_3: begin
                muxed_data  = s_axis_tdata_3;
                muxed_last  = s_axis_tlast_3;
                muxed_user  = 2'b11; // TAG_ID_3 = 0x3
            end
            default: begin
                muxed_data  = 8'b0;
                muxed_last  = 1'b0;
                muxed_user  = 2'b00;
            end
        endcase
    end

    // Output Register Updates & Buffering Logic
    // Retains data when m_axis_tready is deasserted (temp flag asserted)
    always_ff @(posedge clk or posedge rst) begin
        if (rst) begin
            m_tdata_reg   <= 8'b0;
            m_tlast_reg   <= 1'b0;
            m_tuser_reg   <= 2'b00;
            temp          <= 1'b0;
        end else begin
            if (m_axis_tready) begin
                // Downstream ready: update registers with current stream data
                m_tdata_reg   <= muxed_data;
                m_tlast_reg   <= muxed_last;
                m_tuser_reg   <= muxed_user;
                temp          <= 1'b0; // Clear stall flag on successful transfer
            end else if (state != STATE_IDLE) begin
                // Downstream stalled: hold previous data, assert temp flag
                temp          <= 1'b1;
            end
        end
    end

    // Assign Merged Output Ports
    assign m_axis_tdata  = m_tdata_reg;
    assign m_axis_tlast  = m_tlast_reg;
    assign m_axis_tuser  = m_tuser_reg;
    assign m_axis_tvalid = (state != STATE_IDLE);

    // Input Ready Signals: Only acknowledge the active stream and only if downstream is ready
    assign s_axis_tready_1 = (state == STATE_1) && m_axis_tready;
    assign s_axis_tready_2 = (state == STATE_2) && m_axis_tready;
    assign s_axis_tready_3 = (state == STATE_3) && m_axis_tready;

    // Busy Status Indicator
    assign busy = (state != STATE_IDLE);

    // FSM State Flip-Flops
    always_ff @(posedge clk or posedge rst) begin
        if (rst)
            state <= STATE_IDLE;
        else
            state <= next_state;
    end

    // Next State Logic (Priority: 1 > 2 > 3)
    always_comb begin
        next_state = state;
        case (state)
            STATE_IDLE: begin
                if (s_axis_tvalid_1)
                    next_state = STATE_1;
                else if (s_axis_tvalid_2)
                    next_state = STATE_2;
                else if (s_axis_tvalid_3)
                    next_state = STATE_3;
            end
            STATE_1: begin
                if (m_axis_tready && s_axis_tlast_1)
                    next_state = STATE_IDLE;
            end
            STATE_2: begin
                if (m_axis_tready && s_axis_tlast_2)
                    next_state = STATE_IDLE;
            end
            STATE_3: begin
                if (m_axis_tready && s_axis_tlast_3)
                    next_state = STATE_IDLE;
            end
            default:
                next_state = STATE_IDLE;
        endcase
    end

endmodule
