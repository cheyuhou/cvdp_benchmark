module axi_register #(
    parameter integer ADDR_WIDTH = 32,
    parameter integer DATA_WIDTH = 32
) (
    input  wire                    clk_i,
    input  wire                    rst_n_i,
    input  wire [ADDR_WIDTH-1:0]   awaddr_i,
    input  wire                    awvalid_i,
    output wire                    awready_o,
    input  wire [DATA_WIDTH-1:0]   wdata_i,
    input  wire                    wvalid_i,
    output wire                    wready_o,
    input  wire [(DATA_WIDTH/8)-1:0] wstrb_i,
    input  wire                    bready_i,
    output wire [1:0]              bresp_o,
    output wire                    bvalid_o,
    input  wire [ADDR_WIDTH-1:0]   araddr_i,
    input  wire                    arvalid_i,
    output wire                    arready_o,
    output wire [DATA_WIDTH-1:0]   rdata_o,
    output wire                    rvalid_o,
    output wire [1:0]              rresp_o,
    input  wire                    done_i,
    output wire [19:0]             beat_o,
    output wire                    start_o,
    output wire                    writeback_o
);

    // Internal Registers
    logic [DATA_WIDTH-1:0] reg_beat;
    logic                  reg_start;
    logic                  reg_done;
    logic                  reg_writeback;

    // Address Decoding
    logic [2:0] w_addr_sel;
    logic [2:0] r_addr_sel;

    always_comb begin
        case (awaddr_i)
            32'h0000_0100: w_addr_sel = 3'd0;
            32'h0000_0200: w_addr_sel = 3'd1;
            32'h0000_0300: w_addr_sel = 3'd2;
            32'h0000_0400: w_addr_sel = 3'd3;
            32'h0000_0500: w_addr_sel = 3'd4;
            default:       w_addr_sel = 3'd5;
        endcase
    end

    always_comb begin
        case (araddr_i)
            32'h0000_0100: r_addr_sel = 3'd0;
            32'h0000_0200: r_addr_sel = 3'd1;
            32'h0000_0300: r_addr_sel = 3'd2;
            32'h0000_0400: r_addr_sel = 3'd3;
            32'h0000_0500: r_addr_sel = 3'd4;
            default:       r_addr_sel = 3'd5;
        endcase
    end

    // Write FSM
    typedef enum logic [1:0] { AW_IDLE, AW_WAIT, AW_DATA, AW_RESP } aw_fsm_t;
    aw_fsm_t aw_state, aw_next;

    // Read FSM
    typedef enum logic [1:0] { AR_IDLE, AR_WAIT, AR_DATA } ar_fsm_t;
    ar_fsm_t ar_state, ar_next;

    always_ff @(posedge clk_i or negedge rst_n_i) begin
        if (!rst_n_i) begin
            aw_state <= AW_IDLE;
            ar_state <= AR_IDLE;
            reg_beat <= '0;
            reg_start <= '0;
            reg_done <= '0;
            reg_writeback <= '0;
        end else begin
            // Write FSM State Transition
            case (aw_state)
                AW_IDLE:    aw_next <= awvalid_i ? AW_WAIT : AW_IDLE;
                AW_WAIT:    aw_next <= (awvalid_i & awready_o) ? AW_DATA : (awvalid_i ? AW_WAIT : AW_IDLE);
                AW_DATA:    aw_next <= (wvalid_i & wready_o) ? AW_RESP : (wvalid_i ? AW_DATA : AW_DATA);
                AW_RESP:    aw_next <= (bready_i & bvalid_o) ? AW_IDLE : AW_RESP;
                default:    aw_next <= AW_IDLE;
            endcase

            // Read FSM State Transition
            case (ar_state)
                AR_IDLE:    ar_next <= arvalid_i ? AR_WAIT : AR_IDLE;
                AR_WAIT:    ar_next <= (arvalid_i & arready_o) ? AR_DATA : (arvalid_i ? AR_WAIT : AR_IDLE);
                AR_DATA:    ar_next <= (rready_i & rvalid_o) ? AR_IDLE : AR_DATA;
                default:    ar_next <= AR_IDLE;
            endcase

            aw_state <= aw_next;
            ar_state <= ar_next;

            // Register Updates on Valid Write Handshake
            if (wvalid_i & wready_o) begin
                logic full_write;
                full_write = (&wstrb_i) && (wstrb_i != 0);
                if (full_write) begin
                    case (w_addr_sel)
                        3'd0: reg_beat  <= wdata_i[19:0];
                        3'd1: reg_start <= wdata_i[0];
                        3'd2: reg_done  <= ~wdata_i[0]; // Clear done status
                        3'd3: reg_writeback <= wdata_i[0];
                        3'd4: ; // ID is Read-Only, no update
                        default: ;
                    endcase
                end
            end
            
            // Hardware done status input updates register
            reg_done <= reg_done | done_i;
        end
    end

    // Handshake & Response Signals
    assign awready_o = (aw_state == AW_IDLE) || (aw_state == AW_WAIT);
    assign wready_o  = (aw_state == AW_DATA);
    assign bvalid_o  = (aw_state == AW_RESP);
    assign bresp_o   = (w_addr_sel == 3'd4 || w_addr_sel == 3'd5) ? 2'b10 : 2'b00;

    assign arready_o = (ar_state == AR_IDLE) || (ar_state == AR_WAIT);
    assign rvalid_o  = (ar_state == AR_DATA);
    assign rresp_o   = (r_addr_sel == 3'd4 || r_addr_sel == 3'd5) ? 2'b10 : 2'b00;

    // Read Data Mux
    always_comb begin
        case (r_addr_sel)
            3'd0: rdata_o = {DATA_WIDTH-20'd0, reg_beat[19:0]};
            3'd1: rdata_o = {DATA_WIDTH-1'd0, reg_start};
            3'd2: rdata_o = {DATA_WIDTH-1'd0, reg_done};
            3'd3: rdata_o = {DATA_WIDTH-1'd0, reg_writeback};
            3'd4: rdata_o = 32'h0001_0001;
            default: rdata_o = '0;
        endcase
    end

    // Output Assignments
    assign beat_o    = reg_beat[19:0];
    assign start_o   = reg_start;
    assign writeback_o = reg_writeback;

endmodule
