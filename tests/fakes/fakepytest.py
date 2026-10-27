"""A stand-in for pytest on the test machines: prints what it got and writes a junit report.

  --version          prints a pytest version (the board's health check)
  --collect-only     lists three test ids (Discover)
  otherwise          prints ARGS, the working folder, ENV lines for T_* and PYTEST_BOARD_* variables, the git
                     branch and the MARK file (if any); then TICKS lines half a second apart; exits with RC
TICKS and RC come from the environment (a card's env, for example).
"""
import os
import subprocess
import sys
import time

if "--version" in sys.argv:
    print("pytest 8.0.0")
    sys.exit(0)
if "--collect-only" in sys.argv:
    print("test_a.py::test_one\ntest_a.py::test_two\nsub/test_b.py::test_three")
    print("\n3 tests collected in 0.01s")
    sys.exit(0)
junit = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--junitxml="))
print("ARGS:", " ".join(a for a in sys.argv[1:] if not a.startswith("--junitxml=")), flush=True)
print("CWD:", os.getcwd(), flush=True)
for k in sorted(os.environ):
    if k.startswith(("T_", "PYTEST_BOARD_")):
        print(f"ENV {k}={os.environ[k]}", flush=True)
try:
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
except OSError:
    branch = ""
print("BRANCH:", branch or "-", "| MARK:", open("MARK").read().strip() if os.path.exists("MARK") else "-", flush=True)
n, rc = int(os.environ.get("TICKS", "0")), int(os.environ.get("RC", "0"))
try:
    for i in range(n):
        print(f"tick {i}", flush=True)
        time.sleep(0.5)
except KeyboardInterrupt:
    print("KeyboardInterrupt", flush=True)
    rc = 2
print("done", flush=True)
with open(junit, "w") as f:
    f.write(f'<testsuite tests="3" failures="{1 if rc == 1 else 0}" errors="0" skipped="0"/>')
sys.exit(rc)
