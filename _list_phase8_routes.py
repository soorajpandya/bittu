import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/openapi.json").read())
for p in sorted(d["paths"]):
    if "fin-reports" in p:
        print(p)
