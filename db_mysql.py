# db_mysql.py
# MySQL-backed data layer for Resume Analyzer
# - Uses SQLAlchemy 2.x ORM
# - Loads .env automatically (DB_* vars)
# - Stores job descriptions, cached scores, roles & skills

import os
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Set
from contextlib import contextmanager

# Load .env from this folder (requires: python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass  # env can still be set via shell

from sqlalchemy import (
    create_engine, Integer, String, Float, DateTime, ForeignKey,
    UniqueConstraint, Index, Text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


# -------------------- Engine / Session --------------------
def _env(k: str, d: Optional[str] = None) -> Optional[str]:
    v = os.getenv(k)
    return v if v is not None else d

driver = _env('DB_DRIVER','mysql+pymysql')
user   = _env('DB_USER','root')
pwd    = _env('DB_PASS','')
host   = _env('DB_HOST','127.0.0.1')
port   = _env('DB_PORT','3306')
name   = _env('DB_NAME','resume_analyzer')

# Extra query params for cloud DBs (e.g., ssl=true for PlanetScale)
# Example: set DB_EXTRAS="ssl=true"  -> ...?charset=utf8mb4&ssl=true
extras = _env('DB_EXTRAS','').strip()
extra_q = f"&{extras}" if extras else ""

DB_URL = f"{driver}://{user}:{pwd}@{host}:{port}/{name}?charset=utf8mb4{extra_q}"

@contextmanager
def session_scope():
    s = Session()
    try:
        yield s
        s.commit()
    except:
        s.rollback()
        raise
    finally:
        s.close()


# -------------------- ORM Models --------------------
class Base(DeclarativeBase):
    pass


class JobDescription(Base):
    __tablename__ = "job_descriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)  # TEXT for long JDs
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CandidateScore(Base):
    __tablename__ = "candidates"
    # Composite PK → same resume can have different scores per JD/role/model
    resume_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    jd_hash: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), primary_key=True, default="v1")

    score: Mapped[float] = mapped_column(Float, nullable=False)
    jd_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_descriptions.id"), nullable=True, index=True)
    source_filename: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    jd = relationship("JobDescription", lazy="joined")

Index("idx_candidates_created", CandidateScore.created_at)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'core' | 'tools' | 'soft'
    __table_args__ = (UniqueConstraint("role_id", "name", "type", name="uq_role_skill_type"),)


# -------------------- Schema Init --------------------
def init_db():
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)


# -------------------- JD Functions --------------------
def add_job_description(name: str, description: str) -> int:
    """Add a new JD or update existing by name. Returns JD ID."""
    now = datetime.utcnow()
    with session_scope() as s:
        jd = s.query(JobDescription).filter_by(name=name).one_or_none()
        if jd:
            jd.description = description
            jd.created_at = now
        else:
            jd = JobDescription(name=name, description=description, created_at=now)
            s.add(jd)
        s.flush()
        return jd.id

def list_job_descriptions() -> List[Tuple[int, str, str, str]]:
    with session_scope() as s:
        rows = s.query(JobDescription).order_by(JobDescription.id.desc()).all()
        return [(r.id, r.name, r.description, r.created_at.isoformat() + "Z") for r in rows]

def get_job_description(jd_id: int) -> Optional[Tuple[int, str, str, str]]:
    with session_scope() as s:
        r = s.get(JobDescription, jd_id)
        return (r.id, r.name, r.description, r.created_at.isoformat() + "Z") if r else None


# -------------------- Cache Functions --------------------
def get_cached_score(resume_hash: str, jd_hash: str, role: str, model_version: str = "v1") -> Optional[float]:
    with session_scope() as s:
        row = s.query(CandidateScore.score).filter_by(
            resume_hash=resume_hash, jd_hash=jd_hash, role=role, model_version=model_version
        ).one_or_none()
        return float(row[0]) if row else None

def save_score(
    resume_hash: str,
    jd_hash: str,
    role: str,
    score: float,
    model_version: str = "v1",
    jd_id: Optional[int] = None,
    source_filename: Optional[str] = None,
):
    now = datetime.utcnow()
    with session_scope() as s:
        existing = s.query(CandidateScore).filter_by(
            resume_hash=resume_hash, jd_hash=jd_hash, role=role, model_version=model_version
        ).one_or_none()
        if existing:
            existing.score = float(score)
            existing.jd_id = jd_id
            existing.source_filename = source_filename
            existing.created_at = now
        else:
            s.add(CandidateScore(
                resume_hash=resume_hash,
                jd_hash=jd_hash,
                role=role,
                score=float(score),
                model_version=model_version,
                jd_id=jd_id,
                source_filename=source_filename,
                created_at=now,
            ))

def list_cached_candidates() -> List[Tuple[str, str, str, float, str, Optional[int], Optional[str], str]]:
    with session_scope() as s:
        rows = s.query(CandidateScore).order_by(CandidateScore.created_at.desc()).all()
        return [
            (
                r.resume_hash, r.jd_hash, r.role, r.score, r.model_version,
                r.jd_id, r.source_filename, r.created_at.isoformat() + "Z"
            )
            for r in rows
        ]

def delete_cached(resume_hash: str, jd_hash: str = None, role: str = None):
    with session_scope() as s:
        q = s.query(CandidateScore).filter(CandidateScore.resume_hash == resume_hash)
        if jd_hash:
            q = q.filter(CandidateScore.jd_hash == jd_hash)
        if role:
            q = q.filter(CandidateScore.role == role)
        q.delete(synchronize_session=False)


# -------------------- Roles / Skills helpers (DB source of truth) --------------------
def upsert_role(name: str) -> int:
    """Create role if missing, return id."""
    name = name.strip()
    if not name:
        raise ValueError("Role name cannot be empty")
    with session_scope() as s:
        r = s.query(Role).filter_by(name=name).one_or_none()
        if not r:
            r = Role(name=name)
            s.add(r)
            s.flush()
        return r.id

def get_role_id_by_name(name: str) -> Optional[int]:
    with session_scope() as s:
        r = s.query(Role.id).filter_by(name=name).one_or_none()
        return int(r[0]) if r else None

def upsert_skill(role_id: int, name: str, typ: str):
    """Create a skill (core/tools/soft) if not already present."""
    name = name.strip()
    typ = typ.lower().strip()
    if not name:
        raise ValueError("Skill name cannot be empty")
    if typ not in {"core", "tools", "soft"}:
        raise ValueError("type must be 'core' | 'tools' | 'soft'")
    with session_scope() as s:
        exists = s.query(Skill).filter_by(role_id=role_id, name=name, type=typ).one_or_none()
        if not exists:
            s.add(Skill(role_id=role_id, name=name, type=typ))

def delete_skill(role_id: int, name: str, typ: str) -> int:
    """Delete a specific skill; returns count deleted."""
    name = name.strip()
    typ = typ.lower().strip()
    with session_scope() as s:
        q = s.query(Skill).filter_by(role_id=role_id, name=name, type=typ)
        n = q.delete(synchronize_session=False)
        return n

def get_roles() -> List[str]:
    """Return all role names (sorted A–Z)."""
    with session_scope() as s:
        rows = s.query(Role.name).order_by(Role.name.asc()).all()
        return [r[0] for r in rows]

def get_role_skills_dict(role_name: str) -> Dict[str, Set[str]]:
    """
    Return {'core': set(), 'tools': set(), 'soft': set()} for a role name.
    If role not found, returns empty sets.
    """
    out: Dict[str, Set[str]] = {"core": set(), "tools": set(), "soft": set()}
    with session_scope() as s:
        role = s.query(Role).filter_by(name=role_name).one_or_none()
        if not role:
            return out
        rows = s.query(Skill.name, Skill.type).filter(Skill.role_id == role.id).all()
        for name, typ in rows:
            out[typ].add(name)
    return out

def get_all_roles_with_skills() -> Dict[str, Dict[str, List[str]]]:
    """Return mapping role_name -> {'core':[...], 'tools':[...], 'soft':[...]} lists (sorted)."""
    result: Dict[str, Dict[str, List[str]]] = {}
    for r in get_roles():
        d = get_role_skills_dict(r)
        result[r] = {
            "core": sorted(list(d["core"])),
            "tools": sorted(list(d["tools"])),
            "soft": sorted(list(d["soft"])),
        }
    return result

def seed_skills_from_dict(skill_db: dict):
    """
    One-time seeding helper. Pass your SKILL_DB dict to insert roles + skills
    if they don't already exist. Safe to re-run; uses upserts.
    """
    for role_name, buckets in skill_db.items():
        rid = upsert_role(role_name)
        for typ in ("core", "tools", "soft"):
            for sk in sorted(set(buckets.get(typ, []))):
                upsert_skill(rid, sk, typ)
