module glitch_free_mux (
    input  wire clk1,
    input  wire clk2,
    input  wire sel,
    input  wire rst_n,
    output wire clkout
);
    reg en1, en2;
    reg sel_d1, sel_d2;
    reg [1:0] state;
    reg sel_chg;
    
    // Sample sel to detect change. 
    // Since sel is synchronous to one of the clocks, sampling on both ensures timely detection.
    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n) begin
            sel_d1 <= 1'b0;
            en1 <= 1'b0;
        end else begin
            sel_d1 <= sel;
        end
    end
    always @(posedge clk2 or negedge rst_n) begin
        if (!rst_n) sel_d2 <= 1'b0;
        else sel_d2 <= sel;
    end
    
    // Register sel change edge to avoid combinatorial glitches
    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n) sel_chg <= 1'b0;
        else sel_chg <= (sel != sel_d1) | (sel != sel_d2);
    end
    
    // Glitch-free switching FSM
    always @(posedge clk1 or negedge rst_n) begin
        if (!rst_n) begin
            state <= 2'd0;
        end else begin
            case (state)
                2'd0: begin // Idle: sel=0, clk1 active
                    en1 <= 1'b1;
                    en2 <= 1'b0;
                    if (sel_chg) state <= 2'd1;
                end
                2'd1: begin // Disable clk1 on next clk1 posedge
                    en1 <= 1'b0;
                    state <= 2'd2;
                end
                2'd2: begin // Enable clk2 on next clk2 posedge
                    en2 <= 1'b1;
                    state <= 2'd3;
                end
                2'd3: begin // Idle: sel=1, clk2 active
                    en1 <= 1'b0;
                    en2 <= 1'b1;
                    if (sel_chg) state <= 2'd4;
                end
                2'd4: begin // Disable clk2 on next clk2 posedge
                    en2 <= 1'b0;
                    state <= 2'd5;
                end
                2'd5: begin // Enable clk1 on next clk1 posedge
                    en1 <= 1'b1;
                    state <= 2'd0;
                end
            endcase
        end
    end
    
    assign clkout = (en1 & clk1) | (en2 & clk2);
endmodule
