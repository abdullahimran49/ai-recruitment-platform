"""One-shot database initializer (SQL Server or MySQL).

    python -m core.init_db

Creates the database (if missing), all tables, seed departments, and a
super admin. Reads the connection settings from .env (see core/db.py).

Seeded super admin (CHANGE THE PASSWORD after first login):
    email:    superadmin@ats.local
    password: admin123
"""

import sys

from sqlalchemy import select, text

from core import db as core_db
from core.models import Base, Candidate, Department, PipelineStage, User
from core.security import hash_password

SEED_DEPARTMENTS = ["Engineering", "Data & AI", "Human Resources"]
SUPER_ADMIN_EMAIL = "superadmin@ats.local"
SUPER_ADMIN_PASSWORD = "admin123"

# Global default Kanban columns (department_id NULL). (name, kind).
DEFAULT_STAGES = [
    ("Applied", "active"),
    ("Screening", "active"),
    ("Test", "active"),
    ("Interview", "active"),
    ("Offer", "active"),
    ("Hired", "hired"),
    ("Rejected", "rejected"),
]

# Best-effort placement of pre-portal candidates onto the new board, by their
# legacy `status` string. Anything unmapped falls back to "Screening".
_STATUS_TO_STAGE = {
    "screened": "Screening", "added_manually": "Screening",
    "invited": "Test", "started": "Test", "tested": "Test",
    "submitted": "Test", "terminated": "Test", "test_terminated": "Test",
    "ai_interview_invited": "Interview", "completed": "Interview",
    "interview_terminated": "Interview", "missed": "Interview",
    "shortlisted_onsite": "Offer", "cancelled": "Rejected",
}


def _dialect() -> str:
    return core_db.engine.dialect.name  # 'mssql' | 'mysql'


def _ensure_columns():
    """Idempotently add columns introduced after the first release.

    create_all() never ALTERs existing tables, so schema additions are applied
    here by checking information_schema and running ADD when missing. On a
    fresh database create_all() already includes these columns, so this is a
    no-op there; it only matters for pre-existing databases.
    """
    is_mssql = _dialect() == "mssql"
    _bool = "BIT" if is_mssql else "TINYINT(1)"
    additions = [
        ("tests", "pass_score", "INT NOT NULL DEFAULT 60"),
        ("tests", "proctored", f"{_bool} NOT NULL DEFAULT 1"),
        ("tests", "max_warnings", "INT NOT NULL DEFAULT 3"),
        ("test_assignments", "proctor_warnings", "INT NOT NULL DEFAULT 0"),
        ("test_assignments", "terminated_reason",
         "NVARCHAR(200) NULL" if is_mssql else "VARCHAR(200) NULL"),
        ("test_assignments", "draft_answers",
         "NVARCHAR(MAX) NULL" if is_mssql else "JSON NULL"),
        ("test_assignments", "last_seen", "DATETIME NULL"),
        ("jobs", "merit_config",
         "NVARCHAR(MAX) NULL" if is_mssql else "JSON NULL"),
        # Per-candidate question papers: the pool/draw rules live on the test.
        # 0 = whole pool, which is exactly how every pre-existing test behaved.
        ("tests", "questions_per_candidate", "INT NOT NULL DEFAULT 0"),
        ("tests", "blueprint_json",
         "NVARCHAR(MAX) NULL" if is_mssql else "JSON NULL"),
        ("questions", "category",
         "NVARCHAR(120) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(120) NOT NULL DEFAULT ''"),
        # Attempt history: a reset supersedes the old row instead of wiping it.
        # Existing rows are attempt 1 and live (superseded_at NULL), which is
        # exactly what they were before this existed.
        ("test_assignments", "attempt_no", "INT NOT NULL DEFAULT 1"),
        ("test_assignments", "superseded_at", "DATETIME NULL"),
        ("test_assignments", "created_at", "DATETIME NULL"),
        # Reset audit: who voided an attempt and why.
        ("test_assignments", "superseded_by",
         "NVARCHAR(255) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("test_assignments", "reset_reason",
         "NVARCHAR(400) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(400) NOT NULL DEFAULT ''"),
        # Public job portal: publish flag + application window + role details.
        ("jobs", "is_published", f"{_bool} NOT NULL DEFAULT 0"),
        ("jobs", "application_deadline", "DATETIME NULL"),
        ("jobs", "location",
         "NVARCHAR(300) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(300) NOT NULL DEFAULT ''"),
        ("jobs", "employment_type",
         "NVARCHAR(60) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(60) NOT NULL DEFAULT ''"),
        ("jobs", "openings", "INT NOT NULL DEFAULT 1"),
        # Candidate now links back to the portal Applicant (NULL for legacy
        # HR-uploaded rows), records how it entered, its resume file, and which
        # Kanban stage it sits in.
        ("candidates", "applicant_uuid",
         "NVARCHAR(36) NULL" if is_mssql else "VARCHAR(36) NULL"),
        ("candidates", "source",
         "NVARCHAR(20) NOT NULL DEFAULT 'upload'" if is_mssql
         else "VARCHAR(20) NOT NULL DEFAULT 'upload'"),
        ("candidates", "resume_path",
         "NVARCHAR(500) NOT NULL DEFAULT ''" if is_mssql
         else "VARCHAR(500) NOT NULL DEFAULT ''"),
        ("candidates", "stage_id", "INT NULL"),
    ]
    with core_db.engine.begin() as conn:
        for table, column, ddl in additions:
            # In MSSQL, INFORMATION_SCHEMA is already scoped to the connected
            # database (TABLE_SCHEMA = 'dbo'); in MySQL, TABLE_SCHEMA is the
            # database name. Filtering by table + column alone is correct for
            # both since each connection is scoped to one database.
            exists = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"),
                {"t": table, "c": column}).scalar()
            if not exists:
                add_kw = "ADD" if is_mssql else "ADD COLUMN"
                conn.execute(text(
                    f"ALTER TABLE {table} {add_kw} {column} {ddl}"))
                print(f"Migrated: added {table}.{column}")


def _create_database():
    """Create the target database if it doesn't exist (dialect-aware)."""
    if _dialect() == "mssql":
        # CREATE DATABASE cannot run inside a transaction -> AUTOCOMMIT.
        server = core_db.make_engine("master")
        with server.connect().execution_options(
                isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(
                f"IF DB_ID('{core_db.DB_NAME}') IS NULL "
                f"CREATE DATABASE [{core_db.DB_NAME}]"))
        print(f"Database '{core_db.DB_NAME}' ready (SQL Server).")
        return
    # MySQL
    try:
        server = core_db.make_engine("")
        with server.connect() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS {core_db.DB_NAME} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()
        print(f"Database '{core_db.DB_NAME}' ready (MySQL).")
    except Exception as e:  # noqa: BLE001
        with core_db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"Database '{core_db.DB_NAME}' already exists ({e}).")


def _seed_pipeline_stages():
    """Create the global default Kanban columns once (idempotent)."""
    with core_db.session() as s:
        existing = s.execute(
            select(PipelineStage).where(PipelineStage.department_id.is_(None))
        ).scalars().first()
        if existing is not None:
            return
        for order, (name, kind) in enumerate(DEFAULT_STAGES):
            s.add(PipelineStage(name=name, kind=kind, sort_order=order,
                                department_id=None))
        print(f"Seeded {len(DEFAULT_STAGES)} default pipeline stages.")


def _backfill_candidate_stages():
    """Place existing candidates (stage_id NULL) onto the board by status."""
    with core_db.session() as s:
        stages = {st.name: st.id for st in s.execute(
            select(PipelineStage).where(
                PipelineStage.department_id.is_(None))).scalars()}
        if not stages:
            return
        fallback = stages.get("Screening")
        rows = s.execute(select(Candidate).where(
            Candidate.stage_id.is_(None))).scalars().all()
        moved = 0
        for c in rows:
            target = _STATUS_TO_STAGE.get(c.status)
            c.stage_id = stages.get(target, fallback)
            moved += 1
        if moved:
            print(f"Backfilled stage for {moved} candidate(s).")


def main():
    if not core_db.db_enabled():
        print("ERROR: database not configured in .env (set DB_PROVIDER and "
              "the matching connection settings).")
        sys.exit(1)

    print(f"Provider: {core_db.PROVIDER}")
    _create_database()

    Base.metadata.create_all(core_db.engine)
    print("Tables created.")
    _ensure_columns()

    with core_db.session() as s:
        for name in SEED_DEPARTMENTS:
            if not s.execute(select(Department).where(
                    Department.name == name)).scalar_one_or_none():
                s.add(Department(name=name))
                print(f"Seeded department: {name}")

        if not s.execute(select(User).where(
                User.email == SUPER_ADMIN_EMAIL)).scalar_one_or_none():
            s.add(User(
                name="Super Admin",
                email=SUPER_ADMIN_EMAIL,
                password_hash=hash_password(SUPER_ADMIN_PASSWORD),
                role="super_admin",
                department_id=None,
            ))
            print(f"Seeded super admin: {SUPER_ADMIN_EMAIL} / "
                  f"{SUPER_ADMIN_PASSWORD}  <-- change this password!")

    _seed_pipeline_stages()
    _backfill_candidate_stages()

    print("\nDone. Start the portal backend with:")
    print("  uvicorn portal.backend.main:app --reload --port 8000")


if __name__ == "__main__":
    main()
