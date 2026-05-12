import sys, json
d = json.load(sys.stdin)
ps = [p for p in d["paths"] if "tax-invoices" in p or "merchant-statements" in p]
print("phase5 routes:", len(ps))
for p in sorted(ps):
    print(" ", p)
