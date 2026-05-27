"""
시스템 구조 및 운영 가이드
"""
import streamlit as st

st.title('🏗️  시스템 구조 및 운영 가이드')
st.caption('개발 이력 및 내부 운영 참고용')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('1.  전체 구조')

st.markdown('''
세 개의 컴포넌트가 각자 역할을 나눠서 동작한다.

```
┌─────────────────────┐        push (3회/일 + 요청 시)        ┌──────────────┐
│   Bloomberg PC      │ ──────────────────────────────────→ │   Supabase   │
│                     │                                      │  (PostgreSQL │
│ pipeline/pusher.py  │ ←────────────────────────────────── │   클라우드)  │
│ (상시 실행)         │     refresh_requests 폴링 (60초)     └──────┬───────┘
└─────────────────────┘                                            │ read
                                                                   ↓
                                                     ┌─────────────────────────┐
                                                     │   Streamlit Cloud       │
                                                     │   kofrdashboard.        │
                                                     │   streamlit.app         │
                                                     │                         │
                                                     │  새로고침 요청 버튼     │
                                                     │  → refresh_requests에   │
                                                     │    insert               │
                                                     └─────────────────────────┘
```

**Bloomberg PC** — 실제 Bloomberg Terminal이 연결된 PC (로컬)
- 금리 데이터를 Bloomberg에서 가져와 Supabase에 저장
- 하루 3회 자동 (09:00, 15:30, 16:30) + 앱 요청 시 1분 내

**Supabase** — 클라우드 PostgreSQL 데이터베이스
- Bloomberg PC와 Streamlit Cloud 사이의 중간 저장소
- Bloomberg Terminal이 없는 클라우드 환경에서 데이터를 읽을 수 있게 해줌

**Streamlit Cloud** — 대시보드 호스팅
- `kofrdashboard.streamlit.app`
- Supabase에서 데이터 읽어서 표시
- 90초마다 자동 갱신
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('2.  파일 구조')

st.markdown('''
```
c:/projects/KOFR/
│
├── pipeline/
│   ├── dashboard.py        ← Streamlit 대시보드 (메인 앱)
│   ├── pusher.py           ← Bloomberg PC 상시 실행 스케줄러
│   ├── cloud_store.py      ← Supabase 읽기/쓰기
│   ├── fetcher.py          ← Bloomberg / Mock 데이터 fetch
│   ├── basis_engine.py     ← 포워드 금리, 시나리오, DV01 계산
│   ├── config.py           ← Bloomberg 티커, 신호 임계값, 설정
│   ├── store.py            ← 로컬 SQLite 읽기/쓰기 (로컬 전용)
│   ├── signals.py          ← 시그널 생성 로직
│   ├── last_price.json     ← Bloomberg 마지막 스냅샷 캐시 (disk)
│   │
│   └── pages/
│       ├── strategy_guide.py   ← 전략 설명 페이지
│       └── system_overview.py  ← 이 페이지
│
├── scripts/
│   ├── setup_env.ps1       ← 환경변수 영구 등록 (최초 1회)
│   ├── run_pusher.bat      ← pusher 실행 (Task Scheduler 등록용)
│   └── push_once.bat       ← 즉시 1회 push (수동용)
│
├── .streamlit/
│   └── secrets.toml        ← Supabase 접속 정보 (gitignore됨)
│
└── requirements.txt        ← Python 패키지 목록
```
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('3.  파일별 역할')

with st.expander('dashboard.py — 대시보드 메인', expanded=False):
    st.markdown('''
Streamlit 앱의 진입점. 세 개의 탭으로 구성.

**Tab 1 — 베이시스 트레이딩**
- KOFR OIS 호가 테이블: Fair Value 대비 Gap, 매수/매도 호가, 시그널
- Gap 차트, Spot Basis 커브 vs 앵커
- BOK 금통위 경로: KOFR OIS 내재 정책금리
- 포워드 베이시스 분석 (expander)

**Tab 2 — 금통위 시나리오**
- 금통위별 인상/인하 가정 입력
- 시나리오 금리 vs 진입금리 손익 계산 (DV01 기준)
- 시장내재 경로 vs 시나리오 경로 비교 차트

**Tab 3 — 캐리 분석**
- 6M, 1Y 테너별 캐리 분해 (고정 레그 + 변동 레그)
- 3개월 보유 기준 롤다운 계산

**클라우드/로컬 자동 전환:**
```python
_USE_CLOUD = False
if 'supabase' in st.secrets:          # Streamlit Cloud
    from pipeline import cloud_store
    _USE_CLOUD = True
```
Streamlit secrets에 `[supabase]` 섹션이 있으면 클라우드 모드,
없으면 로컬 SQLite + Bloomberg 직접 연결 모드.
''')

with st.expander('pusher.py — Bloomberg PC 스케줄러', expanded=False):
    st.markdown('''
Bloomberg PC에서 **상시 실행**하는 프로세스.
`scripts/run_pusher.bat`으로 실행, Task Scheduler에 등록.

**동작:**
1. 60초마다 루프 실행
2. 09:00 / 15:30 / 16:30 도달 시 → `push_snapshot()` 자동 실행
3. 장 중 (08:50~16:40) → Supabase `refresh_requests` 테이블 폴링
4. 미처리 요청 있으면 → `push_snapshot()` 실행 후 fulfilled 처리

**push_snapshot():**
- Bloomberg에서 KOFR OIS, CD IRS 전 테너 fetch
- Supabase `basis_history` 테이블에 upsert

**환경변수 필요:**
```
BBG_ENABLED=1
SUPABASE_URL=https://sxkrwlgggkcsfjqwyntb.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```
`scripts/setup_env.ps1` 로 영구 등록.
''')

with st.expander('cloud_store.py — Supabase 인터페이스', expanded=False):
    st.markdown('''
Supabase REST API 래퍼. pusher.py (쓰기) + dashboard.py (읽기) 모두 사용.

| 함수 | 용도 |
|------|------|
| `save_tick()` | Bloomberg 스냅샷 → basis_history upsert |
| `load_history(days)` | 히스토리 조회 → 차트용 DataFrame |
| `load_latest_rates()` | 최신 행 → kofr/cd dict (실시간 금리 표시용) |
| `request_refresh()` | 앱 → refresh_requests insert |
| `pop_refresh_request()` | pusher → 미처리 요청 확인 + fulfilled |
| `last_update()` | 최종 업데이트 시각 |

`_client()`: Streamlit secrets 또는 환경변수에서 접속 정보 로드.
''')

with st.expander('fetcher.py — Bloomberg 데이터 fetch', expanded=False):
    st.markdown('''
Bloomberg / Mock 데이터 소스 추상화.

**Provider 자동 감지:**
```python
_PROVIDER = (
    'bloomberg' if os.getenv('BBG_ENABLED') == '1' else
    'infomax'   if os.getenv('INFOMAX_API_KEY') else
    'mock'
)
```

**Bloomberg 스냅샷 (`_bbg_fetch_snapshot`):**
- `ReferenceDataRequest`로 전 테너 BID/ASK/PX_LAST 요청
- `last_price.json`에 캐시 저장 (프로세스 재시작 시 복원)
- Bloomberg 실패 시 캐시로 폴백

**중요:** `last_price.json`은 Bloomberg 마지막 성공 스냅샷.
Bloomberg 없이 pusher를 실행해도 캐시값으로 push 가능
(다만 신선도가 떨어질 수 있음).
''')

with st.expander('config.py — 설정값', expanded=False):
    st.markdown('''
**Bloomberg 티커:**
```python
KOFR_TICKERS = {
    '1W': 'KWKON1Z Curncy',
    '1M': 'KWKONA Curncy',
    '2M': 'KWKONB Curncy',
    '3M': 'KWKONC Curncy',
    '6M': 'KWKONF Curncy',
    ...
}
CD_TICKERS = {
    '3M': 'KWCDC Curncy',   # 3M CD rate
    '6M': 'KWSWOF Curncy',
    '1Y': 'KWSWO1 Curncy',
    ...
}
```

**신호 임계값 (dashboard.py에서 직접 설정):**
```python
RECV_THR = 1.0   # 수취베이시스: gap > +1.0bp
PAY_THR  = 2.5   # 지급베이시스: gap < -2.5bp
```

**BOK 금통위 일정:** `BOK_MEETINGS` 리스트 — 매년 갱신 필요.
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('4.  Supabase 테이블 구조')

st.markdown('''
**basis_history** — 금리 스냅샷 (pusher가 push할 때마다 insert)

| 컬럼 | 내용 |
|------|------|
| `ts` | 타임스탬프 (ISO 8601) |
| `kofr_on` | KOFR overnight (KRFRRATE Index) |
| `kofr_1w`, `kofr_1m`, `kofr_2m` | 단기 KOFR OIS |
| `kofr_3m` ~ `kofr_3y` | KOFR OIS 3M~3Y |
| `cd_3m` ~ `cd_3y` | CD IRS 3M~3Y |
| `basis_3m` ~ `basis_3y` | 베이시스 (CD−KOFR, bp) |

**refresh_requests** — 새로고침 요청 큐

| 컬럼 | 내용 |
|------|------|
| `id` | 자동증가 PK |
| `requested_at` | 요청 시각 |
| `fulfilled_at` | 처리 시각 (null이면 미처리) |

pusher가 60초마다 `fulfilled_at IS NULL` 행을 체크하고 처리.
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('5.  일상 운영 방법')

st.markdown('''
### Bloomberg PC 시작 시

1. Bloomberg Terminal 실행
2. `scripts\\push_once.bat` 더블클릭 → 현재 시세 즉시 반영
3. `scripts\\run_pusher.bat` 더블클릭 → 상시 실행 시작
   (또는 Task Scheduler에 등록하면 자동 시작)

### 앱에서 수동 갱신이 필요할 때

대시보드 우상단 **🔄 새로고침 요청** 버튼 → 약 1분 내 갱신
(pusher가 실행 중이어야 함)

### 앱 자동 갱신 주기

- 로컬 (Bloomberg PC 직접 실행): 30초
- 클라우드 (Streamlit Cloud): 90초

### 코드 변경 후 배포

```bash
git add .
git commit -m "변경 내용"
git push
```
GitHub push 후 Streamlit Cloud 자동 재배포 (약 1~2분 소요).

Bloomberg PC의 pusher는 **별도로 재시작** 해야 변경사항 반영.
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('6.  접속 정보')

st.markdown('''
| 항목 | 위치 |
|------|------|
| Streamlit 앱 | `kofrdashboard.streamlit.app` |
| GitHub 리포 | `github.com/sparohawk0219/kofr-dashboard` |
| Supabase 프로젝트 | `sxkrwlgggkcsfjqwyntb.supabase.co` |
| Streamlit Cloud secrets | 앱 → Manage app → Settings → Secrets |
| Bloomberg PC 환경변수 | `scripts/setup_env.ps1` 실행 |

**secrets.toml (로컬, gitignore됨):**
```
.streamlit/secrets.toml
```
Supabase URL, service_role_key 보관. 절대 GitHub에 올리지 말 것.
''')

# ──────────────────────────────────────────────────────────────
st.divider()
st.header('7.  주요 파라미터 변경 방법')

st.markdown('''
### 신호 임계값 변경

`pipeline/dashboard.py` → Tab 1 코드 상단:
```python
HALF_SPREAD = 1.0   # 호가 반스프레드 (bp)
RECV_THR    = 1.0   # 수취베이시스 진입 기준 (gap > +1.0bp)
PAY_THR     = 2.5   # 지급베이시스 진입 기준 (gap < -2.5bp)
```

### BOK 금통위 일정 변경

`pipeline/config.py` → `BOK_MEETINGS` 리스트 갱신.

### pusher 스케줄 변경

`pipeline/pusher.py` → `SCHEDULED` 리스트:
```python
SCHEDULED = ['09:00', '15:30', '16:30']
```

### Bloomberg 티커 변경

`pipeline/config.py` → `KOFR_TICKERS`, `CD_TICKERS` 딕셔너리.
''')

st.divider()
st.caption('KRW 금리 데스크 내부 참고용')
