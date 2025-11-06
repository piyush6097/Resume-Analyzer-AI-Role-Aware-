# db_cache.py
import sqlite3
import os
from datetime import datetime
from typing import Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidates_cache.db")

CREATE_TABLES_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS JOB_DESCRIPTIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS CANDIDATES (
    resume_hash TEXT,
    jd_hash TEXT,
    role TEXT,
    score REAL NOT NULL,
    model_version TEXT DEFAULT 'v1',
    jd_id INTEGER,
    source_filename TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (resume_hash, jd_hash, role, model_version),
    FOREIGN KEY (jd_id) REFERENCES JOB_DESCRIPTIONS(id) ON DELETE SET NULL
);
"""

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    conn.executescript(CREATE_TABLES_SQL)
    conn.commit()
    conn.close()

# Optional migration helper: run once if your old DB lacks jd_hash/role/model_version
# Uncomment and run the file once if you want to migrate in-place.
#
# def update_schema():
#     conn = get_connection(DB_PATH)
#     cur = conn.cursor()
#     try:
#         cur.execute("ALTER TABLE CANDIDATES ADD COLUMN jd_hash TEXT")
#     except Exception:
#         pass
#     try:
#         cur.execute("ALTER TABLE CANDIDATES ADD COLUMN role TEXT")
#     except Exception:
#         pass
#     try:
#         cur.execute("ALTER TABLE CANDIDATES ADD COLUMN model_version TEXT DEFAULT 'v1'")
#     except Exception:
#         pass
#     conn.commit()
#     conn.close()
#     print("DB schema updated")
#
# # Run once then comment out:
# # update_schema()

# ------------------------- JOB DESCRIPTION FUNCTIONS -------------------------
def add_job_description(name: str, description: str, db_path: str = DB_PATH) -> int:
    """Add a new JD or update existing by name. Returns JD ID."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"
    cur.execute(
        "INSERT OR REPLACE INTO JOB_DESCRIPTIONS (name, description, created_at) VALUES (?, ?, ?)",
        (name, description, now),
    )
    conn.commit()
    cur.execute("SELECT id FROM JOB_DESCRIPTIONS WHERE name=?", (name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def list_job_descriptions(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, created_at FROM JOB_DESCRIPTIONS ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_job_description(jd_id: int, db_path: str = DB_PATH) -> Optional[Tuple]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, created_at FROM JOB_DESCRIPTIONS WHERE id=?", (jd_id,))
    row = cur.fetchone()
    conn.close()
    return row

# ------------------------- RESUME CACHE FUNCTIONS -------------------------
def get_cached_score(resume_hash: str, jd_hash: str, role: str, model_version: str = "v1", db_path: str = DB_PATH) -> Optional[float]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT score FROM CANDIDATES WHERE resume_hash=? AND jd_hash=? AND role=? AND model_version=?",
        (resume_hash, jd_hash, role, model_version),
    )
    row = cur.fetchone()
    conn.close()
    return float(row[0]) if row else None

def save_score(
    resume_hash: str,
    jd_hash: str,
    role: str,
    score: float,
    model_version: str = "v1",
    jd_id: int = None,
    source_filename: str = None,
    db_path: str = DB_PATH,
):
    conn = get_connection(db_path)
    cur = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"
    cur.execute(
        "INSERT OR REPLACE INTO CANDIDATES (resume_hash, jd_hash, role, score, model_version, jd_id, source_filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (resume_hash, jd_hash, role, float(score), model_version, jd_id, source_filename, now),
    )
    conn.commit()
    conn.close()

def list_cached_candidates(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT resume_hash, jd_hash, role, score, model_version, jd_id, source_filename, created_at FROM CANDIDATES ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_cached(resume_hash: str, jd_hash: str = None, role: str = None, db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()
    if jd_hash and role:
        cur.execute("DELETE FROM CANDIDATES WHERE resume_hash=? AND jd_hash=? AND role=?", (resume_hash, jd_hash, role))
    else:
        cur.execute("DELETE FROM CANDIDATES WHERE resume_hash=?", (resume_hash,))
    conn.commit()
    conn.close()
