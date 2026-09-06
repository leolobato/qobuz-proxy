"""Compare issue #23/#21 regressions against old source and the working tree.

Run with: uv run python scripts/reproduce_issue_regressions.py
Uses disposable git archives; never checks out branches, changes config, or pushes.
"""

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    (
        "#23",
        "3e61d5c",
        "tests/integration/test_inactive_reconnect_protocol.py",
        "An idle reconnect must not claim playback",
    ),
    (
        "#21",
        "9876048",
        "tests/integration/test_unavailable_skip_protocol.py::test_advances_without_unsolicited_state_response",
        "No next track received after unplayable track 500",
    ),
)


def run_tests(directory: Path, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target, "--tb=short", "--disable-warnings"],
        cwd=directory,
        env={**os.environ, "PYTHONPATH": str(directory)},
        text=True,
        capture_output=True,
    )


def main() -> int:
    for issue, revision, target, expected_failure in CASES:
        with tempfile.TemporaryDirectory(prefix="qobuz-regression-") as directory:
            checkout = Path(directory)
            source = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(source)) as archive:
                archive.extractall(checkout, filter="data")
            shutil.copytree(
                ROOT / "tests/integration",
                checkout / "tests/integration",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            result = run_tests(checkout, target)
            print(f"{issue} baseline {revision}:\n{result.stdout}", flush=True)
            if result.returncode != 1 or expected_failure not in result.stdout:
                print(result.stderr, file=sys.stderr)
                print("Baseline did not fail for the expected behavior.", file=sys.stderr)
                return 1

    result = run_tests(ROOT, "tests/integration/")
    print(f"Current working tree:\n{result.stdout}", flush=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return 1
    print("Both baseline failures reproduced; current protocol regressions pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
