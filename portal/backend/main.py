"""ATS Portal backend.

Run from the project root (D:\\ATSResume):

    uvicorn portal.backend.main:app --reload --port 8000

Requires DB settings in .env (DB_PROVIDER + connection, see core/db.py) and an
initialized database (python -m core.init_db).
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.db import db_enabled
from portal.backend.routers import admin, candidate, interview, portal

load_dotenv()

app = FastAPI(title="ATS Portal API", version="0.1.0")

_frontend_origin = os.getenv("PORTAL_FRONTEND_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(candidate.router)
app.include_router(interview.router)
app.include_router(admin.router)
app.include_router(portal.router)


@app.get("/health")
def health():
    return {"ok": True, "database_configured": db_enabled()}
