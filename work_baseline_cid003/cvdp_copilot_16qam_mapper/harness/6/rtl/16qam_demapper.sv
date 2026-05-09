module qam16_demapper_interpolated #(
    parameter int unsigned N = 4,
    parameter int unsigned IN_WIDTH = 3,
    parameter int unsigned OUT_WIDTH = 4,
    parameter int unsigned ERROR_THRESHOLD = 2
)(
    input  logic [((N + N/2) * IN_WIDTH) - 1 : 0] I,
    input  logic [((N + N/2) * IN_WIDTH) - 1 : 0] Q,
    output logic [N * OUT_WIDTH - 1 : 0] bits,
    output logic error_flag
);

    // Helper function to map 3-bit signed QAM16 amplitude levels to 2-bit symbols
    function automatic logic[1:0] map_symbol(logic [IN_WIDTH-1:0] val);
        if ($signed(val) == -3) return 2'b00;
        if ($signed(val) == -1) return 2'b01;
        if ($signed(val) ==  1) return 2'b10;
        return 2'b11;
    endfunction

    always_comb begin
        bits = '0;
        error_flag = 1'b0;

        // 1. Error Detection on Interpolated Samples
        // Interpolated samples are located at indices: 1, 4, 7, ... (k*3 + 1)
        for (int unsigned k = 0; k < N/2; k++) begin
            int unsigned int_idx  = k * 3 + 1;
            int unsigned left_idx = k * 3;
            int unsigned right_idx = k * 3 + 2;

            logic [IN_WIDTH-1:0] I_int, Q_int;
            logic [IN_WIDTH-1:0] I_left, Q_left;
            logic [IN_WIDTH-1:0] I_right, Q_right;

            // Extract current interpolated and surrounding mapped values
            I_int  = I[IN_WIDTH*int_idx +: IN_WIDTH];
            Q_int  = Q[IN_WIDTH*int_idx +: IN_WIDTH];
            I_left = I[IN_WIDTH*left_idx  +: IN_WIDTH];
            Q_left = Q[IN_WIDTH*left_idx  +: IN_WIDTH];
            I_right = I[IN_WIDTH*right_idx +: IN_WIDTH];
            Q_right = Q[IN_WIDTH*right_idx +: IN_WIDTH];

            // Expected interpolated value = (left + right) / 2
            // Use IN_WIDTH+1 bits to prevent overflow during addition
            logic [IN_WIDTH:0] I_sum, Q_sum;
            I_sum = $signed(I_left) + $signed(I_right);
            Q_sum = $signed(Q_left) + $signed(Q_right);
            
            // Arithmetic right shift by 1 performs division by 2 preserving sign
            logic [IN_WIDTH-1:0] I_exp, Q_exp;
            I_exp = I_sum >>> 1;
            Q_exp = Q_sum >>> 1;

            // Calculate difference: actual - expected
            // Use IN_WIDTH+1 bits to capture full range of possible differences
            logic [IN_WIDTH:0] I_diff, Q_diff;
            I_diff = $signed(I_int) - $signed(I_exp);
            Q_diff = $signed(Q_int) - $signed(Q_exp);

            // Compute absolute magnitude of deviation
            logic [IN_WIDTH:0] I_abs, Q_abs;
            I_abs = I_diff < 0 ? -I_diff : I_diff;
            Q_abs = Q_diff < 0 ? -Q_diff : Q_diff;

            // Flag error if deviation exceeds threshold
            if (I_abs > ERROR_THRESHOLD || Q_abs > ERROR_THRESHOLD) begin
                error_flag = 1'b1;
            end
        end

        // 2. Demapping Mapped Samples
        // Mapped samples occur at indices: 0, 2, 3, 5, 6, 8, ... (k*3 and k*3+2)
        int unsigned bits_idx = 0;
        for (int unsigned k = 0; k < N/2; k++) begin
            int unsigned idx1 = k * 3;
            int unsigned idx2 = k * 3 + 2;

            logic [IN_WIDTH-1:0] I1, Q1, I2, Q2;
            I1 = I[IN_WIDTH*idx1 +: IN_WIDTH];
            Q1 = Q[IN_WIDTH*idx1 +: IN_WIDTH];
            I2 = I[IN_WIDTH*idx2 +: IN_WIDTH];
            Q2 = Q[IN_WIDTH*idx2 +: IN_WIDTH];

            // I component forms MSBs (2 bits), Q component forms LSBs (2 bits)
            bits[bits_idx +: OUT_WIDTH] = { map_symbol(I1), map_symbol(Q1) };
            bits_idx += OUT_WIDTH;

            bits[bits_idx +: OUT_WIDTH] = { map_symbol(I2), map_symbol(Q2) };
            bits_idx += OUT_WIDTH;
        end
    end

endmodule
