import { Link, useSearchParams } from "react-router-dom";
import { api, fmtDate } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { Journey } from "../types";
import Notice, { Spinner } from "../components/Notice";
import { JourneyList } from "./Home";

export default function Results() {
  const [params] = useSearchParams();
  const from = params.get("from") || "";
  const to = params.get("to") || "";
  const date = params.get("date") || "";
  const { data, error, loading } = usePolling(
    () => api.get<Journey[]>("/api/journeys", { params: { source: from || undefined, destination: to || undefined, date: date || undefined } }).then((r) => r.data),
    10000, [from, to, date]);

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-[15px] text-node-b hover:underline">Change search</Link>
        <h1 className="font-display text-4xl font-bold mt-1">
          {from || "Any station"} to {to || "any station"}
        </h1>
        {date && <p className="text-ink-soft">{fmtDate(date)}</p>}
      </div>
      {loading && !data && <Spinner label="Searching trains" />}
      {!!error && <Notice tone="error" title="Search failed">The booking service did not answer. Try again in a moment.</Notice>}
      {data && data.length === 0 && (
        <Notice tone="empty" title="No trains match">Try another date or leave a station empty to see every simulated route.</Notice>
      )}
      {data && data.length > 0 && <JourneyList journeys={data} />}
    </div>
  );
}
