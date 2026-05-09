`timescale 1ns / 1ps

module axis_joiner (
  input wire                clk,
  input wire                rst,
  
  input wire  [7:0]         s_axis_tdata_1,
  input wire                s_axis_tvalid_1,
  output wire               s_axis_tready_1,
  input wire                s_axis_tlast_1,
  
  input wire  [7:0]         s_axis_tdata_2,
  input wire                s_axis_tvalid_2,
  output wire               s_axis_tready_2,
  input wire                s_axis_tlast_2,
  
  input wire  [7:0]         s_axis_tdata_3,
  input wire                s_axis_tvalid_3,
  output wire               s_axis_tready_3,
  input wire                s_axis_tlast_3,
  
  output wire [7:0]         m_axis_tdata,
  output wire               m_axis_tvalid,
  input  wire               m_axis_tready,
  output wire               m_axis_tlast,
  output wire [1:0]         m_axis_tuser,
  output wire               busy
);

  // Parameters for states
  localparam [1:0] STATE_IDLE = 2'd0;
  localparam [1:0] STATE_1    = 2'd1;
  localparam [1:0] STATE_2    = 2'd2;
  localparam [1:0] STATE_3    = 2'd3;

  // Internal registers
  reg    [1:0]    state;
  reg             temp;
  
  reg    [7:0]    m_axis_tdata_reg;
  reg             m_axis_tlast_reg;
  reg    [1:0]    m_axis_tuser_reg;

  // Mux selection wires
  wire   [7:0]    mux_data;
  wire            mux_tlast;
  wire   [1:0]    mux_user;

  // Mux logic (Combinational)
  always_comb begin
    case (state)
      STATE_IDLE: begin
        if (s_axis_tvalid_1) begin
          mux_data    = s_axis_tdata_1;
          mux_tlast   = s_axis_tlast_1;
          mux_user    = 2'h1;
        end else if (s_axis_tvalid_2) begin
          mux_data    = s_axis_tdata_2;
          mux_tlast   = s_axis_tlast_2;
          mux_user    = 2'h2;
        end else if (s_axis_tvalid_3) begin
          mux_data    = s_axis_tdata_3;
          mux_tlast   = s_axis_tlast_3;
          mux_user    = 2'h3;
        end else begin
          mux_data    = 8'd0;
          mux_tlast   = 1'b0;
          mux_user    = 2'd0;
        end
      end
      STATE_1: begin
        mux_data    = s_axis_tdata_1;
        mux_tlast   = s_axis_tlast_1;
        mux_user    = 2'h1;
      end
      STATE_2: begin
        mux_data    = s_axis_tdata_2;
        mux_tlast   = s_axis_tlast_2;
        mux_user    = 2'h2;
      end
      STATE_3: begin
        mux_data    = s_axis_tdata_3;
        mux_tlast   = s_axis_tlast_3;
        mux_user    = 2'h3;
      end
      default: begin
        mux_data    = 8'd0;
        mux_tlast   = 1'b0;
        mux_user    = 2'd0;
      end
    endcase
  end

  // Ready signals for inputs (Backpressure)
  assign s_axis_tready_1 = (state == STATE_1) ? m_axis_tready : 1'b0;
  assign s_axis_tready_2 = (state == STATE_2) ? m_axis_tready : 1'b0;
  assign s_axis_tready_3 = (state == STATE_3) ? m_axis_tready : 1'b0;

  // Output valid signal
  assign m_axis_tvalid = (state != STATE_IDLE) ? 1'b1 : 1'b0;

  // Status signal
  assign busy = (state != STATE_IDLE) ? 1'b1 : 1'b0;

  // Registered outputs
  assign m_axis_tdata  = m_axis_tdata_reg;
  assign m_axis_tlast  = m_axis_tlast_reg;
  assign m_axis_tuser  = m_axis_tuser_reg;

  // FSM and Buffering Logic
  always_ff @(posedge clk or posedge rst) begin
    if (rst) begin
      state          <= STATE_IDLE;
      m_axis_tdata_reg <= 8'd0;
      m_axis_tlast_reg <= 1'b0;
      m_axis_tuser_reg <= 2'd0;
      temp           <= 1'b0;
    end else begin
      // Update temp flag based on stall condition
      // temp is high when module is processing and output is stalled
      temp <= (state != STATE_IDLE && ~m_axis_tready) ? 1'b1 : 1'b0;

      // Data buffering logic
      if (temp) begin
        // Hold data when stalled
        m_axis_tdata_reg <= m_axis_tdata_reg;
        m_axis_tlast_reg <= m_axis_tlast_reg;
        m_axis_tuser_reg <= m_axis_tuser_reg;
      end else begin
        // Update registers with muxed values when ready or not busy
        m_axis_tdata_reg <= mux_data;
        m_axis_tlast_reg <= mux_tlast;
        m_axis_tuser_reg <= mux_user;
      end

      // State transitions
      case (state)
        STATE_IDLE: begin
          // Priority arbitration: 1 > 2 > 3
          if (s_axis_tvalid_1) begin
            state <= STATE_1;
          end else if (s_axis_tvalid_2) begin
            state <= STATE_2;
          end else if (s_axis_tvalid_3) begin
            state <= STATE_3;
          end else begin
            state <= STATE_IDLE;
          end
        end
        STATE_1: begin
          // Stay in STATE_1 until packet completes from source 1
          // Packet completes when valid, ready, and tlast is asserted on the beat
          if (s_axis_tvalid_1 && m_axis_tready && s_axis_tlast_1) begin
            state <= STATE_IDLE;
          end else begin
            state <= STATE_1;
          end
        end
        STATE_2: begin
          if (s_axis_tvalid_2 && m_axis_tready && s_axis_tlast_2) begin
            state <= STATE_IDLE;
          end else begin
            state <= STATE_2;
          end
        end
        STATE_3: begin
          if (s_axis_tvalid_3 && m_axis_tready && s_axis_tlast_3) begin
            state <= STATE_IDLE;
          end else begin
            state <= STATE_3;
          end
        end
        default: begin
          state <= STATE_IDLE;
        end
      endcase
    end
  end

endmodule
