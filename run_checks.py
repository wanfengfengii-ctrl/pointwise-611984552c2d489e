"""Full verification chain: original V1 regressions plus hierarchy business checks.

Runs the complete unittest discovery (V1 and hierarchy regressions), then the
public contract entry points in hierarchy_cases.py. Exits with the first
nonzero status, preserving the original discovery's nonzero failure exit.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMANDS = [
    ["python3.11", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
    ["python3.11", "-B", "hierarchy_cases.py", "v1"],
    ["python3.11", "-B", "hierarchy_cases.py", "verify"],
]

def main():
    for command in COMMANDS:
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode != 0:
            return completed.returncode
    return 0

if __name__ == "__main__":
    sys.exit(main())
