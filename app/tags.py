"""Short labels on jobs and customers ("fleet", "comeback", "tiktok lead") that show on cards and
can be searched across the site.

Tags live in their own table, so nothing else changes shape. The app does not create tables on
start: until migrations/20261123_tags.sql is applied, reads return no tags and adding one says so.
"""
import re
import time
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, UniqueConstraint, func, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import Base

KINDS = ("job", "customer")
MAX_LEN = 30
MAX_PER_ITEM = 12


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("kind", "target_id", "name", name="uq_tags_kind_target_name"),
                      Index("ix_tags_name", "name"))
    id = Column(Integer, primary_key=True)
    kind = Column(String(12), nullable=False)
    target_id = Column(Integer, nullable=False)
    name = Column(String(MAX_LEN), nullable=False)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


_ready: dict = {}  # per database engine: (checked at, table exists)


def ready(db: Session) -> bool:
    """True once the tags table exists. A missing table is checked again a minute later, not on every page."""
    bind = db.get_bind()
    at, ok = _ready.get(id(bind), (0.0, False))
    if ok or (at and time.monotonic() - at < 60):
        return ok
    try:
        ok = inspect(bind).has_table("tags")
    except SQLAlchemyError:
        ok = False
    _ready[id(bind)] = (time.monotonic(), ok)
    return ok


def clean(name: str) -> str:
    """Lower case, single spaces, letters, numbers, spaces and dashes only."""
    name = re.sub(r"[^a-z0-9 \-]", "", (name or "").lower().replace("_", " ").replace("#", ""))
    return re.sub(r"\s+", " ", name).strip()[:MAX_LEN].strip()


def names_for(db: Session, kind: str, ids) -> dict:
    """{target_id: [tag names, A to Z]} for the given jobs or customers."""
    ids = [i for i in ids if i]
    if not ids or not ready(db):
        return {}
    try:
        rows = db.query(Tag.target_id, Tag.name).filter(Tag.kind == kind, Tag.target_id.in_(ids)).order_by(Tag.name).all()
    except SQLAlchemyError:
        db.rollback()
        return {}
    out = {}
    for target_id, name in rows:
        out.setdefault(target_id, []).append(name)
    return out


def matching(db: Session, kind: str, words: str) -> set:
    """Ids whose tags hold every word of the search."""
    words = [w for w in clean(words).split() if w]
    if not words or not ready(db):
        return set()
    try:
        rows = db.query(Tag.target_id, Tag.name).filter(Tag.kind == kind).all()
    except SQLAlchemyError:
        db.rollback()
        return set()
    joined = {}
    for target_id, name in rows:
        joined[target_id] = joined.get(target_id, "") + " " + name
    return {t for t, text in joined.items() if all(w in text for w in words)}


def known(db: Session) -> list:
    """Every tag in use with how many jobs and customers carry it, most used first."""
    if not ready(db):
        return []
    try:
        rows = db.query(Tag.name, func.count(Tag.id)).group_by(Tag.name).order_by(func.count(Tag.id).desc(), Tag.name).limit(200).all()
    except SQLAlchemyError:
        db.rollback()
        return []
    return [{"name": name, "count": count} for name, count in rows]


def add(db: Session, kind: str, target_id: int, name: str, who: str) -> str | None:
    """Adds one tag. Returns a problem to show, or None."""
    name = clean(name)
    if kind not in KINDS:
        return "Unknown kind."
    if not name:
        return "Type a tag first: letters, numbers, spaces or dashes."
    if not ready(db):
        return "Tags aren't switched on yet."
    try:
        if db.query(Tag).filter_by(kind=kind, target_id=target_id, name=name).first():
            return None
        if db.query(Tag).filter_by(kind=kind, target_id=target_id).count() >= MAX_PER_ITEM:
            return f"Up to {MAX_PER_ITEM} tags each."
        db.add(Tag(kind=kind, target_id=target_id, name=name, created_by=(who or "")[:64]))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return "The tag couldn't be saved."
    return None


def remove(db: Session, kind: str, target_id: int, name: str) -> None:
    if kind not in KINDS or not ready(db):
        return
    try:
        db.query(Tag).filter_by(kind=kind, target_id=target_id, name=clean(name)).delete()
        db.commit()
    except SQLAlchemyError:
        db.rollback()
