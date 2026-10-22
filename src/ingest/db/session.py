import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ingest.config import Settings, get_settings
from ingest.db.models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None):
    global _engine, _SessionLocal
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _SessionLocal is not None
    return _SessionLocal


def init_db(settings: Settings | None = None, attempts: int = 10) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(get_engine(settings))
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Database not ready (attempt %s/%s): %s", attempt, attempts, exc)
            time.sleep(2)
    assert last_error is not None
    raise last_error


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
