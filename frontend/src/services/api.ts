import axios, { AxiosError } from "axios";
import type { ApiErrorBody, Booking, Passenger } from "../types";

export const api = axios.create({ baseURL: "/", timeout: 15000 });

// ---- demo identity (DEMONSTRATION ONLY: a random id in localStorage, sent as X-User-Id) ----
const USER_KEY = "tatkalx.user";
export function currentUser(): string {
  try {
    let u = localStorage.getItem(USER_KEY);
    if (!u) {
      u = `demo-${Math.random().toString(36).slice(2, 10)}`;
      localStorage.setItem(USER_KEY, u);
    }
    return u;
  } catch {
    return "demo-anonymous";
  }
}

const ADMIN_KEY = "tatkalx.adminToken";
export function adminToken(): string {
  try { return localStorage.getItem(ADMIN_KEY) || ""; } catch { return ""; }
}
export function setAdminToken(t: string) {
  try { localStorage.setItem(ADMIN_KEY, t); } catch { /* storage unavailable */ }
}
export const adminHeaders = () => ({ "X-Admin-Token": adminToken() });

export function newIdempotencyKey(): string {
  return `web-${crypto.randomUUID()}`;
}

export interface BookingAttemptResult {
  ok: boolean;
  status: number | null;
  booking?: Booking;
  error?: ApiErrorBody["error"];
  servedBy?: string | null;
  replayed?: boolean;
  attempts: number;
}

/**
 * Book with ONE idempotency key per user action. Network errors and 503s are retried at most
 * twice with the SAME key, so a lost response can never turn into a second booking.
 */
export async function bookSeat(
  journeyId: number,
  seatId: number | null,
  passenger: Passenger,
  key: string,
): Promise<BookingAttemptResult> {
  const body = seatId == null
    ? { journey_id: journeyId, auto_allocate: true, passenger }
    : { journey_id: journeyId, seat_id: seatId, passenger };
  let last: BookingAttemptResult = { ok: false, status: null, attempts: 0 };
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await api.post<Booking>("/api/bookings", body, {
        headers: { "X-User-Id": currentUser(), "Idempotency-Key": key },
      });
      return {
        ok: true, status: r.status, booking: r.data, servedBy: r.headers["x-served-by"],
        replayed: r.headers["idempotent-replayed"] === "true", attempts: attempt,
      };
    } catch (e) {
      const err = e as AxiosError<ApiErrorBody>;
      const status = err.response?.status ?? null;
      last = {
        ok: false, status, attempts: attempt, servedBy: err.response?.headers?.["x-served-by"],
        error: err.response?.data?.error ?? { code: "NETWORK_ERROR", message: "Could not reach the booking service." },
      };
      const retryable = status === null || status === 502 || status === 503 || status === 504 ||
        (status === 409 && err.response?.data?.error?.code === "REQUEST_IN_PROGRESS");
      if (!retryable) return last;
      await new Promise((res) => setTimeout(res, 600 * attempt));
    }
  }
  return last;
}

export function describeError(e: unknown): string {
  const err = e as AxiosError<ApiErrorBody>;
  if (err?.response?.data?.error) return err.response.data.error.message;
  if (err?.message) return err.message;
  return "Something went wrong.";
}

export const fmtTime = (t: string) => t.slice(0, 5);
export const fmtDuration = (m: number) => `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
export const fmtDate = (d: string) =>
  new Date(`${d}T00:00:00`).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
