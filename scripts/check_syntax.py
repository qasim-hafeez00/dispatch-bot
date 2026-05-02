"""Syntax-check all files changed in the Copilot review fixes."""
import py_compile, sys, pathlib

FILES = [
    "cortexbot/db/base.py",
    "cortexbot/db/__init__.py",
    "cortexbot/db/models.py",
    "cortexbot/db/score_models.py",
    "cortexbot/db/migrations/versions/003_phase3a_fixes.py",
    "cortexbot/skills/s13_driver_dispatch.py",
    "cortexbot/skills/s19_payment_reconciliation.py",
    "cortexbot/skills/st_quickbooks_sync.py",
    "cortexbot/skills/sy_freight_claims.py",
    "cortexbot/webhooks/twilio.py",
]

root = pathlib.Path(__file__).parent.parent
errors = []

for rel in FILES:
    path = root / rel
    try:
        py_compile.compile(str(path), doraise=True)
        print(f"  OK  {rel}")
    except py_compile.PyCompileError as e:
        print(f"  FAIL {rel}: {e}")
        errors.append(rel)

print()
if errors:
    print(f"FAILED: {len(errors)} file(s) have syntax errors")
    sys.exit(1)
else:
    print(f"ALL {len(FILES)} FILES: syntax OK")
