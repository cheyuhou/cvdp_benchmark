module qam16_mapper_interpolated #(
    parameter int N = 4,
    parameter int IN_WIDTH = 4,
    parameter int OUT_WIDTH = 3
)(
    input  logic [N * IN_WIDTH - 1: 0] bits,
    output logic [(N + N/2) * OUT_WIDTH - 1: 0] I,
    output logic [(N + N/2) * OUT_WIDTH - 1: 0] Q
);

    logic [OUT_WIDTH-1:0] I_mapped [0:N-1];
    logic [OUT_WIDTH-1:0] Q_mapped [0:N-1];
    logic [OUT_WIDTH:0] I_interpolated [0:(N/2)-1];
    logic [OUT_WIDTH:0] Q_interpolated [0:(N/2)-1];

    always_comb begin
        // Mapping Input Bits
        for (int k = 0; k < N; k++) begin
            logic [IN_WIDTH-1:0] sym;
            sym = bits[k*IN_WIDTH +: IN_WIDTH];

            case (sym[IN_WIDTH-1:IN_WIDTH-2])
                2'b00: I_mapped[k] = 3'b101; // -3
                2'b01: I_mapped[k] = 3'b111; // -1
                2'b10: I_mapped[k] = 3'b001; // 1
                2'b11: I_mapped[k] = 3'b011; // 3
                default: I_mapped[k] = 3'b000;
            endcase

            case (sym[1:0])
                2'b00: Q_mapped[k] = 3'b101; // -3
                2'b01: Q_mapped[k] = 3'b111; // -1
                2'b10: Q_mapped[k] = 3'b001; // 1
                2'b11: Q_mapped[k] = 3'b011; // 3
                default: Q_mapped[k] = 3'b000;
            endcase
        end

        // Interpolation between adjacent symbols within pairs
        for (int k = 0; k < N/2; k++) begin
            I_interpolated[k] = (I_mapped[2*k] + I_mapped[2*k+1]) >>> 1;
            Q_interpolated[k] = (Q_mapped[2*k] + Q_mapped[2*k+1]) >>> 1;
        end

        // Output Arrangement: Mapped, Interpolated, Mapped
        I = '0;
        Q = '0;
        for (int k = 0; k < N/2; k++) begin
            int base_idx = 3 * k * OUT_WIDTH;
            I[base_idx +: OUT_WIDTH] = I_mapped[2*k];
            I[base_idx + OUT_WIDTH +: OUT_WIDTH] = I_interpolated[k][OUT_WIDTH-1:0];
            I[base_idx + 2*OUT_WIDTH +: OUT_WIDTH] = I_mapped[2*k+1];

            Q[base_idx +: OUT_WIDTH] = Q_mapped[2*k];
            Q[base_idx + OUT_WIDTH +: OUT_WIDTH] = Q_interpolated[k][OUT_WIDTH-1:0];
            Q[base_idx + 2*OUT_WIDTH +: OUT_WIDTH] = Q_mapped[2*k+1];
        end
    end
endmodule
