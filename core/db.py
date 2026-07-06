"""
core/db.py — SQLAlchemy engine and session factory.

DATABASE_URL is read lazily via settings.require_database() so that
importing this module in tests (or config-only runs) does not fail.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.settings import get_settings

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        url = get_settings().require_database()
        kwargs: dict = {"pool_pre_ping": True, "echo": False}
        # Pool sizing args are invalid for SQLite's SingletonThreadPool
        # (used in tests); only pass them for real server databases.
        if not url.startswith("sqlite"):
            kwargs.update(pool_size=5, max_overflow=10)
        _engine = create_engine(url, **kwargs)
    return _engine


def _get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=_get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context manager yielding a scoped SQLAlchemy session.

    Usage::

        with get_session() as session:
            session.add(obj)
            session.commit()

    Rolls back automatically on exception and always closes the session.
    """
    factory = _get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
