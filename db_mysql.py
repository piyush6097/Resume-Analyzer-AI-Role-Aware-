# db_mysql.py
import os
from typing import Optional, Dict, List, Tuple
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Float, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ---- DB URL builder (env-friendly) ----
def _env(k: str, d: Optional[str] = None) -> Optional[str]:
    v = os.getenv(k)
    return v if v is not None else d

driver = _env('DB_DRIVER', 'mysql+pymysql')
user   = _env('DB_USER', 'root')
pwd    = _env('DB_PASS', '')
host   = _env('DB_HOST', '127.0.0.1')
port   = _env('DB_PORT', '3306')
name   = _env('DB_NAME', 'resume_analyzer')
extras = _env('DB_EXTRAS', '').strip()
extra_q = f"&{extras}" if extras else ""

DB_URL = f"{driver}://{user}:{pwd}@{host}:{port}/{name}?charset=utf8mb4{extra_q}"

# SQLAlchemy setup
engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

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
    """Create tables if missing."""
    Base.metadata.create_all(engine)

# ----- Role / Skill helpers -----
def upsert_role(role_name: str):
    # roles are implicitly created when inserting role_skills; this returns nothing useful but kept for parity
    return role_name

def upsert_skill(role: str, skill: str, skill_type: str = "core"):
    session = SessionLocal()
    skill_str = str(skill).strip()
    rs = session.query(RoleSkill).filter_by(role=role, skill_type=skill_type, skill=skill_str).first()
    if not rs:
        rs = RoleSkill(role=role, skill_type=skill_type, skill=skill_str)
        session.add(rs)
        session.commit()
    session.close()
    return True

def list_roles() -> List[str]:
    session = SessionLocal()
    rows = session.query(RoleSkill.role).distinct().order_by(RoleSkill.role).all()
    session.close()
    return [r[0] for r in rows]

def get_role_skills_dict(role_name: str) -> Dict[str, set]:
    """Return dict with keys core/tools/soft -> set(skills) for a role"""
    session = SessionLocal()
    rows = session.query(RoleSkill.skill_type, RoleSkill.skill).filter_by(role=role_name).all()
    session.close()
    d = {"core": set(), "tools": set(), "soft": set()}
    for typ, skill in rows:
        if typ not in d:
            d[typ] = set()
        d[typ].add(skill.lower())
    return d

def seed_skills_from_dict(skills_dict: dict):
    """Insert or skip existing skills from a SKILL_DB-like dict"""
    session = SessionLocal()
    for role, groups in skills_dict.items():
        for group_name, items in groups.items():
            # group_name expected core/tools/soft
            for item in items:
                item_str = str(item).strip()
                exists = session.query(RoleSkill).filter_by(role=role, skill_type=group_name, skill=item_str).first()
                if not exists:
                    session.add(RoleSkill(role=role, skill_type=group_name, skill=item_str))
    session.commit()
    session.close()

# ----- Job Description helpers -----
def add_job_description(name: str, description: str) -> int:
    session = SessionLocal()
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
    session.close()
    return jd_id

def list_job_descriptions() -> List[Tuple[int, str, str, str]]:
    session = SessionLocal()
    rows = session.query(JobDescription.id, JobDescription.name, JobDescription.description, JobDescription.created_at).order_by(JobDescription.id.desc()).all()
    session.close()
    return rows

def get_job_description(jd_id: int):
    session = SessionLocal()
    row = session.query(JobDescription.id, JobDescription.name, JobDescription.description, JobDescription.created_at).filter_by(id=jd_id).first()
    session.close()
    return row

# ----- Candidate cache -----
def get_cached_score(resume_hash: str, jd_hash: str, role: str, model_version: str = "v1") -> Optional[float]:
    session = SessionLocal()
    row = session.query(CandidateCache).filter_by(resume_hash=resume_hash, jd_hash=jd_hash, role=role, model_version=model_version).first()
    session.close()
    return float(row.score) if row else None

def save_score(resume_hash: str, jd_hash: str, role: str, score: float, model_version: str = "v1", jd_id: int = None, source_filename: str = None):
    session = SessionLocal()
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
    session.close()

def list_cached_candidates():
    session = SessionLocal()
    rows = session.query(CandidateCache.resume_hash, CandidateCache.jd_hash, CandidateCache.role, CandidateCache.score, CandidateCache.model_version, CandidateCache.jd_id, CandidateCache.source_filename, CandidateCache.created_at).order_by(CandidateCache.created_at.desc()).all()
    session.close()
    return rows

def delete_cached(resume_hash: str, jd_hash: str = None, role: str = None):
    session = SessionLocal()
    if jd_hash and role:
        session.query(CandidateCache).filter_by(resume_hash=resume_hash, jd_hash=jd_hash, role=role).delete()
    else:
        session.query(CandidateCache).filter_by(resume_hash=resume_hash).delete()
    session.commit()
    session.close()
