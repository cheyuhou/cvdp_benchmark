// File: rtl/axis_joiner.sv
`timescale 1ns / 1ps

module axis_joiner (
    // Clock and Reset
    input  wire                   clk,
    input  wire                   rst,

    // Input Stream 1
    input  wire [7:0]             s_axis_tdata_1,
    input  wire                   s_axis_tvalid_1,
    output wire                   s_axis_tready_1,
    input  wire                   s_axis_tlast_1,

    // Input Stream 2
    input  wire [7:0]             s_axis_tdata_2,
    input  wire                   s_axis_tvalid_2,
    output wire                   s_axis_tready_2,
    input  wire                   s_axis_tlast_2,

    // Input Stream 3
    input  wire [7:0]             s_axis_tdata_3,
    input  wire                   s_axis_tvalid_3,
    output wire                   s_axis_tready_3,
    input  wire                   s_axis_tlast_3,

    // Merged Output Stream
    output wire [7:0]             m_axis_tdata,
    output wire                   m_axis_tvalid,
    input  wire                   m_axis_tready,
    output wire                   m_axis_tlast,
    output wire [1:0]             m_axis_tuser,

    // Status
    output wire                   busy
);

    // FSM State Definitions
    typedef enum logic [1:0] {
        STATE_IDLE = 2'b00,
        STATE_1    = 2'b01,
        STATE_2    = 2'b10,
        STATE_3    = 2'b11
    } state_t;

    state_t curr_state, next_state;
    
    // Internal MUX outputs (combinational)
    logic [7:0] mux_tdata;
    logic mux_tvalid;
    logic mux_tlast;
    logic [1:0] mux_tuser;
    
    // Stalling/Buffering flag
    logic temp;

    // Internal registers for output wires
    logic [7:0] m_axis_tdata_reg;
    logic m_axis_tvalid_reg;
    logic m_axis_tlast_reg;
    logic [1:0] m_axis_tuser_reg;

    // -----------------------------------------------------------------------
    // Finite State Machine: Next State Logic
    // -----------------------------------------------------------------------
    always_comb begin
        case (curr_state)
            STATE_IDLE: begin
                if (s_axis_tvalid_1) next_state = STATE_1;
                else if (s_axis_tvalid_2) next_state = STATE_2;
                else if (s_axis_tvalid_3) next_state = STATE_3;
                else next_state = STATE_IDLE;
            end
            STATE_1: begin
                if (s_axis_tready_1 && s_axis_tlast_1) next_state = STATE_IDLE;
                else next_state = STATE_1;
            end
            STATE_2: begin
                if (s_axis_tready_2 && s_axis_tlast_2) next_state = STATE_IDLE;
                else next_state = STATE_2;
            end
            STATE_3: begin
                if (s_axis_tready_3 && s_axis_tlast_3) next_state = STATE_IDLE;
                else next_state = STATE_3;
            end
            default: next_state = STATE_IDLE;
        endcase
    end

    // -----------------------------------------------------------------------
    // FSM State Register
    // -----------------------------------------------------------------------
    always_ff @(negedge clk or posedge rst) begin
        if (rst) begin
            curr_state <= STATE_IDLE;
        end else begin
            curr_state <= next_state;
        end
    end

    // -----------------------------------------------------------------------
    // Backpressure Handling & Output Register Update
    // Retains data when m_axis_tready is deasserted using 'temp' flag
    // -----------------------------------------------------------------------
    always_ff @(negedge clk or posedge rst) begin
        if (rst) begin
            m_axis_tdata_reg  <= 8'b0;
            m_axis_tvalid_reg <= 1'b0;
            m_axis_tlast_reg  <= 1'b0;
            m_axis_tuser_reg  <= 2'b0;
            temp              <= 1'b0;
        end else begin
            if (m_axis_tready) begin
                // Ready: update outputs and clear stall flag
                m_axis_tdata_reg  <= mux_tdata;
                m_axis_tvalid_reg <= mux_tvalid;
                m_axis_tlast_reg  <= mux_tlast;
                m_axis_tuser_reg  <= mux_tuser;
                temp              <= 1'b0;
            end else begin
                // Not ready: attempt update, set stall flag for next cycle
                if (mux_tvalid) begin
                    m_axis_tdata_reg  <= mux_tdata;
                    m_axis_tvalid_reg <= mux_tvalid;
                    m_axis_tlast_reg  <= mux_tlast;
                    m_axis_tuser_reg  <= mux_tuser;
                    temp              <= 1'b1;
                end else begin
                    m_axis_tvalid_reg <= 1'b0;
                    temp              <= 1'b0;
                end
            end
        end
    end

    // -----------------------------------------------------------------------
    // Multiplexer Assignments (Data, Valid, Last, User)
    // -----------------------------------------------------------------------
    assign mux_tdata = (curr_state == STATE_1) ? s_axis_tdata_1 :
                       (curr_state == STATE_2) ? s_axis_tdata_2 :
                       (curr_state == STATE_3) ? s_axis_tdata_3 : 8'b0;

    assign mux_tvalid = (curr_state == STATE_1) ? s_axis_tvalid_1 :
                        (curr_state == STATE_2) ? s_axis_tvalid_2 :
                        (curr_state == STATE_3) ? s_axis_tvalid_3 : 1'b0;

    assign mux_tlast = (curr_state == STATE_1) ? s_axis_tlast_1 :
                       (curr_state == STATE_2) ? s_axis_tlast_2 :
                       (curr_state == STATE_3) ? s_axis_tlast_3 : 1'b0;

    assign mux_tuser = (curr_state == STATE_1) ? 2'h1 :
                       (curr_state == STATE_2) ? 2'h2 :
                       (curr_state == STATE_3) ? 2'h3 : 2'h0;

    // -----------------------------------------------------------------------
    // Input Ready Signals (Backpressure to sources)
    // -----------------------------------------------------------------------
    assign s_axis_tready_1 = (curr_state == STATE_1) ? 1'b1 : 1'b0;
    assign s_axis_tready_2 = (curr_state == STATE_2) ? 1'b1 : 1'b0;
    assign s_axis_tready_3 = (curr_state == STATE_3) ? 1'b1 : 1'b0;

    // -----------------------------------------------------------------------
    // Busy Indicator
    // -----------------------------------------------------------------------
    assign busy = (curr_state != STATE_IDLE);

    // -----------------------------------------------------------------------
    // Output Wire Assignments
    // -----------------------------------------------------------------------
    assign m_axis_tdata  = m_axis_tdata_reg;
    assign m_axis_tvalid = m_axis_tvalid_reg;
    assign m_axis_tlast  = m_axis_tlast_reg;
    assign m_axis_tuser  = m_axis_tuser_reg;

endmodule