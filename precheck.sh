#!/usr/bin/env bash
# Pre-release sanity check for sungrow_ems. Run from the repo root.
# Usage: ./precheck.sh v0.1.4
set -u
TAG="${1:-v0.1.4}"
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

echo "== 3. Phase 1 is read-only =="
if ls custom_components/sungrow_ems/{select,number,switch}.py 2>/dev/null; then
  echo "!! Phase 2 platform files present on this branch"; fail=1
else
  echo "ok: no select/number/switch platforms"
fi

[ $fail -eq 0 ] && echo "== All checks passed: tag $TAG and mark it as a pre-release ==" || echo "== Fix the items marked !! before tagging =="
exit $fail
