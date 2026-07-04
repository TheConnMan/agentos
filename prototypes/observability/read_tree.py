import sys, json, urllib.request, base64
trace=open("pt4_trace_id.txt").read().strip()
auth=base64.b64encode(b"pk-lf-pt4-public:sk-lf-pt4-secret").decode()
def get(url):
    r=urllib.request.Request(url, headers={"Authorization":f"Basic {auth}"})
    return json.load(urllib.request.urlopen(r))
d=get(f"http://localhost:3000/api/public/observations?traceId={trace}&limit=50")["data"]
print("=== raw observations ===")
for o in d:
    print(f"  type={o['type']:12} name={str(o.get('name')):16} id={o['id'][:10]} parent={str(o.get('parentObservationId'))[:10]}")
print("\n=== reconstructed tree via parentObservationId ===")
kids={}
for o in d: kids.setdefault(o.get("parentObservationId"), []).append(o)
ids={o["id"] for o in d}
def show(pid, depth):
    for o in sorted(kids.get(pid,[]), key=lambda x:x.get("startTime","")):
        print("    "+"  "*depth+f"|- {o['type']}: {o.get('name')}")
        show(o["id"], depth+1)
roots=[o for o in d if not o.get("parentObservationId") or o.get("parentObservationId") not in ids]
for r in roots:
    print(f"    |- {r['type']}: {r.get('name')}"); show(r["id"],1)
gens=get(f"http://localhost:3000/api/public/observations?traceId={trace}&type=GENERATION&limit=10")["data"]
print("\n=== GENERATION mapping (model-bearing span) ===")
for o in gens:
    print(f"    name={o.get('name')} model={o.get('model')} usageDetails={o.get('usageDetails')}")
