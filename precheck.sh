#!/usr/bin/env bash
# Pre-release sanity check for sungrow_ems. Run from the repo root.
# Usage: ./precheck.sh v0.2.0
set -u
TAG="${1:-v0.2.0}"
fail=0

echo "== 1. Personal / site data =="
# Private IPs, your serial, MAC addresses, emails, HA config paths.
if git grep -nIE '\b(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)\b|A23A0402367|([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|@maximo\.net\.au' -- . ':!precheck.sh'; then
  echo "!! Possible personal data above"; fail=1
else
  echo "ok: no private IPs, serial, MACs or email found"
fi
if git ls-files | grep -iE '(^|/)(secrets\.yaml|configuration\.yaml|automations\.yaml|\.env|.*\.pdf|__pycache__/|.*\.pyc)$'; then
  echo "!! Tracked files above should not be in the repo"; fail=1
else
  echo "ok: no config/secrets/PDF/pycache tracked"
fi

echo "== 2. Version & HACS metadata =="
mver=$(python3 -c 'import json;print(json.load(open("custom_components/sungrow_ems/manifest.json"))["version"])')
if [ "v$mver" = "$TAG" ]; then echo "ok: manifest $mver matches $TAG"; else echo "!! manifest $mver != tag $TAG"; fail=1; fi
if [ -f hacs.json ]; then python3 -m json.tool hacs.json >/dev/null && echo "ok: hacs.json valid"; else echo "!! hacs.json missing at repo root"; fail=1; fi
[ -f README.md ] && echo "ok: README.md present" || { echo "!! README.md missing"; fail=1; }
[ -f LICENSE ] && echo "ok: LICENSE present" || { echo "!! LICENSE missing (check upstream licence terms)"; fail=1; }

echo "== 3. Write safety =="
# Control is opt-in (0.2.0+). pymodbus writes may only appear in modbus.py,
# and only writer.py (SafeWriter) may call the hub's write method.
if grep -rnE 'write_coils?|mask_write|readwrite_registers|encode_(value|block)' custom_components/sungrow_ems --include='*.py'; then
  echo "!! Forbidden write calls found above"; fail=1
else
  echo "ok: no coil / mask / read-write calls"
fi
if grep -rlE '\bwrite_registers?\b' custom_components/sungrow_ems --include='*.py' | grep -v '/modbus.py$'; then
  echo "!! pymodbus write calls outside modbus.py"; fail=1
else
  echo "ok: pymodbus writes only in modbus.py"
fi
if grep -rlE '\basync_write_holding\b' custom_components/sungrow_ems --include='*.py' | grep -vE '/(modbus|writer)\.py$'; then
  echo "!! hub writes outside SafeWriter"; fail=1
else
  echo "ok: hub writes only through writer.SafeWriter"
fi
if python3 -m pytest -q tests/test_write_safety.py >/dev/null 2>&1; then
  echo "ok: write allowlist matches tests/test_write_safety.py"
else
  echo "!! tests/test_write_safety.py fails (allowlist changed?)"; fail=1
fi
if git ls-files | grep -E '(^|/)snapshots/|snapshot_.*\.(json|csv)$'; then
  echo "!! Register snapshots are tracked (they hold the serial and IPs)"; fail=1
else
  echo "ok: no register snapshots tracked"
fi

[ $fail -eq 0 ] && echo "== All checks passed: tag $TAG and mark it as a pre-release ==" || echo "== Fix the items marked !! before tagging =="
exit $fail
