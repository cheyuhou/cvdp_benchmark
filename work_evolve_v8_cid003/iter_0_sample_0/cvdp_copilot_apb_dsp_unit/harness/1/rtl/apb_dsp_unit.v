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
    output wire sram_valid
);

    // Internal Configuration Registers
    reg [7:0] r_operand_1;
    reg [7:0] r_operand_2;
    reg [1:0] r_Enable;
    reg [7:0] r_write_address;
    reg [7:0] r_write_data;
    reg [7:0] result_reg;

    // 1 KB SRAM (1024 bytes x 8 bits)
    reg [7:0] sram_mem [0:1023];

    // APB State Machine
    localparam IDLE = 1'b0;
    localparam ACTIVE = 1'b1;
    reg state;

    // APB Control FSM
    always @(posedge pclk or negedge presetn) begin
        if (!presetn) begin
            state <= IDLE;
            pready <= 1'b0;
            pslverr <= 1'b0;
            prdata <= 8'b0;
            r_operand_1 <= 8'b0;
            r_operand_2 <= 8'b0;
            r_Enable <= 2'b00;
            r_write_address <= 8'b0;
            r_write_data <= 8'b0;
            result_reg <= 8'b0;
        end else begin
            case (state)
                IDLE: begin
                    if (pselx && !penable) begin
                        state <= ACTIVE;
                    end
                end
                ACTIVE: begin
                    pready <= 1'b1;
                    // Address decoding and error handling
                    if (paddr > 5) begin
                        pslverr <= 1'b1;
                        prdata <= 8'b0;
                    end else begin
                        pslverr <= 1'b0;
                        if (pwrite) begin
                            case (paddr)
                                0: r_operand_1 <= pwdata;
                                1: r_operand_2 <= pwdata;
                                2: r_Enable <= pwdata[1:0];
                                3: r_write_address <= pwdata;
                                4: r_write_data <= pwdata;
                                default: ;
                            endcase
                        end else begin
                            case (paddr)
                                0: prdata <= r_operand_1;
                                1: prdata <= r_operand_2;
                                2: prdata <= {6'b0, r_Enable};
                                3: prdata <= r_write_address;
                                4: prdata <= r_write_data;
                                5: prdata <= result_reg;
                                default: prdata <= 8'b0;
                            endcase
                        end
                    end
                    state <= IDLE;
                end
            endcase
        end
    end

    // DSP Functional Logic
    always @(posedge pclk) begin
        case (r_Enable)
            2'b01: result_reg <= sram_mem[r_operand_1] + sram_mem[r_operand_2];
            2'b10: result_reg <= sram_mem[r_operand_1] * sram_mem[r_operand_2];
            default: result_reg <= 8'b0;
        endcase
    end

    // SRAM Interface
    assign sram_valid = (r_Enable == 2'b11) ? 1'b1 : 1'b0;

    // SRAM Write Logic
    always @(posedge pclk) begin
        if (r_Enable == 2'b11) begin
            sram_mem[r_write_address[7:0]] <= r_write_data;
        end
    end

endmodule
