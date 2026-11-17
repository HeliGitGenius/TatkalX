import { useEffect, useMemo, useState } from "react";
import type { AxiosError } from "axios";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { adminHeaders, adminToken, api, describeError, setAdminToken } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { BenchRun, BenchSummary, BookingEvent, Health, Metrics } from "../types";
import Notice, { Spinner } from "../components/Notice";
import RequestGrid from "../components/RequestGrid";

const NODES = ["A", "B", "C"] as const;
const nodeColor: Record<string, string> = { A: "var(--color-node-a)", B: "var(--color-node-b)", C: "var(--color-node-c)" };
const scenarios = [
  { id: "one_seat", name: "One seat, everyone wants it", hint: "1 berth. Expect exactly 1 booking." },
  { id: "limited", name: "Limited inventory", hint: "N berths. Expect exactly N bookings, none twice." },
  { id: "idempotency", name: "Retry storm", hint: "Each request sent 5 times with one key, then retried." },
  { id: "rate_limit", name: "One greedy user", hint: "Requests beyond the per-user limit get 429." },
  { id: "naive_race", name: "Without protection", hint: "Check-then-write on a demo table. Shows the bug." },
];

export default function Admin() {
  const [token, setToken] = useState(adminToken());
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    if (!token) { setAuthed(false); return; }
    api.get("/api/admin/failure/state", { headers: adminHeaders() })
      .then(() => setAuthed(true))
      .catch((e: AxiosError) => setAuthed(e.response?.status === 401 ? false : true));
  }, [token]);

  if (authed === null) return <Spinner label="Checking access" />;
  if (!authed) return <TokenGate onSet={(t) => { setAdminToken(t); setToken(t); }} />;
  return <ControlRoom />;
}

function TokenGate({ onSet }: { onSet: (t: string) => void }) {
  const [v, setV] = useState("");
  return (
    <form className="max-w-md bg-panel border border-rule rounded-lg p-6 space-y-4" onSubmit={(e) => { e.preventDefault(); onSet(v.trim()); }}>
      <h1 className="font-display text-3xl font-bold">Control room</h1>
      <p className="text-ink-soft">Enter the admin token from your <code>.env</code> file (ADMIN_TOKEN). It is kept in this browser only.</p>
      <input className="input" type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="Admin token" autoFocus />
      <button className="btn-primary">Open control room</button>
    </form>
  );
}

function ControlRoom() {
  const metrics = usePolling(() => api.get<Metrics>("/api/metrics", { params: { window_minutes: 60 } }).then((r) => r.data), 4000);
  const m = metrics.data;
  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-5xl font-bold leading-none">Control room</h1>
          <p className="text-ink-soft mt-2">Live view of the three booking servers, the shared database and the concurrency experiments.</p>
        </div>
        {m && <ModeBadge m={m} />}
      </header>

      <NodesPanel />
      {m ? <IntegrityStrip m={m} /> : metrics.error ? <Notice tone="error" title="Metrics unavailable">{describeError(metrics.error)}</Notice> : <Spinner label="Loading metrics" />}
      <BenchmarkPanel />
      {m && <TrafficCharts m={m} />}
      <FailurePanel />
      {m && <ShardPanel m={m} />}
      <EventFeed />
    </div>
  );
}

function ModeBadge({ m }: { m: Metrics }) {
  const redis = m.config.coordination_mode === "redis_lock";
  return (
    <div className="bg-panel border border-rule rounded-lg px-4 py-3 text-[15px] max-w-sm">
      <p className="font-semibold">{redis ? "Redis lock + database claim" : "Database claim only"}</p>
      <p className="text-ink-soft text-[14px]">
        {redis
          ? `Per-berth lock, ${m.config.lock_ttl_ms} ms lease, ${m.config.lock_wait_ms} ms wait. If Redis fails: ${m.config.redis_failure_policy.replace("_", " ")}.`
          : "Every request goes straight to the conditional UPDATE in PostgreSQL."}{" "}
        Rate limit {m.config.rate_limit_per_user}/user/{m.config.rate_limit_window_s}s{m.config.rate_limit_test_mode ? " (benchmark test mode on)" : ""}.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ nodes */
function NodesPanel() {
  return (
    <section>
      <h2 className="font-display text-3xl font-semibold mb-3">Servers</h2>
      <div className="grid md:grid-cols-3 gap-4">{NODES.map((n) => <NodeStrip key={n} node={n} />)}</div>
    </section>
  );
}

function NodeStrip({ node }: { node: string }) {
  const h = usePolling(() => api.get<Health>(`/node/${node.toLowerCase()}/api/health`, { timeout: 1500, validateStatus: () => true })
    .then((r) => (r.status >= 500 && !(r.data as Health)?.node_id ? Promise.reject(new Error(`HTTP ${r.status}`)) : r.data)), 2000);
  const [msg, setMsg] = useState<string | null>(null);
  const d = h.error ? null : h.data;
  const down = !!h.error;
  const status = down ? "down" : d?.injected?.unhealthy_remaining_s ? "browned out" : d?.status ?? "…";
  const dot = down ? "bg-stop" : status === "ok" ? "bg-proceed" : "bg-caution";

  async function act(path: string, body?: object, method: "post" | "delete" = "post") {
    setMsg(null);
    try {
      const r = await api.request({ url: `/node/${node.toLowerCase()}/api/admin/failure/${path}`, method, data: body, headers: adminHeaders() });
      setMsg(path === "crash" ? "Crash sent. The supervisor restarts it in a few seconds." : `Done: ${JSON.stringify(r.data)}`);
      h.refresh();
    } catch (e) {
      setMsg(describeError(e));
    }
  }

  return (
    <div className="bg-panel border border-rule rounded-lg overflow-hidden">
      <div className="h-1.5" style={{ background: nodeColor[node] }} />
      <div className="p-4">
        <div className="flex items-center justify-between">
          <p className="font-display text-2xl font-semibold">Node {node}</p>
          <span className="flex items-center gap-2 text-[15px] font-medium">
            <span className={`size-2.5 rounded-full ${dot} ${down ? "" : "animate-pulse"}`} />{status}
          </span>
        </div>
        {d ? (
          <dl className="grid grid-cols-2 gap-y-1 mt-3 text-[14px]">
            <dt className="text-ink-soft">Process</dt><dd>pid {d.pid}, up {fmtUptime(d.uptime_s)}</dd>
            <dt className="text-ink-soft">PostgreSQL</dt><dd className={d.database.ok ? "" : "text-stop"}>{d.database.ok ? `${d.database.latency_ms} ms` : d.database.error}</dd>
            <dt className="text-ink-soft">Redis</dt><dd className={d.redis.ok ? "" : "text-stop"}>{d.redis.ok ? `${d.redis.latency_ms} ms` : `unreachable (${d.redis.error})`}</dd>
            <dt className="text-ink-soft">Audit log</dt><dd>{d.events.written} written{d.events.dropped ? `, ${d.events.dropped} dropped` : ""}</dd>
          </dl>
        ) : (
          <p className="mt-3 text-[14px] text-ink-soft">{down ? "No answer. Nginx is routing around this node." : "Checking…"}</p>
        )}
        <div className="flex flex-wrap gap-2 mt-4">
          <button className="btn-danger" disabled={down} onClick={() => act("crash")}>Crash process</button>
          {d?.injected?.unhealthy_remaining_s
            ? <button className="btn-quiet" onClick={() => act("unhealthy", undefined, "delete")}>End brown-out</button>
            : <button className="btn-quiet" disabled={down} onClick={() => act("unhealthy", { seconds: 15 })}>Brown-out 15 s</button>}
        </div>
        {msg && <p className="text-[13px] text-ink-soft mt-2 break-words">{msg}</p>}
      </div>
    </div>
  );
}

const fmtUptime = (s: number) => (s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

/* ------------------------------------------------------------------ integrity */
function IntegrityStrip({ m }: { m: Metrics }) {
  const i = m.integrity;
  const ok = i.duplicate_seat_bookings === 0 && i.booked_without_booking === 0 && i.booking_without_seat === 0;
  return (
    <section className={`rounded-lg border-2 px-5 py-4 flex flex-wrap gap-x-10 gap-y-3 items-center ${ok ? "border-proceed bg-proceed-soft" : "border-stop bg-[#f8e7e7]"}`}>
      <p className="font-display text-2xl font-semibold">{ok ? "Database is consistent" : "Inconsistency detected"}</p>
      <Stat k="Confirmed bookings" v={i.confirmed_bookings} />
      <Stat k="Berths with two bookings" v={i.duplicate_seat_bookings} bad={i.duplicate_seat_bookings > 0} />
      <Stat k="Booked berths without a booking" v={i.booked_without_booking} bad={i.booked_without_booking > 0} />
      <Stat k="Bookings without a berth" v={i.booking_without_seat} bad={i.booking_without_seat > 0} />
    </section>
  );
}

function Stat({ k, v, bad }: { k: string; v: number | string; bad?: boolean }) {
  return (
    <div>
      <p className={`font-display text-3xl font-bold leading-none ${bad ? "text-stop" : ""}`}>{v}</p>
      <p className="text-[13px] text-ink-soft">{k}</p>
    </div>
  );
}

/* ------------------------------------------------------------------ benchmark */
function BenchmarkPanel() {
  const runs = usePolling(() => api.get<BenchRun[]>("/api/admin/benchmark/runs", { headers: adminHeaders(), params: { limit: 25 } }).then((r) => r.data), 2500);
  const [scenario, setScenario] = useState("one_seat");
  const [requests, setRequests] = useState(500);
  const [seats, setSeats] = useState(50);
  const [allocation, setAllocation] = useState("targeted");
  const [selected, setSelected] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const list = runs.data ?? [];
  const running = list.some((r) => r.status === "RUNNING" && Date.now() - new Date(r.started_at).getTime() < 120000);
  const current = list.find((r) => r.id === selected) ?? list.find((r) => r.summary);

  async function run() {
    setErr(null);
    setStarting(true);
    try {
      const r = await api.post("/api/admin/benchmark", { scenario, requests: scenario === "rate_limit" ? Math.min(requests, 60) : requests, seats, allocation }, { headers: adminHeaders() });
      setSelected(r.data.run_id);
      runs.refresh();
    } catch (e) {
      setErr(describeError(e));
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="space-y-4">
      <div>
        <h2 className="font-display text-3xl font-semibold">Concurrency experiment</h2>
        <p className="text-ink-soft max-w-[70ch]">
          Fires every request at the same instant through Nginx, so they land on all three servers, then counts the result
          straight from PostgreSQL. Nothing on this panel is precomputed.
        </p>
      </div>
      <div className="grid lg:grid-cols-[300px_1fr] gap-5 items-start">
        <div className="bg-panel border border-rule rounded-lg p-4 space-y-3">
          <fieldset className="space-y-2">
            <legend className="text-[14px] text-ink-soft mb-1">Experiment</legend>
            {scenarios.map((s) => (
              <label key={s.id} className={`block rounded-md border px-3 py-2 cursor-pointer ${scenario === s.id ? "border-ink bg-ground" : "border-rule"}`}>
                <input type="radio" name="scenario" className="sr-only" checked={scenario === s.id} onChange={() => setScenario(s.id)} />
                <span className="font-semibold text-[15px] block">{s.name}</span>
                <span className="text-[13px] text-ink-soft">{s.hint}</span>
              </label>
            ))}
          </fieldset>
          <div className="grid grid-cols-2 gap-3">
            <label className="block"><span className="text-[14px] text-ink-soft">Requests</span>
              <input className="input" type="number" min={1} max={2000} value={requests} onChange={(e) => setRequests(Number(e.target.value))} /></label>
            {(scenario === "limited" || scenario === "idempotency") && (
              <label className="block"><span className="text-[14px] text-ink-soft">Berths</span>
                <input className="input" type="number" min={1} max={500} value={seats} onChange={(e) => setSeats(Number(e.target.value))} /></label>
            )}
          </div>
          {scenario === "limited" && (
            <label className="block"><span className="text-[14px] text-ink-soft">Seat choice</span>
              <select className="input" value={allocation} onChange={(e) => setAllocation(e.target.value)}>
                <option value="targeted">Each request names a berth</option>
                <option value="auto">Server allocates (SKIP LOCKED)</option>
              </select></label>
          )}
          <button className="btn-primary w-full" onClick={run} disabled={running || starting}>
            {running ? "Experiment running…" : starting ? "Starting…" : `Fire ${scenario === "rate_limit" ? Math.min(requests, 60) : requests} requests`}
          </button>
          {err && <Notice tone="error">{err}</Notice>}
        </div>

        <div className="min-w-0 space-y-4">
          {runs.loading && !runs.data && <Spinner label="Loading runs" />}
          {!runs.loading && list.length === 0 && <Notice tone="empty" title="No experiments yet">Choose one on the left and fire it. Results appear here in a few seconds.</Notice>}
          {current?.summary && <RunDetail s={current.summary} />}
          {current && !current.summary && <Spinner label={`Running ${current.id}`} />}
          {list.length > 0 && (
            <div className="bg-panel border border-rule rounded-lg overflow-auto max-h-[420px]">
              <table className="w-full text-[14px]">
                <caption className="sr-only">Recent experiment runs</caption>
                <thead className="text-left text-ink-soft sticky top-0 bg-panel"><tr>
                  <th className="px-3 py-2 font-medium">Run</th><th className="px-3 py-2 font-medium">Mode</th>
                  <th className="px-3 py-2 font-medium text-right">Requests</th><th className="px-3 py-2 font-medium text-right">Booked</th>
                  <th className="px-3 py-2 font-medium text-right">p95</th><th className="px-3 py-2 font-medium">Verdict</th></tr></thead>
                <tbody>
                  {list.map((r) => (
                    <tr key={r.id} onClick={() => setSelected(r.id)} className={`border-t border-rule cursor-pointer hover:bg-ground ${current?.id === r.id ? "bg-ground" : ""}`}>
                      <td className="px-3 py-2"><button className="text-left hover:underline">{scenarios.find((s) => s.id === r.scenario)?.name ?? r.scenario}</button>
                        <span className="block text-[12px] text-ink-soft">{new Date(r.started_at).toLocaleTimeString()}{r.summary?.allocation ? `, ${r.summary.allocation}` : ""}</span></td>
                      <td className="px-3 py-2">{r.summary?.coordination_mode ?? "—"}</td>
                      <td className="px-3 py-2 text-right">{r.summary?.total_requests ?? "—"}</td>
                      <td className="px-3 py-2 text-right">{r.summary ? `${r.summary.successful_bookings} / ${r.summary.seats_available_at_start}` : "—"}</td>
                      <td className="px-3 py-2 text-right">{r.summary ? `${Math.round(r.summary.latency_ms.p95)} ms` : "—"}</td>
                      <td className="px-3 py-2"><Verdict status={r.status} scenario={r.scenario} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function Verdict({ status, scenario }: { status: string; scenario: string }) {
  if (status === "RUNNING") return <span className="text-ink-soft">running</span>;
  if (status === "PASSED") return <span className="text-proceed font-semibold">all checks passed</span>;
  return <span className="text-stop font-semibold">{scenario === "naive_race" ? "double booking shown" : "check failed"}</span>;
}

function RunDetail({ s }: { s: BenchSummary }) {
  async function download(fmt: "json" | "csv") {
    try {
      const r = await api.get(`/api/admin/benchmark/runs/${s.run_id}/report`, { params: { format: fmt }, headers: adminHeaders(), responseType: "blob" });
      const url = URL.createObjectURL(r.data as Blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${s.run_id}.${fmt}`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(describeError(e));
    }
  }
  const naive = s.scenario === "naive_race";
  return (
    <div className="bg-panel border border-rule rounded-lg p-5 space-y-5">
      <div className="flex flex-wrap justify-between gap-4 items-start">
        <div>
          <p className="font-display text-[44px] font-bold leading-none">
            {s.successful_bookings} booked <span className="text-ink-soft font-semibold text-3xl">of {s.total_requests} requests</span>
          </p>
          <p className={`mt-1 font-semibold ${s.all_checks_passed ? "text-proceed" : "text-stop"}`}>
            {s.all_checks_passed
              ? `${s.seats_available_at_start} berth${s.seats_available_at_start > 1 ? "s" : ""} available, ${s.unique_seats_booked} booked, ${s.duplicate_seat_assignments} booked twice. Verified in PostgreSQL.`
              : naive ? `One berth was sold ${s.duplicate_seat_assignments + 1} times. This is the race the real booking path prevents.`
              : "At least one invariant failed. See the checks below."}
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-quiet" onClick={() => download("json")}>Download JSON</button>
          <button className="btn-quiet" onClick={() => download("csv")}>Download CSV</button>
        </div>
      </div>
      {s.request_grid && <RequestGrid grid={s.request_grid} />}
      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-4">
        <Stat k="Seat already taken (409)" v={s.seat_unavailable_responses} />
        <Stat k="Duplicate requests" v={s.duplicate_request_responses} />
        <Stat k="Rate limited (429)" v={s.rate_limited_responses} />
        <Stat k="Unexpected errors" v={s.unexpected_errors} bad={s.unexpected_errors > 0} />
        <Stat k="Average latency" v={`${Math.round(s.latency_ms.avg)} ms`} />
        <Stat k="Median latency" v={`${Math.round(s.latency_ms.median)} ms`} />
        <Stat k="95th percentile" v={`${Math.round(s.latency_ms.p95)} ms`} />
        <Stat k="Whole burst" v={`${s.duration_s.toFixed(2)} s`} />
      </dl>
      <div>
        <p className="font-semibold mb-2">Checks against PostgreSQL</p>
        <ul className="space-y-1.5 text-[15px]">
          {s.checks.map((c) => (
            <li key={c.check} className="flex gap-3">
              <span className={`shrink-0 w-12 font-semibold ${c.pass ? "text-proceed" : "text-stop"}`}>{c.pass ? "Pass" : "Fail"}</span>
              <span>{c.check}<span className="text-ink-soft">: expected {String(c.expected)}, got {String(c.actual)}</span></span>
            </li>
          ))}
        </ul>
        {s.note && <p className="text-[14px] text-ink-soft mt-2">{s.note}</p>}
        {s.warning && <p className="text-[14px] text-stop mt-2">{s.warning}</p>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ charts */
function TrafficCharts({ m }: { m: Metrics }) {
  const perNode = NODES.map((n) => {
    const e = m.nodes[n] ?? {};
    return {
      node: `Node ${n}`,
      Received: e.REQUEST_RECEIVED ?? 0,
      Booked: e.BOOKING_COMMITTED ?? 0,
      Rejected: (e.SEAT_UNAVAILABLE ?? 0) + (e.LOCK_FAILED ?? 0) + (e.RATE_LIMITED ?? 0) + (e.REQUEST_IN_PROGRESS ?? 0) + (e.IDEMPOTENCY_CONFLICT ?? 0),
    };
  });
  const outcomes = [
    { k: "Booked", v: m.outcomes.success, c: "var(--color-proceed)" },
    { k: "Seat taken", v: m.outcomes.conflict, c: "var(--color-stop)" },
    { k: "Duplicate", v: m.outcomes.duplicate, c: "var(--color-node-b)" },
    { k: "Rate limited", v: m.outcomes.rate_limited, c: "var(--color-caution)" },
    { k: "Errors", v: m.outcomes.error, c: "#111827" },
  ];
  return (
    <section className="space-y-3">
      <div>
        <h2 className="font-display text-3xl font-semibold">Traffic, last hour</h2>
        <p className="text-ink-soft">From the shared audit log that all three servers write to.</p>
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        <ChartBox title="Requests handled by each server">
          <BarChart data={perNode} margin={{ left: 4, right: 8 }}>
            <CartesianGrid vertical={false} stroke="var(--color-rule)" />
            <XAxis dataKey="node" tick={{ fontSize: 13 }} /><YAxis tick={{ fontSize: 12 }} width={44} />
            <Tooltip /><Legend />
            <Bar dataKey="Received" fill="var(--color-ink)" radius={[3, 3, 0, 0]} />
            <Bar dataKey="Booked" fill="var(--color-proceed)" radius={[3, 3, 0, 0]} />
            <Bar dataKey="Rejected" fill="var(--color-stop)" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ChartBox>
        <ChartBox title="Booked versus rejected">
          <BarChart data={outcomes} layout="vertical" margin={{ left: 12, right: 16 }}>
            <CartesianGrid horizontal={false} stroke="var(--color-rule)" />
            <XAxis type="number" tick={{ fontSize: 12 }} /><YAxis type="category" dataKey="k" width={92} tick={{ fontSize: 13 }} />
            <Tooltip />
            <Bar dataKey="v" name="Requests" radius={[0, 3, 3, 0]} isAnimationActive={false}
                 shape={(props: unknown) => { const p = props as { x: number; y: number; width: number; height: number; payload: { c: string } };
                   return <rect x={p.x} y={p.y} width={Math.max(p.width, 0)} height={p.height} fill={p.payload.c} rx={3} />; }} />
          </BarChart>
        </ChartBox>
      </div>
      {m.timeline.length > 0 && (
        <ChartBox title="Requests per minute, by server (last 30 minutes)">
          <LineChart data={m.timeline} margin={{ left: 4, right: 12 }}>
            <CartesianGrid vertical={false} stroke="var(--color-rule)" />
            <XAxis dataKey="t" tick={{ fontSize: 12 }} /><YAxis tick={{ fontSize: 12 }} width={44} />
            <Tooltip /><Legend />
            {NODES.map((n) => <Line key={n} type="linear" dataKey={n} name={`Node ${n}`} stroke={nodeColor[n]} strokeWidth={2} dot={{ r: 3 }} connectNulls />)}
          </LineChart>
        </ChartBox>
      )}
    </section>
  );
}

function ChartBox({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <figure className="bg-panel border border-rule rounded-lg p-4">
      <figcaption className="font-semibold mb-2">{title}</figcaption>
      <div className="h-64"><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div>
    </figure>
  );
}

/* ------------------------------------------------------------------ failure injection */
function FailurePanel() {
  const [delay, setDelay] = useState(200);
  const [msg, setMsg] = useState<string | null>(null);
  const state = usePolling(() => api.get<{ delay_ms: number }>("/api/admin/failure/state", { headers: adminHeaders() }).then((r) => r.data), 3000);
  async function call(method: "post" | "delete") {
    try {
      await api.request({ url: "/api/admin/failure/delay", method, data: method === "post" ? { delay_ms: delay, ttl_s: 300 } : undefined, headers: adminHeaders() });
      setMsg(method === "post" ? `Every server now waits ${delay} ms inside the booking transaction for 5 minutes.` : "Delay removed.");
      state.refresh();
    } catch (e) { setMsg(describeError(e)); }
  }
  return (
    <section className="bg-panel border border-rule rounded-lg p-5 grid lg:grid-cols-2 gap-6">
      <div className="space-y-3">
        <h2 className="font-display text-3xl font-semibold">Fault injection</h2>
        <p className="text-ink-soft text-[15px] max-w-[60ch]">
          Crash or brown-out a single server from its card above; Nginx sends its traffic to the other two and the
          supervisor restarts a crashed process. That is a <b className="text-ink">process failure</b>. PostgreSQL, Redis and
          Nginx each run once here, so losing one of those is a different, cluster-wide failure: the system is fault tolerant
          to losing a server, not highly available.
        </p>
      </div>
      <div className="space-y-3">
        <p className="font-semibold">Widen the race window</p>
        <p className="text-[15px] text-ink-soft">Adds a pause after the seat is claimed and before the commit, on every server. Then run “One seat” again and watch more requests queue behind the lock.</p>
        <label className="block">
          <span className="text-[14px] text-ink-soft">Delay: {delay} ms</span>
          <input type="range" min={0} max={1000} step={50} value={delay} onChange={(e) => setDelay(Number(e.target.value))} className="w-full accent-ink" />
        </label>
        <div className="flex gap-2">
          <button className="btn-quiet" onClick={() => call("post")}>Apply delay</button>
          <button className="btn-quiet" onClick={() => call("delete")}>Remove delay</button>
        </div>
        <p className="text-[14px]">Current cluster delay: <b>{state.data?.delay_ms ?? 0} ms</b></p>
        {msg && <p className="text-[14px] text-ink-soft">{msg}</p>}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ shards */
function ShardPanel({ m }: { m: Metrics }) {
  return (
    <section className="grid lg:grid-cols-2 gap-4">
      <div className="bg-panel border border-rule rounded-lg p-5">
        <h2 className="font-display text-2xl font-semibold">Logical seat shards</h2>
        <p className="text-[14px] text-ink-soft mb-3">Each berth hashes to one shard; its lock key carries the shard tag. All shards share one PostgreSQL today.</p>
        <table className="w-full text-[15px]">
          <thead className="text-ink-soft text-left"><tr><th className="font-medium py-1">Shard</th><th className="font-medium">Preferred node</th><th className="font-medium text-right">Booked</th><th className="font-medium text-right">Free</th></tr></thead>
          <tbody>{m.shards.map((s) => (
            <tr key={s.shard_id} className="border-t border-rule">
              <td className="py-1.5">Shard {s.shard_id}</td><td>Node {m.shard_home_nodes[String(s.shard_id)]}</td>
              <td className="text-right">{s.booked}</td><td className="text-right">{s.available}</td></tr>))}
          </tbody>
        </table>
      </div>
      <div className="bg-panel border border-rule rounded-lg p-5">
        <h2 className="font-display text-2xl font-semibold">Inventory</h2>
        <p className="text-[14px] text-ink-soft mb-3">Berth states across all simulated journeys, read from PostgreSQL.</p>
        <table className="w-full text-[15px]">
          <thead className="text-ink-soft text-left"><tr><th className="font-medium py-1">State</th><th className="font-medium text-right">Passenger journeys</th><th className="font-medium text-right">Experiment journey</th></tr></thead>
          <tbody>{["AVAILABLE", "BOOKED", "BLOCKED"].map((k) => (
            <tr key={k} className="border-t border-rule"><td className="py-1.5 capitalize">{k.toLowerCase()}</td>
              <td className="text-right">{m.inventory.live[k] ?? 0}</td><td className="text-right">{m.inventory.test[k] ?? 0}</td></tr>))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ events */
const eventTone: Record<string, string> = {
  BOOKING_COMMITTED: "text-proceed", SEAT_UNAVAILABLE: "text-stop", LOCK_FAILED: "text-caution", RATE_LIMITED: "text-caution",
  UNEXPECTED_ERROR: "text-stop", DEPENDENCY_FAILURE: "text-stop", FAILURE_INJECTED: "text-stop", DUPLICATE_REQUEST: "text-node-b",
};

function EventFeed() {
  const [type, setType] = useState("");
  const ev = usePolling(() => api.get<BookingEvent[]>("/api/admin/booking-events", { headers: adminHeaders(), params: { limit: 60, event_type: type || undefined } }).then((r) => r.data), 3000, [type]);
  const types = useMemo(() => ["", "REQUEST_RECEIVED", "LOCK_ACQUIRED", "LOCK_FAILED", "SEAT_CLAIM_ATTEMPTED", "BOOKING_COMMITTED",
    "SEAT_UNAVAILABLE", "DUPLICATE_REQUEST", "RATE_LIMITED", "DEPENDENCY_FAILURE", "FAILURE_INJECTED", "NODE_STARTED", "UNEXPECTED_ERROR"], []);
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-display text-3xl font-semibold">Event log</h2>
          <p className="text-ink-soft">Newest first. Passenger names are never written here.</p>
        </div>
        <select className="input max-w-xs" value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter events">
          {types.map((t) => <option key={t} value={t}>{t || "All events"}</option>)}
        </select>
      </div>
      <div className="bg-panel border border-rule rounded-lg overflow-x-auto max-h-[420px] overflow-y-auto">
        {ev.data && ev.data.length === 0 && <p className="p-4 text-ink-soft">No events of this type yet.</p>}
        <table className="w-full text-[13.5px]">
          <thead className="text-left text-ink-soft sticky top-0 bg-panel"><tr>
            <th className="px-3 py-2 font-medium">Time</th><th className="px-3 py-2 font-medium">Node</th><th className="px-3 py-2 font-medium">Event</th>
            <th className="px-3 py-2 font-medium">Outcome</th><th className="px-3 py-2 font-medium">Request</th><th className="px-3 py-2 font-medium">Details</th></tr></thead>
          <tbody>
            {ev.data?.map((e) => (
              <tr key={e.id} className="border-t border-rule align-top">
                <td className="px-3 py-1.5 whitespace-nowrap">{new Date(e.event_timestamp).toLocaleTimeString([], { hour12: false })}.{String(new Date(e.event_timestamp).getMilliseconds()).padStart(3, "0")}</td>
                <td className="px-3 py-1.5"><span className="inline-block size-2 rounded-full mr-1.5" style={{ background: nodeColor[e.node_id] ?? "#999" }} />{e.node_id}</td>
                <td className={`px-3 py-1.5 font-medium ${eventTone[e.event_type] ?? ""}`}>{e.event_type}</td>
                <td className="px-3 py-1.5">{e.outcome ?? ""}</td>
                <td className="px-3 py-1.5 text-ink-soft">{e.request_id?.slice(0, 8) ?? ""}</td>
                <td className="px-3 py-1.5 text-ink-soft break-all">{e.metadata ? Object.entries(e.metadata).map(([k, v]) => `${k}=${v}`).join(", ") : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
