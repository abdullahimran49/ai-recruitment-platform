"""Database connection shared by the Streamlit app and the portal backend.

Provider is selected by DB_PROVIDER in .env: "mssql" (SQL Server, default) or
"mysql". Everything above this layer is SQLAlchemy ORM, so nothing else changes
between the two.

SQL Server (.env):
    DB_PROVIDER=mssql
    MSSQL_SERVER=localhost\\SQLEXPRESS
    MSSQL_DB=ats_screener
    MSSQL_AUTH=windows            # windows (trusted) | sql
    MSSQL_USER=ats                # only for MSSQL_AUTH=sql
    MSSQL_PASSWORD=...            # only for MSSQL_AUTH=sql
    MSSQL_DRIVER=ODBC Driver 17 for SQL Server

MySQL (.env):
    DB_PROVIDER=mysql
    MYSQL_URL=mysql+pymysql://user:pass@localhost:3306
    MYSQL_DB=ats_screener
"""

import os
import urllib.parse
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

PROVIDER = os.getenv("DB_PROVIDER", "mssql").lower()

# --- MySQL settings ---
MYSQL_URL = os.getenv("MYSQL_URL", "").strip().rstrip("/")

# --- SQL Server settings ---
MSSQL_SERVER = os.getenv("MSSQL_SERVER", r"localhost\SQLEXPRESS")
MSSQL_AUTH = os.getenv("MSSQL_AUTH", "windows").lower()
MSSQL_USER = os.getenv("MSSQL_USER", "")
MSSQL_PASSWORD = os.getenv("MSSQL_PASSWORD", "")
MSSQL_DRIVER = os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")

# Database name is shared across providers.
DB_NAME = os.getenv("MSSQL_DB") or os.getenv("MYSQL_DB") or "ats_screener"


def _mssql_odbc(database: str) -> str:
    if MSSQL_AUTH == "sql":
        auth = f"UID={MSSQL_USER};PWD={MSSQL_PASSWORD};"
    else:
        auth = "Trusted_Connection=yes;"
    return (f"DRIVER={{{MSSQL_DRIVER}}};SERVER={MSSQL_SERVER};"
            f"DATABASE={database};{auth}TrustServerCertificate=yes;")


def make_url(database: str) -> str:
    """SQLAlchemy URL for a specific database on the configured provider.

    Pass "master" (MSSQL) or "" (MySQL) to connect at server level, e.g. to
    CREATE DATABASE.
    """
    if PROVIDER == "mssql":
        return ("mssql+pyodbc:///?odbc_connect="
                + urllib.parse.quote_plus(_mssql_odbc(database or "master")))
    # mysql
    return f"{MYSQL_URL}/{database}"


def make_engine(database: str):
    return create_engine(make_url(database), pool_pre_ping=True,
                         pool_recycle=3600, echo=False)


def _configured() -> bool:
    if PROVIDER == "mssql":
        return bool(MSSQL_SERVER)
    return bool(MYSQL_URL)


engine = None
SessionLocal = None
if _configured():
    engine = make_engine(DB_NAME)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def db_enabled() -> bool:
    return engine is not None


@contextmanager
def session():
    """Context-managed session: commits on success, rolls back on error."""
    if SessionLocal is None:
        raise RuntimeError("Database not configured — set DB_PROVIDER + "
                           "connection settings in .env")
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
