"""
Data fetcher — provider-agnostic interface.

Provider priority (auto-detected at import):
  1. Bloomberg  (blpapi — Terminal 연결 필요, Python 3.12 권장)
  2. Infomax    (httpx REST + websockets streaming)
  3. Mock       (시뮬레이션 fallback)

Bloomberg 활성화:
  환경변수 BBG_ENABLED=1 설정 후 Bloomberg Terminal 실행 상태에서 기동.

Infomax 활성화:
  INFOMAX_API_KEY 환경변수 설정.
"""

import os
import json
import random
import datetime
import logging
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# ── active provider ───────────────────────────────────────
_PROVIDER = (
    'bloomberg' if os.getenv('BBG_ENABLED') == '1' else
    'infomax'   if os.getenv('INFOMAX_API_KEY') else
    'mock'
)

# ── last price persistence ────────────────────────────────
_LAST_PRICE_PATH = Path(__file__).parent / 'last_price.json'

def _load_last_price() -> tuple[dict, dict]:
    """Disk에서 마지막 Bloomberg 스냅샷 로드. 없으면 빈 dict 반환."""
    try:
        data = json.loads(_LAST_PRICE_PATH.read_text())
        kofr = {k: float(v) for k, v in data.get('kofr', {}).items()}
        cd   = {k: float(v) for k, v in data.get('cd',   {}).items()}
        ts   = data.get('ts', '')
        if kofr or cd:
            log.info(f'Last price loaded from disk (ts={ts})')
        return kofr, cd
    except Exception:
        return {}, {}

def _save_last_price(kofr: dict, cd: dict):
    """Bloomberg 스냅샷을 disk에 저장 (앱 재시작 시 복원용)."""
    try:
        _LAST_PRICE_PATH.write_text(json.dumps({
            'ts':   datetime.datetime.now().isoformat(timespec='seconds'),
            'kofr': kofr,
            'cd':   cd,
        }, indent=2))
    except Exception as e:
        log.warning(f'last_price 저장 실패: {e}')

# 모듈 로드 시 disk 캐시를 메모리로 복원
_bbg_last_kofr, _bbg_last_cd = _load_last_price()
_bbg_last_ts: float = 0.0

def set_provider(name: str):
    global _PROVIDER
    assert name in ('bloomberg', 'infomax', 'mock')
    _PROVIDER = name


# ════════════════════════════════════════════════════════════
#  PUBLIC INTERFACE
# ════════════════════════════════════════════════════════════

def fetch_rates(snapshot_kofr: dict, snapshot_cd: dict) -> tuple[dict, dict]:
    """
    현재 시점 KOFR OIS / CD IRS 금리 스냅샷.
    Returns (kofr_rates, cd_rates) as {tenor: mid_rate_%}.
    """
    if _PROVIDER == 'bloomberg':
        try:
            return _bbg_fetch_snapshot()
        except Exception as e:
            if _bbg_last_kofr and _bbg_last_cd:
                log.warning(f'Bloomberg fetch failed ({e}), using cached values')
                return dict(_bbg_last_kofr), dict(_bbg_last_cd)
            log.warning(f'Bloomberg fetch failed ({e}), falling back to mock')
    elif _PROVIDER == 'infomax':
        try:
            return _infomax_fetch_sync()
        except Exception as e:
            log.warning(f'Infomax fetch failed ({e}), falling back to mock')
    return _mock_fetch(snapshot_kofr, snapshot_cd)


def fetch_history(days: int = 120) -> list:
    """
    과거 일별 금리 데이터 fetch.
    Bloomberg 사용 시 실데이터, 아니면 mock 반환.
    Returns list of dicts compatible with store.seed_history.
    """
    if _PROVIDER == 'bloomberg':
        try:
            return _bbg_fetch_history(days=days)
        except Exception as e:
            log.warning(f'Bloomberg history fetch failed ({e}), using mock')
    from pipeline.config import KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT
    return mock_history(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT, days=days)


async def stream_rates(on_tick, kofr_codes: dict, cd_codes: dict):
    """Async WebSocket stream. Calls on_tick(kofr_dict, cd_dict) on each update."""
    if _PROVIDER == 'bloomberg':
        await _bbg_stream(on_tick)
    elif _PROVIDER == 'infomax':
        await _infomax_stream(on_tick, kofr_codes, cd_codes)
    else:
        await _mock_stream(on_tick)


# ════════════════════════════════════════════════════════════
#  BLOOMBERG
# ════════════════════════════════════════════════════════════

_bbg_session = None        # singleton session

def _get_bbg_session():
    global _bbg_session
    if _bbg_session is not None:
        return _bbg_session
    import blpapi  # type: ignore[import-untyped]
    options = blpapi.SessionOptions()
    session = blpapi.Session(options)
    if not session.start():
        raise RuntimeError('Bloomberg Terminal에 연결할 수 없습니다. Terminal 실행 여부를 확인하세요.')
    session.openService('//blp/refdata')
    _bbg_session = session
    log.info('Bloomberg session connected')
    return session


def _bbg_fetch_snapshot() -> tuple[dict, dict]:
    """
    ReferenceDataRequest로 현재 금리 스냅샷 fetch.
    Field: PX_LAST (전일 종가) 또는 장중 BID/ASK 평균.
    """
    import blpapi  # type: ignore[import-untyped]
    from pipeline.config import KOFR_TICKERS, CD_TICKERS

    session  = _get_bbg_session()
    refdata  = session.getService('//blp/refdata')
    request  = refdata.createRequest('ReferenceDataRequest')

    # 필요한 티커만 요청 (1Y, 2Y, 3Y + 단기 OIS)
    kofr_tenors = ['1W','2W','1M','2M','3M','6M','9M','1Y','2Y','3Y','5Y']
    cd_tenors   = ['3M','6M','9M','1Y','2Y','3Y','5Y']

    ticker_map = {}   # bloomberg ticker → (curve, tenor)
    for t in kofr_tenors:
        if t in KOFR_TICKERS:
            tk = KOFR_TICKERS[t]
            request.getElement('securities').appendValue(tk)
            ticker_map[tk] = ('kofr', t)
    for t in cd_tenors:
        if t in CD_TICKERS:
            tk = CD_TICKERS[t]
            request.getElement('securities').appendValue(tk)
            ticker_map[tk] = ('cd', t)

    for field in ['PX_LAST', 'PX_MID', 'BID', 'ASK']:
        request.getElement('fields').appendValue(field)

    session.sendRequest(request)

    kofr, cd = {}, {}
    while True:
        ev = session.nextEvent(500)
        for msg in ev:
            if not msg.hasElement('securityData'):
                continue
            sec_data = msg.getElement('securityData')
            for i in range(sec_data.numValues()):
                sec    = sec_data.getValueAsElement(i)
                ticker = sec.getElementAsString('security')

                # 보안 에러 체크 (invalid ticker 등)
                if sec.hasElement('securityError'):
                    err = sec.getElement('securityError')
                    log.warning(f'BBG security error [{ticker}]: {err}')
                    continue

                if ticker not in ticker_map:
                    continue

                fields = sec.getElement('fieldData')

                def _fval(f):
                    try:
                        v = fields.getElementAsFloat(f) if fields.hasElement(f) else None
                        return v if v and v != 0.0 else None
                    except Exception:
                        return None

                bid     = _fval('BID')
                ask     = _fval('ASK')
                px_mid  = _fval('PX_MID')
                px_last = _fval('PX_LAST')

                log.debug(f'BBG [{ticker}] BID={bid} ASK={ask} PX_MID={px_mid} PX_LAST={px_last}')

                if bid is not None and ask is not None:
                    mid = round((bid + ask) / 2, 4)
                elif bid is not None or ask is not None:
                    mid = round((bid or ask), 4)
                elif px_mid is not None:
                    mid = round(px_mid, 4)
                elif px_last is not None:
                    mid = round(px_last, 4)
                else:
                    log.warning(f'BBG [{ticker}] 모든 필드 null — 데이터 없음')
                    continue

                curve, tenor = ticker_map[ticker]
                if curve == 'kofr':
                    kofr[tenor] = mid
                else:
                    cd[tenor] = mid

        if ev.eventType() == blpapi.Event.RESPONSE:
            break

    global _bbg_last_kofr, _bbg_last_cd, _bbg_last_ts
    import time

    # 어떤 티커에서 데이터가 왔는지 / 안 왔는지 로그
    missing_kofr = [t for t in ticker_map.values() if t[0]=='kofr' and t[1] not in kofr]
    missing_cd   = [t for t in ticker_map.values() if t[0]=='cd'   and t[1] not in cd]
    if missing_kofr:
        log.warning(f'BBG 데이터 없는 KOFR tenor: {[t for _,t in missing_kofr]}')
    if missing_cd:
        log.warning(f'BBG 데이터 없는 CD tenor: {[t for _,t in missing_cd]}')

    if kofr:
        _bbg_last_kofr.update(kofr)
    if cd:
        _bbg_last_cd.update(cd)
    if kofr or cd:
        _bbg_last_ts = time.time()
        _save_last_price(_bbg_last_kofr, _bbg_last_cd)

    # 일부 티커만 왔어도 직전 캐시로 나머지 보완
    merged_kofr = {**_bbg_last_kofr, **kofr}
    merged_cd   = {**_bbg_last_cd,   **cd}

    if not merged_kofr or not merged_cd:
        raise ValueError(f'Bloomberg 캐시 없음 (kofr={list(kofr)}, cd={list(cd)})')

    age = int(time.time() - _bbg_last_ts) if _bbg_last_ts else 0
    if age > 60:
        log.warning(f'BBG 데이터 {age}초 stale')
    return merged_kofr, merged_cd


def _bbg_hdreq(session, refdata, tickers: list, start_dt, end_dt) -> dict:
    """HistoricalDataRequest 헬퍼: {date: {col: value}} 반환."""
    import blpapi  # type: ignore[import-untyped]
    from collections import defaultdict

    ticker_map = {tk: col for tk, col in tickers}
    request = refdata.createRequest('HistoricalDataRequest')
    for tk, _col in tickers:
        request.getElement('securities').appendValue(tk)
    request.getElement('fields').appendValue('PX_LAST')
    request.set('startDate', start_dt.strftime('%Y%m%d'))
    request.set('endDate',   end_dt.strftime('%Y%m%d'))
    request.set('periodicitySelection', 'DAILY')
    request.set('nonTradingDayFillOption', 'NON_TRADING_WEEKDAYS')
    request.set('nonTradingDayFillMethod', 'PREVIOUS_VALUE')
    session.sendRequest(request)

    by_date: defaultdict = defaultdict(dict)
    while True:
        ev = session.nextEvent(500)
        for msg in ev:
            if not msg.hasElement('securityData'):
                continue
            sd     = msg.getElement('securityData')
            ticker = sd.getElementAsString('security')
            if sd.hasElement('securityError'):
                log.warning(f'BBG history securityError: [{ticker}]')
                continue
            if ticker not in ticker_map:
                log.warning(f'BBG history unknown ticker: [{ticker}]')
                continue
            col = ticker_map[ticker]
            fd  = sd.getElement('fieldData')
            for j in range(fd.numValues()):
                pt   = fd.getValueAsElement(j)
                dval = pt.getElementAsDatetime('date')
                dt   = datetime.date(dval.year, dval.month, dval.day)
                try:
                    v = pt.getElementAsFloat('PX_LAST')
                    if v:
                        by_date[dt][col] = round(v, 4)
                except Exception as e:
                    log.warning(f'BBG history parse error [{ticker}] {dt}: {e}')
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return by_date


def _bbg_fetch_history(days: int = 252) -> list:
    """
    HistoricalDataRequest로 일별 종가 fetch.
    OIS/CD는 Curncy 요청, KRFRRATE는 별도 Index 요청으로 분리.
    Returns list of dicts: {date, kofr_Xm/y, cd_Xm/y, basis_Xm/y, kofr_on, ...}
    """
    from pipeline.config import KOFR_TICKERS, CD_TICKERS

    session  = _get_bbg_session()
    refdata  = session.getService('//blp/refdata')
    end_dt   = datetime.date.today()
    start_dt = end_dt - datetime.timedelta(days=days)

    hist_tenors = ['3M', '6M', '9M', '1Y', '2Y', '3Y']
    ois_cd_pairs = []
    for t in hist_tenors:
        if t in KOFR_TICKERS:
            ois_cd_pairs.append((KOFR_TICKERS[t], f'kofr_{t.lower()}'))
        if t in CD_TICKERS:
            ois_cd_pairs.append((CD_TICKERS[t], f'cd_{t.lower()}'))

    # ── 요청 1: OIS / CD (Curncy 티커) ───────────────────────
    by_date = _bbg_hdreq(session, refdata, ois_cd_pairs, start_dt, end_dt)
    log.info(f'OIS/CD history: {len(by_date)} raw dates')

    # ── 요청 2: KRFRRATE Index (Index 타입이라 별도 요청) ────────
    kofr_on_map: dict = {}
    try:
        on_data = _bbg_hdreq(
            session, refdata,
            [('KRFRRATE Index', 'kofr_on')],
            start_dt, end_dt,
        )
        kofr_on_map = {dt: row['kofr_on'] for dt, row in on_data.items() if 'kofr_on' in row}
        log.info(f'KRFRRATE history: {len(kofr_on_map)} days')
    except Exception as e:
        log.warning(f'KRFRRATE history fetch 실패: {e}')

    # ── 레코드 조립 ───────────────────────────────────────────
    records    = []
    needed_fwd = {f'{c}_{t}' for c in ('kofr', 'cd') for t in ('3m', '6m', '9m', '1y')}
    for dt in sorted(by_date):
        row = by_date[dt]
        if not needed_fwd.issubset(row.keys()):
            continue
        for t in ('3m', '6m', '9m', '1y', '2y', '3y'):
            k, c_v = row.get(f'kofr_{t}'), row.get(f'cd_{t}')
            if k and c_v:
                row[f'basis_{t}'] = round((c_v - k) * 100, 3)
        row['kofr_on'] = kofr_on_map.get(dt)
        row['date'] = dt
        records.append(row)

    n_on = sum(1 for r in records if r.get('kofr_on'))
    log.info(f'Bloomberg history 완료: {len(records)}일, kofr_on 유효: {n_on}일')
    return records


async def _bbg_stream(on_tick, interval: float = 5.0):
    """
    Bloomberg 실시간 구독 (Subscription API).
    blpapi는 동기 이벤트 루프 기반이라 스레드로 분리 필요.
    현재는 5초 간격 스냅샷 폴링으로 구현.
    """
    import asyncio
    while True:
        try:
            kofr, cd = _bbg_fetch_snapshot()
            await on_tick(kofr, cd)
        except Exception as e:
            log.warning(f'Bloomberg stream tick failed: {e}')
        await asyncio.sleep(interval)


# ════════════════════════════════════════════════════════════
#  INFOMAX
# ════════════════════════════════════════════════════════════

_INFOMAX_BASE = os.getenv('INFOMAX_BASE_URL', 'https://api.infomax.co.kr')
_INFOMAX_WS   = os.getenv('INFOMAX_WS_URL',   'wss://realtime.infomax.co.kr/stream')
_INFOMAX_KEY  = os.getenv('INFOMAX_API_KEY',  '')

_KOFR_CODES = {}   # !! Infomax 코드시트 받으면 채울 것
_CD_CODES   = {}


def _infomax_fetch_sync() -> tuple[dict, dict]:
    import httpx  # type: ignore[import-untyped]
    all_codes = list(_KOFR_CODES) + list(_CD_CODES)
    if not all_codes:
        raise ValueError('INFOMAX_CODES not configured')
    url     = f'{_INFOMAX_BASE}/api/v1/quotes'
    headers = {'X-API-Key': _INFOMAX_KEY, 'Accept': 'application/json'}
    resp    = httpx.get(url, headers=headers, params={'codes': ','.join(all_codes)}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get('data', resp.json())

    def extract(code_map):
        out = {}
        for code, tenor in code_map.items():
            rec = data.get(code, {})
            mid = rec.get('mid') or (rec.get('bid', 0) + rec.get('ask', 0)) / 2
            if mid:
                out[tenor] = round(float(mid), 4)
        return out
    return extract(_KOFR_CODES), extract(_CD_CODES)


async def _infomax_stream(on_tick, kofr_codes: dict, cd_codes: dict):
    import websockets  # type: ignore[import-untyped]
    all_codes     = list(kofr_codes) + list(cd_codes)
    subscribe_msg = json.dumps({'action': 'subscribe', 'codes': all_codes, 'apiKey': _INFOMAX_KEY})
    async with websockets.connect(_INFOMAX_WS) as ws:
        await ws.send(subscribe_msg)
        async for raw in ws:
            try:
                tick = json.loads(raw)
                code = tick.get('code')
                mid  = tick.get('mid') or tick.get('last')
                if not code or mid is None:
                    continue
                kofr_p, cd_p = {}, {}
                if code in kofr_codes:
                    kofr_p[kofr_codes[code]] = float(mid)
                elif code in cd_codes:
                    cd_p[cd_codes[code]] = float(mid)
                if kofr_p or cd_p:
                    await on_tick(kofr_p, cd_p)
            except Exception as e:
                log.warning(f'Infomax WS parse error: {e}')


# ════════════════════════════════════════════════════════════
#  MOCK
# ════════════════════════════════════════════════════════════

async def _mock_stream(on_tick, interval: float = 5.0):
    import asyncio
    from pipeline.config import KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT
    while True:
        kofr, cd = _mock_fetch(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT)
        await on_tick(kofr, cd)
        await asyncio.sleep(interval)


def _mock_fetch(snapshot_kofr, snapshot_cd, noise_bps=0.8):
    def perturb(rates):
        return {t: round(r + random.gauss(0, noise_bps / 100), 4) for t, r in rates.items()}
    return perturb(snapshot_kofr), perturb(snapshot_cd)


def mock_history(snapshot_kofr, snapshot_cd, days=120, seed=42):
    """합성 과거 데이터 (Bloomberg 미사용 시 fallback)."""
    rng    = np.random.default_rng(seed)
    today  = datetime.date.today()
    tenors = ['3M', '6M', '9M', '1Y', '2Y', '3Y']
    records = []

    for d in range(days, 0, -1):
        dt = today - datetime.timedelta(days=d)
        if dt.weekday() >= 5:
            continue
        row = {'date': dt}
        for t in tenors:
            col = t.lower()   # '3M' → '3m', '1Y' → '1y'
            k = round(snapshot_kofr.get(t, 2.50) + rng.normal(0, 0.02), 4)
            c = round(snapshot_cd.get(t,   2.80) + rng.normal(0, 0.02), 4)
            row[f'kofr_{col}']  = k
            row[f'cd_{col}']    = c
            row[f'basis_{col}'] = round((c - k) * 100, 3)
        records.append(row)
    return records
