`ifndef CVDPCOPILOT_APB_GPIO_SV
`define CVDPCOPILOT_APB_GPIO_SV

/// @brief APB GPIO Module with configurable width, bidirectional control, and interrupt generation
/// @details Implements a fully compliant Advanced Peripheral Bus (APB) interface for a 
///          configurable GPIO peripheral. Features include input synchronization, 
///          edge/level-sensitive interrupt configuration, polarity control, and 
///          robust reset behavior. No wait states are introduced (pready=1).
module cvdp_copilot_apb_gpio #(
    parameter int unsigned GPIO_WIDTH = 8
)(
    // APB Interface
    input  logic                  pclk,
    input  logic                  preset_n,
    input  logic                  psel,
    input  logic  [7:2]           paddr,
    input  logic                  penable,
    input  logic                  pwrite,
    input  logic  [31:0]          pwdata,
    
    // GPIO Signals
    input  logic  [GPIO_WIDTH-1:0] gpio_in,

    output logic  [31:0]          prdata,
    output logic                  pready,
    output logic                  pslverr,
    output logic  [GPIO_WIDTH-1:0] gpio_out,
    output logic  [GPIO_WIDTH-1:0] gpio_enable,
    output logic  [GPIO_WIDTH-1:0] gpio_int,
    output logic                  comb_int
);

    // =========================================================================
    // Internal Register Declarations
    // =========================================================================
    logic [31:0] reg_out_data;
    logic [31:0] reg_out_en;
    logic [31:0] reg_int_en;
    logic [31:0] reg_int_type;
    logic [31:0] reg_int_pol;

    // =========================================================================
    // Input Synchronization (2-Stage Flip-Flop for Metastability Mitigation)
    // =========================================================================
    logic [GPIO_WIDTH-1:0] sync_d1, sync_d2;

    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            sync_d1 <= '0;
            sync_d2 <= '0;
        end else begin
            sync_d1 <= gpio_in;
            sync_d2 <= sync_d1;
        end
    end

    // =========================================================================
    // APB Read Logic
    // =========================================================================
    always_comb begin
        case (paddr[7:2])
            2'h0: prdata = {24'h0, sync_d2};              // 0x00: GPIO Input Data (RO)
            2'h1: prdata = {24'h0, reg_out_data[GPIO_WIDTH-1:0]}; // 0x04: GPIO Output Data
            2'h2: prdata = {24'h0, reg_out_en[GPIO_WIDTH-1:0]};   // 0x08: GPIO Output Enable
            2'h3: prdata = reg_int_en;                    // 0x0C: GPIO Interrupt Enable
            2'h4: prdata = reg_int_type;                  // 0x10: GPIO Interrupt Type
            2'h5: prdata = reg_int_pol;                   // 0x14: GPIO Interrupt Polarity
            2'h6: prdata = gpio_int;                      // 0x18: GPIO Interrupt State (RO)
            default: prdata = 32'h0;                      // Undefined Address
        endcase
    end

    // =========================================================================
    // APB Write Logic
    // =========================================================================
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            reg_out_data  <= '0;
            reg_out_en    <= '0;
            reg_int_en    <= '0;
            reg_int_type  <= '0;
            reg_int_pol   <= '0;
        end else if (psel & penable & pwrite) begin
            case (paddr[7:2])
                2'h1: reg_out_data  <= pwdata[GPIO_WIDTH-1:0];
                2'h2: reg_out_en    <= pwdata[GPIO_WIDTH-1:0];
                2'h3: reg_int_en    <= pwdata;
                2'h4: reg_int_type  <= pwdata;
                2'h5: reg_int_pol   <= pwdata;
                default: ; // Silently ignore writes to undefined/RO addresses
            endcase
        end
    end

    // =========================================================================
    // GPIO Output & Direction Control
    // =========================================================================
    assign gpio_out = reg_out_data[GPIO_WIDTH-1:0];
    assign gpio_enable = reg_out_en[GPIO_WIDTH-1:0];

    // =========================================================================
    // Interrupt Configuration & Generation
    // =========================================================================
    logic [GPIO_WIDTH-1:0] edge_latch;

    // Edge Detection Latch (Updates synchronously with clk)
    always_ff @(posedge pclk or negedge preset_n) begin
        if (!preset_n) begin
            edge_latch <= '0;
        end else begin
            edge_latch <= sync_d2;
        end
    end

    // Per-Pin Interrupt Logic
    generate
        for (genvar i = 0; i < GPIO_WIDTH; i++) begin : gen_int_logic
            logic is_level_mode;
            logic edge_detected;
            logic raw_active;

            // Type Configuration: 0 = Level Sensitive, 1 = Edge Sensitive
            assign is_level_mode = reg_int_type[i];
            
            // Edge Detection: XOR detects any transition (rising or falling)
            assign edge_detected = (sync_d2[i] ^ edge_latch[i]);
            
            // Determine active condition based on mode
            assign raw_active = is_level_mode ? edge_detected : sync_d2[i];

            // Polarity Configuration: 0 = Active High, 1 = Active Low
            // Logical OR with enable mask
            assign gpio_int[i] = (raw_active ^ reg_int_pol[i]) & reg_int_en[i];
        end
    endgenerate

    // Combined Interrupt Signal (Logical OR of all enabled interrupts)
    assign comb_int = |gpio_int;

    // =========================================================================
    // APB Response Signals
    // =========================================================================
    assign pready  = 1'b1;  // Always ready, no wait states
    assign pslverr = 1'b0;  // Error-free operation

endmodule

`endif
