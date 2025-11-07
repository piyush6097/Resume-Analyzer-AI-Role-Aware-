# db_mysql.py
import os
from typing import Optional, Dict, List, Tuple
from datetime import datetime
import urllib.parse
import traceback

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Float, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError

# ---- helper to read env (keeps your original behavior) ----
def _env(k: str, d: Optional[str] = None) -> Optional[str]:
    v = os.getenv(k)
    return v if v is not None else d

# ---- Database URL builder / resolution ----
def get_database_url():
    """
    Resolve DB URL in this order:
      1) explicit full DATABASE_URL or DB_URL env var
      2) build from DB_* env vars (DB_DRIVER, DB_USER, DB_PASS, DB_HOST, DB_PORT, DB_NAME, DB_EXTRAS)
      3) return None (caller will fall back to sqlite)
    """
    # 1) explicit
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL")
    if db_url:
        return db_url

    # 2) construct from components (your prior approach)
    driver = _env('DB_DRIVER', 'mysql+pymysql')
    user   = _env('DB_USER', 'root') or ""
    pwd    = _env('DB_PASS', '') or ""
    host   = _env('DB_HOST', '127.0.0.1') or ""
    port   = _env('DB_PORT', '3306') or ""
    name   = _env('DB_NAME', 'resume_analyzer') or ""
    extras = _env('DB_EXTRAS', '').strip() or ""
    extra_q = f"&{extras}" if extras else ""

    # URL-encode username/password to safely include special characters
    user_enc = urllib.parse.quote_plus(user)
    pwd_enc = urllib.parse.quote_plus(pwd)

    # If no host/name provided, return None so caller can fallback
    if not host or not name:
        return None

    built = f"{driver}://{user_enc}:{pwd_enc}@{host}:{port}/{name}?charset=utf8mb4{extra_q}"
    return built

def sqlite_url():
    sqlite_path = os.path.join(os.getcwd(), "resume.db")
    return f"sqlite:///{sqlite_path}"

# SQLAlchemy base (keeps the rest of your models intact)
Base = declarative_base()

# Try to create engine using DB URL; if connection fails, fall back to sqlite
def create_db_engine():
    db_url = get_database_url()
    if db_url:
        try:
            # If it's sqlite, pass connect args to avoid threading issues
            if db_url.startswith("sqlite"):
                engine = create_engine(db_url, connect_args={"check_same_thread": False}, echo=False, future=True)
            else:
                engine = create_engine(db_url, echo=False, future=True, pool_pre_ping=True)
            # quick test connect to detect unreachable DB early
            conn = engine.connect()
            conn.close()
            print(f"[db_mysql] using database URL: {db_url}")
            return engine
        except Exception as e:
            # Log full traceback to logs (secrets will be redacted on managed hosts)
            print("[db_mysql] ERROR connecting to primary DB URL:", str(e))
            traceback.print_exc()
            # fall through to sqlite fallback

    # fallback to sqlite
    fallback = sqlite_url()
    print(f"[db_mysql] falling back to SQLite at: {fallback}")
    engine = create_engine(fallback, connect_args={"check_same_thread": False}, echo=False, future=True)
    return engine

# create engine & sessionmaker used by the rest of the module
engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# ---- Models ----
class RoleSkill(Base):
    __tablename__ = "role_skills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String(255), nullable=False, index=True)
    skill_type = Column(String(50), nullable=False)  # 'core', 'tools', 'soft'
    skill = Column(String(255), nullable=False)

    __table_args__ = (UniqueConstraint('role','skill_type','skill', name='u_role_skill'),)

class JobDescription(Base):
    __tablename__ = "job_descriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)

class CandidateCache(Base):
    __tablename__ = "candidates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_hash = Column(String(128), nullable=False, index=True)
    jd_hash = Column(String(128), nullable=False, index=True)
    role = Column(String(255), nullable=False)
    score = Column(Float, nullable=False)
    model_version = Column(String(64), nullable=False, default="v1")
    jd_id = Column(Integer, nullable=True)
    source_filename = Column(String(512), nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint('resume_hash','jd_hash','role','model_version', name='u_resume_jd_role'),)

# ---- Init / helpers ----
def init_db():
    """Create tables if missing. If DB init fails, raise a sanitized RuntimeError."""
    try:
        Base.metadata.create_all(engine)
    except OperationalError:
        # Log the full error to logs (not to UI)
        traceback.print_exc()
        # Re-raise a sanitized message (no secrets)
        raise RuntimeError("Database initialization failed — check logs or your DATABASE_URL/credentials.")

# ----- Role / Skill helpers -----
def upsert_role(role_name: str):
    return role_name

def upsert_skill(role: str, skill: str, skill_type: str = "core"):
    session = SessionLocal()
    try:
        skill_str = str(skill).strip()
        rs = session.query(RoleSkill).filter_by(role=role, skill_type=skill_type, skill=skill_str).first()
        if not rs:
            rs = RoleSkill(role=role, skill_type=skill_type, skill=skill_str)
            session.add(rs)
            session.commit()
    finally:
        session.close()
    return True

def list_roles() -> List[str]:
    session = SessionLocal()
    try:
        rows = session.query(RoleSkill.role).distinct().order_by(RoleSkill.role).all()
        return [r[0] for r in rows]
    finally:
        session.close()

def get_role_skills_dict(role_name: str) -> Dict[str, set]:
    session = SessionLocal()
    try:
        rows = session.query(RoleSkill.skill_type, RoleSkill.skill).filter_by(role=role_name).all()
    finally:
        session.close()
    d = {"core": set(), "tools": set(), "soft": set()}
    for typ, skill in rows:
        if typ not in d:
            d[typ] = set()
        d[typ].add(skill.lower())
    return d

def seed_skills_from_dict(skills_dict: dict):
    session = SessionLocal()
    try:
        for role, groups in skills_dict.items():
            for group_name, items in groups.items():
                for item in items:
                    item_str = str(item).strip()
                    exists = session.query(RoleSkill).filter_by(role=role, skill_type=group_name, skill=item_str).first()
                    if not exists:
                        session.add(RoleSkill(role=role, skill_type=group_name, skill=item_str))
        session.commit()
    finally:
        session.close()

# ----- Job Description helpers -----
def add_job_description(name: str, description: str) -> int:
    session = SessionLocal()
    try:
        now = datetime.utcnow()
        jd = session.query(JobDescription).filter_by(name=name).first()
        if jd:
            jd.description = description
            jd.created_at = now
            session.commit()
            jd_id = jd.id
        else:
            jd = JobDescription(name=name, description=description, created_at=now)
            session.add(jd)
            session.commit()
            jd_id = jd.id
        return jd_id
    finally:
        session.close()

def list_job_descriptions() -> List[Tuple[int, str, str, str]]:
    session = SessionLocal()
    try:
        rows = session.query(JobDescription.id, JobDescription.name, JobDescription.description, JobDescription.created_at).order_by(JobDescription.id.desc()).all()
        return rows
    finally:
        session.close()

def get_job_description(jd_id: int):
    session = SessionLocal()
    try:
        row = session.query(JobDescription.id, JobDescription.name, JobDescription.description, JobDescription.created_at).filter_by(id=jd_id).first()
        return row
    finally:
        session.close()

# ----- Candidate cache -----
def get_cached_score(resume_hash: str, jd_hash: str, role: str, model_version: str = "v1") -> Optional[float]:
    session = SessionLocal()
    try:
        row = session.query(CandidateCache).filter_by(resume_hash=resume_hash, jd_hash=jd_hash, role=role, model_version=model_version).first()
        return float(row.score) if row else None
    finally:
        session.close()

def save_score(resume_hash: str, jd_hash: str, role: str, score: float, model_version: str = "v1", jd_id: int = None, source_filename: str = None):
    session = SessionLocal()
    try:
        now = datetime.utcnow()
        row = session.query(CandidateCache).filter_by(resume_hash=resume_hash, jd_hash=jd_hash, role=role, model_version=model_version).first()
        if row:
            row.score = float(score)
            row.jd_id = jd_id
            row.source_filename = source_filename
            row.created_at = now
        else:
            row = CandidateCache(
                resume_hash=resume_hash, jd_hash=jd_hash, role=role,
                score=float(score), model_version=model_version,
                jd_id=jd_id, source_filename=source_filename, created_at=now
            )
            session.add(row)
        session.commit()
    finally:
        session.close()

def list_cached_candidates():
    session = SessionLocal()
    try:
        rows = session.query(CandidateCache.resume_hash, CandidateCache.jd_hash, CandidateCache.role, CandidateCache.score, CandidateCache.model_version, CandidateCache.jd_id, CandidateCache.source_filename, CandidateCache.created_at).order_by(CandidateCache.created_at.desc()).all()
        return rows
    finally:
        session.close()

def delete_cached(resume_hash: str, jd_hash: str = None, role: str = None):
    session = SessionLocal()
    try:
        if jd_hash and role:
            session.query(CandidateCache).filter_by(resume_hash=resume_hash, jd_hash=jd_hash, role=role).delete()
        else:
            session.query(CandidateCache).filter_by(resume_hash=resume_hash).delete()
        session.commit()
    finally:
        session.close()
