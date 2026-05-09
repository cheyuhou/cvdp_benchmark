parameter TOTAL_SPACES = 12;

module car_parking_system (
    input  logic                  clk,
    input  logic                  reset,
    input  logic                  vehicle_entry_sensor,
    input  logic                  vehicle_exit_sensor,

    output logic [$clog2(TOTAL_SPACES)-1:0] available_spaces,
    output logic [$clog2(TOTAL_SPACES)-1:0] count_car,
    output logic                            led_status,
    output logic [6:0]                      seven_seg_display_available_tens,
    output logic [6:0]                      seven_seg_display_available_units,
    output logic [6:0]                      seven_seg_display_count_tens,
    output logic [6:0]                      seven_seg_display_count_units
);

    localparam WIDTH = $clog2(TOTAL_SPACES);

    // FSM State Encoding
    typedef enum logic [1:0] {
        STATE_IDLE       = 2'b00,
        STATE_ENTRY_PROC = 2'b01,
        STATE_EXIT_PROC  = 2'b10,
        STATE_FULL       = 2'b11
    } fsm_state_t;

    fsm_state_t current_state, next_state;
    logic [WIDTH-1:0] avail_reg, count_reg;
    logic [WIDTH-1:0] avail_next, count_next;

    // Synchronous Register & FSM Update
    always_ff @(posedge clk or posedge reset) begin
        if (reset) begin
            current_state <= STATE_IDLE;
            avail_reg     <= TOTAL_SPACES;
            count_reg     <= '0;
        end else begin
            current_state <= next_state;
            avail_reg     <= avail_next;
            count_reg     <= count_next;
        end
    end

    // Combinational Next State & Data Path Logic
    always_comb begin
        // Default assignments
        next_state  = STATE_IDLE;
        avail_next  = avail_reg;
        count_next  = count_reg;

        case (current_state)
            STATE_IDLE: begin
                if (vehicle_entry_sensor) begin
                    next_state = STATE_ENTRY_PROC;
                end else if (vehicle_exit_sensor) begin
                    next_state = STATE_EXIT_PROC;
                end
            end

            STATE_ENTRY_PROC: begin
                // Decrement available, increment count
                if (avail_reg > 0) begin
                    avail_next = avail_reg - 1;
                    count_next = count_reg + 1;
                end
                // Transition to Full if parking reaches capacity
                if (avail_next == '0) begin
                    next_state = STATE_FULL;
                end else begin
                    next_state = STATE_IDLE;
                end
            end

            STATE_EXIT_PROC: begin
                // Increment available, decrement count
                if (count_reg > 0) begin
                    avail_next = avail_reg + 1;
                    count_next = count_reg - 1;
                end
                next_state = STATE_IDLE;
            end

            STATE_FULL: begin
                // Deny entry. Only process exit to allow transition.
                if (vehicle_exit_sensor) begin
                    next_state = STATE_EXIT_PROC;
                end
                // Entry requests are ignored while in FULL state
            end

            default: next_state = STATE_IDLE;
        endcase
    end

    // Output Assignments
    assign available_spaces = avail_reg;
    assign count_car        = count_reg;
    assign led_status       = (avail_reg == '0) ? 1'b0 : 1'b1;

    // BCD Digit Extraction for 7-Segment Displays
    logic [3:0] avail_tens, avail_units;
    logic [3:0] count_tens, count_units;

    always_comb begin
        avail_tens  = avail_reg / 10;
        avail_units = avail_reg % 10;
        count_tens  = count_reg / 10;
        count_units = count_reg % 10;
    end

    // 7-Segment Decoder
    // Mapping: MSB -> Segment A, LSB -> Segment G
    function automatic logic [6:0] decode_7seg(input logic [3:0] digit);
        case (digit)
            4'd0: return 7'b1111110; // A,B,C,D,E,F = 1; G = 0
            4'd1: return 7'b0110000; // B,C = 1; Others = 0
            4'd2: return 7'b1101101; // A,B,D,E,G = 1; C,F = 0
            4'd3: return 7'b1111001; // A,B,C,D,G = 1; E,F = 0
            4'd4: return 7'b0110011; // B,C,F,G = 1; A,D,E = 0
            4'd5: return 7'b1011011; // A,C,D,F,G = 1; B,E = 0
            4'd6: return 7'b1011111; // A,C,D,E,F,G = 1; B = 0
            4'd7: return 7'b1110000; // A,B,C = 1; Others = 0
            4'd8: return 7'b1111111; // All segments = 1
            4'd9: return 7'b1111011; // A,B,C,D,F,G = 1; E = 0
            default: return 7'b0000000;
        endcase
    endfunction

    assign seven_seg_display_available_tens  = decode_7seg(avail_tens);
    assign seven_seg_display_available_units = decode_7seg(avail_units);
    assign seven_seg_display_count_tens      = decode_7seg(count_tens);
    assign seven_seg_display_count_units     = decode_7seg(count_units);

endmodule