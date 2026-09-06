"""
Phase 9 — Concurrency stress test.
Fires truly simultaneous booking requests at limited inventory and
verifies: winners = seats available, losers get 409, count lands at 0.
Run with the API up:  python concurrency_test.py
"""
import threading
import requests
import uuid

BASE = "http://127.0.0.1:8000"
FLIGHT_ID = 3
results = []
lock = threading.Lock()


def book(seat_class: str, passenger_id: int):
    key = f"stress-{uuid.uuid4()}"          # unique key per attempt (real clients)
    r = requests.post(f"{BASE}/bookings", json={
        "flight_id": FLIGHT_ID,
        "passenger_id": passenger_id,
        "seat_class": seat_class,
        "fare": "basic_economy",
        "idempotency_key": key,
    })
    with lock:
        results.append((r.status_code, r.json()))


def run_wave(n_threads: int, seat_class: str):
    """Launch n truly simultaneous booking attempts."""
    results.clear()
    threads = [threading.Thread(target=book, args=(seat_class, (i % 3) + 1))
               for i in range(n_threads)]
    # start all at once
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results if r[0] == 201]
    fails = [r for r in results if r[0] == 409]
    other = [r for r in results if r[0] not in (201, 409)]
    return wins, fails, other


def seat_count(seat_class: str) -> int:
    """Ask the DB via a throwaway admin read — simplest: hit health + query manually.
    We keep it simple: caller checks Supabase; here we just report responses."""
    return -1


if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: Two requests race for the LAST seat")
    print("  (set first class to available_seats = 1 before running)")
    print("=" * 60)
    wins, fails, other = run_wave(2, "first")
    print(f"  201 Created : {len(wins)}   (must be exactly 1)")
    print(f"  409 Conflict: {len(fails)}  (must be exactly 1)")
    if other:
        print(f"  UNEXPECTED  : {other}")
    verdict1 = len(wins) == 1 and len(fails) == 1 and not other
    print(f"  VERDICT: {'PASS - no oversell' if verdict1 else 'FAIL'}")

    print()
    print("=" * 60)
    print("TEST 2: Ten requests race for THREE business seats")
    print("  (set business to available_seats = 3 before running)")
    print("=" * 60)
    wins, fails, other = run_wave(10, "business")
    print(f"  201 Created : {len(wins)}   (must be exactly 3)")
    print(f"  409 Conflict: {len(fails)}  (must be exactly 7)")
    if other:
        print(f"  UNEXPECTED  : {other}")
    verdict2 = len(wins) == 3 and len(fails) == 7 and not other
    print(f"  VERDICT: {'PASS - exactly 3 sold' if verdict2 else 'FAIL'}")

    print()
    print("=" * 60)
    print(f"OVERALL: {'ALL PASS' if verdict1 and verdict2 else 'INVESTIGATE'}")
    print("Now verify in Supabase: first = 0, business = 0 available.")
    print("=" * 60)