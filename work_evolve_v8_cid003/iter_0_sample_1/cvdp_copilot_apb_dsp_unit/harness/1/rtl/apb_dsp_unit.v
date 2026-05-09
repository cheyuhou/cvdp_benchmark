module apb_dsp_unit (
    input wire pclk,
    input wire presetn,
    input wire [9:0] paddr,
    input wire pselx,
    input wire penable,
    input wire pwrite,
    input wire [7:0] pwdata,
    output reg pready,
    output reg [7:0] prdata,
    output reg pslverr,
    output wire sram_valid
);

    // Internal Configuration Registers
    reg [7:0] r_operand_1;
    reg [7:0] r_operand_2;
    reg [7:0] r_Enable;
    reg [7:0] r_write_address;
    reg [7:0] r_write_data;
    reg [7:0] r_result;

    // Local Parameters for State Machine
    localparam IDLE = 2'b00;
    localparam READ_STATE = 2'b01;
    localparam WRITE_STATE = 2'b10;
    
    reg [1:0] state, next_state;

    // SRAM valid signal is asserted when DSP is in Data Writing mode (3)
    assign sram_valid = (r_Enable == 3);

    // APB Protocol Control FSM
    always @(posedge pclk or negedge presetn) begin
        if (!presetn) begin
            // Reset all outputs and internal registers to their default values
            state <= IDLE;
            prdata <= 8'b0;
            pready <= 1'b1;
            pslverr <= 1'b0;
            r_operand_1 <= 8'b0;
            r_operand_2 <= 8'b0;
            r_Enable <= 8'b0;
            r_write_address <= 8'b0;
            r_write_data <= 8'b0;
            r_result <= 8'b0;
        end else begin
            state <= next_state;
            
            // PREADY is always driven high as per specification (no wait states supported)
            
            case (state)
                IDLE: begin
                    pslverr <= 1'b0;
                    if (pselx) begin
                        if (pwrite)
                            next_state <= WRITE_STATE;
                        else
                            next_state <= READ_STATE;
                    end else begin
                        next_state <= IDLE;
                    end
                end
                READ_STATE: begin
                    // Drive prdata with the register value corresponding to paddr
                    case (paddr[2:0])
                        3'd0: prdata <= r_operand_1;
                        3'd1: prdata <= r_operand_2;
                        3'd2: prdata <= r_Enable;
                        3'd3: prdata <= r_write_address;
                        3'd4: prdata <= r_write_data;
                        3'd5: prdata <= r_result;
                        default: begin 
                            prdata <= 8'b0; 
                            pslverr <= 1'b1; 
                        end
                    endcase
                    next_state <= IDLE;
                end
                WRITE_STATE: begin
                    // Update the register selected by paddr with pwdata
                    case (paddr[2:0])
                        3'd0: r_operand_1 <= pwdata;
                        3'd1: r_operand_2 <= pwdata;
                        3'd2: r_Enable <= pwdata;
                        3'd3: r_write_address <= pwdata;
                        3'd4: r_write_data <= pwdata;
                        default: pslverr <= 1'b1;
                    endcase
                    next_state <= IDLE;
                end
                default: next_state <= IDLE;
            endcase
        end
    end

    // DSP Arithmetic Logic
    // Updates r_result based on r_Enable mode
    always @(posedge pclk) begin
        case (r_Enable)
            2'd1: r_result <= r_operand_1 + r_operand_2; // Addition mode
            2'd2: r_result <= r_operand_1 * r_operand_2; // Multiplication mode
            3'd3: r_result <= 8'b0;                      // Data Writing mode (no arithmetic)
            default: r_result <= 8'b0;                   // Disabled mode
        endcase
    end

endmodule
