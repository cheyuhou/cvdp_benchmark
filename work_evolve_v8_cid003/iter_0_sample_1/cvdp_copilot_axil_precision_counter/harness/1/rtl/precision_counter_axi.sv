`timescale 1ns / 1ps

module precision_counter_axi #(
    parameter integer C_S_AXI_DATA_WIDTH = 32,
    parameter integer C_S_AXI_ADDR_WIDTH = 8
) (
    input  wire                           axi_aclk,
    input  wire                           axi_aresetn,
    
    // Write Address Channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0]  axi_awaddr,
    input  wire                           axi_awvalid,
    output wire                           axi_awready,
    
    // Write Data Channel
    input  wire [C_S_AXI_DATA_WIDTH-1:0]  axi_wdata,
    input  wire [(C_S_AXI_DATA_WIDTH/8)-1:0] axi_wstrb,
    input  wire                           axi_wvalid,
    output wire                           axi_wready,
    
    // Write Response Channel
    output wire [1:0]                     axi_bresp,
    output wire                           axi_bvalid,
    input  wire                           axi_bready,
    
    // Read Address Channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0]  axi_araddr,
    input  wire                           axi_arvalid,
    output wire                           axi_arready,
    
    // Read Data Channel
    output wire [C_S_AXI_DATA_WIDTH-1:0]  axi_rdata,
    output wire [1:0]                     axi_rresp,
    output wire                           axi_rvalid,
    input  wire                           axi_rready,
    
    // Control Outputs
    output wire                           axi_ap_done,
    output wire                           irq
);

    // Internal State Registers for AXI Handshaking
    reg [C_S_AXI_ADDR_WIDTH-1:0] awaddr_reg;
    reg [C_S_AXI_ADDR_WIDTH-1:0] araddr_reg;
    reg [C_S_AXI_DATA_WIDTH-1:0] wdata_reg;
    reg aw_valid_reg;
    reg w_valid_reg;
    reg ar_valid_reg;
    
    // User Defined Registers
    reg [C_S_AXI_DATA_WIDTH-1:0] slv_reg_ctl;
    reg [C_S_AXI_DATA_WIDTH-1:0] slv_reg_t;
    reg [C_S_AXI_DATA_WIDTH-1:0] slv_reg_v;
    reg [C_S_AXI_DATA_WIDTH-1:0] slv_reg_irq_mask;
    reg [C_S_AXI_DATA_WIDTH-1:0] slv_reg_irq_thresh;
    
    // Address Decoding
    // Only offsets 0x00, 0x10, 0x20, 0x24, 0x28 are valid. Others trigger SLVERR.
    wire addr_valid = (awaddr_reg[7:0] == 8'h00) || (awaddr_reg[7:0] == 8'h10) || 
                      (awaddr_reg[7:0] == 8'h20) || (awaddr_reg[7:0] == 8'h24) || 
                      (awaddr_reg[7:0] == 8'h28);
    wire addr_valid_r = (araddr_reg[7:0] == 8'h00) || (araddr_reg[7:0] == 8'h10) || 
                        (araddr_reg[7:0] == 8'h20) || (araddr_reg[7:0] == 8'h24) || 
                        (araddr_reg[7:0] == 8'h28);
    
    // AXI Handshake Assignments
    assign axi_awready = !aw_valid_reg;
    assign axi_wready  = !w_valid_reg;
    assign axi_arready = !ar_valid_reg;
    
    // Write Response
    assign axi_bvalid = w_valid_reg;
    assign axi_bresp  = addr_valid ? 2'b00 : 2'b10;
    
    // Read Response
    assign axi_rvalid = ar_valid_reg;
    assign axi_rresp  = addr_valid_r ? 2'b00 : 2'b10;
    
    // Read Data Mux
    assign axi_rdata = (araddr_reg[7:0] == 8'h00) ? slv_reg_ctl :
                       (araddr_reg[7:0] == 8'h10) ? slv_reg_t :
                       (araddr_reg[7:0] == 8'h20) ? slv_reg_v :
                       (araddr_reg[7:0] == 8'h24) ? slv_reg_irq_mask :
                       (araddr_reg[7:0] == 8'h28) ? slv_reg_irq_thresh : {C_S_AXI_DATA_WIDTH{1'b0}};
                       
    // Control Outputs
    assign axi_ap_done = (slv_reg_v == 0) ? 1'b1 : 1'b0;
    
    // Interrupt Logic
    reg irq_reg;
    assign irq = irq_reg;
    
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            irq_reg <= 1'b0;
        end else begin
            irq_reg <= 1'b0;
            // Cleared when countdown stops or threshold condition no longer met
            if (slv_reg_ctl[0] == 0) begin
                irq_reg <= 1'b0;
            end else if (slv_reg_irq_mask[0] && slv_reg_v == slv_reg_irq_thresh) begin
                irq_reg <= 1'b1;
            end
        end
    end

    // AXI Write Address Channel FSM
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            aw_valid_reg <= 1'b0;
            awaddr_reg   <= {C_S_AXI_ADDR_WIDTH{1'b0}};
        end else begin
            if (axi_awvalid && !aw_valid_reg) begin
                awaddr_reg   <= axi_awaddr;
                aw_valid_reg <= 1'b1;
            end else if (axi_bready && axi_bvalid) begin
                aw_valid_reg <= 1'b0;
            end
        end
    end

    // AXI Write Data Channel FSM
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            w_valid_reg <= 1'b0;
            wdata_reg   <= {C_S_AXI_DATA_WIDTH{1'b0}};
        end else begin
            if (axi_wvalid && !w_valid_reg) begin
                wdata_reg   <= axi_wdata;
                w_valid_reg <= 1'b1;
            end else if (axi_bready && axi_bvalid) begin
                w_valid_reg <= 1'b0;
            end
        end
    end

    // AXI Read Address Channel FSM
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            ar_valid_reg <= 1'b0;
            araddr_reg   <= {C_S_AXI_ADDR_WIDTH{1'b0}};
        end else begin
            if (axi_arvalid && !ar_valid_reg) begin
                araddr_reg   <= axi_araddr;
                ar_valid_reg <= 1'b1;
            end else if (axi_rready && axi_rvalid) begin
                ar_valid_reg <= 1'b0;
            end
        end
    end

    // Register Update Logic on Write
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            slv_reg_ctl        <= {C_S_AXI_DATA_WIDTH{1'b0}};
            slv_reg_t          <= {C_S_AXI_DATA_WIDTH{1'b0}};
            slv_reg_v          <= {C_S_AXI_DATA_WIDTH{1'b0}};
            slv_reg_irq_mask   <= {C_S_AXI_DATA_WIDTH{1'b0}};
            slv_reg_irq_thresh <= {C_S_AXI_DATA_WIDTH{1'b0}};
        end else begin
            // Apply write when data is latched and response is accepted by master
            if (w_valid_reg && axi_bready) begin
                case (awaddr_reg[7:0])
                    8'h00: begin
                        slv_reg_ctl  <= axi_wdata;
                        slv_reg_t    <= {C_S_AXI_DATA_WIDTH{1'b0}}; // Reset elapsed time on control write
                    end
                    8'h20: begin
                        slv_reg_v    <= axi_wdata;
                    end
                    8'h24: begin
                        slv_reg_irq_mask <= axi_wdata;
                    end
                    8'h28: begin
                        slv_reg_irq_thresh <= axi_wdata;
                    end
                    default: begin
                        // Ignored for invalid/reserved addresses
                    end
                endcase
            end
        end
    end

    // Countdown & Elapsed Time Logic
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            slv_reg_v <= {C_S_AXI_DATA_WIDTH{1'b0}};
            slv_reg_t <= {C_S_AXI_DATA_WIDTH{1'b0}};
        end else begin
            // Elapsed time increments when countdown is finished (v==0) and counter is active
            if (slv_reg_v == 0 && slv_reg_ctl[0])
                slv_reg_t <= slv_reg_t + 1;
                
            // Countdown decrements while running
            if (slv_reg_ctl[0]) begin
                if (slv_reg_v > 0)
                    slv_reg_v <= slv_reg_v - 1;
            end
        end
    end

endmodule
