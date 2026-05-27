"""
Walk-forward backtester for S1 mean-reversion strategy.

Methodology:
  - Iterate history_df row by row starting from row `roll_days`
  - At each step, rolling stats use only prior data (no look-ahead)
  - One position at a time (sequential; not concurrent across tenors)
  - Exit conditions: hit target, hit stop, or 25-day timeout
  - P&L = MTM move + daily carry accrual

P&L convention for RECEIVE_BASIS:
  entry_basis=22 bps, exit_basis=18 bps → MTM gain = 4 bps (basis tightened)
  daily carry = entry_basis / 365 bps/day (positive)

P&L convention for PAY_BASIS:
  entry_basis=15 bps, exit_basis=20 bps → MTM gain = 5 bps (basis widened)
  daily carry = -entry_basis / 365 bps/day (negative)
"""
import datetime
import pandas as pd

from pipeline.basis_engine import compute_rolling_stats, dv01
from pipeline.config import SIGNAL, ANCHOR_TENOR

_MAX_HOLD = 25   # calendar days before forced exit


def run_backtest(history_df: pd.DataFrame,
                 roll_days: int = 60,
                 notional: float = 1e9) -> dict:
    """
    Walk-forward simulation on history_df (from store.load_history).
    Returns {'trades': DataFrame, 'summary': dict}.
    """
    from pipeline.signals import generate_signals

    if len(history_df) < roll_days + 5:
        return {'trades': pd.DataFrame(), 'summary': {}}

    _col_map = {
        '1Y': ('kofr_1y', 'cd_1y', 'basis_1y'),
        '2Y': ('kofr_2y', 'cd_2y', 'basis_2y'),
        '3Y': ('kofr_3y', 'cd_3y', 'basis_3y'),
    }

    trades  = []
    open_pos = None  # {'signal': Signal, 'entry_basis': float, 'days': int}

    for idx in range(roll_days, len(history_df)):
        row  = history_df.iloc[idx]
        hist = history_df.iloc[:idx]

        ts_val = row.get('ts') if 'ts' in row else None
        today  = (pd.Timestamp(ts_val).date()
                  if ts_val is not None and pd.notna(ts_val)
                  else datetime.date.today())

        basis_now: dict[str, float] = {}
        for t, (_, _, b_col) in _col_map.items():
            if b_col in row and pd.notna(row[b_col]):
                basis_now[t] = float(row[b_col])
        if not basis_now:
            continue

        stats  = compute_rolling_stats(hist, basis_now, roll_days)
        zscore = {t: s.z for t, s in stats.items()}

        # ── manage open position ──────────────────────────────────────
        if open_pos is not None:
            sig   = open_pos['signal']
            t     = sig.tenor
            b     = basis_now.get(t)
            if b is None:
                open_pos['days'] += 1
                continue

            open_pos['days'] += 1
            days = open_pos['days']
            entry = open_pos['entry_basis']

            if sig.direction == 'RECEIVE_BASIS':
                mtm_bps  = entry - b           # profit when basis tightens
                hit_tgt  = b <= sig.target
                hit_stp  = b >= sig.stop
                carry_day = entry / 365
            else:
                mtm_bps  = b - entry           # profit when basis widens
                hit_tgt  = b >= sig.target
                hit_stp  = b <= sig.stop
                carry_day = -entry / 365       # carry negative for PAY_BASIS

            if hit_tgt or hit_stp or days >= _MAX_HOLD:
                carry_bps = round(carry_day * days, 3)
                total_bps = round(mtm_bps + carry_bps, 3)
                d01 = dv01(t, notional)
                trades.append({
                    'date':        today,
                    'strategy':    sig.strategy,
                    'direction':   sig.direction,
                    'tenor':       t,
                    'entry_bps':   round(entry, 2),
                    'exit_bps':    round(b, 2),
                    'mtm_bps':     round(mtm_bps, 3),
                    'carry_bps':   carry_bps,
                    'total_bps':   total_bps,
                    'pnl_krw_mn':  round(total_bps * d01 / 1e6, 2),
                    'days_held':   days,
                    'exit_type':   ('target' if hit_tgt
                                    else 'stop' if hit_stp
                                    else 'timeout'),
                })
                open_pos = None

        # ── open new position if none ─────────────────────────────────
        if open_pos is None:
            sigs   = generate_signals(basis_now, zscore, {}, today=today, stats=stats)
            active = [s for s in sigs
                      if s.direction != 'WATCH' and s.strategy == 'S1-MeanRevert']
            if active:
                sig = sorted(active, key=lambda s: abs(s.basis_now - _mean(stats, s.tenor)),
                             reverse=True)[0]   # largest z-deviation first
                open_pos = {
                    'signal':       sig,
                    'entry_basis':  basis_now.get(sig.tenor, sig.basis_now),
                    'days':         0,
                }

    if not trades:
        return {'trades': pd.DataFrame(), 'summary': {}}

    df = pd.DataFrame(trades)
    n  = len(df)
    wins = int((df['total_bps'] > 0).sum())

    summary = {
        'total_trades':   n,
        'win_rate_pct':   round(wins / n * 100, 1),
        'avg_pnl_bps':    round(float(df['total_bps'].mean()), 2),
        'total_pnl_bps':  round(float(df['total_bps'].sum()), 2),
        'avg_carry_bps':  round(float(df['carry_bps'].mean()), 2),
        'avg_hold_days':  round(float(df['days_held'].mean()), 1),
        'sharpe_approx':  round(float(df['total_bps'].mean())
                                / max(float(df['total_bps'].std()), 0.01), 2),
        'exits':          df.groupby('exit_type').size().to_dict(),
    }
    return {'trades': df, 'summary': summary}


def _mean(stats: dict, tenor: str) -> float:
    rs = stats.get(tenor)
    return rs.mu if rs else 21.0
