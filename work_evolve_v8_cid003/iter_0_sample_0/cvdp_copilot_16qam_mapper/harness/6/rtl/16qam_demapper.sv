module qam16_demapper_interpolated #(
    parameter int N = 4,
    parameter int IN_WIDTH = 3,
    parameter int OUT_WIDTH = 4,
    parameter int ERROR_THRESHOLD = 1
)(
    input  logic [((N + N/2) * IN_WIDTH) - 1:0] I,
    input  logic [((N + N/2) * IN_WIDTH) - 1:0] Q,
    output logic [(N * OUT_WIDTH) - 1:0] bits,
    output logic error_flag
);

    // Unpacked arrays for mapped and interpolated values
    logic signed [IN_WIDTH-1:0] mapped_I [0:N-1];
    logic signed [IN_WIDTH-1:0] mapped_Q [0:N-1];
    logic signed [IN_WIDTH-1:0] interp_I [0:N/2-1];
    logic signed [IN_WIDTH-1:0] interp_Q [0:N/2-1];

    // Input parsing: Group pattern is [Mapped, Interpolated, Mapped] per block
    generate
        for (genvar k = 0; k < N/2; k++) begin : unpack_blk
            localparam int base = 3*k * IN_WIDTH;
            
            assign mapped_I[2*k]       = signed'(I[base +: IN_WIDTH]);
            assign interp_I[k]         = signed'(I[base + IN_WIDTH +: IN_WIDTH]);
            assign mapped_I[2*k + 1]   = signed'(I[base + 2*IN_WIDTH +: IN_WIDTH]);

            assign mapped_Q[2*k]       = signed'(Q[base +: IN_WIDTH]);
            assign interp_Q[k]         = signed'(Q[base + IN_WIDTH +: IN_WIDTH]);
            assign mapped_Q[2*k + 1]   = signed'(Q[base + 2*IN_WIDTH +: IN_WIDTH]);
        end
    endgenerate

    // Error detection signals
    logic [IN_WIDTH:0] sum_I [0:N/2-1];
    logic [IN_WIDTH:0] sum_Q [0:N/2-1];
    logic [IN_WIDTH:0] expected_I [0:N/2-1];
    logic [IN_WIDTH:0] expected_Q [0:N/2-1];
    logic [IN_WIDTH:0] diff_I [0:N/2-1];
    logic [IN_WIDTH:0] diff_Q [0:N/2-1];
    logic [IN_WIDTH:0] abs_diff_I [0:N/2-1];
    logic [IN_WIDTH:0] abs_diff_Q [0:N/2-1];
    logic err_I [0:N/2-1];
    logic err_Q [0:N/2-1];

    generate
        for (genvar k = 0; k < N/2; k++) begin : err_blk
            // Expected interpolated value = (mapped1 + mapped2) / 2
            assign sum_I[k] = mapped_I[2*k] + mapped_I[2*k+1];
            assign sum_Q[k] = mapped_Q[2*k] + mapped_Q[2*k+1];

            // Arithmetic shift right by 1 for division by 2 (preserves sign)
            assign expected_I[k] = sum_I[k] >>> 1;
            assign expected_Q[k] = sum_Q[k] >>> 1;

            // Difference between actual and expected interpolated values
            assign diff_I[k] = interp_I[k] - expected_I[k];
            assign diff_Q[k] = interp_Q[k] - expected_Q[k];

            // Absolute difference
            assign abs_diff_I[k] = (diff_I[k] >= 0) ? diff_I[k] : (-diff_I[k]);
            assign abs_diff_Q[k] = (diff_Q[k] >= 0) ? diff_Q[k] : (-diff_Q[k]);

            // Error flag per block
            assign err_I[k] = (abs_diff_I[k] > ERROR_THRESHOLD);
            assign err_Q[k] = (abs_diff_Q[k] > ERROR_THRESHOLD);
        end
    endgenerate

    // Global error flag: asserted if any block exceeds threshold
    assign error_flag = |err_I | |err_Q;

    // QAM16 constellation mapping function
    function automatic logic[1:0] map_val(logic signed [IN_WIDTH-1:0] v);
        if (v == (IN_WIDTH)'(3)) return 2'b11;
        if (v == (IN_WIDTH)'(1)) return 2'b10;
        if (v == (IN_WIDTH)'(-1)) return 2'b01;
        return 2'b00; // Maps to -3
    endfunction

    // Bit stream construction: MSBs from I, LSBs from Q
    generate
        for (genvar m = 0; m < N; m++) begin : map_blk
            assign bits[4*m + 3 -: 2] = map_val(mapped_I[m]);
            assign bits[4*m + 1 -: 2] = map_val(mapped_Q[m]);
        end
    endgenerate

endmodule
