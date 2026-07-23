"""Generate a test link for proctoring testing.

Usage:  python generate_test_link.py [email]
If email is omitted, uses the FROM_EMAIL from .env as the candidate email.
"""

import os
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select
from core.db import session
from core.models import (
    Candidate, Department, Job, Question, Test, TestAssignment,
    CandidateAnswer, ProctorEvent,
)

PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "http://localhost:3000").rstrip("/")
CANDIDATE_EMAIL = sys.argv[1] if len(sys.argv) > 1 else os.getenv("FROM_EMAIL", "test@example.com")

SAMPLE_QUESTIONS = [
    {
        "question": "What is the capital of France?",
        "options": ["Berlin", "Madrid", "Paris", "Rome"],
        "correct_index": 2,
    },
    {
        "question": "Which planet is closest to the Sun?",
        "options": ["Venus", "Mercury", "Earth", "Mars"],
        "correct_index": 1,
    },
    {
        "question": "What does HTML stand for?",
        "options": [
            "Hyper Text Markup Language",
            "High Tech Modern Language",
            "Hyper Transfer Markup Language",
            "Home Tool Markup Language",
        ],
        "correct_index": 0,
    },
    {
        "question": "Which data structure uses FIFO?",
        "options": ["Stack", "Queue", "Tree", "Graph"],
        "correct_index": 1,
    },
    {
        "question": "What is 12 × 12?",
        "options": ["124", "144", "132", "156"],
        "correct_index": 1,
    },
]

def main():
    with session() as s:
        # 1. Get or create a department
        dept = s.execute(select(Department)).scalars().first()
        if not dept:
            dept = Department(name="Engineering")
            s.add(dept)
            s.flush()
            print(f"  Created department: {dept.name} (id={dept.id})")

        # 2. Get or create a job
        job = s.execute(
            select(Job).where(Job.title == "Proctor Test Job")
        ).scalars().first()
        if not job:
            job = Job(
                title="Proctor Test Job",
                department_id=dept.id,
                jd_text="Test job for proctoring verification",
                pass_threshold=60,
            )
            s.add(job)
            s.flush()
            print(f"  Created job: {job.title} (uuid={job.uuid})")
        else:
            print(f"  Using existing job: {job.title} (uuid={job.uuid})")

        # 3. Get or create candidate
        cand = s.execute(
            select(Candidate).where(
                Candidate.job_uuid == job.uuid,
                Candidate.email == CANDIDATE_EMAIL.lower(),
            )
        ).scalars().first()
        if not cand:
            cand = Candidate(
                job_uuid=job.uuid,
                name="Test Candidate",
                email=CANDIDATE_EMAIL.lower(),
                status="invited",
                resume_score=80,
            )
            s.add(cand)
            s.flush()
            print(f"  Created candidate: {cand.email} (uuid={cand.uuid})")
        else:
            print(f"  Using existing candidate: {cand.email} (uuid={cand.uuid})")

        # 4. Check for existing pending/started assignment — reset it
        existing_assign = s.execute(
            select(TestAssignment)
            .join(Test, TestAssignment.test_uuid == Test.uuid)
            .where(
                Test.job_uuid == job.uuid,
                TestAssignment.candidate_uuid == cand.uuid,
            )
        ).scalars().first()

        if existing_assign:
            # Reset it so we can reuse
            s.execute(CandidateAnswer.__table__.delete().where(
                CandidateAnswer.assignment_uuid == existing_assign.uuid))
            s.execute(ProctorEvent.__table__.delete().where(
                ProctorEvent.assignment_uuid == existing_assign.uuid))
            existing_assign.status = "pending"
            existing_assign.started_at = None
            existing_assign.submitted_at = None
            existing_assign.test_score = None
            existing_assign.proctor_warnings = 0
            existing_assign.terminated_reason = None
            existing_assign.expires_at = datetime.utcnow() + timedelta(days=7)
            cand.status = "invited"
            s.flush()
            link = f"{PORTAL_BASE_URL}/test/{existing_assign.uuid}"
            print(f"  Reset existing assignment: {existing_assign.uuid}")
            print()
            print("=" * 60)
            print(f"  TEST LINK: {link}")
            print(f"  EMAIL:     {CANDIDATE_EMAIL}")
            print("=" * 60)
            return

        # 5. Create test + questions
        test = Test(
            job_uuid=job.uuid,
            difficulty="medium",
            duration_minutes=15,
            pass_score=60,
            proctored=True,
            approved=True,
        )
        s.add(test)
        s.flush()
        print(f"  Created proctored test (uuid={test.uuid})")

        for q in SAMPLE_QUESTIONS:
            s.add(Question(
                test_uuid=test.uuid,
                question=q["question"],
                options_json=q["options"],
                correct_index=q["correct_index"],
            ))

        # 6. Create assignment
        assign = TestAssignment(
            test_uuid=test.uuid,
            candidate_uuid=cand.uuid,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        s.add(assign)
        cand.status = "invited"
        s.flush()

        link = f"{PORTAL_BASE_URL}/test/{assign.uuid}"
        print(f"  Created assignment (token={assign.uuid})")
        print()
        print("=" * 60)
        print(f"  TEST LINK: {link}")
        print(f"  EMAIL:     {CANDIDATE_EMAIL}")
        print("=" * 60)


if __name__ == "__main__":
    print("Generating proctored test link...")
    main()
    print("\nDone! Open the link above and use the email to verify.")
