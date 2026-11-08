"""Piggyback's history in Tiger Data (Postgres + TimescaleDB): every argument's eye contact, 10x a second.

Two hypertables:
    gaze    one row per 0.1 s during a checkout: is the user present, looking at the pig, speaking an answer
    events  checkouts, verdicts, decisions, things added to the bag, waiting-room items let go
Continuous aggregates keep the dashboard queries fast, and old gaze rows are compressed.

Writes go through a queue to a background thread, so a slow or missing database never holds up the
pig. Without TIGER_DB_URL (or if the database is unreachable) everything here does nothing.

    tiger.start(profile)                  # at startup; copies the seeded purchases in once
    tiger.event("verdict", session_id, item=..., verdict=...)
    tiger.gaze(session_id, vision.state, recording)
    tiger.trends()                         # for GET /trends; None without a database
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Days and hours are counted in local time. Changing it needs the money_by_day view dropped and rebuilt.
TZ = os.getenv("TZ_NAME", "America/New_York")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS gaze (
    ts         timestamptz NOT NULL,
    session_id text        NOT NULL,
    present    boolean     NOT NULL,
    looking    boolean     NOT NULL,
    recording  boolean     NOT NULL,   -- the user was answering (the mic was on)
    yaw        real,
    pitch      real,
    eye_turn   real
);
SELECT create_hypertable('gaze', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS events (
    ts         timestamptz NOT NULL,
    session_id text,
    kind       text        NOT NULL,   -- checkout | verdict | decision | added | let_go | purchase
    item       text,
    price      double precision,
    category   text,
    verdict    text,
    detail     jsonb
);
SELECT create_hypertable('events', by_range('ts', INTERVAL '7 days'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts DESC);
CREATE INDEX IF NOT EXISTS gaze_session_ts ON gaze (session_id, ts DESC);

ALTER TABLE gaze SET (timescaledb.compress, timescaledb.compress_segmentby = 'session_id',
                      timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('gaze', INTERVAL '1 day', if_not_exists => TRUE);

-- Eye contact per argument, per minute (only while the user was answering).
CREATE MATERIALIZED VIEW IF NOT EXISTS eye_contact_by_minute
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket, session_id,
       count(*) AS samples,
       count(*) FILTER (WHERE looking) AS looking_samples,
       avg(abs(yaw)) AS avg_abs_yaw
FROM gaze WHERE recording
GROUP BY bucket, session_id
WITH NO DATA;
SELECT add_continuous_aggregate_policy('eye_contact_by_minute', start_offset => INTERVAL '3 days',
       end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

-- Money per day: spent, saved by letting go, and how each verdict went.
CREATE MATERIALIZED VIEW IF NOT EXISTS money_by_day
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 day', ts, '{TZ}') AS day,
       sum(price) FILTER (WHERE kind = 'purchase') AS spent,
       sum(price) FILTER (WHERE kind = 'let_go') AS saved,
       count(*) FILTER (WHERE kind = 'verdict' AND verdict = 'release') AS released,
       count(*) FILTER (WHERE kind = 'verdict' AND verdict = 'timer') AS timers,
       count(*) FILTER (WHERE kind = 'verdict' AND verdict = 'escalate') AS escalated
FROM events
GROUP BY day
WITH NO DATA;
SELECT add_continuous_aggregate_policy('money_by_day', start_offset => INTERVAL '60 days',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
"""

_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=20000)
_ready = threading.Event()
_read_lock = threading.Lock()
_reader = None
state = {"enabled": False, "connected": False, "error": None, "written": 0, "dropped": 0}


def url() -> str:
    return os.getenv("TIGER_DB_URL", "")


def log(*args):
    print(datetime.now().strftime("%H:%M:%S"), "[tiger]", *args, flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect():
    import psycopg

    return psycopg.connect(url(), connect_timeout=5, autocommit=True)


def start(profile: dict | None = None):
    if not url():
        log("TIGER_DB_URL not set; history isn't recorded")
        return
    try:
        import psycopg  # noqa: F401
    except ImportError:
        state["error"] = "pip install 'psycopg[binary]'"
        log("psycopg isn't installed; history isn't recorded")
        return
    state["enabled"] = True
    threading.Thread(target=_writer, args=(profile,), daemon=True).start()


def _put(row: tuple):
    if not state["enabled"]:
        return
    try:
        _queue.put_nowait(row)
    except queue.Full:
        state["dropped"] += 1


def event(kind: str, session_id: str | None = None, item: str | None = None, price: float | None = None,
          category: str | None = None, verdict: str | None = None, **detail):
    _put(("event", (utcnow(), session_id, kind, item, price, category, verdict,
                    json.dumps(detail) if detail else None)))


def gaze(session_id: str, s: dict, recording: bool):
    present = bool(s.get("present"))
    _put(("gaze", (utcnow(), session_id, present, present and bool(s.get("looking")), recording,
                   s.get("yaw") if present else None, s.get("pitch") if present else None,
                   s.get("eye_turn") if present else None)))


def _backfill(conn, profile: dict):
    """The first time only: copy the seeded month of purchases and past arguments, so the charts have history."""
    if conn.execute("SELECT EXISTS (SELECT 1 FROM events)").fetchone()[0]:
        return
    rows = [(datetime.fromisoformat(p["date"]).replace(hour=12, tzinfo=timezone.utc), None, "purchase",
             p["item"], p["price"], p["category"], None, None) for p in profile.get("purchases", [])]
    rows += [(datetime.fromisoformat(a["time"]).astimezone(timezone.utc), None, "verdict", a["item"], a["price"],
              None, a["verdict"], json.dumps({"rule": a.get("rule"), "need": a.get("need")}))
             for a in profile.get("arguments", [])]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO events VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows)
    log(f"Copied {len(rows)} past purchases and arguments in")


def _writer(profile: dict | None):
    """Batches rows once a second. Reconnects after a failure; rows that pile up meanwhile are kept (up to the
    queue size), and a batch that fails to insert is dropped rather than retried forever."""
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
                conn.execute(SCHEMA)
                if profile is not None:
                    _backfill(conn, profile)
                    profile = None
                state["connected"], state["error"] = True, None
                _ready.set()
                log("Connected")
            time.sleep(1.0)
            batch = {"event": [], "gaze": []}
            while True:
                try:
                    table, row = _queue.get_nowait()
                except queue.Empty:
                    break
                batch[table].append(row)
            if not batch["event"] and not batch["gaze"]:
                continue
            with conn.transaction(), conn.cursor() as cur:
                if batch["event"]:
                    cur.executemany("INSERT INTO events VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", batch["event"])
                if batch["gaze"]:
                    with cur.copy("COPY gaze (ts, session_id, present, looking, recording, yaw, pitch, eye_turn)"
                                  " FROM STDIN") as copy:
                        for row in batch["gaze"]:
                            copy.write_row(row)
            state["written"] += len(batch["event"]) + len(batch["gaze"])
        except Exception as e:
            if state["connected"] or state["error"] != repr(e):
                log("Database error, retrying in 5 s:", repr(e))
            state["connected"], state["error"] = False, repr(e)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(5)


def _query(sql: str, args: tuple = ()) -> list[dict]:
    global _reader
    with _read_lock:
        for attempt in (1, 2):
            try:
                if _reader is None or _reader.closed:
                    _reader = connect()
                with _reader.cursor() as cur:
                    cur.execute(sql, args)
                    cols = [c.name for c in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:
                _reader = None
                if attempt == 2:
                    raise


def num(v, digits=2):
    return None if v is None else round(float(v), digits)


def trends() -> dict | None:
    """Everything the dashboard's history section shows. None without a database; raises if it's unreachable."""
    if not state["enabled"] or not _ready.is_set():
        return None
    arguments = _query("""
        SELECT v.ts, v.item, v.price, v.verdict, v.detail->>'rule' AS rule, v.detail->>'need' AS need,
               sum(e.samples) AS samples, sum(e.looking_samples) AS looking_samples
        FROM events v LEFT JOIN eye_contact_by_minute e ON e.session_id = v.session_id
        WHERE v.kind = 'verdict' AND v.session_id IS NOT NULL
        GROUP BY v.ts, v.item, v.price, v.verdict, rule, need
        ORDER BY v.ts DESC LIMIT 12""")
    days = _query("""
        SELECT day, spent, saved, released, timers, escalated FROM money_by_day
        WHERE day > now() - INTERVAL '30 days' ORDER BY day""")
    by_hour = _query("""
        SELECT extract(hour FROM ts AT TIME ZONE %s)::int AS hour, count(*) AS checkouts,
               count(*) FILTER (WHERE verdict <> 'release') AS stopped
        FROM events WHERE kind = 'verdict' GROUP BY hour ORDER BY hour""", (TZ,))
    totals = _query("""
        SELECT (SELECT count(*) FROM gaze) AS gaze_rows, (SELECT count(*) FROM events) AS events,
               (SELECT avg(looking::int) FROM gaze WHERE recording) AS eye_contact,
               (SELECT pg_size_pretty(hypertable_size('gaze'))) AS gaze_size""")[0]
    compression = _query("""
        SELECT before_compression_total_bytes AS before, after_compression_total_bytes AS after
        FROM hypertable_compression_stats('gaze')""")
    c = compression[0] if compression else {}
    return {
        "arguments": [{"time": a["ts"].isoformat(), "item": a["item"], "price": num(a["price"]),
                       "verdict": a["verdict"], "rule": a["rule"], "need": a["need"],
                       "eye_contact": num(a["looking_samples"] / a["samples"], 3) if a["samples"] else None,
                       "seconds_answering": num((a["samples"] or 0) / 10, 1)}
                      for a in arguments],
        "days": [{"day": d["day"].astimezone(ZoneInfo(TZ)).date().isoformat(), "spent": num(d["spent"]) or 0, "saved": num(d["saved"]) or 0,
                  "released": d["released"], "timers": d["timers"], "escalated": d["escalated"]} for d in days],
        "by_hour": [{"hour": h["hour"], "checkouts": h["checkouts"], "stopped": h["stopped"]} for h in by_hour],
        "gaze_rows": totals["gaze_rows"], "events": totals["events"], "gaze_size": totals["gaze_size"],
        "eye_contact": num(totals["eye_contact"], 3),
        "compression": round(1 - c["after"] / c["before"], 3) if c.get("before") else None,
    }

