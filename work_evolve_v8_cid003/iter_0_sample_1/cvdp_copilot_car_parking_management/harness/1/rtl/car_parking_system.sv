module car_parking_system #(
    parameter int TOTAL_SPACES = 12,
    parameter int SPACE_WIDTH  = $clog2(TOTAL_SPACES)
)(
    input  wire                    clk,
    input  wire                    reset,
    input  wire                    vehicle_entry_sensor,
    input  wire                    vehicle_exit_sensor,
    output wire [SPACE_WIDTH-1:0]  available_spaces,
    output wire [SPACE_WIDTH-1:0]  count_car,
    output wire                    led_status,
    output wire [6:0]              seven_seg_display_available_tens,
    output wire [6:0]              seven_seg_display_available_units,
    output wire [6:0]              seven_seg_display_count_tens,
    output wire [6:0]              seven_seg_display_count_units
);

    // FSM State Encoding
    localparam logic [1:0] IDLE  = 2'b00;
    localparam logic [1:0] ENTRY = 2'b01;
    localparam logic [1:0] EXIT  = 2'b10;
    localparam logic [1:0] FULL  = 2'b11;

    // Internal Registers
    logic [SPACE_WIDTH-1:0] avail_r, count_r;
    logic [1:0] state_r, state_n;

    // 7-Segment Encoder Function
    // MSB = Segment A, LSB = Segment G
    function [6:0] encode_7seg(input int digit);
        case(digit)
            0: return 7'b0111111;
            1: return 7'b0000110;
            2: return 7'b1011011;
            3: return 7'b1001111;
            4: return 7'b1100110;
            5: return 7'b1101101;
            6: return 7'b1111101;
            7: return 7'b0000111;
            8: return 7'b1111111;
            9: return 7'b1101111;
            default: return 7'b0000000;
        endcase
    endfunction

    // Combinational Next-State Logic
    always @(*) begin
        case (state_r)
            IDLE: begin
                if (vehicle_entry_sensor) state_n = ENTRY;
                else if (vehicle_exit_sensor) state_n = EXIT;
                else state_n = IDLE;
            end
            ENTRY: begin
                // After entry, check if parking becomes full
                state_n = (avail_r - 1'd1 == 0) ? FULL : IDLE;
            end
            EXIT: begin
                // After exit, parking is never full
                state_n = IDLE;
            end
            FULL: begin
                // Deny entry, allow exit
                state_n = (vehicle_exit_sensor) ? EXIT : FULL;
            end
            default: state_n = IDLE;
        endcase
    end

    // Sequential Logic (State & Data Updates)
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            state_r <= IDLE;
            avail_r <= TOTAL_SPACES;
            count_r <= 0;
        end else begin
            state_r <= state_n;
            case (state_r)
                ENTRY: begin
                    avail_r <= avail_r - 1'd1;
                    count_r <= count_r + 1'd1;
                end
                EXIT: begin
                    avail_r <= avail_r + 1'd1;
                    count_r <= count_r - 1'd1;
                end
                default: begin
                    // No data update in IDLE or FULL states
                end
            endcase
        end
    end

    // Output Assignments
    assign available_spaces = avail_r;
    assign count_car        = count_r;
    assign led_status       = (avail_r == 0) ? 1'b0 : 1'b1;

    // Display Digit Extraction (Supports up to 2 digits / 99)
    logic [3:0] avail_tens, avail_units, count_tens, count_units;
    assign avail_tens  = (avail_r >= 10) ? avail_r / 10 : 4'b0;
    assign avail_units = avail_r % 10;
    assign count_tens  = (count_r >= 10) ? count_r / 10 : 4'b0;
    assign count_units = count_r % 10;

    // 7-Segment Display Encoding
    assign seven_seg_display_available_tens  = encode_7seg(avail_tens);
    assign seven_seg_display_available_units = encode_7seg(avail_units);
    assign seven_seg_display_count_tens      = encode_7seg(count_tens);
    assign seven_seg_display_count_units     = encode_7seg(count_units);

endmodule
