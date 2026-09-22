"""
Test bootstrap.

Every test run gets its own throwaway SQLite file so that running the suite
never touches the demo database in backend/data/.
"""
import os
import tempfile
from pathlib import Path

_tmpdir = Path(tempfile.mkdtemp(prefix="btp2027-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_tmpdir / 'test.db').as_posix()}")
os.environ.setdefault("DEMO_MODE", "false")
os.environ.setdefault("HOMOGRAPHY_FILE", str(_tmpdir / "no-such-calibration.json"))
