import json, sys, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/openapi.json").read())
for p in sorted(d["paths"]):
    if "refund" in p or "dispute" in p:
        print(p)
