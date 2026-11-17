export interface Train {
  id: number;
  train_number: string;
  train_name: string;
  source: string;
  destination: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  is_simulated: boolean;
}

export interface Journey {
  id: number;
  journey_date: string;
  status: string;
  is_test: boolean;
  label: string;
  train: Train;
  total_seats: number;
  available_seats: number;
  booked_seats: number;
}

export type SeatStatus = "AVAILABLE" | "HELD" | "BOOKED" | "BLOCKED";

export interface Seat {
  id: number;
  coach_number: string;
  seat_number: number;
  berth_type: string;
  shard_id: number;
  status: SeatStatus;
}

export interface SeatMap {
  journey: Journey;
  coaches: string[];
  seats: Seat[];
  coordination_mode: string;
}

export interface Passenger {
  name: string;
  age: number;
  gender: "M" | "F" | "O";
}

export interface Booking {
  booking_id: string;
  pnr: string;
  status: string;
  user_id: string;
  journey_id: number;
  seat: { id: number; coach_number: string; seat_number: number; berth_type: string; shard_id: number };
  passenger_name: string;
  passenger_age: number;
  passenger_gender: string;
  node_id: string;
  created_at: string;
  train_number?: string;
  train_name?: string;
  journey_date?: string;
  coordination?: string;
  request_id?: string;
}

export interface ApiErrorBody {
  error: { code: string; message: string };
  node_id?: string;
  details?: { loc: (string | number)[]; msg: string }[];
}

export interface Health {
  node_id: string;
  pid: number;
  uptime_s: number;
  coordination_mode: string;
  status: "ok" | "degraded" | "unhealthy";
  database: { ok: boolean; latency_ms?: number; error?: string };
  redis: { ok: boolean; latency_ms?: number; error?: string };
  injected: { unhealthy_remaining_s: number; delay_ms: number };
  events: { written: number; dropped: number; backlog: number };
}

export interface Metrics {
  window_minutes: number;
  nodes: Record<string, Record<string, number>>;
  outcomes: Record<string, number>;
  timeline: Record<string, string | number>[];
  inventory: { live: Record<string, number>; test: Record<string, number> };
  shards: { shard_id: number; booked: number; available: number; total: number }[];
  integrity: {
    duplicate_seat_bookings: number;
    booked_without_booking: number;
    booking_without_seat: number;
    confirmed_bookings: number;
  };
  config: {
    coordination_mode: string;
    redis_failure_policy: string;
    lock_ttl_ms: number;
    lock_wait_ms: number;
    rate_limit_enabled: boolean;
    rate_limit_per_user: number;
    rate_limit_per_ip: number;
    rate_limit_window_s: number;
    rate_limit_test_mode: boolean;
    num_shards: number;
  };
  shard_home_nodes: Record<string, string>;
}

export interface BenchCheck {
  check: string;
  expected: unknown;
  actual: unknown;
  pass: boolean;
}

export interface BenchSummary {
  run_id: string;
  scenario: string;
  allocation: string;
  coordination_mode: string;
  total_requests: number;
  successful_bookings: number;
  seat_unavailable_responses: number;
  duplicate_request_responses: number;
  rate_limited_responses: number;
  validation_errors: number;
  unexpected_errors: number;
  unique_seats_booked: number;
  duplicate_seat_assignments: number;
  seats_available_at_start: number;
  requests_by_node: Record<string, number>;
  latency_ms: { avg: number; median: number; p95: number; p99: number; max: number; min: number };
  duration_s: number;
  throughput_rps: number;
  consistency_result: string;
  all_checks_passed: boolean;
  checks: BenchCheck[];
  request_grid?: [string, string][];
  note?: string;
  warning?: string;
}

export interface BenchRun {
  id: string;
  scenario: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  summary: BenchSummary | null;
}

export interface BookingEvent {
  id: number;
  event_timestamp: string;
  node_id: string;
  event_type: string;
  outcome: string | null;
  request_id: string | null;
  booking_id: string | null;
  journey_id: number | null;
  metadata: Record<string, unknown> | null;
}
