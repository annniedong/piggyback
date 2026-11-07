"""Pretend to be the Chrome extension: send a checkout to brain.py and answer its questions.

  python test_client.py                          # interactive, default item
  python test_client.py "Smart toaster" 400 kitchen
  python test_client.py --auto "Socks" 12        # canned answers, no typing
"""

import asyncio
import json
import sys
import time

import websockets

URL = "ws://localhost:8000/ws"
CANNED = ["I need it for work.", "No, I don't have one already."]


async def main(args):
    auto = "--auto" in args
    args = [a for a in args if a != "--auto"]
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
            if msg["type"] == "question":
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
    asyncio.run(main(sys.argv[1:]))
