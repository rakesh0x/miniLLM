from collections.abc import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine

from .config import settings


def create_db_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine with per-dialect settings.

    SQLite deliberately gets `NullPool`. Pooling file-backed SQLite connections
    buys nothing — each connection is another handle on the same file, so a pool
    only serialises them — and it actively hurts under concurrency: the queue
    fills, a request waits out `pool_timeout` for a connection, and the failure
    surfaces as a pool timeout rather than as a busy database. Opening a handle
    for the length of a statement is cheaper.

    Postgres gets a real connection pool so concurrent requests share a bounded
    set of connections instead of opening one per call. `pool_pre_ping` replaces
    a connection the server dropped while idle (restart, firewall, idle
    timeout), which would otherwise surface as a random failure.
    """
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )

    return create_engine(
        database_url,
        # Cheap liveness check so stale connections (e.g. after a DB restart
        # or an idle timeout) are transparently replaced instead of 500ing.
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        # Recycle long-lived connections; Postgres and firewalls drop idle ones.
        pool_recycle=settings.db_pool_recycle,
    )


engine: Engine = create_db_engine(settings.database_url)


def init_db() -> None:
    from . import models

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
