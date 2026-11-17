import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, fmtDate, fmtDuration, fmtTime } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { Journey } from "../types";
import Notice, { Spinner } from "../components/Notice";

const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

export default function Home() {
  const nav = useNavigate();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [date, setDate] = useState(today());
  const { data, error, loading } = usePolling(
    () => api.get<Journey[]>("/api/journeys", { params: { date: today() } }).then((r) => r.data), 10000);

  const stations = useMemo(() => {
    const s = new Set<string>();
    data?.forEach((j) => { s.add(j.train.source); s.add(j.train.destination); });
    return [...s].sort();
  }, [data]);

  return (
    <div className="space-y-12">
      <section className="grid lg:grid-cols-[1.1fr_1fr] gap-10 items-end">
        <div>
          <h1 className="font-display text-5xl sm:text-6xl font-bold leading-[0.95] tracking-tight">
            Book in the Tatkal rush.
          </h1>
          <p className="mt-4 text-lg text-ink-soft max-w-[52ch]">
            Hundreds of people press “Book” for the same few berths at once. Pick a berth and race them:
            three servers take the requests, and the database lets exactly one of you have it.
          </p>
        </div>
        <form
          className="bg-panel rounded-lg p-5 border border-rule grid gap-3 sm:grid-cols-2"
          onSubmit={(e) => { e.preventDefault(); nav(`/search?${new URLSearchParams({ from, to, date })}`); }}
        >
          <Field label="From">
            <input list="stations" value={from} onChange={(e) => setFrom(e.target.value)} placeholder="Any station"
                   className="input" maxLength={80} />
          </Field>
          <Field label="To">
            <input list="stations" value={to} onChange={(e) => setTo(e.target.value)} placeholder="Any station"
                   className="input" maxLength={80} />
          </Field>
          <Field label="Journey date">
            <input type="date" value={date} min={today()} onChange={(e) => setDate(e.target.value)} className="input" required />
          </Field>
          <div className="flex items-end">
            <button className="btn-primary w-full">Find trains</button>
          </div>
          <datalist id="stations">{stations.map((s) => <option key={s} value={s} />)}</datalist>
        </form>
      </section>

      <section>
        <h2 className="font-display text-3xl font-semibold mb-4">Leaving today</h2>
        {loading && !data && <Spinner label="Loading departures" />}
        {!!error && <Notice tone="error" title="Departures could not be loaded">Check that the backend nodes are running, then refresh.</Notice>}
        {data && data.length === 0 && <Notice tone="empty" title="No trains today">Run the seed script to create simulated journeys.</Notice>}
        {data && data.length > 0 && <JourneyList journeys={data} />}
      </section>
    </div>
  );
}

export function JourneyList({ journeys }: { journeys: Journey[] }) {
  return (
    <ul className="divide-y divide-rule border-y border-rule bg-panel">
      {journeys.map((j) => {
        const pct = j.total_seats ? j.available_seats / j.total_seats : 0;
        const tight = j.available_seats < 20;
        return (
          <li key={j.id}>
            <Link to={`/journey/${j.id}`} className="grid grid-cols-[1fr_auto] sm:grid-cols-[1.4fr_1fr_8rem_9rem] gap-x-6 gap-y-1 items-center px-4 py-4 hover:bg-ground/60">
              <div>
                <p className="font-display text-xl font-semibold leading-tight">{j.train.train_name}</p>
                <p className="text-[14px] text-ink-soft">Train {j.train.train_number}, {fmtDate(j.journey_date)}</p>
              </div>
              <div className="hidden sm:block">
                <p className="font-medium">{fmtTime(j.train.departure_time)} {j.train.source}</p>
                <p className="text-[14px] text-ink-soft">{fmtTime(j.train.arrival_time)} {j.train.destination}</p>
              </div>
              <p className="hidden sm:block text-ink-soft text-[15px]">{fmtDuration(j.train.duration_minutes)}</p>
              <div className="text-right">
                <p className={`font-display text-2xl font-semibold leading-none ${tight ? "text-stop" : "text-proceed"}`}>{j.available_seats}</p>
                <p className="text-[13px] text-ink-soft">of {j.total_seats} berths left</p>
                <div className="h-1 bg-ground rounded mt-1.5 overflow-hidden" aria-hidden>
                  <div className={`h-full ${tight ? "bg-stop" : "bg-proceed"}`} style={{ width: `${pct * 100}%` }} />
                </div>
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[14px] text-ink-soft mb-1">{label}</span>
      {children}
    </label>
  );
}
