"""Stricter orphan finder.
For each table, count references in:
  A) app/services/  app/api/  (real usage)
  B) app/models/    (just an ORM mapping — doesn't prove use)
  C) migrations/*.sql + alembic/  (DDL only)
Flag tables with A=0 (no service / api references).
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent
APP = ROOT / "app"

raw = (ROOT / "_all_tables.txt").read_bytes()
text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "ignore")
tables = []
for line in text.splitlines():
    if "\t" in line:
        n, c = line.split("\t", 1)
        tables.append((n.strip(), c.strip()))

PARTITION_RE = re.compile(r"_(default|y\d{4}m\d{2})$")

services_files = list((APP / "services").rglob("*.py")) + list((APP / "api").rglob("*.py")) + list((APP / "core").rglob("*.py")) + list((APP / "dependencies").rglob("*.py")) + list((APP / "middleware").rglob("*.py")) + list((APP / "realtime").rglob("*.py"))
models_files   = list((APP / "models").rglob("*.py")) + list((APP / "schemas").rglob("*.py"))

def load(files):
    return {p: p.read_text(encoding="utf-8", errors="ignore").lower() for p in files}

services_text = load(services_files)
models_text   = load(models_files)

print(f"service/api/core files: {len(services_text)}   models/schemas: {len(models_text)}\n")

zero_service = []
model_only = []
for name, count in tables:
    if PARTITION_RE.search(name):
        continue
    needle = name.lower()
    # Use word-boundary-ish check: surround with non-alnum or start/end
    pattern = re.compile(r"(^|[^a-z0-9_])" + re.escape(needle) + r"([^a-z0-9_]|$)")
    s_hits = sum(1 for txt in services_text.values() if pattern.search(txt))
    m_hits = sum(1 for txt in models_text.values() if pattern.search(txt))
    if s_hits == 0 and m_hits == 0:
        zero_service.append((name, count, "NO refs anywhere in app/"))
    elif s_hits == 0 and m_hits > 0:
        model_only.append((name, count, f"only in models/schemas ({m_hits} files)"))

print("=== A. Tables NOT referenced anywhere in app/ ===")
for n, c, why in zero_service:
    print(f"  {n:40s} rows={c:6s}  {why}")
print(f"  total: {len(zero_service)}\n")

print("=== B. Tables referenced ONLY in app/models or app/schemas (no services/api use) ===")
for n, c, why in model_only:
    print(f"  {n:40s} rows={c:6s}  {why}")
print(f"  total: {len(model_only)}")
