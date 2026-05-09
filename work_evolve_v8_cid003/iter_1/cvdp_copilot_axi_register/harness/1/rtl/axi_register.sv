module axi_register #(
    parameter int ADDR_WIDTH = 32,
    parameter int DATA_WIDTH = 32
)(
    input  logic                        clk_i,
    input  logic                        rst_n_i,
    input  logic [ADDR_WIDTH-1:0]       awaddr_i,
    input  logic                        awvalid_i,
    input  logic [DATA_WIDTH-1:0]       wdata_i,
    input  logic                        wvalid_i,
    input  logic [(DATA_WIDTH/8)-1:0]   wstrb_i,
    input  logic                        bready_i,
    input  logic [ADDR_WIDTH-1:0]       araddr_i,
    input  logic                        arvalid_i,
    input  logic                        rready_i,
    input  logic                        done_i,

    output logic                        awready_o,
    output logic                        wready_o,
    output logic [1:0]                  bresp_o,
    output logic                        bvalid_o,
    output logic                        arready_o,
    output logic [DATA_WIDTH-1:0]       rdata_o,
    output logic                        rvalid_o,
    output logic [1:0]                  rresp_o,
    output logic [19:0]                 beat_o,
    output logic                        start_o,
    output logic                        writeback_o
);

    // ------------------------------------------------------------------
    // Internal Signals for AXI Handshaking & Flow Control
    // ------------------------------------------------------------------
    logic aw_busy;
    logic w_busy;
    logic ar_busy;

    // Latched addresses for decode during data/response phases
    logic [ADDR_WIDTH-1:0] aw_addr_latch;
    logic [ADDR_WIDTH-1:0] ar_addr_latch;

    // Register storage
    logic [19:0] beat_reg;
    logic start_reg;
    logic done_reg;
    logic writeback_reg;

    // Error flags for response generation
    logic aw_err;
    logic ar_err;

    // ------------------------------------------------------------------
    // AXI Handshaking Assignments
    // ------------------------------------------------------------------
    assign awready_o = ~aw_busy;
    assign wready_o = aw_busy;
    assign bvalid_o = w_busy;
    assign arready_o = ~ar_busy;
    assign rvalid_o = ar_busy;

    // ------------------------------------------------------------------
    // Address Latching
    // ------------------------------------------------------------------
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) aw_addr_latch <= '0;
        else if (awvalid_i && awready_o) aw_addr_latch <= awaddr_i;
    end

    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) ar_addr_latch <= '0;
        else if (arvalid_i && arready_o) ar_addr_latch <= araddr_i;
    end

    // ------------------------------------------------------------------
    // Write Channel FSM (Busy Flags)
    // ------------------------------------------------------------------
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) aw_busy <= 1'b0;
        else if (awvalid_i && awready_o) aw_busy <= 1'b1;
        else if (wready_o) aw_busy <= 1'b0;
    end

    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) w_busy <= 1'b0;
        else if (wvalid_i && wready_o) w_busy <= 1'b1;
        else if (bready_i) w_busy <= 1'b0;
    end

    // ------------------------------------------------------------------
    // Read Channel FSM (Busy Flags)
    // ------------------------------------------------------------------
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) ar_busy <= 1'b0;
        else if (arvalid_i && arready_o) ar_busy <= 1'b1;
        else if (rready_i) ar_busy <= 1'b0;
    end

    // ------------------------------------------------------------------
    // Address Decoding & Error Detection
    // ------------------------------------------------------------------
    always_comb begin
        aw_err = 1'b0;
        ar_err = 1'b0;

        // Write Address Decode
        case (aw_addr_latch[11:0])
            12'h100, 12'h200, 12'h300, 12'h400: ; // Valid writable registers
            12'h500: aw_err = 1'b1; // ID is Read-Only
            default: aw_err = 1'b1; // Invalid address
        endcase

        // Read Address Decode
        case (ar_addr_latch[11:0])
            12'h100, 12'h200, 12'h300, 12'h400, 12'h500: ; // All registers readable
            default: ar_err = 1'b1; // Invalid address
        endcase
    end

    // ------------------------------------------------------------------
    // Response Logic
    // ------------------------------------------------------------------
    assign bresp_o = w_busy ? (aw_err ? 2'b10 : 2'b00) : 2'b00;
    assign rresp_o = ar_busy ? (ar_err ? 2'b10 : 2'b00) : 2'b00;

    // ------------------------------------------------------------------
    // Read Data Mux
    // ------------------------------------------------------------------
    logic [DATA_WIDTH-1:0] rdata_int;
    always_comb begin
        rdata_int = '0;
        case (ar_addr_latch[11:0])
            12'h100: rdata_int = {DATA_WIDTH-20'b0, beat_reg};
            12'h200: rdata_int = {DATA_WIDTH-1'b0, start_reg};
            12'h300: rdata_int = {DATA_WIDTH-1'b0, done_reg};
            12'h400: rdata_int = {DATA_WIDTH-1'b0, writeback_reg};
            12'h500: rdata_int = {DATA_WIDTH-32'b0, 32'h0001_0001};
            default: rdata_int = '0;
        endcase
    end
    assign rdata_o = ar_busy ? rdata_int[DATA_WIDTH-1:0] : '0;

    // ------------------------------------------------------------------
    // Output Assignments
    // ------------------------------------------------------------------
    assign beat_o = beat_reg;
    assign start_o = start_reg;
    assign writeback_o = writeback_reg;

    // ------------------------------------------------------------------
    // Register Updates & Hardware Status Reflection
    // ------------------------------------------------------------------
    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) begin
            beat_reg      <= '0;
            start_reg     <= '0;
            done_reg      <= '0;
            writeback_reg <= '0;
        end else begin
            // External hardware updates done status continuously
            done_reg <= done_i;

            // AXI Write Operations
            if (w_busy && wvalid_i && wready_o) begin
                if (wstrb_i == '1) begin // Full write: all bytes enabled
                    case (aw_addr_latch[11:0])
                        12'h100: beat_reg <= wdata_i[19:0];
                        12'h200: start_reg <= wdata_i[0];
                        12'h300: if (wdata_i[0]) done_reg <= 1'b0; // Clear done on write
                        12'h400: writeback_reg <= wdata_i[0];
                        default: ; // Invalid/RO address: no register update
                    endcase
                end
                // Partial write: spec requires acknowledgment without register modification
            end
        end
    end

endmodule