"""Integration tests: a real brain (uvicorn) on a spare port, driven the way the extension drives it.

    python -m unittest discover tests -v        # about a minute

Each brain runs with the mock AI, typed answers, no camera or mic, no ESP32, a silent `say`, and its
own profile and decisions files, so nothing real is touched. ESP32 commands are read from its log
("ESP32 (offline) > ..."). The Tiger Data tests use a throwaway schema in the TIGER_DB_URL database
(from the environment or .env) and are skipped without it; the demo's tables are never touched.
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
EASY, CLOSE, OVER = 20, 150, 300

# Every test starts from this profile: $369 spent of $600, one goal, nothing waiting.
PURCHASES = [("Coffee subscription", 24, "food"), ("Groceries", 142, "food"), ("Headphones case", 18, "electronics"),
             ("Concert ticket", 85, "fun"), ("Takeout", 37, "food"), ("Phone charger", 22, "electronics"),
             ("Takeout", 41, "food")]
REMAINING = 600 - sum(price for _, price, _ in PURCHASES)
PROFILE = {"monthly_budget": 600, "waiting": [], "arguments": [],
           "purchases": [{"item": i, "price": p, "category": c, "date": "2026-11-01"} for i, p, c in PURCHASES],
           "goals": [{"id": "goal1", "name": "Spring break trip", "target": 800, "saved": 420, "deadline": None}]}


def env_file_value(key: str) -> str:
    """One value from the git-ignored .env, without loading the rest into this process."""
    path = ROOT / ".env"
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        m = re.match(rf"^\s*(?:export\s+)?{key}\s*=\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip("'\"")
    return ""


TIGER_DB_URL = os.getenv("TIGER_DB_URL") or env_file_value("TIGER_DB_URL")


class Brain:
    """A brain.py server in a subprocess, with its log in a file."""

    def __init__(self, **env):
        self.dir = Path(tempfile.mkdtemp(prefix="piggyback-test-"))
        (self.dir / "bin").mkdir()
        say = self.dir / "bin" / "say"
        say.write_text("#!/bin/sh\nexit 0\n")  # Piggyback talks to nobody; `say -a ?` lists no outputs
        say.chmod(0o755)
        self.profile, self.decisions, self.log = self.dir / "profile.json", self.dir / "decisions.jsonl", self.dir / "brain.log"
        self.reset()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        full_env = {**os.environ, "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "PYTHONUNBUFFERED": "1",
                    "LLM_PROVIDER": "mock", "LISTEN": "0", "VISION": "0", "SCRAPE_AI": "never",
                    "SERIAL_PORT": "/dev/cu.piggyback-test-none", "PROFILE_FILE": str(self.profile),
                    "DECISIONS_FILE": str(self.decisions), "TIGER_DB_URL": "",
                    # The budget review's AI call fails fast, so the computed fallback is what's tested.
                    "REVIEW_PROVIDER": "claude", "ANTHROPIC_API_KEY": "not-a-real-key",
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", **env}
        self._log_file = self.log.open("w")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "brain:app", "--port", str(self.port)],
                                     cwd=ROOT, env=full_env, stdout=self._log_file, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while True:
            try:
                self.get("/health")
                return
            except (urllib.error.URLError, ConnectionError):
                if self.proc.poll() is not None or time.time() > deadline:
                    raise RuntimeError("brain didn't start:\n" + self.log.read_text()[-3000:])
                time.sleep(0.3)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)
        self._log_file.close()

    def reset(self):
        """The starting profile and no decisions."""
        self.profile.write_text(json.dumps(PROFILE))
        self.decisions.unlink(missing_ok=True)

    # ---- HTTP
    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get("content-type", "").startswith("application/json") else raw.decode()

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def status_of(self, method, path, body=None) -> int:
        try:
            self.request(method, path, body)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    # ---- log
    def log_size(self) -> int:
        return len(self.log.read_text())

    def wait_log(self, pattern: str, after: int = 0, timeout: float = 15) -> tuple[int, float]:
        """Wait for `pattern` (regex) in the log after offset `after`; returns (offset past it, when it was seen)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = re.compile(pattern).search(self.log.read_text(), after)
            if m:
                return m.end(), time.monotonic()
            time.sleep(0.05)
        raise AssertionError(f"{pattern!r} not in the brain's log within {timeout}s:\n" + self.log.read_text()[after:][-3000:])

    # ---- the extension's side of a checkout
    def argue(self, item: str, price: float, category: str = "other", decision: str | None = None,
              url: str | None = None, close_after: float = 0.3) -> list[dict]:
        return asyncio.run(self._argue(item, price, category, decision, url, close_after))

    async def _argue(self, item, price, category, decision, url, close_after):
        msgs = []
        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws") as ws:
            checkout = {"type": "checkout", "item": item, "price": price, "category": category}
            if url:
                checkout["url"] = url
            await ws.send(json.dumps(checkout))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                msgs.append(msg)
                if msg["type"] == "question":
                    await ws.send(json.dumps({"type": "answer", "text": "I just really want it."}))
                elif msg["type"] == "verdict":
                    break
            if decision:
                await ws.send(json.dumps({"type": "decision", "item": item, "price": price,
                                          "outcome": decision, "seconds_on_page": 30}))
            await asyncio.sleep(close_after)  # the extension closes ~0.3 s after the decision
        return msgs


def verdict_of(msgs: list[dict]) -> dict:
    return msgs[-1]


def questions_in(msgs: list[dict]) -> int:
    return sum(m["type"] == "question" for m in msgs)


class CheckoutTest(unittest.TestCase):
    """The checkout argument end to end, and the budget rules that typed answers can't get around."""

    @classmethod
    def setUpClass(cls):
        cls.brain = Brain()

    @classmethod
    def tearDownClass(cls):
        cls.brain.stop()

    def setUp(self):
        self.brain.reset()

    def test_health_and_pages(self):
        h = self.brain.get("/health")
        self.assertEqual((h["ok"], h["llm"], h["esp32"], h["remaining"]), (True, "mock", False, REMAINING))
        self.assertFalse(h["tiger"]["connected"])
        self.assertIn("Stare history", self.brain.get("/"))
        self.assertIn('data-piggyback="place-order"', self.brain.get("/store/"))
        self.assertEqual(self.brain.get("/trends"), {"enabled": False})

    def test_easy_purchase_two_questions_then_bought(self):
        msgs = self.brain.argue("Phone case", EASY, "electronics", decision="completed")
        self.assertEqual(questions_in(msgs), 2)
        v = verdict_of(msgs)
        self.assertIn(v["verdict"], ("release", "timer"))  # the mock AI picks either; no rule overrides it
        self.assertIsNone(v["rule"])
        self.assertTrue(v["line"])
        self.assertIs(v["demo"], False)  # the extension may place real orders
        p = self.brain.get("/profile")
        self.assertEqual(p["remaining"], REMAINING - EASY)
        self.assertEqual(p["purchases"][0]["item"], "Phone case")
        decision = json.loads(self.brain.decisions.read_text().splitlines()[-1])
        self.assertEqual((decision["item"], decision["outcome"], decision["verdict"]),
                         ("Phone case", "completed", v["verdict"]))

    def test_close_to_budget_typed_answers_never_release(self):
        msgs = self.brain.argue("Air fryer", CLOSE, "kitchen", decision="abandoned")
        self.assertEqual(questions_in(msgs), 3)
        v = verdict_of(msgs)
        self.assertEqual(v["verdict"], "timer")  # typed answers are never a clean argument
        self.assertEqual(v["seconds"], 60)
        self.assertIn(v["rule"], ("half_budget", None))  # None when the mock AI already said timer
        waiting = self.brain.get("/profile")["waiting"]
        self.assertEqual([(w["item"], w["status"]) for w in waiting], [("Air fryer", "waiting")])
        self.assertEqual(self.brain.get("/profile")["remaining"], REMAINING)  # put back: nothing spent

    def test_over_budget_escalates_and_retries_count(self):
        for attempt in (1, 2):
            v = verdict_of(self.brain.argue("Designer jacket", OVER, "clothing", decision="abandoned"))
            self.assertEqual((v["verdict"], v["rule"]), ("escalate", "over_budget"))
        p = self.brain.get("/profile")
        self.assertEqual(p["waiting"][0]["tries"], 2)
        self.assertEqual([a["verdict"] for a in p["arguments"][:2]], ["escalate", "escalate"])
        self.assertEqual((p["arguments_won"], p["arguments_total"]), (0, 2))

    def test_bought_anyway_finishes_the_disappointed_shake(self):
        """The extension disconnects ~0.3 s after the decision; idle must wait for the 4.6 s shake."""
        before = self.brain.log_size()
        self.brain.argue("Designer jacket", OVER, "clothing", decision="completed", close_after=0.3)
        pos, _ = self.brain.wait_log(r'"expr": "disappointed"\}\n.*You went for it anyway', before)
        shake_start = time.monotonic()
        self.brain.wait_log("Bot disconnected", pos)
        _, idle_at = self.brain.wait_log(r'"expr": "idle"', pos, timeout=10)
        self.assertGreaterEqual(idle_at - shake_start, 4.0, "idle cut the disappointed shake short")
        self.assertEqual(self.brain.get("/profile")["purchases"][0]["item"], "Designer jacket")

    def test_disconnect_mid_argument_then_next_checkout_works(self):
        async def leave_after_first_question():
            async with websockets.connect(f"ws://127.0.0.1:{self.brain.port}/ws") as ws:
                await ws.send(json.dumps({"type": "checkout", "item": "Lamp", "price": EASY}))
                first = json.loads(await asyncio.wait_for(ws.recv(), 20))
                self.assertEqual(first["type"], "question")
        asyncio.run(leave_after_first_question())
        v = verdict_of(self.brain.argue("Socks", 12, "clothing"))
        self.assertIn(v["verdict"], ("release", "timer"))
        self.assertTrue(self.brain.get("/health")["ok"])

    def test_typed_answer_without_a_checkout_is_ignored(self):
        async def stray_answer():
            async with websockets.connect(f"ws://127.0.0.1:{self.brain.port}/ws") as ws:
                await ws.send(json.dumps({"type": "answer", "text": "hello?"}))
                await asyncio.sleep(0.3)
        before = self.brain.log_size()
        asyncio.run(stray_answer())
        self.brain.wait_log("Answer with no open session, ignoring", before)


class ApiTest(unittest.TestCase):
    """The HTTP side: add to bag, browsing signals, dashboard profile API, budget review."""

    @classmethod
    def setUpClass(cls):
        cls.brain = Brain()

    @classmethod
    def tearDownClass(cls):
        cls.brain.stop()

    def setUp(self):
        self.brain.reset()

    def test_added_to_bag_speaks_once_then_waits(self):
        before = self.brain.log_size()
        first = self.brain.post("/added", {"website": "https://shop.example/p/1", "item": "Wool pants", "price": 120})
        self.assertTrue(first["line"])
        self.brain.wait_log(r"Added to bag: .*Wool pants", before)
        self.brain.wait_log(re.escape(f"Piggyback says: {first['line']}"), before)
        again = self.brain.post("/added", {"website": "https://shop.example/p/2", "item": "Belt", "price": 30})
        self.assertIsNone(again["line"])  # within 5 s of the last remark: no double talk
        self.assertEqual(self.brain.status_of("POST", "/added", {"website": "not a url"}), 422)

    def test_browsing_signals_are_silent(self):
        before = self.brain.log_size()
        ok = self.brain.post("/alert", {"type": "ad_referral", "website": "https://shop.example/p/1", "detail": "a Google ad"})
        self.assertEqual(ok, {"ok": True})
        self.brain.wait_log("Browser signal: .*ad_referral", before)
        time.sleep(0.3)
        self.assertNotIn("Piggyback says", self.brain.log.read_text()[before:])
        self.assertEqual(self.brain.status_of("POST", "/alert", {"type": "nonsense", "website": "https://x.example"}), 422)

    def test_goals_budget_and_waiting_room(self):
        p = self.brain.get("/profile")
        goal = p["goals"][0]
        self.assertEqual(goal["name"], "Spring break trip")

        p = self.brain.request("PUT", "/budget", {"monthly_budget": 700})
        self.assertEqual((p["monthly_budget"], p["remaining"]), (700, REMAINING + 100))
        self.assertEqual(self.brain.status_of("PUT", "/budget", {"monthly_budget": 0}), 400)
        self.brain.request("PUT", "/budget", {"monthly_budget": 600})  # back, so the tiers below hold

        p = self.brain.post("/goals", {"name": "New laptop", "target": 1200})
        laptop = next(g for g in p["goals"] if g["name"] == "New laptop")
        p = self.brain.post(f"/goals/{laptop['id']}/save", {"amount": 50})
        self.assertEqual(next(g for g in p["goals"] if g["id"] == laptop["id"])["saved"], 50)
        p = self.brain.request("DELETE", f"/goals/{laptop['id']}")
        self.assertNotIn(laptop["id"], [g["id"] for g in p["goals"]])
        self.assertEqual(self.brain.status_of("POST", "/goals/nope/save", {"amount": 5}), 404)

        # Two items go to the waiting room; one is let go into the goal, the other kept.
        self.brain.argue("Air fryer", CLOSE, "kitchen", decision="abandoned")
        self.brain.argue("Designer jacket", OVER, "clothing", decision="abandoned")
        waiting = {w["item"]: w for w in self.brain.get("/profile")["waiting"]}
        p = self.brain.post(f"/waiting/{waiting['Designer jacket']['id']}/let_go", {"goal_id": goal["id"]})
        self.assertEqual(p["saved_by_letting_go"], OVER)
        self.assertEqual(next(g for g in p["goals"] if g["id"] == goal["id"])["saved"], goal["saved"] + OVER)
        p = self.brain.post(f"/waiting/{waiting['Air fryer']['id']}/still_want")
        self.assertEqual([(w["item"], w["status"]) for w in p["waiting"]], [("Air fryer", "still_want")])
        self.assertEqual(self.brain.status_of("POST", "/waiting/nope/still_want"), 404)
        self.assertEqual(self.brain.status_of("POST", "/demo/reset"), 403)  # only in demo mode

    def test_budget_review_numbers_without_ai(self):
        self.brain.argue("Phone case", EASY, "electronics", decision="completed")
        r = self.brain.get("/review")
        p = self.brain.get("/profile")
        self.assertEqual((r["spent"], r["remaining"]), (p["spent"], p["remaining"]))
        self.assertEqual(r["checkouts_completed"], 1)
        self.assertIn("food", r["by_category"])
        self.assertTrue(r["error"])  # the AI is unreachable here, so the summary is the computed one
        self.assertIn(f"${p['remaining']:.0f} left", r["summary"])


class DemoModeTest(unittest.TestCase):
    """The judging table: verdicts tell the extension it's a demo, and the profile resets for each visitor."""

    @classmethod
    def setUpClass(cls):
        cls.brain = Brain(DEMO_MODE="1", DEMO_RESET_MINUTES="0.05")  # 3 s idle resets

    @classmethod
    def tearDownClass(cls):
        cls.brain.stop()

    def assert_fresh_demo(self, p):
        self.assertEqual((p["remaining"], p["saved_by_letting_go"], p["arguments_total"]), (231, 463, 11))
        self.assertEqual({g["name"] for g in p["goals"]}, {"Spring break trip", "New laptop"})

    def test_reset_button_and_demo_flag(self):
        self.assertTrue(self.brain.get("/health")["demo"])
        v = verdict_of(self.brain.argue("Designer jacket", OVER, "clothing", decision="completed"))
        self.assertIs(v["demo"], True)
        self.assert_fresh_demo(self.brain.post("/demo/reset"))
        decisions = self.brain.decisions.read_text().splitlines()
        self.assertEqual(len(decisions), 11)
        self.assertNotIn("Designer jacket", "".join(decisions))

    def test_resets_by_itself_after_idle(self):
        self.brain.post("/demo/reset")
        before = self.brain.log_size()
        self.brain.argue("Phone case", EASY, "electronics", decision="completed")
        self.assertEqual(self.brain.get("/profile")["remaining"], 231 - EASY)
        self.brain.wait_log("Demo: reset for the next visitor", before, timeout=20)
        self.assert_fresh_demo(self.brain.get("/profile"))


@unittest.skipUnless(TIGER_DB_URL, "TIGER_DB_URL not set (environment or .env)")
class TigerDataTest(unittest.TestCase):
    """History in Tiger Data, in a throwaway schema that's dropped afterwards."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.schema = f"piggyback_test_{uuid.uuid4().hex[:8]}"
        cls.admin = psycopg.connect(TIGER_DB_URL, autocommit=True)
        cls.admin.execute(f"CREATE SCHEMA {cls.schema}")
        sep = "&" if "?" in TIGER_DB_URL else "?"
        cls.url = f"{TIGER_DB_URL}{sep}options=-csearch_path%3D{cls.schema},public"
        cls.brain = Brain(TIGER_DB_URL=cls.url)
        deadline = time.time() + 30
        while not cls.brain.get("/health")["tiger"]["connected"]:
            if time.time() > deadline:
                raise RuntimeError("brain didn't connect to Tiger Data:\n" + cls.brain.log.read_text()[-2000:])
            time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.brain.stop()
        cls.admin.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.admin.close()

    def query(self, sql, args=()):
        import psycopg

        with psycopg.connect(self.url, autocommit=True) as c:
            return c.execute(sql, args).fetchall()

    def wait_for(self, check, timeout=15):
        deadline = time.time() + timeout
        while True:
            result = check()
            if result or time.time() > deadline:
                return result
            time.sleep(0.5)

    def test_argument_lands_in_history_with_eye_contact(self):
        # The first connect copied the seeded purchases in.
        self.assertEqual(self.query("SELECT count(*) FROM events WHERE kind = 'purchase'")[0][0], 7)

        self.brain.argue("Tiger jacket", OVER, "clothing", decision="completed")
        kinds = self.wait_for(lambda: {k for (k,) in self.query(
            "SELECT kind FROM events WHERE item = 'Tiger jacket'")} >= {"checkout", "verdict", "decision", "purchase"})
        self.assertTrue(kinds, "checkout, verdict, decision and purchase events weren't all written")
        (session_id, verdict, detail), = self.query(
            "SELECT session_id, verdict, detail FROM events WHERE item = 'Tiger jacket' AND kind = 'verdict'")
        self.assertEqual((verdict, detail["rule"], detail["tier"], detail["clean"]), ("escalate", "over_budget", "over", False))

        # No camera in tests: feed 5 s of answering gaze, 80% looking, the way the brain's tracking loop does.
        import psycopg
        from datetime import datetime, timedelta, timezone
        t0 = datetime.now(timezone.utc)
        rows = [(t0 + timedelta(seconds=i / 10), session_id, True, i % 5 != 0, True, 0.05, 0.5, 0.1) for i in range(50)]
        with psycopg.connect(self.url, autocommit=True) as c, c.cursor() as cur:
            with cur.copy("COPY gaze (ts, session_id, present, looking, recording, yaw, pitch, eye_turn) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)

        t = self.brain.get("/trends")
        self.assertTrue(t["enabled"])
        arg = next(a for a in t["arguments"] if a["item"] == "Tiger jacket")
        self.assertEqual((arg["verdict"], arg["eye_contact"], arg["seconds_answering"]), ("escalate", 0.8, 5.0))
        self.assertEqual(t["gaze_rows"], 50)
        today = max(t["days"], key=lambda d: d["day"])
        self.assertEqual((today["spent"], today["escalated"]), (OVER, 1))

    def test_gaze_writer_batches_rows(self):
        """tiger.gaze() as the tracking loop calls it, through the background writer."""
        import tiger

        os.environ["TIGER_DB_URL"] = self.url
        try:
            tiger.start()
            sid = "gazetest"
            for i in range(30):
                tiger.gaze(sid, {"present": True, "looking": i < 15, "yaw": 0.1, "pitch": 0.5, "eye_turn": 0.1}, True)
            tiger.gaze(sid, {"present": False}, False)  # nobody there: looking is false, numbers empty
            written = self.wait_for(lambda: self.query("SELECT count(*), count(*) FILTER (WHERE looking), "
                                                       "count(yaw) FROM gaze WHERE session_id = %s", (sid,))[0][0] == 31)
            self.assertTrue(written, "gaze rows weren't written")
            self.assertEqual(self.query("SELECT count(*) FILTER (WHERE looking), count(yaw) FROM gaze "
                                        "WHERE session_id = %s", (sid,))[0], (15, 30))
        finally:
            os.environ.pop("TIGER_DB_URL", None)


class TigerDownTest(unittest.TestCase):
    """An unreachable database must never get in the pig's way."""

    def test_checkout_works_without_the_database(self):
        brain = Brain(TIGER_DB_URL="postgresql://nobody:nothing@127.0.0.1:1/none")
        try:
            v = verdict_of(brain.argue("Socks", 12, "clothing", decision="completed"))
            self.assertIn(v["verdict"], ("release", "timer"))
            h = brain.get("/health")
            self.assertFalse(h["tiger"]["connected"])
            self.assertTrue(h["tiger"]["error"])
            self.assertEqual(brain.get("/trends"), {"enabled": False})
            self.assertIn("Stare history", brain.get("/"))  # the card just stays hidden
        finally:
            brain.stop()


if __name__ == "__main__":
    unittest.main()
