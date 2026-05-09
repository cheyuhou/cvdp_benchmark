module precision_counter_axi #(
    parameter integer C_S_AXI_DATA_WIDTH = 32,
    parameter integer C_S_AXI_ADDR_WIDTH = 8
)(
    input  wire                     axi_aclk,
    input  wire                     axi_aresetn,
    // Write address channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr,
    input  wire                     axi_awvalid,
    output wire                    axi_awready,
    // Write data channel
    input  wire [C_S_AXI_DATA_WIDTH-1:0] axi_wdata,
    input  wire [(C_S_AXI_DATA_WIDTH/8)-1:0] axi_wstrb,
    input  wire                     axi_wvalid,
    output wire                    axi_wready,
    // Write response channel
    output wire [1:0]             axi_bresp,
    output wire                   axi_bvalid,
    input  wire                   axi_bready,
    // Read address channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr,
    input  wire                   axi_arvalid,
    output wire                  axi_arready,
    // Read response channel
    output wire [C_S_AXI_DATA_WIDTH-1:0] axi_rdata,
    output wire [1:0]             axi_rresp,
    output wire                   axi_rvalid,
    input  wire                   axi_rready,
    // Control outputs
    output wire                   axi_ap_done,
    output wire                   irq
);

localparam [2:0] IDLE_S = 3'b000;
localparam [2:0] WADDR_S = 3'b001;
localparam [2:0] WDATA_S = 3'b010;
localparam [2:0] RADDR_S = 3'b011;

logic [2:0] fsm_state;
logic [C_S_AXI_ADDR_WIDTH-1:0] slv_reg_addr;
logic [C_S_AXI_DATA_WIDTH-1:0] slv_reg_wdata;
logic [(C_S_AXI_DATA_WIDTH/8)-1:0] slv_reg_wstrb;
logic aw_ready;
logic w_ready;
logic b_valid;
logic ar_ready;
logic r_valid;

// Internal registers
logic [31:0] reg_ctl;
logic [31:0] reg_t;
logic [31:0] reg_v;
logic [31:0] reg_irq_mask;
logic [31:0] reg_irq_thresh;
logic ap_done_int;
logic irq_int;

// AXI Ready/Valid assignments
assign axi_awready = (fsm_state == IDLE_S) ? aw_ready : 1'b0;
assign axi_wready  = (fsm_state == WADDR_S) ? w_ready : 1'b0;
assign axi_bvalid  = (fsm_state == WDATA_S) ? b_valid : 1'b0;
assign axi_arready = (fsm_state == IDLE_S) ? ar_ready : 1'b0;
assign axi_rvalid  = (fsm_state == RADDR_S) ? r_valid : 1'b0;

// FSM for AXI Handshaking
always @(posedge axi_aclk or negedge axi_aresetn) begin
    if (!axi_aresetn) begin
        fsm_state <= IDLE_S;
        aw_ready <= 1'b0;
        w_ready <= 1'b0;
        b_valid <= 1'b0;
        ar_ready <= 1'b0;
        r_valid <= 1'b0;
    end else begin
        aw_ready <= 1'b0;
        w_ready <= 1'b0;
        b_valid <= 1'b0;
        ar_ready <= 1'b0;
        r_valid <= 1'b0;
        
        case (fsm_state)
            IDLE_S: begin
                if (axi_awvalid) begin
                    aw_ready <= 1'b1;
                    if (axi_awvalid && aw_ready) begin
                        slv_reg_addr <= axi_awaddr;
                        fsm_state <= WADDR_S;
                    end
                end else if (axi_arvalid) begin
                    ar_ready <= 1'b1;
                    if (axi_arvalid && ar_ready) begin
                        slv_reg_addr <= axi_araddr;
                        fsm_state <= RADDR_S;
                    end
                end
            end
            WADDR_S: begin
                w_ready <= 1'b1;
                if (axi_wvalid && w_ready) begin
                    slv_reg_wdata <= axi_wdata;
                    slv_reg_wstrb <= axi_wstrb;
                    fsm_state <= WDATA_S;
                end
            end
            WDATA_S: begin
                b_valid <= 1'b1;
                if (axi_bready && b_valid) begin
                    fsm_state <= IDLE_S;
                end
            end
            RADDR_S: begin
                r_valid <= 1'b1;
                if (axi_rready && r_valid) begin
                    fsm_state <= IDLE_S;
                end
            end
            default: fsm_state <= IDLE_S;
        endcase
    end
end

// Register Update Logic
always @(posedge axi_aclk or negedge axi_aresetn) begin
    if (!axi_aresetn) begin
        reg_ctl <= 0;
        reg_t <= 0;
        reg_v <= 0;
        reg_irq_mask <= 0;
        reg_irq_thresh <= 0;
    end else begin
        if (fsm_state == WDATA_S && axi_bready && b_valid) begin
            case (slv_reg_addr)
                8'h00: begin
                    if (slv_reg_wstrb[0]) reg_ctl[ 7: 0] <= slv_reg_wdata[ 7: 0];
                    if (slv_reg_wstrb[1]) reg_ctl[15: 8] <= slv_reg_wdata[15: 8];
                    if (slv_reg_wstrb[2]) reg_ctl[23:16] <= slv_reg_wdata[23:16];
                    if (slv_reg_wstrb[3]) reg_ctl[31:24] <= slv_reg_wdata[31:24];
                    reg_t <= 0; // Reset elapsed time on write to control register
                end
                8'h10: begin
                    if (slv_reg_wstrb[0]) reg_t[ 7: 0] <= slv_reg_wdata[ 7: 0];
                    if (slv_reg_wstrb[1]) reg_t[15: 8] <= slv_reg_wdata[15: 8];
                    if (slv_reg_wstrb[2]) reg_t[23:16] <= slv_reg_wdata[23:16];
                    if (slv_reg_wstrb[3]) reg_t[31:24] <= slv_reg_wdata[31:24];
                end
                8'h20: begin
                    if (slv_reg_wstrb[0]) reg_v[ 7: 0] <= slv_reg_wdata[ 7: 0];
                    if (slv_reg_wstrb[1]) reg_v[15: 8] <= slv_reg_wdata[15: 8];
                    if (slv_reg_wstrb[2]) reg_v[23:16] <= slv_reg_wdata[23:16];
                    if (slv_reg_wstrb[3]) reg_v[31:24] <= slv_reg_wdata[31:24];
                end
                8'h24: begin
                    if (slv_reg_wstrb[0]) reg_irq_mask[ 7: 0] <= slv_reg_wdata[ 7: 0];
                    if (slv_reg_wstrb[1]) reg_irq_mask[15: 8] <= slv_reg_wdata[15: 8];
                    if (slv_reg_wstrb[2]) reg_irq_mask[23:16] <= slv_reg_wdata[23:16];
                    if (slv_reg_wstrb[3]) reg_irq_mask[31:24] <= slv_reg_wdata[31:24];
                end
                8'h28: begin
                    if (slv_reg_wstrb[0]) reg_irq_thresh[ 7: 0] <= slv_reg_wdata[ 7: 0];
                    if (slv_reg_wstrb[1]) reg_irq_thresh[15: 8] <= slv_reg_wdata[15: 8];
                    if (slv_reg_wstrb[2]) reg_irq_thresh[23:16] <= slv_reg_wdata[23:16];
                    if (slv_reg_wstrb[3]) reg_irq_thresh[31:24] <= slv_reg_wdata[31:24];
                end
                default: ; // Ignore writes to undefined addresses
            endcase
        end
    end
end

// Countdown Logic
always @(posedge axi_aclk or negedge axi_aresetn) begin
    if (!axi_aresetn) begin
        ap_done_int <= 1'b0;
    end else begin
        if (reg_ctl[0]) begin // Counter running
            if (reg_v > 0) begin
                reg_v <= reg_v - 1;
                ap_done_int <= 1'b0;
            end else begin
                ap_done_int <= 1'b1; // Countdown reached zero
            end
        end else begin
            ap_done_int <= 1'b0; // Counter stopped
        end
    end
end

// Elapsed Time Logic
always @(posedge axi_aclk or negedge axi_aresetn) begin
    if (!axi_aresetn) begin
        reg_t <= 0;
    end else begin
        // Increment only when countdown is finished
        if (ap_done_int) begin
            reg_t <= reg_t + 1;
        end
    end
end

// Interrupt Logic
always @(posedge axi_aclk or negedge axi_aresetn) begin
    if (!axi_aresetn) begin
        irq_int <= 1'b0;
    end else begin
        // Asserted when mask is enabled and current value matches threshold
        if (reg_ctl[0] == 0) begin
            irq_int <= 1'b0;
        end else if (reg_irq_mask[0] && (reg_v == reg_irq_thresh)) begin
            irq_int <= 1'b1;
        end else begin
            irq_int <= 1'b0;
        end
    end
end

// Read Data Mux & Response Registration
logic [C_S_AXI_DATA_WIDTH-1:0] r_data_reg;
logic [1:0] r_resp_reg;
always @(posedge axi_aclk) begin
    if (fsm_state == RADDR_S) begin
        r_data_reg <= (slv_reg_addr == 8'h00) ? reg_ctl :
                      (slv_reg_addr == 8'h10) ? reg_t :
                      (slv_reg_addr == 8'h20) ? reg_v :
                      (slv_reg_addr == 8'h24) ? reg_irq_mask :
                      (slv_reg_addr == 8'h28) ? reg_irq_thresh : 0;
        r_resp_reg <= (slv_reg_addr == 8'h00 || slv_reg_addr == 8'h10 || slv_reg_addr == 8'h20 || slv_reg_addr == 8'h24 || slv_reg_addr == 8'h28) ? 2'b00 : 2'b10;
    end
end
assign axi_rdata = r_data_reg;
assign axi_rresp = r_resp_reg;

// Write Response Registration
logic [1:0] b_resp_reg;
always @(posedge axi_aclk) begin
    if (fsm_state == WDATA_S) begin
        b_resp_reg <= (slv_reg_addr == 8'h00 || slv_reg_addr == 8'h10 || slv_reg_addr == 8'h20 || slv_reg_addr == 8'h24 || slv_reg_addr == 8'h28) ? 2'b00 : 2'b10;
    end
end
assign axi_bresp = b_resp_reg;

assign axi_ap_done = ap_done_int;
assign irq = irq_int;

endmodule
