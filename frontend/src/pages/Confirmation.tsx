import { Link, useLocation, useParams } from "react-router-dom";
import { api, currentUser } from "../services/api";
import { usePolling } from "../hooks/usePolling";
import type { Booking } from "../types";
import TicketSlip from "../components/TicketSlip";
import Notice, { Spinner } from "../components/Notice";

export default function Confirmation() {
  const { id } = useParams();
  const state = useLocation().state as { booking?: Booking; servedBy?: string; replayed?: boolean; attempts?: number } | null;
  const { data, error } = usePolling(
    () => state?.booking ? Promise.resolve(state.booking)
      : api.get<Booking>(`/api/bookings/${id}`, { headers: { "X-User-Id": currentUser() } }).then((r) => r.data), 0, [id]);

  if (error) return <Notice tone="error" title="Booking not found">This booking does not exist or belongs to another demo user.</Notice>;
  if (!data) return <Spinner label="Loading ticket" />;
  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="font-display text-5xl font-bold leading-none">Berth confirmed.</h1>
        <p className="text-ink-soft mt-2 text-lg">
          Your PNR is {data.pnr}. Node {data.node_id} committed it to PostgreSQL in one transaction with the seat claim.
          {state?.servedBy && state.servedBy !== data.node_id && ` This response came from node ${state.servedBy}, replaying the stored result.`}
        </p>
      </div>
      <TicketSlip b={data} />
      <div className="flex gap-3 flex-wrap">
        <Link to="/bookings" className="btn-quiet inline-flex items-center">See all my bookings</Link>
        <Link to="/" className="btn-quiet inline-flex items-center">Book another berth</Link>
      </div>
    </div>
  );
}
