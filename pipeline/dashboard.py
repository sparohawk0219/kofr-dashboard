"""
Streamlit real-time dashboard — KOFR OIS Market Making
Run: streamlit run pipeline/dashboard.py  (from c:/projects/KOFR)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import datetime
import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]
import streamlit as st             # type: ignore[import-untyped]

from pipeline.config import (
    KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT, DB_PATH, REFRESH_SEC
)
from pipeline.fetcher import fetch_rates, mock_history, _PROVIDER, _bbg_last_ts
from pipeline.basis_engine import (
    forward_rates_table, flat_forward_buckets, solve_meeting_path, hike_probabilities,
    fwd_basis_stats, fwd_basis_history,
    scenario_par_rate, dv01,
    SETTLEMENT, BOK_RATE, BOK_MEETINGS, _TENOR_ENDS,
)
from pipeline.store import init_db, seed_history, clear_history, load_history as _load_local

# ── Supabase or local ─────────────────────────────────────────────────
_USE_CLOUD = False
try:
    if 'supabase' in st.secrets:
        from pipeline import cloud_store
        _USE_CLOUD = True
except Exception:
    pass

def load_history(days: int = 120):
    if _USE_CLOUD:
        return cloud_store.load_history(days)
    return _load_local(DB_PATH, days)

st.set_page_config(page_title='KOFR OIS Market Making', layout='wide', page_icon='💹')


if not _USE_CLOUD:
    init_db(DB_PATH)

@st.cache_resource
def _seed_once():
    if not _USE_CLOUD and _PROVIDER != 'bloomberg':
        seed_history(DB_PATH, mock_history(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT, days=120))
    return True

_seed_once()

# ── fetch ──────────────────────────────────────────────────────────────
if _USE_CLOUD:
    _cloud_kofr, _cloud_cd = cloud_store.load_latest_rates()
    if _cloud_kofr and _cloud_cd:
        kofr, cd = _cloud_kofr, _cloud_cd
        src = '🟢 Bloomberg (Supabase)'
    else:
        kofr, cd = fetch_rates(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT)
        src = '🟡 Mock (Supabase 데이터 없음)'
else:
    import time as _t
    kofr, cd = fetch_rates(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT)
    if _PROVIDER == 'bloomberg':
        age   = int(_t.time() - _bbg_last_ts) if _bbg_last_ts else None
        stale = f' ⚠️{age}s stale' if age and age > 60 else ''
        src   = f'🟢 Bloomberg{stale}'
    else:
        src = '🟡 Mock' if _PROVIDER == 'mock' else '🔵 Infomax'

fwd_rows   = forward_rates_table(kofr, cd)
anchor_bps = fwd_rows[0]['basis_bps'] if fwd_rows else 19.5

# ── last update (cloud) ────────────────────────────────────────────────
_last_upd_str = ''
if _USE_CLOUD:
    _lu = cloud_store.last_update()
    _last_upd_str = f'  |  최종업데이트 **{_lu[:16]}**' if _lu else '  |  데이터 없음'

st.title('💹  KOFR OIS Market Making')

_hdr_left, _hdr_right = st.columns([8, 2])
with _hdr_left:
    st.caption(
        f'{src}  |  {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  '
        f'|  BOK 기준금리 2.50%  |  신용앵커 (Spot-3M) **{anchor_bps:.2f}bp**'
        f'{_last_upd_str}'
    )
with _hdr_right:
    if _USE_CLOUD:
        if st.button('🔄 새로고침 요청', use_container_width=True):
            cloud_store.request_refresh()
            st.info('Bloomberg PC에 요청 전송 — 약 1분 내 업데이트됩니다.')

tab1, tab2, tab3 = st.tabs(['📊 베이시스 트레이딩', '🎯 금통위 시나리오', '💰 캐리 분석'])

# ══════════════════════════════════════════════════════════════════════
# TAB 1 — 베이시스 트레이딩
# ══════════════════════════════════════════════════════════════════════
with tab1:
    hist_df = load_history(days=400)
    col_main, col_rates = st.columns([7, 3])

    # ── RIGHT: live rates ──────────────────────────────────────────────
    with col_rates:
        st.subheader('실시간 금리')
        _ord = {'1W': 0.02, '2W': 0.04, '1M': 1, '2M': 2, '3M': 3,
                '6M': 6, '9M': 9, '1Y': 12, '2Y': 24, '3Y': 36, '5Y': 60}
        all_t = sorted(set(kofr) | set(cd), key=lambda x: _ord.get(x, 99))
        rate_rows = []
        for t in all_t:
            k = kofr.get(t)
            c = cd.get(t)
            b = round((c - k) * 100, 1) if k and c else None
            rate_rows.append({
                'Tenor': t,
                'KOFR':  f'{k:.3f}' if k else '—',
                'CD':    f'{c:.3f}' if c else '—',
                'Basis': f'{b:+.1f}' if b is not None else '—',
            })
        st.dataframe(pd.DataFrame(rate_rows).set_index('Tenor'),
                     use_container_width=True, height=340)

    # ── LEFT ───────────────────────────────────────────────────────────
    with col_main:
        HALF_SPREAD = 1.0
        RECV_THR    = 1.0   # 수취베이시스: 캐리 +3bp → 낮은 진입 기준
        PAY_THR     = 2.5   # 지급베이시스: 캐리 -3bp, 역사적 상위 11% 기준

        # ── 테너별 Fair Basis: 현재 Spot-3M 앵커 (= 순수 크레딧 프리미엄)
        # 모든 테너에 동일 적용 — 정책경로 기대 차이는 signal gap으로 포착
        _basis_cols = {'6M': 'basis_6m', '9M': 'basis_9m',
                       '1Y': 'basis_1y', '2Y': 'basis_2y', '3Y': 'basis_3y'}
        fair_basis: dict = {_t: anchor_bps for _t in _basis_cols}

        # ── 1. KOFR OIS 호가 테이블 ───────────────────────────────────
        st.subheader('KOFR OIS 호가 — Fair Value 기준')

        MM_TENORS = ['6M', '9M', '1Y', '2Y', '3Y']
        mm_rows = []
        for t in MM_TENORS:
            k = kofr.get(t)
            c = cd.get(t)
            if k is None or c is None:
                continue
            fb   = fair_basis.get(t, anchor_bps)
            fair = round(c - fb / 100, 4)
            bid  = round(fair - HALF_SPREAD / 100, 4)
            ask  = round(fair + HALF_SPREAD / 100, 4)
            gap  = round((k - fair) * 100, 2)

            if gap > RECV_THR:
                signal = '🔴 수취베이시스'
            elif gap < -PAY_THR:
                signal = '🔵 지급베이시스'
            else:
                signal = '⚪ 관망'

            mm_rows.append({
                'Tenor':      t,
                'CD IRS':     f'{c:.4f}%',
                'Fair basis': f'{fb:.1f}bp',
                'KOFR Fair':  f'{fair:.4f}%',
                'KOFR Mkt':   f'{k:.4f}%',
                'Gap':        f'{gap:+.2f}bp',
                'Bid':        f'{bid:.4f}%',
                'Ask':        f'{ask:.4f}%',
                '시그널':     signal,
                '_gap':       gap,
            })

        def _row_color(row):
            g = row['_gap']
            if g > RECV_THR:
                return ['background-color: #fff0f0'] * len(row)
            elif g < -PAY_THR:
                return ['background-color: #f0f0ff'] * len(row)
            return [''] * len(row)

        df_mm = pd.DataFrame(mm_rows)
        display_cols = ['Tenor', 'CD IRS', 'Fair basis', 'KOFR Fair', 'KOFR Mkt', 'Gap', 'Bid', 'Ask', '시그널']
        st.dataframe(
            df_mm[display_cols + ['_gap']].style.apply(_row_color, axis=1),
            use_container_width=True, height=212, hide_index=True,
            column_config={'_gap': None},
        )
        st.caption(
            f'Fair basis = Spot-3M 앵커 {anchor_bps:.1f}bp  '
            f'🔴 수취: gap>+{RECV_THR}bp (캐리 +3bp)  '
            f'🔵 지급: gap<−{PAY_THR}bp (역사적 상위 11%, 캐리 -3bp)'
        )

        # ── 2. Gap 차트 + Basis 커브 ──────────────────────────────────
        c1, c2 = st.columns(2)

        with c1:
            st.subheader('Gap (시장KOFR − Fair)')
            tenors = [r['Tenor'] for r in mm_rows]
            gaps   = [r['_gap']  for r in mm_rows]
            colors = ['tomato' if g > RECV_THR else 'royalblue' if g < -PAY_THR
                      else 'lightgray' for g in gaps]
            fig_gap = go.Figure(go.Bar(
                x=tenors, y=gaps,
                marker_color=colors,
                text=[f'{g:+.2f}bp' for g in gaps],
                textposition='outside',
            ))
            fig_gap.add_hline(y= RECV_THR, line_dash='dot', line_color='tomato',
                              annotation_text='수취', annotation_font_size=10)
            fig_gap.add_hline(y=-PAY_THR, line_dash='dot', line_color='royalblue',
                              annotation_text='지급', annotation_font_size=10)
            fig_gap.add_hline(y=0, line_color='gray', line_width=1)
            fig_gap.update_layout(height=200, margin=dict(l=10, r=10, t=20, b=10),
                                   yaxis_title='bps', showlegend=False)
            st.plotly_chart(fig_gap, use_container_width=True)

        with c2:
            st.subheader('Spot Basis 커브 vs 앵커')
            bases = [round((cd.get(t, 0) - kofr.get(t, 0)) * 100, 2) for t in tenors]
            fig_basis = go.Figure()
            fig_basis.add_trace(go.Scatter(
                x=tenors, y=bases, mode='lines+markers',
                name='Spot Basis', line=dict(color='steelblue', width=2),
                text=[f'{b:.1f}bp' for b in bases], textposition='top center',
            ))
            fig_basis.add_hline(y=anchor_bps, line_dash='dash', line_color='darkorange',
                                 line_width=2,
                                 annotation_text=f'앵커 {anchor_bps:.1f}bp',
                                 annotation_font_size=11)
            fig_basis.update_layout(height=200, margin=dict(l=10, r=10, t=20, b=10),
                                     yaxis_title='bps', showlegend=False)
            st.plotly_chart(fig_basis, use_container_width=True)

        # ── 3. BOK 금통위 경로 ────────────────────────────────────────
        st.subheader('BOK 금통위 경로 — KOFR OIS 내재 정책금리')
        tenor_order = [t for t in ['1M', '2M', '3M', '6M', '9M', '1Y'] if t in kofr]
        buckets = flat_forward_buckets(kofr, tenor_order)
        rows    = solve_meeting_path(buckets, 2.50)
        hp      = hike_probabilities(kofr)

        if rows:
            sx = ([pd.Timestamp('2026-05-19')]
                  + [pd.Timestamp(r['date']) for r in rows]
                  + [pd.Timestamp('2027-05-25')])
            sy = [2.50] + [r['rate_after'] for r in rows] + [rows[-1]['rate_after']]
            fig_bok = go.Figure()
            fig_bok.add_trace(go.Scatter(
                x=sx, y=sy, mode='lines', name='KOFR OIS implied',
                line=dict(shape='hv', color='darkorange', width=2.5)
            ))
            fig_bok.add_hline(y=2.50, line_dash='dash', line_color='gray',
                              annotation_text='기준금리 2.50%',
                              annotation_position='bottom right', annotation_font_size=11)
            for r in rows:
                fig_bok.add_vline(x=pd.Timestamp(r['date']).timestamp() * 1000,
                                  line_dash='dot', line_color='red', opacity=0.4)
                fig_bok.add_annotation(
                    x=pd.Timestamp(r['date']), y=r['rate_after'] + 0.04,
                    text=f"{r['rate_after']:.3f}%",
                    showarrow=False, font=dict(size=11, color='darkorange'), yref='y',
                )
                fig_bok.add_annotation(
                    x=pd.Timestamp(r['date']), y=0, yref='paper',
                    text=pd.Timestamp(r['date']).strftime('%m/%d'),
                    showarrow=False, font=dict(size=10, color='tomato'),
                    textangle=-45, yshift=-2,
                )
            fig_bok.update_layout(height=200, margin=dict(l=10, r=10, t=10, b=40),
                                   yaxis_title='Rate (%)', showlegend=False)
            st.plotly_chart(fig_bok, use_container_width=True)

            if hp:
                prob_cols = st.columns(min(len(hp), 6))
                for i, h in enumerate(hp[:6]):
                    d   = pd.Timestamp(h['date']).strftime('%m/%d')
                    cut = h['cut_prob']
                    hik = h['hike_prob']
                    if cut > hik:
                        prob_cols[i].metric(d, f'인하 {cut:.0f}%', delta_color='inverse')
                    else:
                        prob_cols[i].metric(d, f'인상 {hik:.0f}%', delta_color='normal')

        # ── 4. 포워드 분석 (collapsed) ───────────────────────────────
        stats = fwd_basis_stats(hist_df, fwd_rows)

        with st.expander('포워드 베이시스 텀프리미엄 분석', expanded=False):
            btn_col, _ = st.columns([2, 5])
            with btn_col:
                if not _USE_CLOUD and st.button('Bloomberg 히스토리 업데이트 (1년)', type='primary'):
                    with st.spinner('Bloomberg BDH 로딩 중...'):
                        from pipeline.fetcher import fetch_history
                        records = fetch_history(days=365)
                        clear_history(DB_PATH)
                        seed_history(DB_PATH, records, force=True)
                    st.success(f'{len(records)}일 로딩 완료')
                    st.rerun()

            if stats:
                stat_rows = []
                for s in stats:
                    stat_rows.append({
                        '기간':         s['period'],
                        '현재(bp)':     f"{s['current']:.2f}"  if s['current']  is not None else '—',
                        '평균(bp)':     f"{s['mean']:.2f}",
                        '표준편차(bp)': f"{s['std']:.2f}",
                        '범위':         f"{s['min']:.1f}~{s['max']:.1f}",
                        '퍼센타일':     f"{s['pct_rank']:.0f}%" if s['pct_rank'] is not None else '—',
                        'z-score':      f"{s['z']:+.2f}"        if s['z']        is not None else '—',
                    })
                st.dataframe(pd.DataFrame(stat_rows), hide_index=True, use_container_width=True)

                sp3m  = next((s for s in stats if s['period'] == 'Spot-3M'), None)
                f3m6m = next((s for s in stats if s['period'] == '3M-6M'),   None)
                f6m9m = next((s for s in stats if s['period'] == '6M-9M'),   None)
                f9m1y = next((s for s in stats if s['period'] == '9M-1Y'),   None)
                if sp3m and f3m6m:
                    mc1, mc2, mc3 = st.columns(3)
                    mc1.metric('텀프리미엄 3M-6M vs Spot-3M',
                               f'{round(f3m6m["mean"] - sp3m["mean"], 2):+.2f}bp')
                    if f6m9m:
                        mc2.metric('텀프리미엄 6M-9M vs Spot-3M',
                                   f'{round(f6m9m["mean"] - sp3m["mean"], 2):+.2f}bp')
                    if f9m1y:
                        mc3.metric('텀프리미엄 9M-1Y vs Spot-3M',
                                   f'{round(f9m1y["mean"] - sp3m["mean"], 2):+.2f}bp')

                fwd_hist = fwd_basis_history(hist_df)
                if not fwd_hist.empty and 'ts' in fwd_hist.columns:
                    fig_hp = go.Figure()
                    hp_colors = {'Spot-3M': 'steelblue', '3M-6M': 'darkorange',
                                 '6M-9M': 'mediumseagreen', '9M-1Y': 'orchid'}
                    for col in ['Spot-3M', '3M-6M', '6M-9M', '9M-1Y']:
                        if col in fwd_hist.columns:
                            fig_hp.add_trace(go.Scatter(
                                x=fwd_hist['ts'], y=fwd_hist[col],
                                name=col, mode='lines',
                                line=dict(color=hp_colors.get(col), width=1.5),
                            ))
                    fig_hp.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10),
                                          yaxis_title='bps', legend=dict(orientation='h'))
                    st.plotly_chart(fig_hp, use_container_width=True)
            else:
                st.info('히스토리 데이터 없음 — Bloomberg 연결 후 위 버튼 클릭')

            with st.expander('포워드 버킷 분해 (내부 분석용)', expanded=False):
                if fwd_rows:
                    fd_cols = ['period', 'fwd_kofr', 'fwd_cd', 'basis_bps']
                    df_fwd  = pd.DataFrame(fwd_rows)[fd_cols].copy()
                    df_fwd['vs 앵커'] = (df_fwd['basis_bps'] - anchor_bps).round(2)
                    df_fwd.columns    = ['기간', 'fwd KOFR(%)', 'fwd CD(%)', '베이시스(bp)', 'vs앵커(bp)']
                    st.dataframe(df_fwd, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════
# TAB 2 — 금통위 시나리오
# ══════════════════════════════════════════════════════════════════════
with tab2:
    st.subheader('금통위 시나리오 — KOFR OIS 기대금리 & 손익')

    t2_hist = load_history(days=400)
    on_series = t2_hist['kofr_on'].dropna() if 'kofr_on' in t2_hist.columns else pd.Series(dtype=float)

    # ── KOFR ON basis (collapsed) ─────────────────────────────────────
    with st.expander('KOFR overnight vs 기준금리 basis (조정 참고용)', expanded=False):
        upd_col, diag_col = st.columns([2, 5])
        with upd_col:
            if not _USE_CLOUD and st.button('Bloomberg 히스토리 업데이트', key='upd_tab2'):
                with st.spinner('Bloomberg BDH 로딩 중...'):
                    from pipeline.fetcher import fetch_history
                    records = fetch_history(days=365)
                    clear_history(DB_PATH)
                    seed_history(DB_PATH, records, force=True)
                st.success(f'{len(records)}일 로딩 완료')
                st.rerun()
        with diag_col:
            n_total = len(t2_hist)
            n_on    = int(t2_hist['kofr_on'].notna().sum()) if 'kofr_on' in t2_hist.columns else 0
            st.caption(f'DB: 총 {n_total}일  |  KOFR ON 유효: {n_on}일')

        if not on_series.empty:
            on_basis = (on_series - BOK_RATE) * 100
            m60      = on_basis.tail(60)
            on_mean  = round(float(m60.mean()), 2)
            on_std   = round(float(m60.std()),  2)
            on_last  = round(float(on_basis.iloc[-1]), 2)

            oc1, oc2, oc3 = st.columns(3)
            oc1.metric('60일 평균 (KOFR ON − 기준금리)', f'{on_mean:+.2f}bp')
            oc2.metric('표준편차', f'{on_std:.2f}bp')
            oc3.metric('최근값', f'{on_last:+.2f}bp')

            fig_on = go.Figure()
            fig_on.add_trace(go.Scatter(
                x=t2_hist.loc[on_series.index, 'ts'] if 'ts' in t2_hist.columns else list(range(len(on_basis))),
                y=on_basis.values,
                mode='lines', name='KOFR ON − 기준금리',
                line=dict(color='steelblue', width=1.5),
            ))
            fig_on.add_hline(y=on_mean, line_dash='dash', line_color='darkorange',
                             annotation_text=f'60일평균 {on_mean:+.2f}bp', annotation_font_size=10)
            fig_on.add_hline(y=0, line_color='gray', line_width=1)
            fig_on.update_layout(height=180, margin=dict(l=10, r=10, t=10, b=10),
                                  yaxis_title='bps', showlegend=False)
            st.plotly_chart(fig_on, use_container_width=True)
            default_adj = on_mean
        else:
            st.info('Bloomberg 히스토리 업데이트 필요')
            default_adj = 0.0

    # ── 입력 컨트롤 ─────────────────────────────────────────────────
    sc1, sc2 = st.columns([3, 7])

    with sc1:
        tenor = st.radio('테너', ['1M', '2M', '3M', '6M', '9M', '1Y'], horizontal=True)
        entry_rate = st.number_input(
            '진입금리 (%)',
            value=float(kofr.get(tenor, 2.50)),
            step=0.001, format='%.4f',
        )
        direction = st.radio('포지션', ['Receive', 'Pay'], horizontal=True)
        default_adj = on_mean if not on_series.empty else 0.0
        basis_adj = st.number_input(
            'KOFR ON basis 조정 (bp)',
            value=float(default_adj),
            step=0.5, format='%.2f',
        )

    with sc2:
        end_date = _TENOR_ENDS.get(tenor)
        mtgs     = [m for m in BOK_MEETINGS if SETTLEMENT < m <= end_date] if end_date else []

        st.markdown('**금통위별 시나리오 (bp)**')
        delta_opts = [-50, -25, 0, 25, 50]
        fmt_delta  = {-50: '−50', -25: '−25', 0: '동결', 25: '+25', 50: '+50'}
        mtg_cols   = st.columns(len(mtgs)) if mtgs else []
        meeting_deltas = {}
        for i, m in enumerate(mtgs):
            d = mtg_cols[i].select_slider(
                m.strftime('%m/%d'),
                options=delta_opts,
                value=0,
                format_func=lambda x: fmt_delta[x],
                key=f'delta_{m}',
            )
            meeting_deltas[m] = d

    # ── 계산 결과 ────────────────────────────────────────────────────
    NOTIONAL  = 10e9
    scen_rate = scenario_par_rate(tenor, meeting_deltas, basis_adj_bps=basis_adj)
    diff_bp   = round((scen_rate - entry_rate) * 100, 2)
    sign      = -1 if direction == 'Receive' else 1
    pnl_bp    = round(diff_bp * sign, 2)
    dv01_val  = dv01(tenor, notional=NOTIONAL)
    pnl_krw   = round(pnl_bp * dv01_val / 1e6, 1)

    r1, r2, r3, r4 = st.columns(4)
    r1.metric('진입금리', f'{entry_rate:.4f}%')
    r2.metric(f'시나리오 금리 (ON {basis_adj:+.1f}bp)',
              f'{scen_rate:.4f}%', delta=f'{diff_bp:+.2f}bp')
    r3.metric(f'손익 ({direction})', f'{pnl_bp:+.2f}bp',
              delta='이익' if pnl_bp > 0 else ('손실' if pnl_bp < 0 else '—'),
              delta_color='normal' if pnl_bp >= 0 else 'inverse')
    r4.metric(f'P&L (100억, DV01={dv01_val/1e6:.2f}M/bp)',
              f'{pnl_krw:+.1f}백만원',
              delta_color='normal' if pnl_krw >= 0 else 'inverse')

    # ── 정책금리 경로 ────────────────────────────────────────────────
    st.markdown('**시나리오 vs 시장내재 정책금리 경로**')

    end_d    = _TENOR_ENDS.get(tenor)
    all_mtgs = [m for m in BOK_MEETINGS if SETTLEMENT < m <= end_d] if end_d else []

    path_dates = [SETTLEMENT]
    path_rates = [BOK_RATE]
    cur_rate   = BOK_RATE
    for m in all_mtgs:
        cur_rate += meeting_deltas.get(m, 0) / 100
        path_dates.append(m)
        path_rates.append(cur_rate)
    if end_d:
        path_dates.append(end_d)
        path_rates.append(cur_rate)

    mkt_rows  = solve_meeting_path(
        flat_forward_buckets(kofr, [t for t in ['1M','2M','3M','6M','9M','1Y'] if t in kofr]),
        BOK_RATE
    )
    mkt_dates = [SETTLEMENT] + [r['date'] for r in mkt_rows] + [datetime.date(2027, 5, 25)]
    mkt_rates = [BOK_RATE] + [r['rate_after'] for r in mkt_rows] + \
                ([mkt_rows[-1]['rate_after']] if mkt_rows else [BOK_RATE])

    fig_sc = go.Figure()
    fig_sc.add_trace(go.Scatter(
        x=[pd.Timestamp(d) for d in path_dates], y=path_rates,
        mode='lines', name='시나리오',
        line=dict(color='tomato', width=2.5, shape='hv'),
    ))
    fig_sc.add_trace(go.Scatter(
        x=[pd.Timestamp(d) for d in mkt_dates], y=mkt_rates,
        mode='lines', name='시장내재',
        line=dict(color='steelblue', width=1.5, dash='dot', shape='hv'),
    ))
    fig_sc.add_hline(y=BOK_RATE, line_dash='dash', line_color='gray',
                     annotation_text=f'기준금리 {BOK_RATE:.2f}%',
                     annotation_position='bottom right', annotation_font_size=10)
    if end_d:
        fig_sc.add_vline(x=pd.Timestamp(end_d).timestamp() * 1000,
                         line_dash='dot', line_color='green', opacity=0.5,
                         annotation_text=f'{tenor} 만기', annotation_font_size=10)
    fig_sc.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10),
                          yaxis_title='Rate (%)', legend=dict(orientation='h'))
    st.plotly_chart(fig_sc, use_container_width=True)

    with st.expander('금통위별 시나리오 상세'):
        cum, detail_rows = BOK_RATE, []
        for m in all_mtgs:
            d   = meeting_deltas.get(m, 0)
            cum += d / 100
            detail_rows.append({
                '날짜':             m.strftime('%Y-%m-%d'),
                '변화(bp)':         f'{d:+d}' if d != 0 else '동결',
                '정책금리':         f'{cum:.4f}%',
                'KOFR ON 조정 포함': f'{cum + basis_adj/100:.4f}%',
            })
        st.dataframe(pd.DataFrame(detail_rows), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# TAB 3 — 캐리 분석
# ══════════════════════════════════════════════════════════════════════
with tab3:
    st.subheader('캐리 분석 — Receive CD IRS / Pay KOFR OIS (지급베이시스)')

    t3_hist    = load_history(days=60)
    on_series3 = t3_hist['kofr_on'].dropna() if 'kofr_on' in t3_hist.columns else pd.Series(dtype=float)
    kofr_on_db = float(on_series3.iloc[-1]) if not on_series3.empty else (BOK_RATE + 0.02)

    t3c1, t3c2 = st.columns([3, 7])
    with t3c1:
        kofr_on_input = st.number_input(
            'KOFR ON (%)',
            value=kofr_on_db,
            step=0.001, format='%.4f',
            help='Bloomberg KRFRRATE Index. DB 미연결 시 수동 입력',
        )
    with t3c2:
        st.caption(
            f'Bloomberg DB 최근값: **{kofr_on_db:.4f}%**  '
            f'(기준금리 {BOK_RATE:.2f}% {(kofr_on_db - BOK_RATE)*100:+.1f}bp)  '
            f'|  3M CD: **{cd.get("3M", 0):.3f}%**'
        )

    cd_3m = cd.get('3M')

    # ── 캐리 + 롤다운 계산 함수 ─────────────────────────────────────
    _TENOR_DAYS  = {'6M': 184, '1Y': 365}
    _ROLL_TENOR  = {'6M': '3M', '1Y': '9M'}   # 3M 보유 후 남는 테너
    _ROLL_DAYS   = {'6M': 92,   '1Y': 275}
    HOLD_DAYS    = 92                           # 3개월 보유 가정

    def _carry(tenor_str: str):
        days      = _TENOR_DAYS[tenor_str]
        kofr_fix  = kofr.get(tenor_str)
        cd_fix    = cd.get(tenor_str)
        if kofr_fix is None or cd_fix is None or cd_3m is None:
            return None

        # KOFR ON daily compounding → 동일기간 연환산
        kofr_on_daily   = kofr_on_input / 100 / 365
        kofr_compounded = ((1 + kofr_on_daily) ** days - 1) * (365 / days) * 100

        # 캐리 분해
        fixed_bps      = (cd_fix - kofr_fix) * 100
        float_drag_bps = (kofr_compounded - cd_3m) * 100
        net_carry_yr   = fixed_bps + float_drag_bps
        net_carry_hold = net_carry_yr * days / 365

        # 롤다운 — 3M 보유 후 남은 테너 basis로 MTM 평가
        # 모두 entry DV01 기준 bp로 환산: gain × remaining_days / entry_days
        roll_t     = _ROLL_TENOR[tenor_str]
        rem_days   = _ROLL_DAYS[tenor_str]
        roll_kofr  = kofr.get(roll_t)
        roll_cd    = cd.get(roll_t)
        entry_basis = fixed_bps  # = (cd_fix − kofr_fix) × 100

        if roll_kofr is not None and roll_cd is not None:
            shorter_basis  = (roll_cd - roll_kofr) * 100
            rolldown_bp    = (entry_basis - shorter_basis) * rem_days / days
            carry_3m_bp    = net_carry_yr * HOLD_DAYS / days
            net_3m_bp      = rolldown_bp + carry_3m_bp
        else:
            shorter_basis  = rolldown_bp = carry_3m_bp = net_3m_bp = None

        return dict(
            kofr_fix=kofr_fix, cd_fix=cd_fix,
            kofr_compounded=kofr_compounded,
            compounding_bp=(kofr_compounded - kofr_on_input) * 100,
            fixed_bps=fixed_bps,
            float_drag_bps=float_drag_bps,
            net_carry_yr=net_carry_yr,
            net_carry_hold=net_carry_hold,
            days=days,
            roll_t=roll_t, rem_days=rem_days,
            entry_basis=entry_basis,
            shorter_basis=shorter_basis,
            rolldown_bp=rolldown_bp,
            carry_3m_bp=carry_3m_bp,
            net_3m_bp=net_3m_bp,
        )

    # ── 두 테너 나란히 ───────────────────────────────────────────────
    col6m, col1y = st.columns(2)

    for col, tenor_str, label in [(col6m, '6M', '6개월'), (col1y, '1Y', '1년')]:
        res = _carry(tenor_str)
        with col:
            st.markdown(f'#### {tenor_str} ({res["days"]}일)' if res else f'#### {tenor_str}')
            if res is None:
                st.warning('금리 데이터 없음')
                continue

            # 금리 구성 표
            rate_rows = [
                {'항목': '▲ CD IRS 수취 (고정)',   '금리': f'{res["cd_fix"]:.4f}%'},
                {'항목': '▼ KOFR OIS 지급 (고정)',  '금리': f'{res["kofr_fix"]:.4f}%'},
                {'항목': '▲ KOFR ON 복리수취',      '금리': f'{res["kofr_compounded"]:.4f}%'},
                {'항목': '▼ 3M CD 지급 (변동)',      '금리': f'{cd_3m:.4f}%' if cd_3m else '—'},
            ]
            st.dataframe(pd.DataFrame(rate_rows).set_index('항목'),
                         use_container_width=True, height=176)

            # ── 캐리 ─────────────────────────────────────────────────
            st.markdown('**캐리**')
            ma, mb = st.columns(2)
            ma.metric('고정 수익 (CD−KOFR)', f'{res["fixed_bps"]:+.2f}bp/yr')
            mb.metric('변동 비용 (KOFR ON−3M CD)', f'{res["float_drag_bps"]:+.2f}bp/yr')

            mc, md = st.columns(2)
            dc = 'normal' if res['net_carry_yr'] >= 0 else 'inverse'
            mc.metric('순 캐리 (연환산)', f'{res["net_carry_yr"]:+.2f}bp/yr', delta_color=dc)
            md.metric(f'순 캐리 ({label})', f'{res["net_carry_hold"]:+.2f}bp', delta_color=dc)

            st.caption(
                f'복리효과: ON {kofr_on_input:.4f}% → {res["kofr_compounded"]:.4f}% '
                f'(+{res["compounding_bp"]:.2f}bp)'
            )

            # ── 롤다운 (3M 보유, entry DV01 기준 bp) ────────────────
            st.markdown('**롤다운 — 3M 보유 기준** *(entry DV01 환산 bp)*')
            if res['rolldown_bp'] is not None:
                ra, rb, rc = st.columns(3)
                ra.metric(
                    f'롤다운 ({tenor_str}→{res["roll_t"]})',
                    f'{res["rolldown_bp"]:+.2f}bp',
                    help=f'entry {res["entry_basis"]:.1f}bp → {res["roll_t"]} {res["shorter_basis"]:.1f}bp',
                )
                rb.metric('캐리 (3M)', f'{res["carry_3m_bp"]:+.2f}bp')
                dc2 = 'normal' if res['net_3m_bp'] >= 0 else 'inverse'
                rc.metric('합계 (3M)', f'{res["net_3m_bp"]:+.2f}bp', delta_color=dc2)
                st.caption(
                    f'베이시스 커브: {res["roll_t"]} {res["shorter_basis"]:.1f}bp  →  '
                    f'{tenor_str} {res["entry_basis"]:.1f}bp  '
                    f'(커브 기울기 {res["entry_basis"]-res["shorter_basis"]:+.1f}bp)'
                )

    st.divider()
    st.markdown(
        '**해석**  지급베이시스(Receive CD IRS / Pay KOFR OIS)\n\n'
        '- **고정 수익(+)**: CD IRS 고정 수취 > KOFR OIS 고정 지급 → 스프레드 수익\n'
        '- **변동 비용(−)**: KOFR ON 복리 수취 < 3M CD 변동 지급 이므로 순비용 발생 (현재 KOFR ON < CD)\n'
        '- 순 캐리가 음수면 수렴(베이시스 축소) 트레이딩: 베이시스가 줄어야 이익\n'
        '- CD 크레딧 리스크: 3M마다 CD 픽싱 재노출, 스프레드 급등 시 변동 비용 확대'
    )


# ── auto-refresh ───────────────────────────────────────────────────────
if not _USE_CLOUD:
    st.caption(f'Next refresh in {REFRESH_SEC}s')
    time.sleep(REFRESH_SEC)
    st.rerun()
