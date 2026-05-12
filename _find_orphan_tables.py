"""Find tables NOT referenced anywhere in app/ source."""
import re
from pathlib import Path

ROOT = Path(__file__).parent
APP = ROOT / "app"
TABLES_FILE = ROOT / "_all_tables.txt"

tables = []
raw = TABLES_FILE.read_bytes()
if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
    text = raw.decode("utf-16")
else:
    text = raw.decode("utf-8", errors="ignore")
for line in text.splitlines():
    if "\t" in line:
        name, count = line.split("\t", 1)
        tables.append((name.strip(), count.strip()))

# Skip partition children — they belong to a parent partitioned table.
PARTITION_RE = re.compile(r"_(default|y\d{4}m\d{2})$")
# Skip sequence helper tables
SEQ_RE = re.compile(r"_seq$")

# Collect all .py source under app/
sources = {}
for p in APP.rglob("*.py"):
    sources[p] = p.read_text(encoding="utf-8", errors="ignore").lower()

print(f"scanned {len(sources)} python files\n")

unref = []
parents_only = []
for name, count in tables:
    if PARTITION_RE.search(name):
        continue
    if SEQ_RE.match(name) or name.endswith("_seq"):
        continue
    needle = name.lower()
    hits = sum(1 for txt in sources.values() if needle in txt)
    if hits == 0:
        unref.append((name, count))

print("=== Tables with NO references in app/ ===")
for n, c in unref:
    print(f"  {n:40s} rows={c}")
print(f"\ntotal orphan tables: {len(unref)}")
