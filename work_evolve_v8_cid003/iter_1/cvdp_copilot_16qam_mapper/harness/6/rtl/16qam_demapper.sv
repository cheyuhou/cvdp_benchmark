module qam16_mapper_interpolated #(
    parameter int unsigned N = 4,
    parameter int unsigned IN_WIDTH = 4,
    parameter int unsigned OUT_WIDTH = 3
)(
    input  logic [N * IN_WIDTH - 1 : 0] bits,
    output logic [(N + N/2) * OUT_WIDTH - 1 : 0] I,
    output logic [(N + N/2) * OUT_WIDTH - 1 : 0] Q
);

    localparam int unsigned NUM_INTERP = N / 2;

    // Internal arrays to hold mapped and interpolated values
    logic [OUT_WIDTH - 1 : 0] i_map [0:N-1];
    logic [OUT_WIDTH - 1 : 0] q_map [0:N-1];
    logic [OUT_WIDTH - 1 : 0] i_int [0:NUM_INTERP-1];
    logic [OUT_WIDTH - 1 : 0] q_int [0:NUM_INTERP-1];

    // Mapping Logic
    always_comb begin
        for (int k = 0; k < N; k++) begin
            logic [IN_WIDTH-1:0] sym = bits[k * IN_WIDTH +: IN_WIDTH];

            // Map MSBs (bits 3:2) to I component
            case (sym[3:2])
                2'b00: i_map[k] = 3'sd-3;
                2'b01: i_map[k] = 3'sd-1;
                2'b10: i_map[k] = 3'sd1;
                2'b11: i_map[k] = 3'sd3;
            endcase

            // Map LSBs (bits 1:0) to Q component
            case (sym[1:0])
                2'b00: q_map[k] = 3'sd-3;
                2'b01: q_map[k] = 3'sd-1;
                2'b10: q_map[k] = 3'sd1;
                2'b11: q_map[k] = 3'sd3;
            endcase
        end
    end

    // Interpolation Logic
    always_comb begin
        for (int k = 0; k < NUM_INTERP; k++) begin
            // Use one extra bit for intermediate addition to prevent overflow
            logic signed [OUT_WIDTH:0] sum_i = $signed(i_map[2*k]) + $signed(i_map[2*k+1]);
            logic signed [OUT_WIDTH:0] sum_q = $signed(q_map[2*k]) + $signed(q_map[2*k+1]);

            // Arithmetic right shift by 1 for division by 2 (arithmetic mean)
            i_int[k] = $unsigned(sum_i >>> 1);
            q_int[k] = $unsigned(sum_q >>> 1);
        end
    end

    // Output Arrangement
    always_comb begin
        int i_idx = 0;
        int q_idx = 0;
        for (int k = 0; k < NUM_INTERP; k++) begin
            // I components: Mapped[2k], Interp[k], Mapped[2k+1]
            I[i_idx +: OUT_WIDTH] = i_map[2*k];
            i_idx += OUT_WIDTH;
            I[i_idx +: OUT_WIDTH] = i_int[k];
            i_idx += OUT_WIDTH;
            I[i_idx +: OUT_WIDTH] = i_map[2*k+1];
            i_idx += OUT_WIDTH;

            // Q components: same pattern
            Q[q_idx +: OUT_WIDTH] = q_map[2*k];
            q_idx += OUT_WIDTH;
            Q[q_idx +: OUT_WIDTH] = q_int[k];
            q_idx += OUT_WIDTH;
            Q[q_idx +: OUT_WIDTH] = q_map[2*k+1];
            q_idx += OUT_WIDTH;
        end
    end
endmodule