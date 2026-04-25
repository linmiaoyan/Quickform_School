import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session


logger = logging.getLogger(__name__)


def build_database_url() -> str:
    """
    Campus edition: PostgreSQL-first.

    Prefer `DATABASE_URL` (full SQLAlchemy URL). If absent, build from POSTGRES_* vars.
    """
    explicit = (os.getenv('DATABASE_URL') or '').strip()
    if explicit:
        return explicit

    host = (os.getenv('POSTGRES_HOST') or os.getenv('PGHOST') or 'localhost').strip()
    port = (os.getenv('POSTGRES_PORT') or os.getenv('PGPORT') or '5432').strip()
    user = (os.getenv('POSTGRES_USER') or os.getenv('PGUSER') or 'postgres').strip()
    password = os.getenv('POSTGRES_PASSWORD') or os.getenv('PGPASSWORD') or ''
    database = (os.getenv('POSTGRES_DB') or os.getenv('PGDATABASE') or 'quickform').strip()

    auth = user
    if password != '':
        auth = f"{user}:{password}"

    return f"postgresql+psycopg://{auth}@{host}:{port}/{database}"


def init_engine_and_session():
    """
    Initialize SQLAlchemy engine and scoped session factory for PostgreSQL.
    """
    database_url = build_database_url()
    # pool_pre_ping helps recover from stale connections.
    engine = create_engine(database_url, pool_pre_ping=True, echo=False)
    session_local = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    try:
        logger.info("Database engine initialized: %s", engine.dialect.name)
    except Exception:
        pass
    return database_url, engine, session_local


# Initialize on import for the running app.
DATABASE_URL, engine, SessionLocal = init_engine_and_session()

