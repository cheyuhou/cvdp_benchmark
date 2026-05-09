module bcd_counter (
    input  logic clk,
    input  logic rst,
    output logic [3:0] ms_hr,
    output logic [3:0] ls_hr,
    output logic [3:0] ms_min,
    output logic [3:0] ls_min,
    output logic [3:0] ms_sec,
    output logic [3:0] ls_sec
);
    logic [3:0] n_ls_sec, n_ms_sec;
    logic [3:0] n_ls_min, n_ms_min;
    logic [3:0] n_ls_hr,  n_ms_hr;

    // Combinational logic to compute next states
    always_comb begin
        // Default: hold current value
        n_ls_sec = ls_sec; n_ms_sec = ms_sec;
        n_ls_min = ls_min; n_ms_min = ms_min;
        n_ls_hr  = ls_hr;  n_ms_hr  = ms_hr;

        // Seconds logic
        if (ls_sec == 4'd9) begin
            n_ls_sec = 4'd0;
            if (ms_sec == 4'd5) begin
                n_ms_sec = 4'd0;
                // Carry to Minutes
                if (ls_min == 4'd9) begin
                    n_ls_min = 4'd0;
                    if (ms_min == 4'd5) begin
                        n_ms_min = 4'd0;
                        // Carry to Hours
                        if (ms_hr == 4'd2 && ls_hr == 4'd3) begin
                            n_ms_hr = 4'd0;
                            n_ls_hr = 4'd0;
                        end else if (ls_hr == 4'd9) begin
                            n_ls_hr = 4'd0;
                            n_ms_hr = ms_hr + 1'b1;
                        end else begin
                            n_ls_hr = ls_hr + 1'b1;
                        end
                    end else begin
                        n_ms_min = ms_min + 1'b1;
                    end
                end else begin
                    n_ls_min = ls_min + 1'b1;
                end
            end else begin
                n_ms_sec = ms_sec + 1'b1;
            end
        end else begin
            n_ls_sec = ls_sec + 1'b1;
        end
    end

    // Sequential logic to register outputs
    always_ff @(posedge clk or posedge rst) begin
        if (rst) begin
            ms_hr <= 4'd0;
            ls_hr <= 4'd0;
            ms_min <= 4'd0;
            ls_min <= 4'd0;
            ms_sec <= 4'd0;
            ls_sec <= 4'd0;
        end else begin
            ms_hr <= n_ms_hr;
            ls_hr <= n_ls_hr;
            ms_min <= n_ms_min;
            ls_min <= n_ls_min;
            ms_sec <= n_ms_sec;
            ls_sec <= n_ls_sec;
        end
    end
endmodule
