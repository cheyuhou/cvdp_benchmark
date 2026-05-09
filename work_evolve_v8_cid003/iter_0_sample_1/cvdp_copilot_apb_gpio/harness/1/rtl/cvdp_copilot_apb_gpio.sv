/**
 * @file cvdp_copilot_apb_gpio.sv
 * @brief APB GPIO Module
 * 
 * A configurable GPIO peripheral compatible with the APB protocol.
 * Supports bidirectional control, edge/level interrupts, and synchronization.
 */
module cvdp_copilot_apb_gpio #(
    parameter int GPIO_WIDTH = 8
)(
    input  logic                   pclk,
    input  logic                   preset_n,
    input  logic                   psel,
    input  logic [7:2]             paddr,
    input  logic                   penable,
    input  logic                   pwrite,
    input  logic [31:0]            pwdata,
    input  logic [GPIO_WIDTH-1:0]  gpio_in,
    
    output logic [31:0]            prdata,
    output logic                   pready,
    output logic                   pslverr,
    output logic [GPIO_WIDTH-1:0]  gpio_out,
    output logic [GPIO_WIDTH-1:0]  gpio_enable,
    output logic [GPIO_WIDTH-1:0]  gpio_int,
    output logic                   comb_int
);

    // ========================================================================
    // Internal Signals & Registers
    // ========================================================================
    
    // APB Data Bus
    logic [31:0] prdata_reg;
    
    // GPIO Registers
    logic [GPIO_WIDTH-1:0] reg_out;   // Output Data
    logic [GPIO_WIDTH-1:0] reg_en;    // Output Enable
    logic [GPIO_WIDTH-1:0] reg_ie;    // Interrupt Enable
    logic [GPIO_WIDTH-1:0] reg_it;    // Interrupt Type (0: Level, 1: Edge)
    logic [GPIO_WIDTH-1:0] reg_ip;    // Interrupt Polarity (0: Low, 1: High)
    logic [GPIO_WIDTH-1:0] reg_is;    // Interrupt Status
    
    // Synchronization & Edge Detection
    logic [GPIO_WIDTH-1:0] gpio_in_sync;
    logic [GPIO_WIDTH-1:0] prev_gpio_in;
    
    // Interrupt Logic Signals
    logic [GPIO_WIDTH-1:0] trigger_rising;
    logic [GPIO_WIDTH-1:0] trigger_falling;
    logic [GPIO_WIDTH-1:0] int_edge;
    logic [GPIO_WIDTH-1:0] int_level;
    logic [GPIO_WIDTH-1:0] int_active;
    logic [GPIO_WIDTH-1:0] gpio_int_d;

    // ========================================================================
    // APB Protocol Signals
    // ========================================================================
    
    // Always Ready, No Errors
    assign pready   = 1'b1;
    assign pslverr  = 1'b0;

    // ========================================================================
    // Synchronization
    // ========================================================================
    
    // Two-stage flip-flop for metastability mitigation
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            gpio_in_sync  <= '0;
            prev_gpio_in  <= '0;
        end else begin
            gpio_in_sync  <= gpio_in;
            prev_gpio_in  <= gpio_in_sync;
        end
    end

    // ========================================================================
    // Interrupt Logic
    // ========================================================================
    
    // Edge Detection on Synchronized Signal
    assign trigger_rising = (~prev_gpio_in) & gpio_in_sync;
    assign trigger_falling = prev_gpio_in & (~gpio_in_sync);
    
    // Interrupt Triggering Logic
    // Edge Mode (reg_it == 1):
    //   Active High (reg_ip == 1) -> Rising Edge
    //   Active Low (reg_ip == 0)  -> Falling Edge
    assign int_edge = (reg_it & trigger_rising & reg_ip) | 
                      (reg_it & trigger_falling & ~reg_ip);
    
    // Level Mode (reg_it == 0):
    //   Active High (reg_ip == 1) -> Level High
    //   Active Low (reg_ip == 0)  -> Level Low
    // Note: Level logic uses current sync state directly.
    assign int_level = (~reg_it & gpio_in_sync & reg_ip) | 
                       (~reg_it & ~gpio_in_sync & ~reg_ip);
    
    // Combine triggers and mask with Enable
    assign int_active = int_edge | int_level;
    assign gpio_int_d = reg_ie & int_active;

    // Output Interrupts
    assign gpio_int = gpio_int_d;
    assign comb_int = |gpio_int_d;

    // ========================================================================
    // Register Updates (Sequential)
    // ========================================================================
    
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            reg_out <= '0;
            reg_en  <= '0;
            reg_ie  <= '0;
            reg_it  <= '0;
            reg_ip  <= '0;
            reg_is  <= '0;
        end else begin
            // APB Write Logic
            if (psel && penable && pwrite) begin
                case (paddr)
                    8'h04: reg_out <= pwdata[GPIO_WIDTH-1:0]; // Output Data
                    8'h08: reg_en  <= pwdata[GPIO_WIDTH-1:0]; // Output Enable
                    8'h0C: reg_ie  <= pwdata[GPIO_WIDTH-1:0]; // Interrupt Enable
                    8'h10: reg_it  <= pwdata[GPIO_WIDTH-1:0]; // Interrupt Type
                    8'h14: reg_ip  <= pwdata[GPIO_WIDTH-1:0]; // Interrupt Polarity
                    // 0x00 (Input) and 0x18 (Status) are read-only; writes ignored.
                    default: ;
                endcase
            end
            
            // Interrupt Status Reflection
            reg_is <= gpio_int_d;
        end
    end

    // ========================================================================
    // APB Read Logic
    // ========================================================================
    
    always_comb begin
        case (paddr)
            8'h00: prdata_reg = {24'd0, gpio_in_sync};   // Synchronized Input
            8'h04: prdata_reg = {24'd0, reg_out};
            8'h08: prdata_reg = {24'd0, reg_en};
            8'h0C: prdata_reg = {24'd0, reg_ie};
            8'h10: prdata_reg = {24'd0, reg_it};
            8'h14: prdata_reg = {24'd0, reg_ip};
            8'h18: prdata_reg = {24'd0, reg_is};
            default: prdata_reg = 32'h0;
        endcase
    end
    
    // Registered Read Data for single-cycle latency compliance
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            prdata <= '0;
        end else begin
            if (psel && penable && !pwrite) begin
                prdata <= prdata_reg;
            end
        end
    end

    // ========================================================================
    // GPIO Output Control
    // ========================================================================
    
    assign gpio_enable = reg_en;
    // Drive value if enabled (Output), High-Z if disabled (Input)
    assign gpio_out = reg_en ? reg_out : 'bz;

endmodule
