"""
SQLite data store: persists every tick snapshot for z-score history.
"""
import sqlite3
import datetime
import pandas as pd
from pathlib import Path


def _conn(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(db_path: str):
    with _conn(db_path) as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS basis_history (
                ts        TEXT PRIMARY KEY,
                kofr_1y   REAL, kofr_2y REAL, kofr_3y REAL,
                cd_1y     REAL, cd_2y   REAL, cd_3y   REAL,
                basis_1y  REAL, basis_2y REAL, basis_3y REAL,
                z_1y      REAL, z_2y    REAL, z_3y    REAL,
                signal    TEXT
            )
        ''')
        existing = {row[1] for row in c.execute("PRAGMA table_info(basis_history)")}
        for col in ['kofr_3m','kofr_6m','kofr_9m',
                    'cd_3m',  'cd_6m',  'cd_9m',
                    'basis_3m','basis_6m','basis_9m',
                    'kofr_on']:
            if col not in existing:
                c.execute(f"ALTER TABLE basis_history ADD COLUMN {col} REAL")
        c.execute('''
            CREATE TABLE IF NOT EXISTS signals_log (
                ts        TEXT,
                strategy  TEXT,
                direction TEXT,
                tenor     TEXT,
                basis_now REAL,
                target    REAL,
                stop      REAL,
                reason    TEXT
            )
        ''')


def save_tick(db_path: str, kofr: dict, cd: dict,
              basis: dict, zscore: dict, signals: list):
    ts = datetime.datetime.now().isoformat(timespec='seconds')
    row = dict(
        ts=ts,
        kofr_3m=kofr.get('3M'), kofr_6m=kofr.get('6M'), kofr_9m=kofr.get('9M'),
        kofr_1y=kofr.get('1Y'), kofr_2y=kofr.get('2Y'), kofr_3y=kofr.get('3Y'),
        cd_3m=cd.get('3M'),     cd_6m=cd.get('6M'),     cd_9m=cd.get('9M'),
        cd_1y=cd.get('1Y'),     cd_2y=cd.get('2Y'),     cd_3y=cd.get('3Y'),
        basis_3m=basis.get('3M'), basis_6m=basis.get('6M'), basis_9m=basis.get('9M'),
        basis_1y=basis.get('1Y'), basis_2y=basis.get('2Y'), basis_3y=basis.get('3Y'),
        z_1y=zscore.get('1Y'), z_2y=zscore.get('2Y'), z_3y=zscore.get('3Y'),
        signal=','.join(s.strategy for s in signals if s.direction != 'WATCH') or None,
    )
    cols = ', '.join(row.keys())
    placeholders = ', '.join(['?'] * len(row))
    with _conn(db_path) as c:
        c.execute(f'INSERT OR REPLACE INTO basis_history ({cols}) VALUES ({placeholders})',
                  list(row.values()))
        for s in signals:
            c.execute('''
                INSERT INTO signals_log VALUES (?,?,?,?,?,?,?,?)
            ''', (ts, s.strategy, s.direction, s.tenor,
                  s.basis_now, s.target, s.stop, s.reason))


def load_history(db_path: str, days: int = 120) -> pd.DataFrame:
    try:
        with _conn(db_path) as c:
            df = pd.read_sql(
                f"SELECT * FROM basis_history "
                f"WHERE ts >= datetime('now', '-{days} days') "
                f"ORDER BY ts",
                c, parse_dates=['ts']
            )
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def load_signals_log(db_path: str, limit: int = 50) -> pd.DataFrame:
    try:
        with _conn(db_path) as c:
            return pd.read_sql(
                f"SELECT * FROM signals_log ORDER BY ts DESC LIMIT {limit}", c
            )
    except Exception:
        return pd.DataFrame()


def clear_history(db_path: str):
    with _conn(db_path) as c:
        c.execute("DELETE FROM basis_history")


def seed_history(db_path: str, history_records: list, force: bool = False):
    """Seed DB with history records. Skips if data already exists unless force=True."""
    with _conn(db_path) as c:
        count = c.execute("SELECT COUNT(*) FROM basis_history").fetchone()[0]
        if count > 0 and not force:
            return
    with _conn(db_path) as c:
        for rec in history_records:
            ts = datetime.datetime.combine(rec['date'],
                                           datetime.time(9, 0)).isoformat()
            c.execute('''
                INSERT OR REPLACE INTO basis_history
                (ts, kofr_3m, kofr_6m, kofr_9m, kofr_1y, kofr_2y, kofr_3y,
                 cd_3m, cd_6m, cd_9m, cd_1y, cd_2y, cd_3y,
                 basis_3m, basis_6m, basis_9m, basis_1y, basis_2y, basis_3y,
                 kofr_on, z_1y, z_2y, z_3y, signal)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)
            ''', (ts,
                  rec.get('kofr_3m'), rec.get('kofr_6m'), rec.get('kofr_9m'),
                  rec.get('kofr_1y'), rec.get('kofr_2y'), rec.get('kofr_3y'),
                  rec.get('cd_3m'),   rec.get('cd_6m'),   rec.get('cd_9m'),
                  rec.get('cd_1y'),   rec.get('cd_2y'),   rec.get('cd_3y'),
                  rec.get('basis_3m'), rec.get('basis_6m'), rec.get('basis_9m'),
                  rec.get('basis_1y'), rec.get('basis_2y'), rec.get('basis_3y'),
                  rec.get('kofr_on')))
