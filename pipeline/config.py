"""
Pipeline configuration: tickers, thresholds, meeting calendar
"""
from datetime import date

# ── BOK MPC meeting dates ─────────────────────────────────
BOK_MEETINGS = [
    date(2026,  5, 29),
    date(2026,  7, 10),
    date(2026,  8, 27),
    date(2026, 10, 16),
    date(2026, 11, 27),
    date(2027,  1, 16),
    date(2027,  2, 26),
    date(2027,  4, 16),
]

# ── Bloomberg tickers ─────────────────────────────────────
# KOFR OIS  월물: KWKON + 알파벳(A=1M, B=2M, C=3M, F=6M, I=9M)
#           연물: KWKON + 숫자(1=1Y, 2=2Y, 3=3Y, 5=5Y)
KOFR_TICKERS = {
    '1W': 'KWKON1Z Curncy',
    '2W': 'KWKON2Z Curncy',
    '1M': 'KWKONA Curncy',
    '2M': 'KWKONB Curncy',
    '3M': 'KWKONC Curncy',
    '6M': 'KWKONF Curncy',
    '9M': 'KWKONI Curncy',
    '1Y': 'KWKON1 Curncy',
    '2Y': 'KWKON2 Curncy',
    '3Y': 'KWKON3 Curncy',
    '5Y': 'KWKON5 Curncy',
}

# CD IRS  3M CD rate(KWCDC) + IRS: KWSWO + 알파벳/숫자(F=6M, I=9M, 1=1Y, 2=2Y ...)
CD_TICKERS = {
    '3M': 'KWCDC Curncy',    # 3M CD rate (starting pillar)
    '6M': 'KWSWOF Curncy',
    '9M': 'KWSWOI Curncy',
    '1Y': 'KWSWO1 Curncy',
    '2Y': 'KWSWO2 Curncy',
    '3Y': 'KWSWO3 Curncy',
    '5Y': 'KWSWO5 Curncy',
}

# ── Current rates (bootstrap on startup if Bloomberg unavailable) ──
KOFR_RATES_SNAPSHOT = {
    '1M': 2.5500, '2M': 2.5800, '3M': 2.6150,
    '6M': 2.7525, '9M': 2.9375, '1Y': 3.1350,
    '2Y': 3.5300, '3Y': 3.6750, '5Y': 3.7950,
}
CD_RATES_SNAPSHOT = {
    '3M': 2.8100,
    '6M': 2.9725, '9M': 3.1550, '1Y': 3.3550,
    '2Y': 3.7470, '3Y': 3.8940, '5Y': 4.0250,
}

# ── Basis signal thresholds ───────────────────────────────
SIGNAL = {
    # z-score entry/exit
    'z_entry':            1.5,   # RECEIVE_BASIS entry (carry-positive, standard)
    'pay_basis_z_entry':  2.0,   # PAY_BASIS entry — higher bar (carry-negative)
    'z_exit':             0.3,   # exit when z reverts to near mean
    'z_stop':             2.5,   # stop-loss: z continues in adverse direction
    'roll_days':           60,   # rolling window for z-score and std
    # event signals
    'pre_meeting_days':     5,   # S3: days before BOK meeting to watch
    'basis_widening_alert':  28.0,   # bps alert (wide)
    'basis_tightening_alert': 14.0,  # bps alert (tight)
    # S2 forward basis
    'fwd_premium_entry':   8.0,  # fwd bucket premium vs spot 2Y to trigger S2
    # S4 policy regime
    'cut_prob_entry':     40.0,  # % implied cut prob → PAY 1Y basis
    'hike_prob_entry':    65.0,  # % implied hike prob → RECEIVE 3Y basis
}

# ── Strategy tenors to monitor ────────────────────────────
MONITOR_TENORS = ['1Y', '2Y', '3Y']
ANCHOR_TENOR   = '2Y'   # primary signal tenor

# ── Refresh interval ──────────────────────────────────────
REFRESH_SEC = 30
DB_PATH     = 'pipeline/basis_history.db'
