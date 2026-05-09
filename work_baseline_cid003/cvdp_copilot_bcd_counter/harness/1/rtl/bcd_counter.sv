module bcd_counter (
    input  wire clk,
    input  wire rst,
    output logic [3:0] ms_hr,
    output logic [3:0] ls_hr,
    output logic [3:0] ms_min,
    output logic [3:0] ls_min,
    output logic [3:0] ms_sec,
    output logic [3:0] ls_sec
);

    // Internal registers for each BCD digit
    logic [3:0] sec_l, sec_m;
    logic [3:0] min_l, min_m;
    logic [3:0] hr_l,  hr_m;

    always @(posedge clk) begin
        if (rst) begin
            // Synchronous active-high reset
            sec_l <= 4'd0; sec_m <= 4'd0;
            min_l <= 4'd0; min_m <= 4'd0;
            hr_l  <= 4'd0; hr_m  <= 4'd0;
        end else begin
            // Seconds Counter (00-59)
            sec_l <= (sec_l == 4'd9) ? 4'd0 : sec_l + 1'b1;
            sec_m <= (sec_m == 4'd5) ? 4'd0 : (sec_l == 4'd9 ? sec_m + 1'b1 : sec_m);

            // Minutes Counter (00-59)
            min_l <= (min_l == 4'd9) ? 4'd0 : (sec_m == 4'd5 ? min_l + 1'b1 : min_l);
            min_m <= (min_m == 4'd5) ? 4'd0 : (min_l == 4'd9 ? min_m + 1'b1 : min_m);

            // Hours Counter (00-23)
            // hr_l resets to 0 at 9, or at 3 when ms_hr is 2
            hr_l  <= (hr_l == 4'd9 || (hr_m == 4'd2 && hr_l == 4'd3)) ? 4'd0 : hr_l + 1'b1;
            // hr_m resets to 0 when hour rolls from 23->00, otherwise increments on hr_l rollover
            hr_m  <= (hr_m == 4'd2 && hr_l == 4'd3) ? 4'd0 : 
                     (hr_l == 4'd9 || (hr_m == 4'd2 && hr_l == 4'd3)) ? hr_m + 1'b1 : hr_m;
        end
    end

    // Map internal registers to module outputs
    assign ls_sec = sec_l;
    assign ms_sec = sec_m;
    assign ls_min = min_l;
    assign ms_min = min_m;
    assign ls_hr  = hr_l;
    assign ms_hr  = hr_m;

endmodule
