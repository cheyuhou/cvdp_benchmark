module APBGlobalHistoryRegister (
    input  wire         pclk,
    input  wire         presetn,
    input  wire  [9:0]  paddr,
    input  wire         pselx,
    input  wire         penable,
    input  wire         pwrite,
    input  wire  [7:0]  pwdata,
    output reg          pready,
    output reg  [7:0]   prdata,
    output reg          pslverr,
    input  wire         history_shift_valid,
    input  wire         clk_gate_en,
    output wire         history_full,
    output wire         history_empty,
    output reg          error_flag,
    output wire         interrupt_full,
    output wire         interrupt_error
);

    // Internal CSR Registers
    reg [7:0] control_register;
    reg [7:0] train_history;
    reg [7:0] predict_history;

    // APB Read Data Multiplexer
    // Masks reserved bits [7:4] for control_register and [7] for train_history to 0
    assign prdata = pselx ?
                    ((paddr == 10'h000) ? {1'b0, control_register[3:0], 3'b0} :
                     (paddr == 10'h001) ? {1'b0, train_history[6:0]}       :
                     (paddr == 10'h002) ? predict_history                    :
                     8'h0) :
                    8'h0;

    // APB Control Signals
    // Specification requires pready to be always high (no wait states)
    assign pready = 1'b1;
    
    // Assert error on invalid address selection
    assign pslverr = (pselx && (paddr != 10'h000 && paddr != 10'h001 && paddr != 10'h002)) ? 1'b1 : 1'b0;

    // Error Flag Latch
    always @(posedge pclk or negedge presetn) begin
        if (!presetn)
            error_flag <= 1'b0;
        else if (pslverr)
            error_flag <= 1'b1;
        else
            error_flag <= 1'b0;
    end

    // APB Write Logic
    // Registers are updated on the rising edge of pclk when pselx and penable are high
    always @(posedge pclk or negedge presetn) begin
        if (!presetn) begin
            control_register <= 8'b0;
            train_history    <= 8'b0;
        end else if (pselx && penable) begin
            case (paddr)
                10'h000: control_register <= pwdata;
                10'h001: train_history    <= pwdata;
                default: ; // predict_history is read-only via APB
            endcase
        end
    end

    // Global History Shift Register Update
    // Gated clock logic to prevent glitches and minimize switching power
    // clk_gate_en assertion gates the clock. Active-low effective gating.
    wire gated_shift_clk = history_shift_valid & ~clk_gate_en;

    always @(posedge gated_shift_clk or negedge presetn) begin
        if (!presetn) begin
            predict_history <= 8'b0;
        end else begin
            // Misprediction handling has highest priority
            if (control_register[2]) begin
                // Load 7-bit train_history + 1-bit train_taken at LSB
                predict_history <= {train_history[6:0], control_register[3]};
            end else if (control_register[0]) begin
                // Normal prediction update: shift in predict_taken at LSB [0]
                predict_history <= {predict_history[6:0], control_register[1]};
            end
            // If neither valid, hold current state
        end
    end

    // Status & Interrupt Outputs
    assign history_full  = (predict_history == 8'hFF) ? 1'b1 : 1'b0;
    assign history_empty = (predict_history == 8'h00) ? 1'b1 : 1'b0;
    assign interrupt_full  = history_full;
    assign interrupt_error = error_flag;

endmodule