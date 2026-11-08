"""The user's profile: budget, purchases, savings goals, the waiting room, and past arguments.

STORE=json (default) keeps it in profile.json next to brain.py. STORE=firestore keeps it in one
Firestore document (profiles/<PROFILE_ID>) so the API can run on Cloud Run. Same functions either way:

    with edit() as p:      # load, change, save
        add_goal(p, "Spring break", 800)
    p = load()             # read only
"""

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

STORE = os.getenv("STORE", "json")  # json | firestore
PROFILE_ID = os.getenv("PROFILE_ID", "default")
MONTHLY_BUDGET = 600
HERE = Path(__file__).parent
PROFILE_FILE = Path(os.getenv("PROFILE_FILE", HERE / "profile.json"))  # tests point this elsewhere
OLD_BUDGET_FILE = HERE / "budget.json"  # before profiles existed: just budget + purchases
MAX_ARGUMENTS = 50

_lock = threading.Lock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def seed() -> dict:
    """A fake month of spending and one goal, so the demo has history to judge against."""
    today = date.today()
    spending = [("Coffee subscription", 24, "food"), ("Groceries", 142, "food"), ("Headphones case", 18, "electronics"),
                ("Concert ticket", 85, "fun"), ("Takeout", 37, "food"), ("Phone charger", 22, "electronics"),
                ("Takeout", 41, "food")]
    purchases = [{"item": i, "price": p, "category": c, "date": str(today - timedelta(days=3 * n + 1))}
                 for n, (i, p, c) in enumerate(spending)]
    return {"monthly_budget": MONTHLY_BUDGET, "purchases": purchases,
            "goals": [{"id": new_id(), "name": "Spring break trip", "target": 800, "saved": 420,
                       "deadline": f"{today.year + 1}-03-01"}],
            "waiting": [], "arguments": []}


def upgrade(p: dict) -> dict:
    """Fill in fields missing from older files."""
    p.setdefault("monthly_budget", MONTHLY_BUDGET)
    for key in ("purchases", "goals", "waiting", "arguments"):
        p.setdefault(key, [])
    return p


# ---------------------------------------------------------------- backends

class JsonBackend:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict | None:
        if self.path.exists():
            return json.loads(self.path.read_text())
        if OLD_BUDGET_FILE.exists():  # keep the spending history from budget.json
            return json.loads(OLD_BUDGET_FILE.read_text())
        return None

    def write(self, p: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, indent=2))
        tmp.replace(self.path)  # a crash mid-write can't leave half a file


class FirestoreBackend:
    """One document per user. Needs `pip install google-cloud-firestore` and the same ADC sign-in as Gemini."""

    def __init__(self, doc_id: str):
        from google.cloud import firestore
        self.doc = firestore.Client().collection("profiles").document(doc_id)

    def read(self) -> dict | None:
        snap = self.doc.get()
        return snap.to_dict() if snap.exists else None

    def write(self, p: dict):
        self.doc.set(p)


_backend = None


def backend():
    global _backend
    if _backend is None:
        _backend = FirestoreBackend(PROFILE_ID) if STORE == "firestore" else JsonBackend(PROFILE_FILE)
    return _backend


def load() -> dict:
    with _lock:
        p = backend().read()
        if p is None:
            p = seed()
            backend().write(p)
        return upgrade(p)


@contextmanager
def edit():
    """Load the profile, let the caller change it, then save it. Not saved if the caller raises."""
    with _lock:
        p = upgrade(backend().read() or seed())
        yield p
        backend().write(p)


# ---------------------------------------------------------------- reading

def spent(p: dict) -> float:
    return sum(x["price"] for x in p["purchases"])


def remaining(p: dict) -> float:
    return p["monthly_budget"] - spent(p)


def same_item(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def find_waiting(p: dict, item: str) -> dict | None:
    return next((w for w in p["waiting"] if same_item(w["item"], item)), None)


def saved_by_letting_go(p: dict) -> float:
    return sum(w["price"] for w in p["waiting"] if w["status"] == "let_go")


def summary(p: dict) -> dict:
    """What the dashboard shows. Every number is computed here, never by the AI."""
    args = p["arguments"]
    return {
        "monthly_budget": p["monthly_budget"], "spent": spent(p), "remaining": remaining(p),
        "saved_by_letting_go": saved_by_letting_go(p),
        "arguments_won": sum(a["verdict"] == "release" for a in args), "arguments_total": len(args),
        "goals": p["goals"],
        "waiting": [w for w in reversed(p["waiting"]) if w["status"] in ("waiting", "still_want")],
        "arguments": list(reversed(args[-10:])),
        "purchases": list(reversed(p["purchases"][-10:])),
    }


def days_ago(stamp: str) -> str:
    days = (date.today() - date.fromisoformat(stamp[:10])).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def context_for(p: dict, item: str, price: float) -> str:
    """Facts for the AI about this user beyond the budget: goals, past attempts, what they said before."""
    lines = []
    for g in p["goals"]:
        left = max(g["target"] - g["saved"], 0)
        when = f", due {g['deadline']}" if g.get("deadline") else ""
        lines.append(f"Saving for {g['name']}: ${g['saved']:.0f} of ${g['target']:.0f} (${left:.0f} to go{when}). "
                     f"This purchase is {price / left:.0%} of what's left to save." if left else
                     f"Saving for {g['name']}: already reached ${g['target']:.0f}.")
    w = find_waiting(p, item)
    if w:
        lines.append(f"This is attempt #{w['tries']} at buying this; first tried {days_ago(w['first_wanted'])}, "
                     f"last verdict: {w['last_verdict']}."
                     + (" They told the dashboard they still want it." if w["status"] == "still_want" else ""))
    past = [a for a in p["arguments"] if same_item(a["item"], item)][-2:]
    for a in past:
        said = " / ".join(a["said"]) or "(nothing)"
        lines.append(f"Last time ({days_ago(a['time'])}) they said: \"{said}\" → {a['verdict']}.")
    let_go = saved_by_letting_go(p)
    if let_go:
        lines.append(f"They've saved ${let_go:.0f} so far by letting things go.")
    return "\n".join(lines)


# ---------------------------------------------------------------- changing

def note_checkout(p: dict, item: str, price: float):
    """Each new checkout of an item already in the waiting room counts as another try."""
    w = find_waiting(p, item)
    if w and w["status"] in ("waiting", "still_want"):
        w["tries"] += 1
        w["price"] = price
        w["last_wanted"] = now()


def note_verdict(p: dict, item: str, price: float, category: str, verdict: str, rule, need: str, said: list):
    """Log the argument; a timer or escalate puts the item in the waiting room."""
    p["arguments"].append({"time": now(), "item": item, "price": price, "verdict": verdict, "rule": rule,
                           "need": need, "said": said})
    p["arguments"] = p["arguments"][-MAX_ARGUMENTS:]
    w = find_waiting(p, item)
    if verdict in ("timer", "escalate"):
        if w is None or w["status"] in ("let_go", "bought"):
            if w:
                p["waiting"].remove(w)
            w = {"id": new_id(), "item": item, "price": price, "category": category, "tries": 1,
                 "first_wanted": now(), "last_wanted": now(), "status": "waiting"}
            p["waiting"].append(w)
        w["last_verdict"] = verdict
    elif w:
        w["last_verdict"] = verdict


def note_purchase(p: dict, item: str, price: float, category: str):
    p["purchases"].append({"item": item, "price": price, "category": category, "date": str(date.today())})
    w = find_waiting(p, item)
    if w and w["status"] in ("waiting", "still_want"):
        w["status"] = "bought"


def get(items: list, item_id: str) -> dict:
    found = next((x for x in items if x["id"] == item_id), None)
    if found is None:
        raise KeyError(item_id)
    return found


def let_go(p: dict, waiting_id: str, goal_id: str | None = None):
    """Give up a waiting item. With a goal, its price moves into that goal's savings."""
    w = get(p["waiting"], waiting_id)
    w["status"], w["let_go_at"] = "let_go", now()
    if goal_id:
        get(p["goals"], goal_id)["saved"] += w["price"]


def still_want(p: dict, waiting_id: str):
    w = get(p["waiting"], waiting_id)
    w["status"], w["last_wanted"] = "still_want", now()


def add_goal(p: dict, name: str, target: float, deadline: str | None = None, saved: float = 0) -> dict:
    g = {"id": new_id(), "name": name, "target": target, "saved": saved, "deadline": deadline}
    p["goals"].append(g)
    return g


def save_toward(p: dict, goal_id: str, amount: float):
    g = get(p["goals"], goal_id)
    g["saved"] = max(g["saved"] + amount, 0)


def delete_goal(p: dict, goal_id: str):
    p["goals"].remove(get(p["goals"], goal_id))
