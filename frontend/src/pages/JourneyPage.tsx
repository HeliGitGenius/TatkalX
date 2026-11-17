import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, bookSeat, fmtDate, fmtTime, newIdempotencyKey, type BookingAttemptResult } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { Passenger, Seat, SeatMap as SeatMapT } from "../types";
import SeatMap, { SeatLegend } from "../components/SeatMap";
import Notice, { Spinner } from "../components/Notice";

const conflictText: Record<string, string> = {
  SEAT_UNAVAILABLE: "Someone else's request reached the database first. Pick another berth.",
  SEAT_BUSY: "Another passenger is booking this berth at this moment. Pick another berth.",
  SOLD_OUT: "Every berth on this train is now booked.",
  REQUEST_IN_PROGRESS: "Your earlier attempt is still being processed. Wait a second and press Book again; it will not double-book.",
  RATE_LIMITED: "Too many booking attempts from you in the last minute. Wait and try again.",
};

export default function JourneyPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const journeyId = Number(id);
  const { data, error, loading, refresh } = usePolling(
    () => api.get<SeatMapT>(`/api/journeys/${journeyId}/seats`).then((r) => r.data), 3000, [journeyId]);

  const [coach, setCoach] = useState<string | null>(null);
  const [seat, setSeat] = useState<Seat | null>(null);
  const [auto, setAuto] = useState(false);
  const [p, setP] = useState<Passenger>({ name: "", age: 25, gender: "F" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BookingAttemptResult | null>(null);
  const keyRef = useRef<string | null>(null);

  useEffect(() => { if (data && !coach) setCoach(data.coaches[0] ?? null); }, [data, coach]);
  // if the selected seat stops being available while the user is filling the form, say so
  const live = useMemo(() => data?.seats.find((s) => s.id === seat?.id), [data, seat]);
  const lostSeat = seat && live && live.status !== "AVAILABLE" && !result?.ok;

  if (loading && !data) return <Spinner label="Loading seat map" />;
  if (error && !data) return <Notice tone="error" title="Seat map unavailable">This journey could not be loaded. Go back and choose another train.</Notice>;
  if (!data) return null;

  const j = data.journey;
  const coachSeats = data.seats.filter((s) => s.coach_number === coach);
  const counts = (c: string) => data.seats.filter((s) => s.coach_number === c && s.status === "AVAILABLE").length;
  const canBook = !busy && p.name.trim().length >= 2 && (auto || (seat && !lostSeat));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canBook) return;
    setBusy(true);
    setResult(null);
    // one idempotency key per user action; kept across automatic retries of that action
    keyRef.current = keyRef.current ?? newIdempotencyKey();
    const r = await bookSeat(journeyId, auto ? null : seat!.id, { ...p, name: p.name.trim() }, keyRef.current);
    setBusy(false);
    setResult(r);
    if (r.ok && r.booking) {
      keyRef.current = null;
      nav(`/booking/${r.booking.booking_id}`, { state: { booking: r.booking, servedBy: r.servedBy, replayed: r.replayed, attempts: r.attempts } });
    } else {
      if (r.status !== null && r.status < 500 && r.error?.code !== "REQUEST_IN_PROGRESS") keyRef.current = null;
      refresh();
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-[15px] text-node-b hover:underline">All trains</Link>
        <h1 className="font-display text-4xl font-bold leading-tight mt-1">{j.train.train_name}</h1>
        <p className="text-ink-soft text-[16px]">
          Train {j.train.train_number}, {fmtDate(j.journey_date)}: {fmtTime(j.train.departure_time)} {j.train.source} to{" "}
          {fmtTime(j.train.arrival_time)} {j.train.destination}. {j.available_seats} of {j.total_seats} berths free.
        </p>
      </div>

      <div className="grid lg:grid-cols-[1fr_340px] gap-6 items-start">
        <section className="bg-panel border border-rule rounded-lg p-4 sm:p-5 min-w-0">
          <div className="flex flex-wrap gap-2 mb-4" role="tablist" aria-label="Coach">
            {data.coaches.map((c) => (
              <button key={c} role="tab" aria-selected={c === coach} onClick={() => setCoach(c)}
                className={`h-10 px-4 rounded-md border text-[15px] font-semibold ${c === coach ? "bg-ink text-white border-ink" : "bg-panel border-rule hover:border-ink"}`}>
                {c} <span className={`font-normal ${c === coach ? "text-white/70" : "text-ink-soft"}`}>{counts(c)} free</span>
              </button>
            ))}
          </div>
          <SeatMap seats={coachSeats} selectedId={auto ? null : seat?.id ?? null}
                   onSelect={(s) => { setSeat(s); setAuto(false); setResult(null); keyRef.current = null; }} />
          <div className="mt-4"><SeatLegend /></div>
          <p className="mt-3 text-[13px] text-ink-soft">Refreshes every 3 seconds. Amber berths are locked by a request on one of the servers right now.</p>
        </section>

        <form onSubmit={submit} className="bg-panel border border-rule rounded-lg p-5 space-y-4 lg:sticky lg:top-4">
          <h2 className="font-display text-2xl font-semibold">Passenger</h2>
          <div className="rounded-md bg-ground px-3 py-2.5 text-[15px]">
            {auto ? (
              <span>Any free berth. The server picks one for you.</span>
            ) : seat ? (
              <span>Berth <b>{seat.coach_number} {seat.seat_number}</b> ({seat.berth_type}), shard {seat.shard_id}</span>
            ) : (
              <span className="text-ink-soft">Choose a green berth on the plan.</span>
            )}
          </div>
          <label className="flex items-center gap-2 text-[15px]">
            <input type="checkbox" checked={auto} onChange={(e) => { setAuto(e.target.checked); setResult(null); keyRef.current = null; }} className="size-4 accent-ink" />
            Give me any free berth
          </label>
          <label className="block">
            <span className="block text-[14px] text-ink-soft mb-1">Full name</span>
            <input className="input" value={p.name} maxLength={80} autoComplete="name" required
                   onChange={(e) => setP({ ...p, name: e.target.value })} placeholder="As on ID card" />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="block text-[14px] text-ink-soft mb-1">Age</span>
              <input className="input" type="number" min={1} max={120} value={p.age} required
                     onChange={(e) => setP({ ...p, age: Number(e.target.value) })} />
            </label>
            <label className="block">
              <span className="block text-[14px] text-ink-soft mb-1">Gender</span>
              <select className="input" value={p.gender} onChange={(e) => setP({ ...p, gender: e.target.value as Passenger["gender"] })}>
                <option value="F">Female</option><option value="M">Male</option><option value="O">Other</option>
              </select>
            </label>
          </div>
          {lostSeat && <Notice tone="conflict" title="That berth just went">It was taken while you were typing. Pick another one.</Notice>}
          {result && !result.ok && result.error && (
            <Notice tone={result.status === 409 || result.status === 429 ? "conflict" : "error"}
                    title={result.status === 409 ? "Not booked" : result.status === 429 ? "Slow down" : "Booking failed"}>
              {conflictText[result.error.code] ?? result.error.message}
              {result.servedBy && <span className="block text-[13px] text-ink-soft mt-1">Answered by node {result.servedBy} after {result.attempts} attempt{result.attempts > 1 ? "s" : ""}.</span>}
            </Notice>
          )}
          <button className="btn-primary w-full" disabled={!canBook}>{busy ? "Booking…" : "Book this berth"}</button>
          <p className="text-[13px] text-ink-soft">
            Each press sends one idempotency key. If the network drops we retry with the same key, so you can never be charged for two berths.
          </p>
        </form>
      </div>
    </div>
  );
}
