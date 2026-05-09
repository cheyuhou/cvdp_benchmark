module qam16_mapper_interpolated #(
    parameter int N = 4,
    parameter int IN_WIDTH = 4,
    parameter int OUT_WIDTH = 3
)(
    input  logic [N*IN_WIDTH - 1 : 0] bits,
    output logic [N + N/2 - 1][OUT_WIDTH - 1 : 0] I,
    output logic [N + N/2 - 1][OUT_WIDTH - 1 : 0] Q
);
    logic signed [OUT_WIDTH-1:0] mapped_I [0:N-1];
    logic signed [OUT_WIDTH-1:0] mapped_Q [0:N-1];

    always_comb begin
        // Initialize outputs to prevent latch inference
        for (int k = 0; k < N + N/2; k++) begin
            I[k] = '0;
            Q[k] = '0;
        end

        // 1. Mapping Input Bits
        for (int i = 0; i < N; i++) begin
            logic [IN_WIDTH-1:0] sym = bits[i*IN_WIDTH +: IN_WIDTH];
            logic [1:0] msb = sym[IN_WIDTH-1 -: 2];
            logic [1:0] lsb = sym[1:0];

            // Map MSBs to I component
            mapped_I[i] = (msb == 2'b00) ? -3 :
                          (msb == 2'b01) ? -1 :
                          (msb == 2'b10) ?  1 : 3;

            // Map LSBs to Q component
            mapped_Q[i] = (lsb == 2'b00) ? -3 :
                          (lsb == 2'b01) ? -1 :
                          (lsb == 2'b10) ?  1 : 3;
        end

        // 2. Interpolation & Output Arrangement
        for (int i = 0; i < N; i += 2) begin
            int pair_idx = i / 2;
            int out_base = pair_idx * 2;

            // First mapped symbol
            I[out_base]     = mapped_I[i];
            Q[out_base]     = mapped_Q[i];

            // Interpolated value (requires 1 extra bit for addition to avoid overflow)
            logic signed [OUT_WIDTH:0] sum_I = mapped_I[i] + mapped_I[i+1];
            logic signed [OUT_WIDTH:0] sum_Q = mapped_Q[i] + mapped_Q[i+1];

            I[out_base + 1] = sum_I >>> 1;
            Q[out_base + 1] = sum_Q >>> 1;

            // Second mapped symbol
            I[out_base + 2] = mapped_I[i+1];
            Q[out_base + 2] = mapped_Q[i+1];
        end
    end
endmodule
