import type { ReactNode } from "react";

type Tone = "error" | "conflict" | "info" | "empty";
const tones: Record<Tone, string> = {
  error: "border-stop/40 bg-[#f8e7e7] text-stop",
  conflict: "border-caution/50 bg-caution-soft text-ink",
  info: "border-node-b/30 bg-[#e3f2f4] text-ink",
  empty: "border-rule bg-panel text-ink-soft",
};

export default function Notice({ tone = "info", title, children }: { tone?: Tone; title?: string; children?: ReactNode }) {
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`border rounded-lg px-4 py-3 ${tones[tone]}`}>
      {title && <p className="font-semibold">{title}</p>}
      {children && <div className="text-[15px] mt-0.5">{children}</div>}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 text-ink-soft py-6" role="status">
      <span className="size-4 rounded-full border-2 border-rule border-t-ink animate-spin" />
      {label}…
    </div>
  );
}
