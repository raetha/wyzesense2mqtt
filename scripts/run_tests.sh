#!/usr/bin/env bash
# run_tests.sh — run the WyzeSense2MQTT test suite
#
# Usage:
#   bash scripts/run_tests.sh                        # lint + unit/integration tests
#   bash scripts/run_tests.sh --hardware             # also run dongle hardware smoke tests
#   bash scripts/run_tests.sh --hardware --dongle /dev/hidraw1
#   bash scripts/run_tests.sh --coverage             # show per-module coverage report
#   bash scripts/run_tests.sh -k test_sensor         # pass -k filter to pytest
#   bash scripts/run_tests.sh -x                     # stop on first failure
#   bash scripts/run_tests.sh -v                     # extra verbose pytest output
#
# Hardware tests require access to /dev/hidraw* which typically needs sudo:
#   sudo bash scripts/run_tests.sh --hardware [--dongle /dev/wyzesense]
#
# The venv is created automatically on first run at .venv/ and reused on
# subsequent runs.  To force a clean rebuild, delete it first:
#   rm -rf .venv && bash scripts/run_tests.sh
#
# Hardware tests (--hardware):
#   Require a physical WyzeSense USB dongle.  The bridge service must NOT be
#   running while these tests execute.  By default the dongle is located
#   automatically (the same auto-detection the bridge uses at startup).
#   Pass --dongle /dev/hidrawN to target a specific device.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Argument parsing ──────────────────────────────────────────────────────────

HARDWARE=0
COVERAGE=0
DONGLE="auto"
PYTEST_EXTRA_ARGS=""
VENV="${VENV:-.venv}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hardware)       HARDWARE=1; shift ;;
        --dongle)         DONGLE="$2"; shift 2 ;;
        --coverage)       COVERAGE=1; shift ;;
        --venv)           VENV="$2"; shift 2 ;;
        -v|--verbose)     PYTEST_EXTRA_ARGS="$PYTEST_EXTRA_ARGS -v"; shift ;;
        -x)               PYTEST_EXTRA_ARGS="$PYTEST_EXTRA_ARGS -x"; shift ;;
        -k)               PYTEST_EXTRA_ARGS="$PYTEST_EXTRA_ARGS -k $2"; shift 2 ;;
        --help|-h)
            sed -n '/^# Usage:/,/^[^#]/{ /^[^#]/d; s/^# \{0,2\}//; p }' "$0"
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

STEPS=3
[[ $HARDWARE -eq 1 ]] && STEPS=4

# ── Venv setup ────────────────────────────────────────────────────────────────

if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "[ setup ] Creating venv at $VENV ..."
    python3 -m venv "$VENV" \
        || { echo "ERROR: Could not create venv — is python3 installed?"; exit 1; }
    echo "[ setup ] Installing dependencies..."
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r wyzesense2mqtt/requirements.txt -q
    "$VENV/bin/pip" install pytest pytest-cov ruff -q
    echo "[ setup ] Done."
    echo ""
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
PYTHON="$VENV/bin/python"
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "unknown")

echo "========================================"
echo " WyzeSense2MQTT test runner"
echo " Python:   $PY_VERSION  ($PYTHON)"
echo " Venv:     $VENV"
echo " Hardware: $([[ $HARDWARE -eq 1 ]] && echo "yes (dongle: $DONGLE)" || echo "no")"
echo " Coverage: $([[ $COVERAGE -eq 1 ]] && echo "yes" || echo "no")"
echo "========================================"
echo ""

FAILED=0

# ── Step 1: ruff check ────────────────────────────────────────────────────────
echo "[ 1/$STEPS ] ruff check wyzesense2mqtt/..."
ruff check wyzesense2mqtt/ && echo "        PASS" || { echo "        FAIL"; FAILED=1; }

# ── Step 2: ruff format check ─────────────────────────────────────────────────
echo "[ 2/$STEPS ] ruff format --check wyzesense2mqtt/..."
ruff format --check wyzesense2mqtt/ && echo "        PASS" || { echo "        FAIL"; FAILED=1; }

# ── Step 3: pytest unit / integration tests ───────────────────────────────────
echo "[ 3/$STEPS ] pytest (unit + integration tests)..."
if [[ $COVERAGE -eq 1 ]]; then
    # shellcheck disable=SC2086
    "$PYTHON" -m pytest \
        --rootdir="$REPO_ROOT" \
        --cov=wyzesense2mqtt \
        --cov-report=term-missing \
        $PYTEST_EXTRA_ARGS \
        && echo "        PASS" || { echo "        FAIL"; FAILED=1; }
else
    # shellcheck disable=SC2086
    "$PYTHON" -m pytest --rootdir="$REPO_ROOT" $PYTEST_EXTRA_ARGS \
        && echo "        PASS" || { echo "        FAIL"; FAILED=1; }
fi

# ── Step 4: hardware smoke tests (optional) ───────────────────────────────────
if [[ $HARDWARE -eq 1 ]]; then
    echo "[ 4/$STEPS ] pytest hardware smoke tests (dongle: $DONGLE)..."
    echo "             NOTE: bridge service must not be running"
    # shellcheck disable=SC2086
    "$PYTHON" -m pytest --rootdir="$REPO_ROOT" -m dongle --dongle "$DONGLE" $PYTEST_EXTRA_ARGS \
        && echo "        PASS" || { echo "        FAIL"; FAILED=1; }
fi

echo ""
echo "========================================"
if [[ $FAILED -eq 0 ]]; then
    echo " ALL CHECKS PASSED"
else
    echo " ONE OR MORE CHECKS FAILED"
fi
echo "========================================"

exit $FAILED
