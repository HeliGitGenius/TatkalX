# TatkalX — Viva-voce guide

Short, examinable answers, each tied to something you can show in TatkalX.

---

**1. What makes this application a distributed system?**
Three independent server processes (Node A, B, C) — separate memory, separate connection pools, separate failure —
cooperate through the network to provide one booking service. They coordinate only through shared services
(PostgreSQL, Redis) and a load balancer. Any request can land on any node, and one node can fail while the others continue.
*Show:* the `X-Served-By` header / the Control room's request grid split across A, B, C.

**2. Why are three backend instances useful?**
(a) **Capacity** — load is spread (500 requests → 167/167/166). (b) **Fault tolerance** — when Node A was killed in the
failure demo, B and C served everything and 480/480 bookings succeeded. (c) It makes the concurrency problem *real*:
two requests for berth 17 can be inside two different processes at the same instant, so an in-memory Python lock cannot help.

**3. What is mutual exclusion?**
Guaranteeing that at most one actor is inside a *critical section* at a time. In TatkalX the critical section is
"check that berth 17 is free, then mark it booked". Two mechanisms give it: the Redis lock (one holder per seat key)
and, authoritatively, PostgreSQL's row lock taken by `UPDATE … WHERE status='AVAILABLE'`.

**4. What is a race condition?**
When the result depends on the timing of concurrent operations. Classic form: *check-then-act*. Request X reads
"AVAILABLE", request Y reads "AVAILABLE", both write "BOOKED" — two tickets, one berth.
*Show:* the "Without protection" experiment sold one berth **4 times** out of 500 requests.

**5. What is distributed locking?**
A lock held in a service all nodes can reach, so processes on different machines exclude each other.
TatkalX: `SET tatkalx:{shard-2}:lock:journey:68:seat:9012 <random-token> NX PX 5000`.
`NX` = only if absent (atomic acquire), `PX` = lease so a crashed holder can't block forever,
unique token + Lua compare-and-delete = release only your own lock (tested: a stale holder cannot delete the new holder's lock).

**6. Why is a database transaction necessary?**
Because a booking is *several* writes that must all happen or none: claim the seat, insert the booking, store the
idempotent response. If the node crashes between them, the transaction rolls back — no seat stuck as BOOKED without a
booking, no booking pointing at a free seat. Atomicity + isolation (READ COMMITTED + row locks) are exactly the
properties we need.

**7. Why is a unique database constraint necessary?**
It is a safety net that does not depend on application code being right. The partial unique index
`ON bookings(journey_seat_id) WHERE status='CONFIRMED'` makes a second confirmed booking for the same seat
*impossible to store*, even from a buggy code path or a manual SQL insert (there is a test that tries exactly that and gets an IntegrityError).
`UNIQUE(user_id, idempotency_key)` does the same for duplicate requests.

**8. What is idempotency?**
Doing an operation twice has the same effect as doing it once. The client sends `Idempotency-Key` with each booking
attempt; the server stores the outcome under that key. A retry after a timeout gets the *stored* result (header
`Idempotent-Replayed: true`) instead of a new booking. Same key + different body → 422.
*Differs from locking:* a lock separates *different* requests competing for one seat; idempotency recognises the
*same* request arriving again. *Differs from the seat uniqueness constraint:* that stops two bookings of one seat;
idempotency stops one user's retry from booking a *second, different* seat (e.g. with auto-allocation).
*Show:* retry storm — 500 requests (100 × 5 copies) produced exactly 100 bookings; a retry pass returned the same ids 100/100.

**9. What is sharding?**
Splitting data into partitions so each can live on (and be served by) different machines. TatkalX hashes each seat
(`crc32(coach-seat) % 3 + 1`) to logical shard 1–3. The shard id is stored on the seat, tags the Redis lock key
(`{shard-2}` → same Redis Cluster slot), and is reported per shard in the dashboard. Requests for the same seat always map to
the same shard and lock key, whichever node receives them. *Honest limitation:* all three shards live in one PostgreSQL
database — `shard_dsn()` is the extension point for real per-shard databases.

**10. What is load balancing?**
Distributing requests across servers. Nginx uses round robin across A/B/C, passive health checks
(`max_fails=1 fail_timeout=5s`) to stop sending to a failed node, and `proxy_next_upstream` to re-send a failed request
to another node. Re-sending a POST is normally dangerous; here it is safe *because* of idempotency keys.

**11. What happens when a node fails?**
Its in-flight requests fail at Nginx, which retries them on another node (same idempotency key ⇒ no double booking).
New requests skip it for 5 s, then Nginx probes it again. The supervisor (or Docker `restart: on-failure`) restarts it;
it reconnects to PostgreSQL/Redis on start-up. In the demo: A down ~8 s, 0 failed bookings, 4 transparent re-sends.
If a node dies *after* reserving an idempotency key but before committing, the record stays PROCESSING; after 10 s a
retry may take it over — and the old owner, if it wakes up, is fenced out because its final UPDATE is conditional on its own token.

**12. What are the limitations of Redis locks?**
- **Lease expiry vs. pauses:** if the holder stalls (GC, VM pause) longer than the TTL, the lock expires and a second process acquires it while the first still thinks it holds it.
- **Asynchronous replication:** a Redis primary can grant a lock and fail over before replicating it.
- **Redlock** (majority of N Redis masters) narrows these windows but still assumes bounded clock drift and pauses — it is not a correctness guarantee.
That is why TatkalX treats the lock as an *optimisation*: in run 8, Redis granted **102 locks for 50 seats**, yet exactly 50
bookings committed because PostgreSQL's conditional UPDATE is the real arbiter. Fencing tokens would be the next step.

**13. How does the 500-request test demonstrate correctness?**
It creates a fresh test journey, builds all 500 requests, releases them at the same instant through Nginx (so they hit
all three nodes), records every response, then **queries PostgreSQL directly** and checks invariants: no seat with two
CONFIRMED bookings; bookings ≤ seats (and = seats when there were no errors); every BOOKED seat ↔ its booking; the
booking ids clients were told about = the ids in the database; zero unexpected errors. Nothing is hard-coded — and the
same harness *fails* loudly on the unprotected endpoint, which shows the checks can detect a bug.

**14. Fault tolerance vs. high availability?**
*Fault tolerance:* the system keeps working correctly despite some component failures (TatkalX tolerates losing any one
backend node, or Redis). *High availability:* the service stays up nearly all the time, which requires *no* single
point of failure. TatkalX is not HA: Nginx, PostgreSQL and Redis each run once — if PostgreSQL stops, bookings return 503 (safely retryable).

**15. What would change for production-scale ticket booking?**
- PostgreSQL primary + synchronous replica with automatic failover (Patroni), PgBouncer; real horizontal sharding by train/date.
- Redis Sentinel/Cluster; fencing tokens stored on the seat row.
- Redundant load balancers; autoscaled stateless nodes; health-checked by an orchestrator (Kubernetes).
- A **virtual waiting room / queue** in front of Tatkal opening time, so 1M users don't hit the DB at once; seat holds with expiry and a payment step (saga / outbox pattern).
- OAuth2/OIDC auth, CAPTCHA/bot defence, sliding-window or token-bucket rate limits per account and device.
- Observability: OpenTelemetry traces, Prometheus metrics, alerting; load tests on production-like hardware.

---

### Ten-second demo script
1. Control room → "One seat, everyone wants it" → **Fire 500 requests** → one green square among 500, checks all pass.
2. "Without protection" → same load → **one berth sold several times**.
3. Click **Crash process** on Node A → watch it go *down* and come back with a new pid while bookings continue.
4. Set **300 ms delay** → rerun "Limited inventory" → more locks than seats, still exactly N bookings.
