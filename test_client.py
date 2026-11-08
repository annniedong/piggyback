"""Pretend to be the page-reading bot: send a checkout to brain.py and answer its questions.

  python test_client.py                          # interactive, default item
  python test_client.py "Smart toaster" 400 kitchen
  python test_client.py --auto "Socks" 12        # canned answers, no typing
  python test_client.py --voice "Socks" 12       # answer out loud; the brain hears you (LISTEN=1)
"""

import asyncio
import json
import os
import sys
import time

import websockets

URL = os.getenv("BRAIN_URL", "ws://localhost:8000/ws")
CANNED = ["I need it for work.", "No, I don't have one already."]


async def main(args):
    auto = "--auto" in args
    voice = "--voice" in args
    args = [a for a in args if a not in ("--auto", "--voice")]
    item = args[0] if len(args) > 0 else "Smart toaster"
    price = float(args[1]) if len(args) > 1 else 400
    category = args[2] if len(args) > 2 else "other"

    async with websockets.connect(URL) as ws:
        start = time.time()
        await ws.send(json.dumps({"type": "checkout", "item": item, "price": price, "category": category}))
        print(f"> checkout {item} ${price:g}")
        answers = iter(CANNED)
        while True:
            msg = json.loads(await ws.recv())
            print(f"< [{time.time() - start:4.1f}s] {msg}")
            if msg["type"] == "question" and voice:
                print("  (answer out loud)")
            elif msg["type"] == "question":
                text = next(answers, "Yes.") if auto else input("your answer> ")
                await ws.send(json.dumps({"type": "answer", "text": text}))
                print(f"> answer {text!r}")
            elif msg["type"] == "verdict":
                outcome = "completed" if msg["verdict"] == "release" else "abandoned"
                await ws.send(json.dumps({"type": "decision", "item": item, "outcome": outcome,
                                          "seconds_on_page": round(time.time() - start)}))
                print(f"> decision {outcome}")
                await asyncio.sleep(0.3)
                return


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except OSError:
        sys.exit(f"Couldn't reach the brain at {URL}. Start it first in another Terminal tab:\n"
                 "  uvicorn brain:app --port 8000")
