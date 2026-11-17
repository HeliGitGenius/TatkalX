/* One cell per benchmark request, grouped by the node that served it, coloured by outcome.
   This is the whole experiment at a glance: 500 requests, one green cell. */

export const outcomeColor: Record<string, string> = {
  success: "var(--color-proceed)",
  seat_unavailable: "var(--color-stop)",
  duplicate_replayed: "var(--color-node-b)",
  duplicate_in_progress: "#7fb8bf",
  rate_limited: "var(--color-caution)",
  validation_error: "#8b6bb3",
  unexpected_error: "#111827",
};

export const outcomeLabel: Record<string, string> = {
  success: "Booked",
  seat_unavailable: "Seat already taken",
  duplicate_replayed: "Retry answered from stored result",
  duplicate_in_progress: "Retry while original in flight",
  rate_limited: "Rate limited (429)",
  validation_error: "Validation error",
  unexpected_error: "Unexpected error",
};

export default function RequestGrid({ grid }: { grid: [string, string][] }) {
  // Ring the green cells only when they are rare enough to get lost among the red ones.
  const ringSuccess = grid.filter(([, o]) => o === "success").length <= 10;
  const nodes = Array.from(new Set(grid.map(([n]) => n))).sort();
  const present = Array.from(new Set(grid.map(([, o]) => o)));
  return (
    <div>
      <div className="grid gap-4 sm:grid-cols-3">
        {nodes.map((node) => {
          const cells = grid.filter(([n]) => n === node);
          return (
            <div key={node}>
              <p className="text-[14px] text-ink-soft mb-1.5">
                Node {node} <span className="text-ink font-semibold">{cells.length}</span> requests
              </p>
              <div className="flex flex-wrap gap-[3px]" role="img"
                   aria-label={`Node ${node}: ${present.map((o) => `${cells.filter(([, x]) => x === o).length} ${outcomeLabel[o] ?? o}`).join(", ")}`}>
                {cells.map(([, o], i) => (
                  <span key={i} className="cell-in block size-[9px] rounded-[2px]"
                        style={{ background: outcomeColor[o] ?? "#999", animationDelay: `${Math.min(i * 3, 600)}ms`,
                                 boxShadow: o === "success" && ringSuccess ? "0 0 0 2px var(--color-panel), 0 0 0 4px var(--color-proceed)" : undefined }} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
      <ul className="flex flex-wrap gap-x-5 gap-y-1 mt-4 text-[14px] text-ink-soft">
        {present.map((o) => (
          <li key={o} className="flex items-center gap-2">
            <span className="size-3 rounded-[2px]" style={{ background: outcomeColor[o] }} />
            {outcomeLabel[o] ?? o} <span className="text-ink font-semibold">{grid.filter(([, x]) => x === o).length}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
