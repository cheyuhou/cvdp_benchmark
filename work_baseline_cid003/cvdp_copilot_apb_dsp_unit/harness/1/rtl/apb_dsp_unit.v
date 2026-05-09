`timescale 1ns / 1ps

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
    output reg sram_valid
);

localparam IDLE = 2'b00,
           READ_STATE = 2'b01,
           WRITE_STATE = 2'b10;

reg [1:0] pstate;
reg [2:0] addr_reg;

// Internal CSR registers
reg [7:0] r_operand_1;
reg [7:0] r_operand_2;
reg [1:0] r_Enable;
reg [7:0] r_write_address;
reg [7:0] r_write_data;
reg [7:0] result_reg;

// Address decoding for CSR space (0x00 to 0x05)
wire valid_addr = (paddr[2:0] >= 3'd0) && (paddr[2:0] <= 3'd5);

// Combinational DSP result computation
always @(*) begin
    case (r_Enable)
        2'd1: result_reg = r_operand_1 + r_operand_2;
        2'd2: result_reg = r_operand_1 * r_operand_2;
        default: result_reg = 8'd0;
    endcase
end

// Main APB State Machine & Register Logic
always @(posedge pclk or negedge presetn) begin
    if (!presetn) begin
        pstate          <= IDLE;
        addr_reg        <= 3'd0;
        prdata          <= 8'd0;
        pslverr         <= 1'b0;
        sram_valid      <= 1'b0;
        pready          <= 1'b1;
        r_operand_1     <= 8'd0;
        r_operand_2     <= 8'd0;
        r_Enable        <= 2'd0;
        r_write_address <= 8'd0;
        r_write_data    <= 8'd0;
    end else begin
        addr_reg  <= paddr[2:0];
        pready    <= 1'b1;
        sram_valid<= 1'b0;

        // APB Phase 1: Selection & Address Decoding
        if (pselx & !penable) begin
            pslverr <= valid_addr ? 1'b0 : 1'b1;
            if (pwrite) begin
                pstate <= WRITE_STATE;
            end else begin
                pstate <= READ_STATE;
            end
        end
        // APB Phase 2: Enable & Data Transfer
        else if (pselx & penable) begin
            if (pstate == READ_STATE) begin
                case (addr_reg)
                    3'd0: prdata <= r_operand_1;
                    3'd1: prdata <= r_operand_2;
                    3'd2: prdata <= r_Enable;
                    3'd3: prdata <= r_write_address;
                    3'd4: prdata <= r_write_data;
                    3'd5: prdata <= result_reg;
                    default: prdata <= 8'd0;
                endcase
            end else if (pstate == WRITE_STATE) begin
                case (addr_reg)
                    3'd0: r_operand_1 <= pwdata;
                    3'd1: r_operand_2 <= pwdata;
                    3'd2: r_Enable <= pwdata[1:0];
                    3'd3: r_write_address <= pwdata;
                    3'd4: r_write_data <= pwdata;
                    default: ;
                endcase
                // Assert sram_valid for one cycle when Data Writing mode was active
                if (r_Enable == 2'b11) begin
                    sram_valid <= 1'b1;
                end
            end
            pstate <= IDLE;
        end
    end
end

endmodule
