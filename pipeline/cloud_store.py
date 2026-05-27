"""
Supabase cloud store — mirrors store.py but uses Supabase REST API.
Used by Streamlit Cloud dashboard (reads) and pusher.py (writes).
"""
import datetime
import os
import pandas as pd

def _client():
    try:
        import streamlit as st
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["service_role_key"]
    except Exception:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    from supabase import create_client
    return create_client(url, key)


def save_tick(kofr: dict, cd: dict, basis: dict, zscore: dict, signals: list):
    ts = datetime.datetime.now().isoformat(timespec='seconds')
    row = dict(
        ts=ts,
        kofr_on=kofr.get('ON'),
        kofr_1w=kofr.get('1W'),
        kofr_1m=kofr.get('1M'), kofr_2m=kofr.get('2M'),
        kofr_3m=kofr.get('3M'), kofr_6m=kofr.get('6M'), kofr_9m=kofr.get('9M'),
        kofr_1y=kofr.get('1Y'), kofr_2y=kofr.get('2Y'), kofr_3y=kofr.get('3Y'),
        cd_3m=cd.get('3M'),     cd_6m=cd.get('6M'),     cd_9m=cd.get('9M'),
        cd_1y=cd.get('1Y'),     cd_2y=cd.get('2Y'),     cd_3y=cd.get('3Y'),
        basis_3m=basis.get('3M'), basis_6m=basis.get('6M'), basis_9m=basis.get('9M'),
        basis_1y=basis.get('1Y'), basis_2y=basis.get('2Y'), basis_3y=basis.get('3Y'),
        z_1y=zscore.get('1Y'), z_2y=zscore.get('2Y'), z_3y=zscore.get('3Y'),
        signal=','.join(s.strategy for s in signals if s.direction != 'WATCH') or None,
    )
    c = _client()
    c.table('basis_history').upsert(row).execute()
    for s in signals:
        c.table('signals_log').insert(dict(
            ts=ts, strategy=s.strategy, direction=s.direction, tenor=s.tenor,
            basis_now=s.basis_now, target=s.target, stop=s.stop, reason=s.reason,
        )).execute()


def load_history(days: int = 120) -> pd.DataFrame:
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        c = _client()
        res = c.table('basis_history').select('*').gte('ts', cutoff).order('ts').execute()
        df = pd.DataFrame(res.data)
        if not df.empty and 'ts' in df.columns:
            df['ts'] = pd.to_datetime(df['ts'])
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def load_signals_log(limit: int = 50) -> pd.DataFrame:
    try:
        c = _client()
        res = c.table('signals_log').select('*').order('ts', desc=True).limit(limit).execute()
        return pd.DataFrame(res.data)
    except Exception:
        return pd.DataFrame()


def request_refresh():
    """Streamlit Cloud 앱에서 Bloomberg PC에 새로고침 요청."""
    c = _client()
    c.table('refresh_requests').insert({'fulfilled_at': None}).execute()


def pop_refresh_request() -> bool:
    """Bloomberg PC pusher에서 호출. 미처리 요청 있으면 True 반환 후 fulfilled 처리."""
    c = _client()
    res = c.table('refresh_requests').select('id').is_('fulfilled_at', 'null').limit(1).execute()
    if not res.data:
        return False
    rid = res.data[0]['id']
    c.table('refresh_requests').update(
        {'fulfilled_at': datetime.datetime.now().isoformat()}
    ).eq('id', rid).execute()
    return True


def last_update() -> str | None:
    """가장 최근 업데이트 시각."""
    try:
        c = _client()
        res = c.table('basis_history').select('ts').order('ts', desc=True).limit(1).execute()
        return res.data[0]['ts'] if res.data else None
    except Exception:
        return None


def load_latest_rates() -> tuple[dict, dict] | tuple[None, None]:
    """Supabase 최신 행에서 kofr/cd 딕셔너리 복원."""
    try:
        c = _client()
        res = c.table('basis_history').select('*').order('ts', desc=True).limit(1).execute()
        if not res.data:
            return None, None
        r = res.data[0]
        kofr = {k: r[v] for k, v in [
            ('ON','kofr_on'),
            ('1W','kofr_1w'),
            ('1M','kofr_1m'),('2M','kofr_2m'),
            ('3M','kofr_3m'),('6M','kofr_6m'),('9M','kofr_9m'),
            ('1Y','kofr_1y'),('2Y','kofr_2y'),('3Y','kofr_3y'),
        ] if r.get(v) is not None}
        cd = {k: r[v] for k, v in [
            ('3M','cd_3m'),('6M','cd_6m'),('9M','cd_9m'),
            ('1Y','cd_1y'),('2Y','cd_2y'),('3Y','cd_3y'),
        ] if r.get(v) is not None}
        return kofr, cd
    except Exception:
        return None, None
