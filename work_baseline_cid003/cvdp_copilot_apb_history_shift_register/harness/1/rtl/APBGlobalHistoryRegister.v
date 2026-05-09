// rtl/APBGlobalHistoryRegister.v

module APBGlobalHistoryRegister (
    input  wire pclk,
    input  wire presetn,
    input  wire [9:0] paddr,
    input  wire pselx,
    input  wire penable,
    input  wire pwrite,
    input  wire [7:0] pwdata,
    output reg  pready,
    output reg  [7:0] prdata,
    output reg  pslverr,
    input  wire history_shift_valid,
    input  wire clk_gate_en,
    output wire history_full,
    output wire history_empty,
    output wire error_flag,
    output wire interrupt_full,
    output wire interrupt_error
);

    // ---------------------------------------------------------
    // Clock Gating Logic
    // ---------------------------------------------------------
    // Samples clk_gate_en on negative edge of pclk to prevent glitches
    reg clk_gate_en_d;
    always @(negedge pclk or negedge presetn) begin
        if (!presetn)
            clk_gate_en_d <= 1'b0;
        else
            clk_gate_en_d <= clk_gate_en;
    end
    wire pclk_gated = pclk & clk_gate_en_d;

    // ---------------------------------------------------------
    // Internal State Registers
    // ---------------------------------------------------------
    reg [7:0] control_register;
    reg [7:0] train_history;
    reg [7:0] predict_history;

    reg apb_ack;

    // ---------------------------------------------------------
    // APB Interface & Register Access Logic
    // ---------------------------------------------------------
    always @(posedge pclk_gated or negedge presetn) begin
        if (!presetn) begin
            apb_ack   <= 1'b0;
            pready    <= 1'b0;
            pslverr   <= 1'b0;
            prdata    <= 8'b0;
            control_register <= 8'b0;
            train_history      <= 8'b0;
            predict_history    <= 8'b0;
        end else begin
            if (pselx && !penable) begin
                apb_ack <= 1'b1;
                pready  <= 1'b0;
            end else if (pselx && penable && apb_ack) begin
                apb_ack <= 1'b0;
                pready  <= 1'b1;

                // Address Decoding & Error Handling
                if (paddr != 10'h000 && paddr != 10'h001 && paddr != 10'h002) begin
                    pslverr <= 1'b1;
                end else begin
                    pslverr <= 1'b0;
                    
                    if (pwrite) begin
                        // Write Operations
                        case (paddr)
                            10'h000: control_register <= pwdata;
                            10'h001: train_history    <= pwdata;
                            10'h002: ; // predict_history is read-only, ignore write
                            default: ;
                        endcase
                    end else begin
                        // Read Operations
                        case (paddr)
                            10'h000: prdata <= {4'b0000, control_register[3:0]}; // Reserved bits read 0
                            10'h001: prdata <= {1'b0, train_history[6:0]};       // Reserved bit read 0
                            10'h002: prdata <= predict_history;                  // Shift register state
                            default: prdata <= 8'b0;
                        endcase
                    end
                end
            end else begin
                apb_ack <= 1'b0;
                pready  <= 1'b0;
                pslverr <= 1'b0;
            end
        end
    end

    // ---------------------------------------------------------
    // Prediction Update Logic (Synchronous to history_shift_valid)
    // ---------------------------------------------------------
    // Updates on rising edge of history_shift_valid
    // Priority: Misprediction > Normal Prediction
    always @(posedge history_shift_valid or negedge presetn) begin
        if (!presetn) begin
            predict_history <= 8'b0;
        end else begin
            if (control_register[2]) begin // train_mispredicted has highest priority
                // Load train_history[6:0] concatenated with train_taken (control_register[3])
                predict_history <= {train_history[6:0], control_register[3]};
            end else if (control_register[0]) begin // predict_valid
                // Shift left, insert predict_taken (control_register[1]) at LSB
                predict_history <= {predict_history[7:1], control_register[1]};
            end
        end
    end

    // ---------------------------------------------------------
    // Status & Interrupt Signals
    // ---------------------------------------------------------
    assign history_full   = (predict_history == 8'hFF);
    assign history_empty  = (predict_history == 8'h00);
    assign error_flag     = pslverr;
    assign interrupt_full = history_full;
    assign interrupt_error= error_flag;

endmodule
