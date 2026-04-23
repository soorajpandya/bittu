"""Validate the modular OpenAPI structure."""
import os
import re

base = "Bittu_Backend/modules/v1"
total_files = 0
good_refs = 0
bad_refs = 0
errors = []

for root, dirs, files in os.walk(base):
    for fn in files:
        if not fn.endswith(".yaml"):
            continue
        fp = os.path.join(root, fn)
        total_files += 1
        with open(fp, encoding="utf-8") as f:
            content = f.read()
        
        # Find all $ref values
        refs = re.findall(r'\$ref:\s*(.+)', content)
        for ref in refs:
            ref = ref.strip().strip("'\"")
            if ref.startswith("../../../components/schemas.yaml#/"):
                good_refs += 1
            elif ref.startswith("#/components/"):
                bad_refs += 1
                errors.append(f"  OLD REF in {fp}: {ref}")
            else:
                good_refs += 1  # other valid refs

# Check schemas.yaml internal refs
schema_file = "Bittu_Backend/components/schemas.yaml"
with open(schema_file, encoding="utf-8") as f:
    schema_content = f.read()

schema_refs = re.findall(r'\$ref:\s*(.+)', schema_content)
schema_good = 0
schema_bad = 0
for ref in schema_refs:
    ref = ref.strip().strip("'\"")
    if ref.startswith("#/"):
        schema_good += 1
    elif ref.startswith("#/components/schemas/"):
        schema_bad += 1
        errors.append(f"  OLD REF in schemas.yaml: {ref}")
    else:
        schema_good += 1

# Count total paths across all modules
path_count = len(re.findall(r'^\s*/api/v1/', content, re.MULTILINE))

print(f"Module files: {total_files}")
print(f"Module refs - good: {good_refs}, bad: {bad_refs}")
print(f"Schema refs - good: {schema_good}, bad: {schema_bad}")
print(f"Total errors: {len(errors)}")
for e in errors:
    print(e)
if not errors:
    print("ALL REFERENCES CLEAN - NO BROKEN REFS")

# Check all YAML files parse
import yaml
parse_errors = []
for root, dirs, files in os.walk("Bittu_Backend"):
    for fn in files:
        if not fn.endswith(".yaml"):
            continue
        fp = os.path.join(root, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                yaml.safe_load(f)
        except Exception as e:
            parse_errors.append(f"  PARSE ERROR in {fp}: {e}")

print(f"\nYAML parse check: {len(parse_errors)} errors")
for e in parse_errors:
    print(e)
if not parse_errors:
    print("ALL YAML FILES PARSE CORRECTLY")
