module glitch_free_mux (
    input  wire        clk1,
    input  wire        clk2,
    input  wire        sel,
    input  wire        rst_n,
    output reg         clkout
);

    // Synchronize sel to both clock domains to handle metastability and 
    // align transitions with respective clock edges.
    reg sel_sync_clk1;
    reg sel_sync_clk2;

    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n)
            sel_sync_clk1 <= 1'b0;
        else
            sel_sync_clk1 <= sel;
    end

    always @(posedge clk2 or negedge rst_n) begin
        if (!rst_n)
            sel_sync_clk2 <= 1'b0;
        else
            sel_sync_clk2 <= sel;
    end

    // Enable signals for glitch-free switching.
    // en1 controls clk1, en2 controls clk2.
    reg en1;
    reg en2;

    // Generate en1 synchronized to clk1.
    // Logic:
    // - Goes low when sel goes high (synchronized to clk1).
    // - Goes high when sel goes low, provided en2 is already low.
    // This ensures clk1 is disabled first during 0->1 switch,
    // and re-enabled only after clk2 is fully disabled during 1->0 switch.
    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n)
            en1 <= 1'b0;
        else begin
            if (sel_sync_clk1)
                en1 <= 1'b0;
            else if (!en2)
                en1 <= 1'b1;
        end
    end

    // Generate en2 synchronized to clk2.
    // Logic:
    // - Goes low when sel goes low (synchronized to clk2).
    // - Goes high when sel goes high, provided en1 is already low.
    // This ensures clk2 is disabled first during 1->0 switch,
    // and re-enabled only after clk1 is fully disabled during 0->1 switch.
    always @(posedge clk2 or negedge rst_n) begin
        if (!rst_n)
            en2 <= 1'b0;
        else begin
            if (!sel_sync_clk2)
                en2 <= 1'b0;
            else if (!en1)
                en2 <= 1'b1;
        end
    end

    // Final mux output.
    // Since en1 and en2 are mutually exclusive and glitch-free,
    // this combination produces a glitch-free clkout.
    assign clkout = (clk1 & en1) | (clk2 & en2);

endmodule
