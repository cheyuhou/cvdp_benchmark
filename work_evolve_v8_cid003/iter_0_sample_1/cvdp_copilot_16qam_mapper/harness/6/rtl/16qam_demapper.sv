module qam16_demapper_interpolated #(
    parameter int N = 4,
    parameter int OUT_WIDTH = 4,
    parameter int IN_WIDTH = 3,
    parameter int ERROR_THRESHOLD = 1
)(
    input  logic [((N + N/2) * IN_WIDTH) - 1 : 0] I,
    input  logic [((N + N/2) * IN_WIDTH) - 1 : 0] Q,
    output logic [N * OUT_WIDTH - 1 : 0] bits,
    output logic error_flag
);
    localparam int TOT = N + N/2;
    
    // Arrays to hold parsed mapped and interpolated samples
    logic [IN_WIDTH-1:0] mapped_I [0:N-1];
    logic [IN_WIDTH-1:0] mapped_Q [0:N-1];
    logic [IN_WIDTH-1:0] interp_I [0:N/2-1];
    logic [IN_WIDTH-1:0] interp_Q [0:N/2-1];

    always_comb begin
        bits = '0;
        error_flag = 0;

        // 1. Parse Inputs according to [Mapped, Interpolated, Mapped] repeating pattern
        int i_mapped = 0;
        int i_interp = 0;
        int pattern = 0;

        for (int i = 0; i < TOT; i++) begin
            logic [IN_WIDTH-1:0] sig_i = I[i*IN_WIDTH +: IN_WIDTH];
            logic [IN_WIDTH-1:0] sig_q = Q[i*IN_WIDTH +: IN_WIDTH];

            if (pattern == 0 || pattern == 2) begin // Mapped samples
                mapped_I[i_mapped] = sig_i;
                mapped_Q[i_mapped] = sig_q;
                i_mapped++;
            end else if (pattern == 1) begin // Interpolated samples
                interp_I[i_interp] = sig_i;
                interp_Q[i_interp] = sig_q;
                i_interp++;
            end
            pattern = (pattern + 1) % 3;
        end

        // 2. Error Detection on Interpolated Values
        // Expected interpolation = (left_mapped + right_mapped) / 2
        for (int k = 0; k < N/2; k++) begin
            logic signed [IN_WIDTH:0] left_i = signed'(mapped_I[2*k]);
            logic signed [IN_WIDTH:0] right_i = signed'(mapped_I[2*k+1]);
            logic signed [IN_WIDTH:0] left_q = signed'(mapped_Q[2*k]);
            logic signed [IN_WIDTH:0] right_q = signed'(mapped_Q[2*k+1]);

            // Arithmetic right shift for signed division by 2, using IN_WIDTH+1 bits to prevent overflow
            logic signed [IN_WIDTH:0] expected_i = (left_i + right_i) >>> 1;
            logic signed [IN_WIDTH:0] expected_q = (left_q + right_q) >>> 1;

            // Calculate difference using IN_WIDTH+1 bits
            logic signed [IN_WIDTH:0] diff_i = signed'(interp_I[k]) - expected_i;
            logic signed [IN_WIDTH:0] diff_q = signed'(interp_Q[k]) - expected_q;

            // Compute absolute value of difference
            logic signed [IN_WIDTH:0] abs_diff_i = (diff_i < 0) ? -diff_i : diff_i;
            logic signed [IN_WIDTH:0] abs_diff_q = (diff_q < 0) ? -diff_q : diff_q;

            // Set global error flag if deviation exceeds threshold
            if (abs_diff_i > ERROR_THRESHOLD || abs_diff_q > ERROR_THRESHOLD) begin
                error_flag = 1;
            end
        end

        // 3. Map Mapped I/Q Components to Bits
        for (int k = 0; k < N; k++) begin
            logic signed [IN_WIDTH-1:0] v_i = mapped_I[k];
            logic signed [IN_WIDTH-1:0] v_q = mapped_Q[k];
            logic [1:0] code_i, code_q;

            // Map I component (MSBs of the 4-bit symbol)
            case (v_i)
                3'sd3: code_i = 2'b11;
                3'sd1: code_i = 2'b10;
                3'sd-1: code_i = 2'b01;
                3'sd-3: code_i = 2'b00;
                default: code_i = 2'b00;
            endcase

            // Map Q component (LSBs of the 4-bit symbol)
            case (v_q)
                3'sd3: code_q = 2'b11;
                3'sd1: code_q = 2'b10;
                3'sd-1: code_q = 2'b01;
                3'sd-3: code_q = 2'b00;
                default: code_q = 2'b00;
            endcase

            // Pack I and Q codes into the output bit stream
            bits[4*k + 3 : 4*k] = {code_i, code_q};
        end
    end
endmodule
