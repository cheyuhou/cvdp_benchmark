`timescale 1ns / 1ps

module precision_counter_axi #(
    parameter integer C_S_AXI_DATA_WIDTH = 32,
    parameter integer C_S_AXI_ADDR_WIDTH = 8
)(
    input  wire                    axi_aclk,
    input  wire                    axi_aresetn,

    // Write Address Channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr,
    input  wire                          axi_awvalid,
    output wire                          axi_awready,

    // Write Data Channel
    input  wire [C_S_AXI_DATA_WIDTH-1:0] axi_wdata,
    input  wire [(C_S_AXI_DATA_WIDTH/8)-1:0] axi_wstrb,
    input  wire                          axi_wvalid,
    output wire                          axi_wready,

    // Write Response Channel
    output wire [1:0] axi_bresp,
    output wire                     axi_bvalid,
    input  wire                     axi_bready,

    // Read Address Channel
    input  wire [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr,
    input  wire                          axi_arvalid,
    output wire                          axi_arready,

    // Read Response Channel
    output wire [C_S_AXI_DATA_WIDTH-1:0] axi_rdata,
    output wire [1:0]                    axi_rresp,
    output wire                          axi_rvalid,
    input  wire                          axi_rready,

    // Control Outputs
    output wire axi_ap_done,
    output wire irq
);

    localparam integer NUM_REGS = 16;
    reg [C_S_AXI_DATA_WIDTH-1:0] reg_array [0:NUM_REGS-1];

    // Latched current write address for data phase matching
    reg [C_S_AXI_ADDR_WIDTH-1:0] curr_addr;

    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn)
            curr_addr <= 'h0;
        else if (axi_awready && axi_awvalid)
            curr_addr <= axi_awaddr;
    end

    // Address Decoding
    wire is_reg_0  = (curr_addr == 'h00);
    wire is_reg_10 = (curr_addr == 'h10);
    wire is_reg_20 = (curr_addr == 'h20);
    wire is_reg_24 = (curr_addr == 'h24);
    wire is_reg_28 = (curr_addr == 'h28);
    wire reg_valid = is_reg_0 | is_reg_10 | is_reg_20 | is_reg_24 | is_reg_28;

    // Exposed register names for internal logic
    wire [C_S_AXI_DATA_WIDTH-1:0] slv_reg_ctl;
    wire [C_S_AXI_DATA_WIDTH-1:0] slv_reg_t;
    wire [C_S_AXI_DATA_WIDTH-1:0] slv_reg_v;
    wire [C_S_AXI_DATA_WIDTH-1:0] slv_reg_irq_mask;
    wire [C_S_AXI_DATA_WIDTH-1:0] slv_reg_irq_thresh;

    assign slv_reg_ctl       = reg_array[0];
    assign slv_reg_t         = reg_array[4];
    assign slv_reg_v         = reg_array[8];
    assign slv_reg_irq_mask  = reg_array[9];
    assign slv_reg_irq_thresh = reg_array[10];

    // Write Logic
    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) begin
            reg_array[0]  <= 'h0;
            reg_array[4]  <= 'h0;
            reg_array[8]  <= 'h0;
            reg_array[9]  <= 'h0;
            reg_array[10] <= 'h0;
        end else if (axi_wready && axi_wvalid && reg_valid) begin
            case (1'b1)
                is_reg_0: begin
                    if (axi_wstrb[0]) reg_array[0][7:0]   <= axi_wdata[7:0];
                    if (axi_wstrb[1]) reg_array[0][15:8]  <= axi_wdata[15:8];
                    if (axi_wstrb[2]) reg_array[0][23:16] <= axi_wdata[23:16];
                    if (axi_wstrb[3]) reg_array[0][31:24] <= axi_wdata[31:24];
                    reg_array[4] <= 'h0; // Reset elapsed time on write to ctl
                end
                is_reg_10: begin
                    if (axi_wstrb[0]) reg_array[4][7:0]   <= axi_wdata[7:0];
                    if (axi_wstrb[1]) reg_array[4][15:8]  <= axi_wdata[15:8];
                    if (axi_wstrb[2]) reg_array[4][23:16] <= axi_wdata[23:16];
                    if (axi_wstrb[3]) reg_array[4][31:24] <= axi_wdata[31:24];
                end
                is_reg_20: begin
                    if (axi_wstrb[0]) reg_array[8][7:0]   <= axi_wdata[7:0];
                    if (axi_wstrb[1]) reg_array[8][15:8]  <= axi_wdata[15:8];
                    if (axi_wstrb[2]) reg_array[8][23:16] <= axi_wdata[23:16];
                    if (axi_wstrb[3]) reg_array[8][31:24] <= axi_wdata[31:24];
                end
                is_reg_24: begin
                    if (axi_wstrb[0]) reg_array[9][7:0]   <= axi_wdata[7:0];
                    if (axi_wstrb[1]) reg_array[9][15:8]  <= axi_wdata[15:8];
                    if (axi_wstrb[2]) reg_array[9][23:16] <= axi_wdata[23:16];
                    if (axi_wstrb[3]) reg_array[9][31:24] <= axi_wdata[31:24];
                end
                is_reg_28: begin
                    if (axi_wstrb[0]) reg_array[10][7:0]   <= axi_wdata[7:0];
                    if (axi_wstrb[1]) reg_array[10][15:8]  <= axi_wdata[15:8];
                    if (axi_wstrb[2]) reg_array[10][23:16] <= axi_wdata[23:16];
                    if (axi_wstrb[3]) reg_array[10][31:24] <= axi_wdata[31:24];
                end
            endcase
        end
    end

    // Counter & Timer Logic
    always @(posedge axi_aclk) begin
        // Countdown value updates
        if (slv_reg_ctl[0] == 1'b1) begin
            if (slv_reg_v > 'h0)
                slv_reg_v <= slv_reg_v - 1'b1;
            else
                slv_reg_v <= slv_reg_v;
        end else begin
            slv_reg_v <= slv_reg_v;
        end

        // Elapsed time updates only when countdown is finished
        if (slv_reg_v == 'h0)
            slv_reg_t <= slv_reg_t + 1'b1;
        else
            slv_reg_t <= slv_reg_t;
    end

    // Status Outputs
    assign axi_ap_done = (slv_reg_v == 'h0);
    assign irq = (slv_reg_ctl[0] == 1'b1) && (slv_reg_v == slv_reg_irq_thresh) && (slv_reg_irq_mask[0] == 1'b1);

    // AXI Write FSM States
    localparam AXI_WR_IDLE = 2'b00;
    localparam AXI_WR_ADDR = 2'b01;
    localparam AXI_WR_DATA = 2'b10;
    localparam AXI_WR_RESP = 2'b11;
    reg [1:0] wr_state, next_wr_state;

    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) wr_state <= AXI_WR_IDLE;
        else wr_state <= next_wr_state;
    end

    always @(*) begin
        case (wr_state)
            AXI_WR_IDLE: next_wr_state = axi_awvalid ? AXI_WR_ADDR : AXI_WR_IDLE;
            AXI_WR_ADDR: next_wr_state = (axi_awvalid && axi_awready) ? AXI_WR_DATA : AXI_WR_ADDR;
            AXI_WR_DATA: next_wr_state = (axi_wvalid && axi_wready) ? AXI_WR_RESP : AXI_WR_DATA;
            AXI_WR_RESP: next_wr_state = (axi_bvalid && axi_bready) ? AXI_WR_IDLE : AXI_WR_RESP;
            default:     next_wr_state = AXI_WR_IDLE;
        endcase
    end

    assign axi_awready = (wr_state == AXI_WR_IDLE);
    assign axi_wready  = (wr_state == AXI_WR_DATA);
    assign axi_bvalid  = (wr_state == AXI_WR_RESP);
    assign axi_bresp   = reg_valid ? 2'b00 : 2'b10;

    // AXI Read FSM States
    localparam AXI_RD_IDLE = 2'b00;
    localparam AXI_RD_ADDR = 2'b01;
    localparam AXI_RD_RESP = 2'b10;
    reg [1:0] rd_state, next_rd_state;

    always @(posedge axi_aclk or negedge axi_aresetn) begin
        if (!axi_aresetn) rd_state <= AXI_RD_IDLE;
        else rd_state <= next_rd_state;
    end

    always @(*) begin
        case (rd_state)
            AXI_RD_IDLE: next_rd_state = axi_arvalid ? AXI_RD_ADDR : AXI_RD_IDLE;
            AXI_RD_ADDR: next_rd_state = (axi_arvalid && axi_arready) ? AXI_RD_RESP : AXI_RD_ADDR;
            AXI_RD_RESP: next_rd_state = (axi_rvalid && axi_rready) ? AXI_RD_IDLE : AXI_RD_RESP;
            default:     next_rd_state = AXI_RD_IDLE;
        endcase
    end

    assign axi_arready = (rd_state == AXI_RD_IDLE);
    assign axi_rvalid  = (rd_state == AXI_RD_RESP);
    assign axi_rresp   = ((axi_araddr == 'h00) || (axi_araddr == 'h10) || (axi_araddr == 'h20) || 
                          (axi_araddr == 'h24) || (axi_araddr == 'h28)) ? 2'b00 : 2'b10;

    // Read Data Mux
    always @(*) begin
        case (axi_araddr)
            'h00:  axi_rdata = reg_array[0];
            'h10:  axi_rdata = reg_array[4];
            'h20:  axi_rdata = reg_array[8];
            'h24:  axi_rdata = reg_array[9];
            'h28:  axi_rdata = reg_array[10];
            default: axi_rdata = 'h0;
        endcase
    end

endmodule