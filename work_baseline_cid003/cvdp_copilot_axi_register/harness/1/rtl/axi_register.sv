// rtl/axi_register.sv
module axi_register #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned DATA_WIDTH = 32
)(
    input  logic                             clk_i,
    input  logic                             rst_n_i,
    input  logic [ADDR_WIDTH-1:0]            awaddr_i,
    input  logic                             awvalid_i,
    output logic                             awready_o,
    input  logic [DATA_WIDTH-1:0]            wdata_i,
    input  logic                             wvalid_i,
    output logic                             wready_o,
    input  logic [(DATA_WIDTH/8)-1:0]        wstrb_i,
    input  logic                             bready_i,
    output logic [1:0]                       bresp_o,
    output logic                             bvalid_o,
    input  logic [ADDR_WIDTH-1:0]            araddr_i,
    input  logic                             arvalid_i,
    output logic                             arready_o,
    output logic [DATA_WIDTH-1:0]            rdata_o,
    output logic                             rvalid_o,
    output logic [1:0]                       rresp_o,
    input  logic                             done_i,
    output logic [19:0]                      beat_o,
    output logic                             start_o,
    output logic                             writeback_o
);

    // Internal register storage
    logic [19:0] beat_reg;
    logic        start_reg;
    logic        done_reg;
    logic        writeback_reg;

    // FSM State Definitions
    localparam [2:0] WR_IDLE = 3'b000, WR_AW = 3'b001, WR_W = 3'b010, WR_B = 3'b011;
    localparam [2:0] RD_IDLE = 3'b000, RD_AR = 3'b001, RD_R = 3'b010;

    logic [2:0] wr_state, rd_state;
    logic [ADDR_WIDTH-1:0] awaddr_q, araddr_q;
    logic [1:0] bresp_q, rresp_q;

    // Local parameters for address decoding and byte width
    localparam int unsigned BYTE_WIDTH = DATA_WIDTH / 8;

    // Address decoding logic
    logic [ADDR_WIDTH-1:0] wr_addr_dec = awaddr_q;
    logic [ADDR_WIDTH-1:0] rd_addr_dec = araddr_q;

    logic is_wr_valid, is_wr_ro, is_rd_valid;
    logic full_write;

    assign full_write = (wstrb_i == {BYTE_WIDTH{1'b1}});

    assign is_wr_valid = (wr_addr_dec[ADDR_WIDTH-1:8] == 8'h01) |
                         (wr_addr_dec[ADDR_WIDTH-1:8] == 8'h02) |
                         (wr_addr_dec[ADDR_WIDTH-1:8] == 8'h03) |
                         (wr_addr_dec[ADDR_WIDTH-1:8] == 8'h04);

    assign is_wr_ro = (wr_addr_dec[ADDR_WIDTH-1:8] == 8'h05);

    assign is_rd_valid = is_wr_valid | (rd_addr_dec[ADDR_WIDTH-1:8] == 8'h05);

    // Register Update Logic
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) begin
            beat_reg     <= 20'b0;
            start_reg    <= 1'b0;
            done_reg     <= 1'b0;
            writeback_reg<= 1'b0;
        end else begin
            // External hardware completion signal sets done status
            done_reg <= done_i;

            // Apply write to registers only when full write is performed
            if (wvalid_i && wready_o && full_write) begin
                case (awaddr_q[ADDR_WIDTH-1:8])
                    8'h01: beat_reg     <= wdata_i[19:0];
                    8'h02: start_reg    <= wdata_i[0];
                    8'h03: done_reg     <= ~wdata_i[0];
                    8'h04: writeback_reg<= wdata_i[0];
                    default: ;
                endcase
            end
        end
    end

    // AXI4-Lite FSM and Handshaking
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) begin
            wr_state    <= WR_IDLE;
            rd_state    <= RD_IDLE;
            awready_o   <= 1'b0;
            wready_o    <= 1'b0;
            bvalid_o    <= 1'b0;
            arready_o   <= 1'b0;
            rvalid_o    <= 1'b0;
            bresp_q     <= 2'b00;
            rresp_q     <= 2'b00;
        end else begin
            // Write FSM
            case (wr_state)
                WR_IDLE: begin
                    awready_o <= awvalid_i;
                    if (awvalid_i) wr_state <= WR_AW;
                end
                WR_AW: begin
                    awready_o <= 1'b0;
                    wready_o  <= 1'b0;
                    awaddr_q  <= awaddr_i;
                    if (wvalid_i) wr_state <= WR_W;
                end
                WR_W: begin
                    wready_o  <= 1'b1;
                    if (wvalid_i) begin
                        bresp_q <= (is_wr_valid && ~is_wr_ro) ? 2'b00 : 2'b10;
                        wr_state <= WR_B;
                    end
                end
                WR_B: begin
                    bvalid_o  <= 1'b1;
                    if (bvalid_o && bready_i) begin
                        bvalid_o <= 1'b0;
                        wr_state <= WR_IDLE;
                    end
                end
            endcase

            // Read FSM
            case (rd_state)
                RD_IDLE: begin
                    arready_o <= arvalid_i;
                    if (arvalid_i) rd_state <= RD_AR;
                end
                RD_AR: begin
                    arready_o <= 1'b0;
                    araddr_q  <= araddr_i;
                    rresp_q   <= is_rd_valid ? 2'b00 : 2'b10;
                    rd_state  <= RD_R;
                end
                RD_R: begin
                    rvalid_o  <= 1'b1;
                    if (rvalid_o && rready_i) begin
                        rvalid_o <= 1'b0;
                        rd_state <= RD_IDLE;
                    end
                end
            endcase
            bresp_o <= bresp_q;
            rresp_o <= rresp_q;
        end
    end

    // Read Data Mux
    always_comb begin
        unique case (araddr_i[ADDR_WIDTH-1:8])
            8'h01: rdata_o = {DATA_WIDTH-20{1'b0}, beat_reg};
            8'h02: rdata_o = {DATA_WIDTH-1{1'b0}, start_reg};
            8'h03: rdata_o = {DATA_WIDTH-1{1'b0}, done_reg};
            8'h04: rdata_o = {DATA_WIDTH-1{1'b0}, writeback_reg};
            8'h05: rdata_o = 32'h00010001;
            default: rdata_o = '0;
        endcase
    end

    // Output Assignments
    assign beat_o     = beat_reg;
    assign start_o    = start_reg;
    assign writeback_o= writeback_reg;

endmodule
