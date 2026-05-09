`timescale 1ns / 1ps

/// @file cvdp_copilot_apb_gpio.sv
/// @brief APB-compatible GPIO controller with configurable width, interrupt generation, 
///        and robust input synchronization.
/// @note This module implements the specification exactly as described. 
///       Undefined addresses return 0 on read and are ignored on write.
///       pready is hardwired high, pslverr is hardwired low.

module cvdp_copilot_apb_gpio #(
    parameter integer GPIO_WIDTH = 8
)(
    input  logic                     pclk,
    input  logic                     preset_n,
    input  logic                     psel,
    input  logic [7:2]               paddr,
    input  logic                     penable,
    input  logic                     pwrite,
    input  logic [31:0]              pwdata,
    input  logic [GPIO_WIDTH-1:0]    gpio_in,
    output logic [31:0]              prdata,
    output logic                     pready,
    output logic                     pslverr,
    output logic [GPIO_WIDTH-1:0]    gpio_out,
    output logic [GPIO_WIDTH-1:0]    gpio_enable,
    output logic [GPIO_WIDTH-1:0]    gpio_int,
    output logic                     comb_int
);

    // ==================================================================
    // Register Map Addresses (Word-aligned byte addresses decoded from paddr[7:2])
    // ==================================================================
    localparam logic [7:2] ADDR_IN_DATA  = 2'h0; // 0x00: GPIO Input Data (RO)
    localparam logic [7:2] ADDR_OUT_DATA = 2'h1; // 0x04: GPIO Output Data  (RW)
    localparam logic [7:2] ADDR_OUT_EN   = 2'h2; // 0x08: GPIO Output Enable(RW)
    localparam logic [7:2] ADDR_INT_EN   = 2'h3; // 0x0C: GPIO Interrupt En.(RW)
    localparam logic [7:2] ADDR_INT_TYPE = 2'h4; // 0x10: GPIO Interrupt Type(RW)
    localparam logic [7:2] ADDR_INT_POL  = 2'h5; // 0x14: GPIO Interrupt Pol.(RW)
    localparam logic [7:2] ADDR_INT_STATE= 2'h6; // 0x18: GPIO Interrupt State(RO)

    // Internal Register Storage
    logic [31:0] reg_output_data;
    logic [31:0] reg_output_enable;
    logic [31:0] reg_int_enable;
    logic [31:0] reg_int_type;     // 0 = Edge-Sensitive, 1 = Level-Sensitive
    logic [31:0] reg_int_polarity; // 0 = Active-Low, 1 = Active-High
    logic [31:0] reg_int_state;
    logic [31:0] prdata_d;

    // APB Address Validation
    logic addr_valid;
    assign addr_valid = (paddr == ADDR_IN_DATA)  | (paddr == ADDR_OUT_DATA)  |
                        (paddr == ADDR_OUT_EN)   | (paddr == ADDR_INT_EN)    |
                        (paddr == ADDR_INT_TYPE) | (paddr == ADDR_INT_POL)   |
                        (paddr == ADDR_INT_STATE);

    // ==================================================================
    // Input Synchronization (2-Stage Flip-Flop)
    // ==================================================================
    logic [GPIO_WIDTH-1:0] gpio_in_sync_d1;
    logic [GPIO_WIDTH-1:0] gpio_in_sync_d2;
    wire  [GPIO_WIDTH-1:0] gpio_in_sync = gpio_in_sync_d2;

    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            gpio_in_sync_d1 <= '0;
            gpio_in_sync_d2 <= '0;
        end else begin
            gpio_in_sync_d1 <= gpio_in;
            gpio_in_sync_d2 <= gpio_in_sync_d1;
        end
    end

    // ==================================================================
    // Interrupt Detection & Configuration Logic
    // ==================================================================
    // Edge detection using synchronized previous and current states
    logic [GPIO_WIDTH-1:0] rising_edge;
    logic [GPIO_WIDTH-1:0] falling_edge;
    assign rising_edge  = ~gpio_in_sync_d1 & gpio_in_sync;
    assign falling_edge =  gpio_in_sync_d1 & ~gpio_in_sync;
    logic [GPIO_WIDTH-1:0] edge_det = rising_edge | falling_edge;

    // Level detection uses the synchronized input directly
    logic [GPIO_WIDTH-1:0] level_det = gpio_in_sync;

    // Determine per-pin trigger condition based on Type Register (0=Edge, 1=Level)
    logic [GPIO_WIDTH-1:0] is_level_cfg = reg_int_type & reg_int_enable;
    logic [GPIO_WIDTH-1:0] is_edge_cfg  = ~reg_int_type & reg_int_enable;
    logic [GPIO_WIDTH-1:0] int_detect   = (is_level_cfg & level_det) | 
                                          (is_edge_cfg  & edge_det);

    // Apply Polarity Configuration to generate individual interrupt signals
    // Polarity = 1 -> Active High, Polarity = 0 -> Active Low
    assign gpio_int = int_detect ^ (~reg_int_polarity);

    // Combined interrupt is the logical OR of all individual interrupts
    assign comb_int = |gpio_int;

    // Interrupt State Register (Read-only, latched on trigger assertion)
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n)
            reg_int_state <= '0;
        else
            reg_int_state <= (int_detect | reg_int_state);
    end

    // ==================================================================
    // APB Write Logic
    // ==================================================================
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            reg_output_data  <= '0;
            reg_output_enable<= '0;
            reg_int_enable   <= '0;
            reg_int_type     <= '0;
            reg_int_polarity <= '0;
        end else if (psel & penable & pwrite & addr_valid) begin
            case (paddr)
                ADDR_OUT_DATA:  reg_output_data  <= pwdata[GPIO_WIDTH-1:0];
                ADDR_OUT_EN:    reg_output_enable<= pwdata[GPIO_WIDTH-1:0];
                ADDR_INT_EN:    reg_int_enable   <= pwdata[GPIO_WIDTH-1:0];
                ADDR_INT_TYPE:  reg_int_type     <= pwdata[GPIO_WIDTH-1:0];
                ADDR_INT_POL:   reg_int_polarity <= pwdata[GPIO_WIDTH-1:0];
                default:        ; // Silent ignore for writes to undefined/RO registers
            endcase
        end
    end

    // ==================================================================
    // APB Read Logic
    // ==================================================================
    always_ff @(posedge pclk) begin
        if (psel & penable & !pwrite & addr_valid) begin
            case (paddr)
                ADDR_OUT_DATA:  prdata_d <= reg_output_data;
                ADDR_OUT_EN:    prdata_d <= reg_output_enable;
                ADDR_INT_EN:    prdata_d <= reg_int_enable;
                ADDR_INT_TYPE:  prdata_d <= reg_int_type;
                ADDR_INT_POL:   prdata_d <= reg_int_polarity;
                ADDR_INT_STATE: prdata_d <= reg_int_state;
                default:        prdata_d <= '0; // Undefined reads return 0
            endcase
        end
    end
    // Input Data Register is combinatorial, zero-extended to 32 bits
    logic [31:0] reg_input_data = { {(32-GPIO_WIDTH){1'b0}}, gpio_in_sync };
    assign prdata_d[GPIO_WIDTH-1:0] = (paddr == ADDR_IN_DATA) ? gpio_in_sync : prdata_d[GPIO_WIDTH-1:0];
    assign prdata = prdata_d;

    // ==================================================================
    // GPIO Output & Direction Control
    // ==================================================================
    assign gpio_out  = reg_output_data[GPIO_WIDTH-1:0];
    assign gpio_enable = reg_output_enable[GPIO_WIDTH-1:0];

    // ==================================================================
    // APB Response Signals (Fixed per specification)
    // ==================================================================
    assign pready  = 1'b1; // Always ready, no wait states
    assign pslverr = 1'b0; // Error-free operation

endmodule
