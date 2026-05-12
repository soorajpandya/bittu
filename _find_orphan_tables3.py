"""For each table find:
   - INSERT INTO <t> writes (in app/ + migrations/ + alembic/)
   - SELECT FROM/JOIN <t> reads (in app/ + migrations/)
   - row count
A table with 0 inserts AND 0 reads (excluding its CREATE TABLE) is a likely orphan.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent
SEARCH_DIRS = [ROOT / "app", ROOT / "migrations", ROOT / "alembic"]

raw = (ROOT / "_all_tables.txt").read_bytes()
text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "ignore")
tables = []
for line in text.splitlines():
    if "\t" in line:
        n, c = line.split("\t", 1)
        tables.append((n.strip(), c.strip()))

PARTITION_RE = re.compile(r"_(default|y\d{4}m\d{2})$")

# Load all source files
files = {}
for d in SEARCH_DIRS:
    if not d.exists():
        continue
    for ext in ("*.py", "*.sql"):
        for p in d.rglob(ext):
            files[p] = p.read_text(encoding="utf-8", errors="ignore").lower()

print(f"scanned {len(files)} files in app/, migrations/, alembic/\n")

results = []
for name, count in tables:
    if PARTITION_RE.search(name):
        continue
    nl = name.lower()
    # Patterns. Word-boundary-ish (no a-z0-9_ on either side).
    bnd = r"(?<![a-z0-9_])" + re.escape(nl) + r"(?![a-z0-9_])"
    insert_re = re.compile(r"insert\s+into\s+" + bnd, re.I | re.S)
    update_re = re.compile(r"update\s+" + bnd, re.I | re.S)
    delete_re = re.compile(r"delete\s+from\s+" + bnd, re.I | re.S)
    from_re   = re.compile(r"(?:from|join)\s+" + bnd, re.I | re.S)
    create_re = re.compile(r"create\s+(?:unlogged\s+)?(?:table|index|trigger|policy)[^;]*?" + bnd, re.I | re.S)

    inserts = updates = deletes = reads = creates = 0
    for txt in files.values():
        if insert_re.search(txt): inserts += 1
        if update_re.search(txt): updates += 1
        if delete_re.search(txt): deletes += 1
        if from_re.search(txt):   reads   += 1
        if create_re.search(txt): creates += 1
    results.append((name, count, inserts, updates, deletes, reads, creates))

# Truly orphan: 0 inserts AND 0 updates AND 0 deletes AND 0 reads (only CREATE present).
print("=== Tables with NO INSERT/UPDATE/DELETE/SELECT anywhere (only CREATE) ===")
print(f"{'table':40s} {'rows':>6s}  ins upd del read create")
orphan_count = 0
for name, count, ins, upd, dele, rd, cr in sorted(results, key=lambda r: (r[2]+r[3]+r[4]+r[5], r[0])):
    if ins == 0 and upd == 0 and dele == 0 and rd == 0:
        print(f"  {name:38s} {count:>6s}   {ins:>3d} {upd:>3d} {dele:>3d} {rd:>4d} {cr:>6d}")
        orphan_count += 1
print(f"\norphan total: {orphan_count}")

print("\n=== Tables with 0 rows AND only writes through fns (no python INSERT) ===")
# This view is for the user to spot empty tables.
for name, count, ins, upd, dele, rd, cr in sorted(results, key=lambda r: r[0]):
    if count == "0" and (ins + upd + dele + rd) <= 2:
        print(f"  {name:38s} rows={count:>6s}   ins={ins} upd={upd} del={dele} rd={rd}")
