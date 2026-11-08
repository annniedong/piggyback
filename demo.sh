#!/bin/bash
# Judging table: start Piggyback in demo mode and keep it running.
#
#   ./demo.sh               fresh demo data (profile, decisions, Tiger Data history), then the brain
#   ./demo.sh --keep        keep today's history (after a restart mid-judging)
#
# Demo mode: the extension never places a real order (real stores' "Place order" / "Pay now" are
# blocked even after Piggyback says yes), the profile goes back to the demo for each visitor (the
# dashboard's "Next visitor" button, or by itself after 10 idle minutes), and the cool-off timer is
# 20 s so visitors can see it end. If the brain crashes, it restarts. Ctrl+C stops everything.
# Settings in .env still apply; these defaults only fill in what's not set.

cd "$(dirname "$0")" || exit 1
source .venv/bin/activate || { echo "No .venv here: see Run in CLAUDE.md"; exit 1; }

export DEMO_MODE=1
export TIMER_SECONDS="${TIMER_SECONDS:-20}"
export DEMO_RESET_MINUTES="${DEMO_RESET_MINUTES:-10}"

if [ "$1" != "--keep" ]; then
  python seed_demo.py --yes || exit 1
fi

trap 'echo; echo "Piggyback stopped."; exit 0' INT TERM
while true; do
  uvicorn brain:app --port 8000
  echo "Brain exited ($?). Restarting in 2 s; Ctrl+C to stop."
  sleep 2
done
