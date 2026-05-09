// File: rtl/car_parking_system.sv

module car_parking_system #
(
    parameter TOTAL_SPACES = 12
)
(
    input  wire                     clk,
    input  wire                     reset,
    input  wire                     vehicle_entry_sensor,
    input  wire                     vehicle_exit_sensor,
    output reg  [$clog2(TOTAL_SPACES)-1:0] available_spaces,
    output reg  [$clog2(TOTAL_SPACES)-1:0] count_car,
    output reg                      led_status,
    output reg  [6:0]               seven_seg_display_available_tens,
    output reg  [6:0]               seven_seg_display_available_units,
    output reg  [6:0]               seven_seg_display_count_tens,
    output reg  [6:0]               seven_seg_display_count_units
);

    // FSM State Encoding
    typedef enum logic [1:0] {
        Idle    = 2'b00,
        Entry   = 2'b01,
        Exit    = 2'b10,
        Full    = 2'b11
    } state_t;

    state_t current_state, next_state;

    // 7-Segment Encoding Function
    // MSB (bit 6) is Segment A, LSB (bit 0) is Segment G
    function automatic [6:0] encode_digit;
        input [3:0] digit;
        case (digit)
            4'd0: encode_digit = 7'b1111110; // ABCDEF
            4'd1: encode_digit = 7'b0110000; // BC
            4'd2: encode_digit = 7'b1101101; // ABDEG
            4'd3: encode_digit = 7'b1111001; // ABCDG
            4'd4: encode_digit = 7'b0110011; // BCFG
            4'd5: encode_digit = 7'b1011011; // ACDG
            4'd6: encode_digit = 7'b1011111; // ACDEFG
            4'd7: encode_digit = 7'b1110000; // ABC
            4'd8: encode_digit = 7'b1111111; // ABCDEFG
            4'd9: encode_digit = 7'b1111011; // ABCDFG
            default: encode_digit = 7'b0000000;
        endcase
    endfunction

    // FSM and Register Updates
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            current_state <= Idle;
            available_spaces <= TOTAL_SPACES;
            count_car <= 0;
            led_status <= 1;
        end else begin
            case (current_state)
                Idle: begin
                    if (vehicle_entry_sensor && available_spaces > 0) begin
                        next_state <= Entry;
                    end else if (vehicle_exit_sensor && count_car > 0) begin
                        next_state <= Exit;
                    end else if (count_car == TOTAL_SPACES) begin
                        next_state <= Full;
                    end else begin
                        next_state <= Idle;
                    end
                end
                Entry: begin
                    available_spaces <= available_spaces - 1;
                    count_car <= count_car + 1;
                    next_state <= Idle;
                end
                Exit: begin
                    available_spaces <= available_spaces + 1;
                    count_car <= count_car - 1;
                    next_state <= Idle;
                end
                Full: begin
                    if (vehicle_exit_sensor && count_car > 0) begin
                        next_state <= Exit;
                    end else begin
                        next_state <= Full;
                    end
                end
            endcase
            
            // Update outputs based on state transitions or current values
            // led_status is high if spaces are available
            led_status <= (available_spaces > 0) ? 1 : 0;
        end
    end

    // Combinational logic for 7-segment displays
    // Tens and Units calculation
    wire [3:0] avail_tens = available_spaces / 10;
    wire [3:0] avail_units = available_spaces % 10;
    wire [3:0] count_tens = count_car / 10;
    wire [3:0] count_units = count_car % 10;

    // Encoding and driving outputs
    assign seven_seg_display_available_tens = encode_digit(avail_tens);
    assign seven_seg_display_available_units = encode_digit(avail_units);
    assign seven_seg_display_count_tens = encode_digit(count_tens);
    assign seven_seg_display_count_units = encode_digit(count_units);

endmodule
