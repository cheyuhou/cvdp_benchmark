module glitch_free_mux (
    input  wire        clk1,
    input  wire        clk2,
    input  wire        sel,
    input  wire        rst_n,
    output wire        clkout
);
    reg clk1_en;
    reg clk2_en;

    // Enable logic for clk1, synchronized to clk1 domain
    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n) begin
            clk1_en <= 1'b1;
        end else if (sel & !clk2_en) begin
            // sel=1: disable clk1 on its next positive edge
            clk1_en <= 1'b0;
        end else if (!sel & !clk2_en) begin
            // sel=0: enable clk1 on its next positive edge after clk2 is disabled
            clk1_en <= 1'b1;
        end
    end

    // Enable logic for clk2, synchronized to clk2 domain
    always @(posedge clk2 or negedge rst_n) begin
        if (!rst_n) begin
            clk2_en <= 1'b0;
        end else if (!sel & clk2_en) begin
            // sel=0: disable clk2 on its next positive edge
            clk2_en <= 1'b0;
        end else if (sel & !clk1_en) begin
            // sel=1: enable clk2 on its next positive edge after clk1 is disabled
            clk2_en <= 1'b1;
        end
    end

    // Output generation
    // During reset, clkout is forced low. Otherwise, outputs the active clock.
    assign clkout = rst_n & ((clk1 & clk1_en) | (clk2 & clk2_en));

endmodule