import { Link } from "react-router-dom";
import { api, currentUser } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { Booking } from "../types";
import TicketSlip from "../components/TicketSlip";
import Notice, { Spinner } from "../components/Notice";

export default function MyBookings() {
  const user = currentUser();
  const { data, error, loading } = usePolling(
    () => api.get<Booking[]>(`/api/users/${user}/bookings`, { headers: { "X-User-Id": user } }).then((r) => r.data), 0);
  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h1 className="font-display text-4xl font-bold">My bookings</h1>
        <p className="text-ink-soft">Signed in as demo user <b className="text-ink">{user}</b>. This id lives in your browser; real sign-in would replace it.</p>
      </div>
      {loading && !data && <Spinner label="Loading bookings" />}
      {!!error && <Notice tone="error" title="Bookings could not be loaded">Refresh the page to try again.</Notice>}
      {data && data.length === 0 && (
        <Notice tone="empty" title="No tickets yet">
          <Link to="/" className="text-node-b underline">Pick a train</Link> and book a berth; it will appear here.
        </Notice>
      )}
      <div className="space-y-4">{data?.map((b) => <TicketSlip key={b.booking_id} b={b} compact />)}</div>
    </div>
  );
}
