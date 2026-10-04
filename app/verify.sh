#!/usr/bin/env bash
# verify container entrypoint:
#   1) build/static checks (python byte-compile, asset presence)
#   2) rule tests (pytest)
#   3) API/HTTP smoke against the running web service
# Exits non-zero (Compose reports the code) if any stage fails.
set -u

FAIL=0
SERVICE_URL="${SERVICE_URL:-http://app:8080}"

echo "== [1/3] build / static checks =="
python -m py_compile app.py engine.py tests/test_engine.py tests/test_api.py \
    && echo "py_compile OK" || { echo "py_compile FAILED"; FAIL=1; }

for f in static/index.html static/app.js static/styles.css; do
    if [ -s "$f" ]; then echo "asset $f OK"; else echo "asset $f MISSING"; FAIL=1; fi
done

echo "== [2/3] rule tests (pytest) =="
python -m pytest -q tests || { echo "pytest FAILED"; FAIL=1; }

echo "== [3/3] API/HTTP smoke against ${SERVICE_URL} =="
python /srv/smoke.py "${SERVICE_URL}" || { echo "HTTP smoke FAILED"; FAIL=1; }

if [ "$FAIL" -eq 0 ]; then
    echo "VERIFY: ALL CHECKS PASSED"
else
    echo "VERIFY: FAILURES DETECTED (exit 1)"
fi
exit "$FAIL"
