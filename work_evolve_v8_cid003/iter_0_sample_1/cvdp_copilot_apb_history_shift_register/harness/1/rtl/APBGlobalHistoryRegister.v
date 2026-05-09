module APBGlobalHistoryRegister (
    input wire pclk,
    input wire presetn,
    
    // APB signals
    input wire [9:0] paddr,
    input wire pselx,
    input wire penable,
    input wire pwrite,
    input wire [7:0] pwdata,
    output reg pready,
    output reg [7:0] prdata,
    output reg pslverr,
    
    // History Shift Interface
    input wire history_shift_valid,
    
    // Clock Gating Enable
    input wire clk_gate_en,
    
    // Status & Interrupt Signals
    output wire history_full,
    output wire history_empty,
    output wire error_flag,
    output wire interrupt_full,
    output wire interrupt_error
);

    // Internal Clock Gating Logic
    // Toggles on negative edge of pclk to avoid glitches
    reg clk_gate_d;
    always @(negedge pclk) begin
        clk_gate_d <= clk_gate_en;
    end
    wire pclk_gated = pclk & clk_gate_d;

    // Internal Registers
    reg [7:0] control_register;
    reg [7:0] train_history;
    reg [7:0] predict_history;
    
    // APB FSM State Encodings
    localparam APB_IDLE = 2'b00;
    localparam APB_SETUP = 2'b01;
    localparam APB_TRANSFER = 2'b10;
    reg [1:0] apb_state;
    
    // Control & Train Bit Extraction
    wire predict_valid = control_register[0];
    wire predict_taken = control_register[1];
    wire train_mispredicted = control_register[2];
    wire train_taken = control_register[3];
    
    // APB State Machine & Register Access Logic
    always @(posedge pclk or negedge presetn) begin
        if (!presetn) begin
            apb_state <= APB_IDLE;
            pready <= 1'b0;
            pslverr <= 1'b0;
            prdata <= 8'b0;
            control_register <= 8'b0;
            train_history <= 8'b0;
            predict_history <= 8'b0;
        end else begin
            case (apb_state)
                APB_IDLE: begin
                    pready <= 1'b0;
                    pslverr <= 1'b0;
                    if (pselx) begin
                        apb_state <= APB_SETUP;
                    end
                end
                APB_SETUP: begin
                    // Address decode and error flag handling
                    if (paddr == 10'h0 || paddr == 10'h1 || paddr == 10'h2) begin
                        pslverr <= 1'b0;
                    end else begin
                        pslverr <= 1'b1;
                    end
                    if (penable) begin
                        apb_state <= APB_TRANSFER;
                    end
                end
                APB_TRANSFER: begin
                    pready <= 1'b1;
                    if (pwrite) begin
                        if (paddr == 10'h0) begin
                            control_register <= pwdata;
                        end else if (paddr == 10'h1) begin
                            train_history <= pwdata;
                        end
                        // Address 0x2 is read-only; writes are ignored.
                    end else begin
                        if (paddr == 10'h0) begin
                            // Mask reserved bits 7:4 to 0 on read
                            prdata <= {4'b0, control_register[3:0]};
                        end else if (paddr == 10'h1) begin
                            // Mask reserved bit 7 to 0 on read
                            prdata <= {1'b0, train_history[6:0]};
                        end else if (paddr == 10'h2) begin
                            prdata <= predict_history;
                        end
                    end
                    apb_state <= APB_IDLE;
                end
                default: apb_state <= APB_IDLE;
            endcase
        end
    end
    
    // Prediction Update Logic
    // Triggered by history_shift_valid rising edge
    // Priority: train_mispredicted > predict_valid
    always @(posedge history_shift_valid) begin
        if (train_mispredicted) begin
            // Load train_history (7 bits) concatenated with train_taken (1 bit)
            predict_history <= {train_history, train_taken};
        end else if (predict_valid) begin
            // Shift in predict_taken at LSB, pushing older history up
            predict_history <= {predict_history[6:0], predict_taken};
        end
        // If neither condition is met, predict_history remains unchanged
    end
    
    // Status & Interrupt Signals
    assign history_full = (predict_history == 8'hFF) ? 1'b1 : 1'b0;
    assign history_empty = (predict_history == 8'h00) ? 1'b1 : 1'b0;
    assign interrupt_full = history_full;
    assign error_flag = pslverr;
    assign interrupt_error = error_flag;

endmodule
