import sys, json
d = json.load(sys.stdin)
ps = [p for p in d["paths"] if "/audit/events" in p]
print("phase6 routes:", len(ps))
for p in sorted(ps):
    print(" ", p)
