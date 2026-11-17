import type { Seat } from "../types";

/* Sleeper-coach plan: bays of 8 berths. Six main berths face each other across the bay
   (1-3 and 4-6: lower/middle/upper), then the aisle, then two side berths (7 side lower, 8 side upper). */

const stateClass: Record<string, string> = {
  AVAILABLE: "bg-panel border-proceed text-proceed hover:bg-proceed-soft cursor-pointer",
  HELD: "bg-caution border-caution text-ink cursor-not-allowed",
  BOOKED: "bg-stop border-stop text-white cursor-not-allowed",
  BLOCKED: "blocked-hatch border-blocked text-ink-soft cursor-not-allowed",
};

const label: Record<string, string> = {
  AVAILABLE: "available",
  HELD: "being booked by someone right now",
  BOOKED: "booked",
  BLOCKED: "not for sale",
};

function Berth({ seat, selected, onSelect }: { seat?: Seat; selected: boolean; onSelect: (s: Seat) => void }) {
  if (!seat) return <span className="h-9" aria-hidden />;
  const free = seat.status === "AVAILABLE";
  return (
    <button
      type="button"
      disabled={!free}
      onClick={() => onSelect(seat)}
      aria-pressed={selected}
      aria-label={`Berth ${seat.seat_number} ${seat.berth_type}, ${label[seat.status]}`}
      title={`${seat.seat_number} ${seat.berth_type} · shard ${seat.shard_id} · ${label[seat.status]}`}
      className={`h-9 rounded-[5px] border-2 text-[13px] font-semibold leading-none flex flex-col items-center justify-center transition-colors
        ${selected ? "bg-ink border-ink text-white" : stateClass[seat.status]}`}
    >
      {seat.seat_number}
      <span className="text-[9px] font-medium opacity-80 mt-0.5">{seat.berth_type}</span>
    </button>
  );
}

export default function SeatMap({ seats, selectedId, onSelect }: { seats: Seat[]; selectedId: number | null; onSelect: (s: Seat) => void }) {
  const byNumber = new Map(seats.map((s) => [s.seat_number, s]));
  const maxNo = seats.reduce((m, s) => Math.max(m, s.seat_number), 0);
  const bays = Math.ceil(maxNo / 8);
  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {Array.from({ length: bays }, (_, b) => {
          const n = (k: number) => byNumber.get(b * 8 + k);
          return (
            <div key={b} className="w-[124px] rounded-md bg-ground/70 border border-rule p-1.5">
              <div className="grid grid-cols-3 gap-1">
                {[1, 2, 3].map((k) => <Berth key={k} seat={n(k)} selected={n(k)?.id === selectedId} onSelect={onSelect} />)}
              </div>
              <div className="h-1.5" />
              <div className="grid grid-cols-3 gap-1">
                {[4, 5, 6].map((k) => <Berth key={k} seat={n(k)} selected={n(k)?.id === selectedId} onSelect={onSelect} />)}
              </div>
              <div className="my-1.5 border-t border-dashed border-rule" aria-hidden />
              <div className="grid grid-cols-2 gap-1">
                {[7, 8].map((k) => <Berth key={k} seat={n(k)} selected={n(k)?.id === selectedId} onSelect={onSelect} />)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SeatLegend() {
  const items = [
    ["bg-panel border-proceed", "Available"],
    ["bg-caution border-caution", "Held — a booking is in flight"],
    ["bg-stop border-stop", "Booked"],
    ["blocked-hatch border-blocked", "Not for sale"],
    ["bg-ink border-ink", "Your selection"],
  ];
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-2 text-[14px] text-ink-soft">
      {items.map(([cls, text]) => (
        <li key={text} className="flex items-center gap-2">
          <span className={`inline-block size-4 rounded-[4px] border-2 ${cls}`} aria-hidden />
          {text}
        </li>
      ))}
    </ul>
  );
}
