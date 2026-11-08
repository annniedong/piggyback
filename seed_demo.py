"""Fill Piggyback with three weeks of believable history for the demo.

Writes profile.json (goals, purchases, waiting room, past arguments), decisions.jsonl, and, when
TIGER_DB_URL is set, the same history in Tiger Data, with a gaze time series for every past argument.
The old profile.json and decisions.jsonl are copied to *.backup-<time> first. Tiger Data's tables are
emptied and refilled. Safe to run while the brain is running (it re-reads profile.json every request).

    python seed_demo.py          # asks before replacing anything
    python seed_demo.py --yes

Numbers are picked so the demo tiers work: $231 left of $600, so $40 is easy, $150 is close, $300 is over.
"""

import json
import random
import shutil
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sales import load_env_file

load_env_file()

import store  # noqa: E402  (after .env, so PROFILE_FILE from .env is used)
import tiger  # noqa: E402

TODAY = date.today()


def at(days_ago: int, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()).replace(hour=h, minute=m)


# (days ago, time, item, price, category, verdict, rule, need, eye contact while answering, what they said)
ARGUMENTS = [
    (20, "23:40", "Limited sneakers", 240, "clothing", "timer", "half_budget", "want", 0.48,
     ["They're limited edition, they won't restock.", "I'd wear them every day, probably."]),
    (18, "14:10", "Phone charger", 22, "electronics", "release", None, "need", 0.94,
     ["Mine frayed and my laptop died in class.", "I have a midterm on it Friday."]),
    (16, "00:50", "Mechanical keyboard", 165, "electronics", "timer", "half_budget", "want", 0.52,
     ["My old one works, but this one sounds so good.", "Okay, I don't need it need it."]),
    (14, "19:30", "Wool socks", 12, "clothing", "release", None, "need", 0.9,
     ["Every pair I own has a hole in it.", "It's twelve dollars, come on."]),
    (12, "23:15", "Another grey hoodie", 58, "clothing", "timer", None, "want", 0.41,
     ["It's a different shade of grey.", "Fine, I have four."]),
    (10, "13:05", "Lab goggles", 16, "school", "release", None, "need", 0.97,
     ["They're required for chem lab on Thursday.", "They won't let me in without them."]),
    (8, "01:20", "Noise-cancelling headphones", 279, "electronics", "escalate", "over_budget", "want", 0.38,
     ["Everyone in the library has them.", "The library is loud, okay?"]),
    (6, "22:45", "Air fryer", 129, "kitchen", "timer", "half_budget", "want", 0.63,
     ["I'd cook more, I swear.", "My roommate might split it."]),
    (4, "23:55", "Noise-cancelling headphones", 279, "electronics", "escalate", "over_budget", "want", 0.45,
     ["But they're on sale now!", "Thirty percent off is basically free."]),
    (3, "15:20", "Birthday card", 14, "gifts", "release", None, "want", 0.88,
     ["It's for my mom's birthday on Sunday.", "Yes, I'll write something nice in it."]),
    (1, "21:06", "Dior pants", 1200, "clothing", "escalate", "over_budget", "want", 0.33,
     ["They are really nice pants.", "I really want Dior pants."]),
]

# Bought this month: $369, so $231 is left of $600. The released arguments above are in here.
PURCHASES = [
    (19, "Groceries", 118, "food"), (17, "Phone charger", 22, "electronics"), (15, "Coffee subscription", 24, "food"),
    (14, "Wool socks", 12, "clothing"), (11, "Concert ticket", 85, "fun"), (10, "Lab goggles", 16, "school"),
    (7, "Takeout", 37, "food"), (5, "Takeout", 41, "food"), (3, "Birthday card", 14, "gifts"),
]

# Things that went to the waiting room: (item, status, days ago it was let go or last wanted)
WAITING = {
    "Limited sneakers": ("let_go", 17), "Mechanical keyboard": ("let_go", 13), "Another grey hoodie": ("let_go", 9),
    "Noise-cancelling headphones": ("waiting", 4), "Air fryer": ("still_want", 2), "Dior pants": ("waiting", 1),
}


def build_profile() -> dict:
    p = {"monthly_budget": 600, "purchases": [], "waiting": [], "arguments": [],
         "goals": [{"id": store.new_id(), "name": "Spring break trip", "target": 800, "saved": 540,
                    "deadline": f"{TODAY.year + 1}-03-01"},
                   {"id": store.new_id(), "name": "New laptop", "target": 1200, "saved": 385, "deadline": None}]}
    for days, item, price, category in PURCHASES:
        p["purchases"].append({"item": item, "price": price, "category": category,
                               "date": str(TODAY - timedelta(days=days))})
    for days, hhmm, item, price, category, verdict, rule, need, _, said in ARGUMENTS:
        when = at(days, hhmm).isoformat(timespec="seconds")
        p["arguments"].append({"time": when, "item": item, "price": price, "verdict": verdict, "rule": rule,
                               "need": need, "said": said})
        if item in WAITING:
            w = store.find_waiting(p, item)
            if w:
                w["tries"] += 1
                w["last_wanted"] = when
            else:
                p["waiting"].append({"id": store.new_id(), "item": item, "price": price, "category": category,
                                     "tries": 1, "first_wanted": when, "last_wanted": when, "status": "waiting"})
            store.find_waiting(p, item)["last_verdict"] = verdict
    for w in p["waiting"]:
        status, days = WAITING[w["item"]]
        w["status"] = status
        stamp = at(days, "20:00").isoformat(timespec="seconds")
        if status == "let_go":
            w["let_go_at"] = stamp
        elif status == "still_want":
            w["last_wanted"] = stamp
    return p


def decisions() -> list[dict]:
    return [{"type": "decision", "item": item, "outcome": "completed" if verdict == "release" else "abandoned",
             "seconds_on_page": random.randint(40, 300), "time": (at(days, hhmm) + timedelta(minutes=2)).isoformat(),
             "verdict": verdict}
            for days, hhmm, item, price, category, verdict, *_ in ARGUMENTS]


def gaze_rows(session_id: str, start: datetime, eye_contact: float, answers: int):
    """10 rows a second through a whole argument: Piggyback asks (not recording), the user answers (recording).
    While answering they look at the pig in long stretches with short glances away, about eye_contact of the time."""
    t = start
    for _ in range(answers):
        for _ in range(random.randint(25, 40)):  # Piggyback asking, 2.5-4 s
            yield (t, session_id, True, random.random() < 0.6, False, 0.1, 0.5, 0.1)
            t += timedelta(seconds=0.1)
        rows, looking = random.randint(45, 90), True
        for _ in range(rows):
            if random.random() < (0.08 if looking else 0.08 * eye_contact / max(1 - eye_contact, 0.05)):
                looking = not looking
            yaw = random.gauss(0.04, 0.03) if looking else random.choice([-1, 1]) * random.uniform(0.2, 0.5)
            yield (t, session_id, True, looking, True, round(yaw, 3), round(random.gauss(0.5, 0.05), 3), 0.1)
            t += timedelta(seconds=0.1)


def fill_tiger(p: dict):
    tz = ZoneInfo(tiger.TZ)
    conn = tiger.connect()
    conn.execute(tiger.SCHEMA)
    conn.execute("TRUNCATE gaze, events")
    events, gaze = [], []
    for x in p["purchases"]:
        events.append((datetime.fromisoformat(x["date"] + "T18:30").replace(tzinfo=tz), None, "purchase", x["item"],
                       x["price"], x["category"], None, None))
    for days, hhmm, item, price, category, verdict, rule, need, eye, said in ARGUMENTS:
        sid, start = store.new_id(), at(days, hhmm).replace(tzinfo=tz)
        tier = "over" if rule == "over_budget" else "close" if rule == "half_budget" else "easy"
        answers = 2 if tier == "easy" else 3
        rows = list(gaze_rows(sid, start + timedelta(seconds=2), eye, answers))
        # Pin each argument's answering eye contact to its listed number, so the dashboard matches the story.
        answering = [i for i, r in enumerate(rows) if r[4]]
        want = round(eye * len(answering))
        looking = [i for i in answering if rows[i][3]]
        flip = (looking[want:] if len(looking) > want else
                [i for i in answering if not rows[i][3]][:want - len(looking)])
        for i in flip:
            r = rows[i]
            rows[i] = (r[0], r[1], r[2], not r[3], r[4], r[5], r[6], r[7])
        gaze += rows
        end = rows[-1][0]
        detail = json.dumps({"rule": rule, "need": need, "tier": tier, "clean": eye > 0.85})
        events += [(start, sid, "checkout", item, price, category, None, json.dumps({"tier": tier})),
                   (end, sid, "verdict", item, price, category, verdict, detail),
                   (end + timedelta(minutes=2), sid, "decision", item, price, category, verdict,
                    json.dumps({"outcome": "completed" if verdict == "release" else "abandoned"}))]
    for w in p["waiting"]:
        if w["status"] == "let_go":
            events.append((datetime.fromisoformat(w["let_go_at"]).replace(tzinfo=tz), None, "let_go", w["item"],
                           w["price"], w["category"], None, None))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO events VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", events)
        with cur.copy("COPY gaze (ts, session_id, present, looking, recording, yaw, pitch, eye_turn) FROM STDIN") as copy:
            for row in gaze:
                copy.write_row(row)
    conn.execute("CALL refresh_continuous_aggregate('money_by_day', NULL, NULL)")
    conn.execute("CALL refresh_continuous_aggregate('eye_contact_by_minute', NULL, NULL)")
    # Compress the old gaze chunks now instead of waiting for the daily policy, so the dashboard shows it.
    conn.execute("SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('gaze', older_than => INTERVAL '1 day') c")
    print(f"Tiger Data: {len(events)} events, {len(gaze):,} gaze rows")


def main():
    if "--yes" not in sys.argv:
        answer = input("Replace profile.json, decisions.jsonl and the Tiger Data history with demo data? [y/N] ")
        if answer.strip().lower() != "y":
            return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    decisions_file = store.HERE / "decisions.jsonl"
    for path in (store.PROFILE_FILE, decisions_file):
        if path.exists():
            backup = path.with_name(f"{path.name}.backup-{stamp}")
            shutil.copy(path, backup)
            print("Backed up", path.name, "to", backup.name)
    random.seed(7)  # same demo every time
    p = build_profile()
    store.backend().write(p)
    decisions_file.write_text("".join(json.dumps(d) + "\n" for d in decisions()))
    s = store.summary(p)
    print(f"Profile: ${s['remaining']:.0f} left of ${s['monthly_budget']}, ${s['saved_by_letting_go']:.0f} saved "
          f"by letting go, {s['arguments_won']}/{s['arguments_total']} arguments won, {len(s['waiting'])} waiting")
    if tiger.url():
        fill_tiger(p)
    else:
        print("TIGER_DB_URL not set: skipped the Tiger Data history")


if __name__ == "__main__":
    main()
