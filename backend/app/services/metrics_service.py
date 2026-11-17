"""Cluster metrics computed from authoritative tables and the shared event log."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.monitoring.events import recorder

REJECT_TYPES = ("SEAT_UNAVAILABLE", "LOCK_FAILED", "RATE_LIMITED", "IDEMPOTENCY_CONFLICT", "REQUEST_IN_PROGRESS")


async def cluster_metrics(s: AsyncSession, window_minutes: int) -> dict:
    params = {"w": window_minutes}
    per_node_rows = (await s.execute(text(
        """
        SELECT node_id, event_type, count(*) AS n
          FROM booking_events
         WHERE event_timestamp > now() - make_interval(mins => :w)
         GROUP BY node_id, event_type
        """), params)).all()
    nodes: dict[str, dict[str, int]] = {}
    for node, etype, n in per_node_rows:
        nodes.setdefault(node, {})[etype] = n

    outcome_rows = (await s.execute(text(
        """
        SELECT CASE WHEN event_type = 'BOOKING_COMMITTED' THEN 'success'
                    WHEN event_type = 'RATE_LIMITED' THEN 'rate_limited'
                    WHEN event_type = 'DUPLICATE_REQUEST' THEN 'duplicate'
                    WHEN event_type = 'UNEXPECTED_ERROR' THEN 'error'
                    ELSE 'conflict' END AS bucket,
               count(*)
          FROM booking_events
         WHERE event_timestamp > now() - make_interval(mins => :w)
           AND (event_type IN ('BOOKING_COMMITTED','RATE_LIMITED','DUPLICATE_REQUEST','UNEXPECTED_ERROR','REQUEST_IN_PROGRESS','IDEMPOTENCY_CONFLICT')
                OR (event_type = 'SEAT_UNAVAILABLE')
                OR (event_type = 'LOCK_FAILED'))
         GROUP BY 1
        """), params)).all()
    outcomes = {"success": 0, "conflict": 0, "rate_limited": 0, "duplicate": 0, "error": 0}
    outcomes.update({b: n for b, n in outcome_rows})

    timeline_rows = (await s.execute(text(
        """
        SELECT to_char(date_trunc('minute', event_timestamp), 'HH24:MI') AS t, node_id, count(*)
          FROM booking_events
         WHERE event_type = 'REQUEST_RECEIVED' AND event_timestamp > now() - interval '30 minutes'
         GROUP BY 1, 2 ORDER BY 1
        """))).all()
    timeline: dict[str, dict[str, int]] = {}
    for t, node, n in timeline_rows:
        timeline.setdefault(t, {"t": t})[node] = n

    inventory = (await s.execute(text(
        """
        SELECT j.is_test, s.status, count(*) FROM journey_seats s JOIN journeys j ON j.id = s.journey_id
         GROUP BY 1, 2
        """))).all()
    inv = {"live": {}, "test": {}}
    for is_test, status, n in inventory:
        inv["test" if is_test else "live"][status] = n

    shards = (await s.execute(text(
        """
        SELECT s.shard_id, count(*) FILTER (WHERE s.status = 'BOOKED') AS booked,
               count(*) FILTER (WHERE s.status = 'AVAILABLE') AS available, count(*) AS total
          FROM journey_seats s GROUP BY 1 ORDER BY 1
        """))).mappings().all()

    integrity = (await s.execute(text(
        """
        SELECT
          (SELECT count(*) FROM (SELECT journey_seat_id FROM bookings WHERE status='CONFIRMED'
                                  GROUP BY 1 HAVING count(*) > 1) d) AS duplicate_seat_bookings,
          (SELECT count(*) FROM journey_seats s WHERE s.status='BOOKED' AND NOT EXISTS
              (SELECT 1 FROM bookings b WHERE b.id = s.booking_id AND b.status='CONFIRMED' AND b.journey_seat_id = s.id))
                                                                                         AS booked_without_booking,
          (SELECT count(*) FROM bookings b WHERE b.status='CONFIRMED' AND NOT EXISTS
              (SELECT 1 FROM journey_seats s WHERE s.id = b.journey_seat_id AND s.booking_id = b.id AND s.status='BOOKED'))
                                                                                         AS booking_without_seat,
          (SELECT count(*) FROM bookings WHERE status='CONFIRMED') AS confirmed_bookings
        """))).mappings().one()

    st = get_settings()
    return {
        "window_minutes": window_minutes,
        "nodes": nodes,
        "outcomes": outcomes,
        "timeline": list(timeline.values()),
        "inventory": inv,
        "shards": [dict(r) for r in shards],
        "integrity": dict(integrity),
        "config": {
            "coordination_mode": st.coordination_mode,
            "redis_failure_policy": st.redis_failure_policy,
            "lock_ttl_ms": st.lock_ttl_ms,
            "lock_wait_ms": st.lock_wait_ms,
            "rate_limit_enabled": st.rate_limit_enabled,
            "rate_limit_per_user": st.rate_limit_per_user,
            "rate_limit_per_ip": st.effective_ip_limit,
            "rate_limit_window_s": st.rate_limit_window_s,
            "rate_limit_test_mode": st.rate_limit_test_mode,
            "num_shards": st.num_shards,
        },
        "reporting_node": {"node_id": st.node_id, "events_written": recorder.written,
                           "events_dropped": recorder.dropped, "event_backlog": recorder.backlog},
    }
