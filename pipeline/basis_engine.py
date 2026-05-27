"""
Basis engine: flat-forward bootstrap, basis metrics, forward curve.
Core analytics that run on every data refresh.
"""
from dataclasses import dataclass
from datetime import date
import pandas as pd
from scipy.optimize import fsolve


# ── Flat-forward bootstrap (same logic as bok_meeting_path.py) ───────
SETTLEMENT  = date(2026, 5, 19)
BOK_RATE    = 2.50
STEP_SIZE   = 0.25

BOK_MEETINGS = [
    date(2026,  5, 29), date(2026,  7, 10),
    date(2026,  8, 27), date(2026, 10, 16),
    date(2026, 11, 27), date(2027,  1, 16),
    date(2027,  2, 26), date(2027,  4, 16),
]

_TENOR_ENDS = {
    '1M': date(2026,  6, 19), '2M': date(2026,  7, 19),
    '3M': date(2026,  8, 19), '6M': date(2026, 11, 19),
    '9M': date(2027,  2, 19), '1Y': date(2027,  5, 19),
}

def D(d): return (d - SETTLEMENT).days

def flat_forward_buckets(rates: dict, tenor_order: list) -> list:
    # multiplicative OIS bootstrap: (1 + r·D/365) 누적곱 기반
    buckets, prev_end, prev_acc = [], SETTLEMENT, 1.0
    for t in tenor_order:
        if t not in rates or t not in _TENOR_ENDS:
            continue
        end_d   = _TENOR_ENDS[t]
        d_end   = D(end_d)
        d_prev  = D(prev_end)
        acc_end = 1 + rates[t] / 100 * d_end / 365
        ff      = (acc_end / prev_acc - 1) * 365 / (d_end - d_prev) * 100
        meetings = [m for m in BOK_MEETINGS if prev_end < m <= end_d]
        buckets.append(dict(tenor=t, start=prev_end, end=end_d, ff=ff, meetings=meetings))
        prev_end, prev_acc = end_d, acc_end
    return buckets


def solve_meeting_path(buckets: list, start_rate: float) -> list:
    rows, rate = [], start_rate
    for b in buckets:
        bkpts    = [b['start']] + b['meetings'] + [b['end']]
        meetings = b['meetings']
        total_d  = D(b['end']) - D(b['start'])
        if not meetings:
            continue

        base = rate
        def avg(delta, _base=base):
            r, wd = _base, 0.0
            for i in range(len(bkpts) - 1):
                d = (bkpts[i+1] - bkpts[i]).days
                wd += d * r
                if bkpts[i+1] in meetings:
                    r += delta
            return wd / total_d

        delta = fsolve(lambda x: avg(x) - b['ff'], 0.0)[0]
        for m in meetings:
            rate += delta
            rows.append(dict(
                date       = m,
                rate_before= round(rate - delta, 4),
                rate_after = round(rate, 4),
                delta_bps  = round(delta * 100, 2),
                hike_prob  = round(max(0, min(100,  delta / STEP_SIZE * 100)), 1),
                cut_prob   = round(max(0, min(100, -delta / STEP_SIZE * 100)), 1),
            ))
    return rows


# ── Basis metrics ─────────────────────────────────────────────────────
def compute_basis(kofr: dict, cd: dict, tenors=None) -> dict:
    """Spot CD-KOFR basis in bps for each tenor."""
    tenors = tenors or ['1Y', '2Y', '3Y', '5Y']
    out = {}
    for t in tenors:
        if t in kofr and t in cd:
            out[t] = round((cd[t] - kofr[t]) * 100, 2)
    return out


def forward_rates_table(kofr: dict, cd: dict) -> list[dict]:
    """
    Period forward rates for KOFR OIS and CD IRS, plus forward basis.

    KOFR OIS: multiplicative bootstrap  [(1+r2*D2/365)/(1+r1*D1/365)-1] * 365/ΔD
      — correct for daily-compounded OIS floating leg (ACT/365)
    CD IRS:   simple bootstrap           (r2*D2 - r1*D1) / ΔD
      — correct for quarterly-reset simple floating leg (ACT/365)

    Both par rates are quoted on the same ACT/365 simple fixed-leg convention,
    so basis_bps = (fwd_cd - fwd_kofr)*100 is directly comparable — no further
    compounding adjustment required.

    Returns list of dicts per period:
      { period, fwd_kofr, fwd_cd, basis_bps, meetings, n_meetings }
    """
    _D = {'3M': 92, '6M': 184, '9M': 275, '1Y': 365}
    _END = {
        '3M': date(2026,  8, 19), '6M': date(2026, 11, 19),
        '9M': date(2027,  2, 19), '1Y': date(2027,  5, 19),
    }
    periods = [
        ('Spot-3M', None,  '3M'),
        ('3M-6M',   '3M',  '6M'),
        ('6M-9M',   '6M',  '9M'),
        ('9M-1Y',   '9M',  '1Y'),
    ]

    def _ois_fwd(curve: dict, t1, t2: str) -> float | None:
        """Multiplicative OIS bootstrap — correct for daily-compounded floating.
        Rates stored in percent (e.g. 2.615); formula works in decimal internally."""
        r2 = curve.get(t2)
        if r2 is None:
            return None
        d2 = _D[t2]
        if t1 is None:
            return r2  # spot par rate IS the period forward
        r1 = curve.get(t1)
        if r1 is None:
            return None
        d1 = _D[t1]
        # convert pct→decimal, compute, convert back to pct
        return ((1 + r2 / 100 * d2 / 365) / (1 + r1 / 100 * d1 / 365) - 1) * 365 / (d2 - d1) * 100

    def _simple_fwd(curve: dict, t1, t2: str) -> float | None:
        """Simple linear bootstrap — correct for quarterly-reset simple floating.
        Scale-invariant: works directly in percent."""
        r2 = curve.get(t2)
        if r2 is None:
            return None
        d2 = _D[t2]
        if t1 is None:
            return r2
        r1 = curve.get(t1)
        if r1 is None:
            return None
        d1 = _D[t1]
        return (r2 * d2 - r1 * d1) / (d2 - d1)

    rows = []
    for label, t1, t2 in periods:
        fk = _ois_fwd(kofr, t1, t2)
        fc = _simple_fwd(cd,  t1, t2)
        if fk is None or fc is None:
            continue
        start_d = _END[t1] if t1 else SETTLEMENT
        end_d   = _END[t2]
        mtgs    = [m for m in BOK_MEETINGS if start_d < m <= end_d]
        rows.append({
            'period':     label,
            'fwd_kofr':   round(fk, 4),
            'fwd_cd':     round(fc, 4),
            'basis_bps':  round((fc - fk) * 100, 2),
            'meetings':   [m.strftime('%m/%d') for m in mtgs],
            'n_meetings': len(mtgs),
        })
    return rows


def fwd_basis_history(df: pd.DataFrame) -> pd.DataFrame:
    """
    일별 포워드 베이시스 계산.
    입력: kofr_3m/6m/9m/1y, cd_3m/6m/9m/1y 컬럼이 있는 DataFrame.
    출력: 'Spot-3M', '3M-6M', '6M-9M', '9M-1Y' 컬럼 (bps).
    """
    _D = {'3m': 92, '6m': 184, '9m': 275, '1y': 365}
    required = ['kofr_3m', 'kofr_6m', 'kofr_9m', 'kofr_1y',
                'cd_3m',   'cd_6m',   'cd_9m',   'cd_1y']
    mask = df[[c for c in required if c in df.columns]].notna().all(axis=1)
    d = df[mask].copy()
    if d.empty:
        return pd.DataFrame()

    def ois_fwd(k1, k2, d1, d2):
        return ((1 + k2/100 * d2/365) / (1 + k1/100 * d1/365) - 1) * 365/(d2-d1) * 100

    def cd_fwd(c1, c2, d1, d2):
        return (c2 * d2 - c1 * d1) / (d2 - d1)

    out = pd.DataFrame(index=d.index)
    if 'ts' in d.columns:
        out['ts'] = d['ts']

    out['Spot-3M'] = ((d['cd_3m'] - d['kofr_3m']) * 100).round(2)

    fk = ois_fwd(d['kofr_3m'], d['kofr_6m'], _D['3m'], _D['6m'])
    fc = cd_fwd(d['cd_3m'], d['cd_6m'], _D['3m'], _D['6m'])
    out['3M-6M'] = ((fc - fk) * 100).round(2)

    fk = ois_fwd(d['kofr_6m'], d['kofr_9m'], _D['6m'], _D['9m'])
    fc = cd_fwd(d['cd_6m'], d['cd_9m'], _D['6m'], _D['9m'])
    out['6M-9M'] = ((fc - fk) * 100).round(2)

    fk = ois_fwd(d['kofr_9m'], d['kofr_1y'], _D['9m'], _D['1y'])
    fc = cd_fwd(d['cd_9m'], d['cd_1y'], _D['9m'], _D['1y'])
    out['9M-1Y'] = ((fc - fk) * 100).round(2)

    return out


def fwd_basis_stats(hist_df: pd.DataFrame, current_fwd_rows: list) -> list:
    """
    현재 포워드 베이시스를 히스토리 분포와 비교.
    Returns: [{period, mean, std, min, max, current, pct_rank, z, n_days}, ...]
    """
    if hist_df.empty:
        return []
    fwd = fwd_basis_history(hist_df)
    if fwd.empty:
        return []
    current_map = {r['period']: r['basis_bps'] for r in current_fwd_rows}

    stats = []
    for period in ['Spot-3M', '3M-6M', '6M-9M', '9M-1Y']:
        if period not in fwd.columns:
            continue
        s = fwd[period].dropna()
        if len(s) < 5:
            continue
        curr = current_map.get(period)
        pct  = float((s < curr).sum()) / len(s) * 100 if curr is not None else None
        z    = (curr - s.mean()) / s.std() if curr is not None and s.std() > 0 else None
        stats.append({
            'period':   period,
            'mean':     round(float(s.mean()), 2),
            'std':      round(float(s.std()),  2),
            'min':      round(float(s.min()),  2),
            'max':      round(float(s.max()),  2),
            'current':  curr,
            'pct_rank': round(pct, 1) if pct is not None else None,
            'z':        round(z,   2) if z   is not None else None,
            'n_days':   len(s),
        })
    return stats


def forward_basis(kofr: dict, cd: dict) -> dict:
    """
    Period forward basis in bps — {label: bps}.
    Uses forward_rates_table for consistent period-matched calculation.
    """
    return {r['period']: r['basis_bps'] for r in forward_rates_table(kofr, cd)}


# ── Rolling stats and z-score ─────────────────────────────────────────

@dataclass
class RollingStat:
    mu:  float    # rolling mean (bps)
    std: float    # rolling std (bps) — floored at 0.5
    z:   float    # z-score: (current - mu) / std
    pct: float    # percentile rank 0-100 (where current sits in history)


def compute_rolling_stats(history_df: pd.DataFrame, current_basis: dict,
                          roll_days: int = 60) -> dict:
    """
    Returns {tenor: RollingStat}. Used by generate_signals for target/stop calc.
    Falls back to std=3.0 if insufficient history.
    """
    col_map = {'1Y': 'basis_1y', '2Y': 'basis_2y', '3Y': 'basis_3y'}

    if history_df.empty or len(history_df) < 10:
        return {t: RollingStat(mu=b, std=3.0, z=0.0, pct=50.0)
                for t, b in current_basis.items() if t in col_map}

    out  = {}
    tail = history_df.tail(roll_days)
    for tenor, col in col_map.items():
        if col not in tail.columns or tenor not in current_basis:
            continue
        series = tail[col].dropna()
        if len(series) < 5:
            continue
        mu  = series.mean()
        std = max(float(series.std()), 0.5)
        b   = current_basis[tenor]
        z   = round((b - mu) / std, 2)
        pct = round(float((series < b).sum()) / len(series) * 100, 1)
        out[tenor] = RollingStat(mu=round(mu, 2), std=round(std, 2), z=z, pct=pct)
    return out


def compute_zscore(history_df: pd.DataFrame, current_basis: dict,
                   roll_days: int = 60) -> dict:
    """Z-score of current basis vs rolling history. Delegates to compute_rolling_stats."""
    stats = compute_rolling_stats(history_df, current_basis, roll_days)
    return {t: s.z for t, s in stats.items()}


# ── DV01 helper ────────────────────────────────────────────────────────
_DV01_TABLE = {
    '1M': 0.00085, '2M': 0.00167, '3M': 0.00252,
    '6M': 0.00500, '9M': 0.00742,
    '1Y': 0.00980, '2Y': 0.01920, '3Y': 0.02840,
    '5Y': 0.04600, '7Y': 0.06200,
}

def dv01(tenor: str, notional: float = 1e9) -> float:
    """Approximate DV01 (KRW, 1bp per notional)."""
    return _DV01_TABLE.get(tenor, 0.02) * notional / 100


def neutral_notional(anchor_tenor: str, hedge_tenor: str, notional: float) -> float:
    """Notional to be DV01-neutral against anchor leg."""
    d_anchor = dv01(anchor_tenor, notional)
    d_hedge  = dv01(hedge_tenor, 1e9)
    return round(d_anchor / d_hedge * 1e9)


# ── Carry and policy helpers ───────────────────────────────────────────

def carry_bps_annual(basis_bps: float) -> float:
    """
    Annual carry for a RECEIVE_BASIS position = basis level itself.
    Carry is positive for receive, negative for pay.
    For 1bn KRW 2Y: KRW carry = basis_bps * DV01_2Y = basis * 192,000 KRW/yr.
    """
    return basis_bps


def scenario_par_rate(tenor: str, meeting_deltas: dict,
                      basis_adj_bps: float = 0.0) -> float:
    """
    BOK 정책금리 시나리오 하에서의 KOFR OIS par rate.
    meeting_deltas: {date: delta_bps} — 각 금통위에서의 변화폭 (bps).
    basis_adj_bps: KOFR overnight − 정책금리 기술적 스프레드 조정 (bps).
    Returns par rate (%).
    """
    if tenor not in _TENOR_ENDS:
        return BOK_RATE
    end_date   = _TENOR_ENDS[tenor]
    total_days = (end_date - SETTLEMENT).days
    meetings   = [m for m in BOK_MEETINGS if SETTLEMENT < m <= end_date]
    bkpts      = [SETTLEMENT] + meetings + [end_date]

    rate, wsum = BOK_RATE, 0.0
    for i in range(len(bkpts) - 1):
        days   = (bkpts[i + 1] - bkpts[i]).days
        wsum  += rate * days
        nxt    = bkpts[i + 1]
        if nxt in meeting_deltas:
            rate += meeting_deltas[nxt] / 100   # bps → %
    return round(wsum / total_days + basis_adj_bps / 100, 4)


def hike_probabilities(kofr: dict) -> list:
    """
    Extract per-meeting hike/cut probabilities from KOFR OIS curve via flat-forward.
    Returns [{date, rate_before, rate_after, delta_bps, hike_prob, cut_prob}, ...]
    """
    tenor_order = [t for t in ['1M', '2M', '3M', '6M', '9M', '1Y'] if t in kofr]
    if not tenor_order:
        return []
    buckets = flat_forward_buckets(kofr, tenor_order)
    return solve_meeting_path(buckets, BOK_RATE)
