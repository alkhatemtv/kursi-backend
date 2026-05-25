"""
TEMPORARY admin routes for one-time ops like seeding the production DB.
Remove this file (and the import in main.py) after seeding is complete.
"""
import os
import secrets
import subprocess
import sys

from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/seed")
def seed_endpoint(x_seed_token: str = Header(default="")):
    """Run seed.py inside the container. Protected by SEED_TOKEN env var."""
    expected = os.environ.get("SEED_TOKEN", "")
    if not expected:
        raise HTTPException(503, "SEED_TOKEN env var not set on server")
    if not secrets.compare_digest(x_seed_token, expected):
        raise HTTPException(403, "Invalid token")

    result = subprocess.run(
        [sys.executable, "seed.py"],
        capture_output=True,
        text=True,
        cwd="/app",
        timeout=120,
    )
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
