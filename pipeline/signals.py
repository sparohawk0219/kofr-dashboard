"""
Signal engine: generates actionable trade signals from basis metrics.

Four strategies:
  S1  Mean-reversion on spot basis (primary; carry-positive for RECEIVE)
  S2  Forward basis curve premium (6M-9M bucket vs spot)
  S3  Pre-BOK-meeting event trade (post-meeting compression)
  S4  Policy regime alignment (cut / hike cycle)

Key design principles:
  - RECEIVE_BASIS = receive CD IRS fixed + pay KOFR OIS fixed → long the spread
    Always carry-positive (basis > 0). Entry at z_entry=1.5.
  - PAY_BASIS = pay CD IRS fixed + receive KOFR OIS fixed → short the spread
    Always carry-negative. Higher z-score hurdle (pay_basis_z_entry=2.0).
  - Targets and stops use actual rolling std (from compute_rolling_stats),
    not a percentage of basis level.
"""
import datetime
from dataclasses import dataclass, field
from typing import Literal, Optional

from pipeline.config import SIGNAL, BOK_MEETINGS, ANCHOR_TENOR


@dataclass
class Signal:
    strategy:         str
    direction:        Literal['RECEIVE_BASIS', 'PAY_BASIS', 'WATCH']
    tenor:            str
    basis_now:        float           # current basis in bps
    target:           float           # exit level (bps)
    stop:             float           # stop-loss level (bps)
    reason:           str
    carry_annual_bps: float = 0.0     # +ve = positive carry (RECEIVE), -ve = negative (PAY)
    std_bps:          float = 3.0     # 1-sigma basis move from rolling history
    conviction:       int   = 1       # 1 = single tenor; 2 = multi-tenor confirmed
    dv01_notional:    float = 1e9     # standard 1bn KRW per leg
    generated_at:     datetime.datetime = field(default_factory=datetime.datetime.now)

    @property
    def carry_30d_bps(self) -> float:
        """30-day carry accrual in bps."""
        return round(self.carry_annual_bps * 30 / 365, 2)

    @property
    def carry_breakeven_days(self) -> Optional[float]:
        """Days of carry to offset one 1-sigma adverse move (PAY_BASIS only)."""
        daily = abs(self.carry_annual_bps) / 365
        if daily < 0.001:
            return None
        return round(self.std_bps / daily)

    def __str__(self):
        d    = '▲ RECEIVE' if self.direction == 'RECEIVE_BASIS' else '▼ PAY'
        conv = '★★' if self.conviction >= 2 else '★'
        carry = f'carry={self.carry_30d_bps:+.2f}bps/30d'
        return (f"[{self.strategy}]{conv} {d} basis {self.tenor}  |  "
                f"now={self.basis_now:.1f}bps  tgt={self.target:.1f}  stop={self.stop:.1f}  "
                f"{carry}  |  {self.reason}")


def generate_signals(basis: dict, zscore: dict, fwd_basis: dict,
                     today: datetime.date = None,
                     stats: dict = None,
                     hike_probs: list = None) -> list[Signal]:
    """
    Returns list of Signal objects. Call on every data refresh.

    basis:      {tenor: bps}  — current spot basis
    zscore:     {tenor: z}    — rolling z-scores (from compute_zscore or compute_rolling_stats)
    fwd_basis:  {label: bps}  — forward basis buckets (from forward_basis)
    stats:      {tenor: RollingStat} — optional; provides actual std for target/stop
    hike_probs: list of meeting dicts — optional; from hike_probabilities()
    """
    today = today or datetime.date.today()
    signals: list[Signal] = []

    def _std(tenor: str) -> float:
        if stats and tenor in stats:
            return max(0.5, stats[tenor].std)
        return 3.0

    def _mean(tenor: str) -> float:
        if stats and tenor in stats:
            return stats[tenor].mu
        return basis.get(tenor, 21.0)

    # ── S1: Spot basis mean-reversion ─────────────────────────────────
    # Check 2Y (anchor), 1Y, and 3Y individually; use multi-tenor count for conviction.
    tenors_s1 = [ANCHOR_TENOR] + [t for t in ['1Y', '3Y'] if t != ANCHOR_TENOR]

    for tenor in tenors_s1:
        z = zscore.get(tenor, 0.0)
        b = basis.get(tenor)
        if b is None:
            continue

        sigma = _std(tenor)
        mu    = _mean(tenor)

        # Conviction: count other tenors with z in the same direction
        others = [t for t in tenors_s1 if t != tenor and t in zscore]
        same_dir = sum(
            1 for t in others
            if (z > 0 and zscore[t] > SIGNAL['z_entry'] * 0.7) or
               (z < 0 and zscore[t] < -SIGNAL['z_entry'] * 0.7)
        )
        conviction = 2 if same_dir >= 1 else 1

        entry_recv = SIGNAL['z_entry']
        entry_pay  = SIGNAL.get('pay_basis_z_entry', SIGNAL['z_entry'] + 0.5)

        if z >= entry_recv:
            signals.append(Signal(
                strategy          = 'S1-MeanRevert',
                direction         = 'RECEIVE_BASIS',
                tenor             = tenor,
                basis_now         = round(b, 2),
                target            = round(mu + SIGNAL['z_exit'] * sigma, 1),
                stop              = round(b  + (SIGNAL['z_stop'] - z) * sigma, 1),
                reason            = f'{tenor} z={z:+.2f}σ, wide vs {mu:.1f}bps mean (σ={sigma:.1f})',
                carry_annual_bps  = round(b, 1),
                std_bps           = round(sigma, 2),
                conviction        = conviction,
            ))
        elif z <= -entry_pay:
            signals.append(Signal(
                strategy          = 'S1-MeanRevert',
                direction         = 'PAY_BASIS',
                tenor             = tenor,
                basis_now         = round(b, 2),
                target            = round(mu - SIGNAL['z_exit'] * sigma, 1),
                stop              = round(b  - (SIGNAL['z_stop'] - abs(z)) * sigma, 1),
                reason            = f'{tenor} z={z:+.2f}σ, tight vs {mu:.1f}bps mean — carry negative',
                carry_annual_bps  = round(-b, 1),
                std_bps           = round(sigma, 2),
                conviction        = conviction,
            ))

    # ── S2: Forward basis curve premium ────────────────────────────────
    # Trade: forward CD-KOFR basis in the 6M-9M bucket is elevated vs spot 2Y.
    # Structure: receive 9M CD IRS, pay 9M KOFR OIS (or via 6M fwd-start swaps).
    # Rationale: excess term/credit premium in 6M-9M bucket should revert to spot.
    fwd_9m  = fwd_basis.get('6M-9M')
    spot_2y = basis.get('2Y')
    if fwd_9m is not None and spot_2y is not None:
        fwd_premium = fwd_9m - spot_2y
        threshold   = SIGNAL.get('fwd_premium_entry', 8.0)
        if fwd_premium > threshold:
            signals.append(Signal(
                strategy          = 'S2-FwdBasis',
                direction         = 'RECEIVE_BASIS',
                tenor             = '9M',
                basis_now         = round(fwd_9m, 2),
                target            = round(fwd_9m - fwd_premium * 0.5, 1),
                stop              = round(fwd_9m + 4.0, 1),
                reason            = (f'6M-9M fwd basis {fwd_9m:.1f}bps >> '
                                     f'2Y spot {spot_2y:.1f}bps (premium={fwd_premium:.1f}bps)'),
                carry_annual_bps  = round(fwd_9m, 1),
                std_bps           = _std('2Y'),
                conviction        = 1,
            ))

    # ── S3: Pre-BOK meeting convergence trade ─────────────────────────
    # Thesis: pre-meeting uncertainty bid widens 1Y basis → post-meeting it compresses.
    # Enter 3-5 days before meeting when 1Y basis is at elevated level.
    # Half-size position (event risk).
    days_to_next = _days_to_next_meeting(today)
    if days_to_next is not None and 1 <= days_to_next <= SIGNAL['pre_meeting_days']:
        b1y = basis.get('1Y')
        if b1y is not None and b1y > SIGNAL['basis_widening_alert']:
            signals.append(Signal(
                strategy          = 'S3-PreMeeting',
                direction         = 'RECEIVE_BASIS',
                tenor             = '1Y',
                basis_now         = round(b1y, 2),
                target            = round(b1y - 4.0, 1),
                stop              = round(b1y + 3.0, 1),
                reason            = (f'BOK meeting in {days_to_next}d; '
                                     f'1Y basis {b1y:.1f}bps — post-meeting compression expected'),
                carry_annual_bps  = round(b1y, 1),
                std_bps           = _std('1Y'),
                conviction        = 1,
                dv01_notional     = 5e8,   # half-size for event risk
            ))

    # ── S4: Policy regime alignment ────────────────────────────────────
    # Cut cycle: KOFR 1Y drops faster than CD 1Y (credit floor) → 1Y basis widens
    #   → PAY BASIS on 1Y (short the spread, profit from widening)
    # Hike cycle: KOFR 3Y OIS rises to meet CD level → 3Y basis tightens
    #   → RECEIVE BASIS on 3Y (long the spread at wide levels, profit from tightening)
    if hike_probs:
        next_mtg  = hike_probs[0]
        cut_prob  = next_mtg.get('cut_prob',  0.0)
        hike_prob = next_mtg.get('hike_prob', 0.0)

        b1y = basis.get('1Y')
        b3y = basis.get('3Y')

        if cut_prob >= SIGNAL.get('cut_prob_entry', 40.0) and b1y is not None:
            if zscore.get('1Y', 0) > -1.0:   # 1Y basis not already tight
                signals.append(Signal(
                    strategy          = 'S4-PolicyRegime',
                    direction         = 'PAY_BASIS',
                    tenor             = '1Y',
                    basis_now         = round(b1y, 2),
                    target            = round(b1y + 5.0, 1),
                    stop              = round(b1y - 3.0, 1),
                    reason            = (f'Cut prob {cut_prob:.0f}% at next BOK — '
                                         f'KOFR 1Y to drop faster than CD → 1Y basis widens'),
                    carry_annual_bps  = round(-b1y, 1),
                    std_bps           = _std('1Y'),
                    conviction        = 1,
                    dv01_notional     = 5e8,
                ))

        if hike_prob >= SIGNAL.get('hike_prob_entry', 65.0) and b3y is not None:
            if zscore.get('3Y', 0) > 0.5:    # 3Y basis slightly wide
                signals.append(Signal(
                    strategy          = 'S4-PolicyRegime',
                    direction         = 'RECEIVE_BASIS',
                    tenor             = '3Y',
                    basis_now         = round(b3y, 2),
                    target            = round(b3y - 3.0, 1),
                    stop              = round(b3y + 5.0, 1),
                    reason            = (f'Hike prob {hike_prob:.0f}% at next BOK — '
                                         f'KOFR 3Y OIS rises, 3Y basis normalises'),
                    carry_annual_bps  = round(b3y, 1),
                    std_bps           = _std('3Y'),
                    conviction        = 1,
                    dv01_notional     = 5e8,
                ))

    # ── Alerts: absolute level breaches (WATCH only) ──────────────────
    for tenor, b in basis.items():
        if b > SIGNAL['basis_widening_alert']:
            signals.append(Signal(
                strategy='ALERT', direction='WATCH',
                tenor=tenor, basis_now=round(b, 2),
                target=round(SIGNAL['basis_widening_alert'] - 3, 1),
                stop=round(b + 5, 1),
                reason=f'{tenor} basis {b:.1f}bps > alert {SIGNAL["basis_widening_alert"]}bps',
                carry_annual_bps=round(b, 1),
            ))
        elif b < SIGNAL['basis_tightening_alert']:
            signals.append(Signal(
                strategy='ALERT', direction='WATCH',
                tenor=tenor, basis_now=round(b, 2),
                target=round(SIGNAL['basis_tightening_alert'] + 3, 1),
                stop=round(b - 5, 1),
                reason=f'{tenor} basis {b:.1f}bps < alert {SIGNAL["basis_tightening_alert"]}bps',
                carry_annual_bps=round(-b, 1),
            ))

    return signals


def _days_to_next_meeting(today: datetime.date) -> Optional[int]:
    future = [m for m in BOK_MEETINGS if m >= today]
    return (future[0] - today).days if future else None
