"""
Headless pipeline runner (no UI).
Fetches → computes → signals → saves → prints every REFRESH_SEC seconds.
Useful for server-side scheduling or background feed.

Usage:  python pipeline/run_pipeline.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import datetime
import schedule  # type: ignore[import-untyped]

from pipeline.config      import (KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT,
                                   DB_PATH, REFRESH_SEC, SIGNAL)
from pipeline.fetcher     import fetch_rates, mock_history
from pipeline.basis_engine import compute_basis, forward_basis, compute_rolling_stats, hike_probabilities
from pipeline.signals     import generate_signals
from pipeline.store       import init_db, save_tick, load_history, seed_history


def run_once():
    now = datetime.datetime.now().strftime('%H:%M:%S')

    kofr, cd   = fetch_rates(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT)
    basis      = compute_basis(kofr, cd, tenors=['1Y', '2Y', '3Y', '5Y'])
    fwd        = forward_basis(kofr, cd)
    history    = load_history(DB_PATH)
    stats      = compute_rolling_stats(history, basis, SIGNAL['roll_days'])
    zscore     = {t: s.z for t, s in stats.items()}
    hike_probs = hike_probabilities(kofr)
    signals    = generate_signals(basis, zscore, fwd, stats=stats, hike_probs=hike_probs)
    save_tick(DB_PATH, kofr, cd, basis, zscore, signals)

    # ── console output ────────────────────────────────────────────────
    print(f"\n[{now}] ── CD-KOFR Basis Snapshot ──────────────────────────")
    print(f"  {'Tenor':<6} {'CD':>8} {'KOFR':>8} {'Basis':>8} {'Z':>7}")
    print(f"  {'-'*44}")
    for t in ['1Y', '2Y', '3Y', '5Y']:
        if t in basis:
            rs = stats.get(t)
            sig_str = f'  s={rs.std:.1f}' if rs else ''
            print(f"  {t:<6} {cd.get(t,0):>7.4f}% {kofr.get(t,0):>7.4f}%"
                  f" {basis[t]:>7.1f}bps {zscore.get(t,0):>+6.2f}z{sig_str}")

    print(f"\n  Forward Basis (flat-fwd buckets):")
    for lbl, bps in fwd.items():
        print(f"    {lbl:<12} {bps:>6.1f} bps")

    active = [s for s in signals if s.direction != 'WATCH']
    if active:
        print(f"\n  *** SIGNALS ***")
        for s in active:
            print(f"    {s}")
    else:
        print(f"\n  No active signals.")


def main():
    print("Initialising pipeline...")
    init_db(DB_PATH)
    hist = mock_history(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT, days=120)
    seed_history(DB_PATH, hist)
    print(f"DB ready at {DB_PATH}")

    run_once()   # run immediately on start
    schedule.every(REFRESH_SEC).seconds.do(run_once)

    print(f"\nRunning every {REFRESH_SEC}s — Ctrl+C to stop\n")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == '__main__':
    main()
