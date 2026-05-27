"""
Bloomberg PC 전용 스케줄러.
- 09:00 / 15:30 / 16:30 자동 push
- 1분마다 refresh_requests 체크 (장 중에만)

실행: python -m pipeline.pusher
환경변수 필요:
  BBG_ENABLED=1
  SUPABASE_URL=https://xxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=eyJ...
"""
import os
import sys
import time
import datetime
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pusher.log', encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

from pipeline.config import KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT
from pipeline.fetcher import fetch_rates
from pipeline.basis_engine import forward_rates_table
from pipeline import cloud_store


def push_snapshot():
    log.info('Bloomberg 데이터 pull 시작')
    try:
        kofr, cd = fetch_rates(KOFR_RATES_SNAPSHOT, CD_RATES_SNAPSHOT)
        basis = {t: round((cd[t] - kofr[t]) * 100, 4)
                 for t in ['3M', '6M', '9M', '1Y', '2Y', '3Y']
                 if t in kofr and t in cd}
        cloud_store.save_tick(kofr, cd, basis, {}, [])
        log.info(f'push 완료 — basis 6M: {basis.get("6M")}bp')
    except Exception as e:
        log.error(f'push 실패: {e}')


def is_market_hours() -> bool:
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(8, 50) <= t <= datetime.time(16, 40)


def main():
    log.info('pusher 시작')

    SCHEDULED = ['09:00', '15:30', '16:30']
    last_run: dict[str, str] = {}

    while True:
        now = datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        hm    = now.strftime('%H:%M')

        # 스케줄 push
        for t in SCHEDULED:
            key = f'{today}_{t}'
            if hm == t and last_run.get(t) != key:
                last_run[t] = key
                push_snapshot()

        # 수동 refresh 요청 체크 (장 중에만)
        if is_market_hours():
            try:
                if cloud_store.pop_refresh_request():
                    log.info('수동 refresh 요청 감지')
                    push_snapshot()
            except Exception as e:
                log.warning(f'refresh 체크 실패: {e}')

        time.sleep(60)


if __name__ == '__main__':
    main()
