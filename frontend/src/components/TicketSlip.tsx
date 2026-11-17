import type { Booking } from "../types";
import { fmtDate } from "../services/api";

const genders: Record<string, string> = { M: "Male", F: "Female", O: "Other" };

export default function TicketSlip({ b, compact = false }: { b: Booking; compact?: boolean }) {
  return (
    <article className="relative bg-panel rounded-lg shadow-[0_1px_0_var(--color-rule),0_8px_24px_-12px_rgba(20,33,61,.25)] overflow-hidden">
      <div className="flex">
        <div className="flex-1 p-5 sm:p-6">
          <div className="flex items-baseline justify-between gap-4 flex-wrap">
            <h3 className="font-display text-2xl font-semibold leading-tight">
              {b.train_number} {b.train_name}
            </h3>
            <span className="text-[13px] font-semibold text-proceed bg-proceed-soft rounded px-2 py-0.5">{b.status}</span>
          </div>
          <dl className={`mt-4 grid gap-x-6 gap-y-3 ${compact ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-2 sm:grid-cols-3"}`}>
            <Field k="Journey date" v={b.journey_date ? fmtDate(b.journey_date) : "—"} />
            <Field k="Coach / berth" v={`${b.seat.coach_number}, berth ${b.seat.seat_number} ${b.seat.berth_type}`} />
            <Field k="Passenger" v={`${b.passenger_name}, ${b.passenger_age} ${genders[b.passenger_gender] ?? ""}`} />
            {!compact && <Field k="Confirmed by" v={`Node ${b.node_id}`} />}
            {!compact && <Field k="Seat shard" v={`Shard ${b.seat.shard_id}`} />}
            {!compact && b.coordination && <Field k="Coordination" v={b.coordination} />}
          </dl>
        </div>
        <div className="w-0 border-l-2 border-dashed border-rule" aria-hidden />
        <div className="w-40 sm:w-52 p-5 bg-ink text-white flex flex-col justify-between shrink-0">
          <div>
            <p className="text-[13px] text-white/70">PNR</p>
            <p className="font-display text-[24px] sm:text-[28px] font-bold tracking-wide leading-none mt-1 whitespace-nowrap">{b.pnr}</p>
          </div>
          <p className="text-[11px] text-white/60 mt-4 break-all">Simulated ticket, ref {b.booking_id.slice(0, 8)}</p>
        </div>
      </div>
    </article>
  );
}

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt className="text-[13px] text-ink-soft">{k}</dt>
      <dd className="font-medium text-[15px]">{v}</dd>
    </div>
  );
}
