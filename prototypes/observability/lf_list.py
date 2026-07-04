import json, urllib.request, base64
auth=base64.b64encode(b"pk-lf-e2-public:sk-lf-e2-secret").decode()
def get(u):
    r=urllib.request.Request(u, headers={"Authorization":f"Basic {auth}"})
    return json.load(urllib.request.urlopen(r))
d=get("http://localhost:3000/api/public/traces?limit=20")["data"]
runner=[t for t in d if True]
print(f"total traces in project: {len(d)}")
for t in d:
    obs=get(f"http://localhost:3000/api/public/observations?traceId={t['id']}&limit=20")["data"]
    svc=set()
    for o in obs:
        ra=(o.get('metadata') or {}).get('resourceAttributes') or {}
        if ra.get('service.name'): svc.add(ra['service.name'])
    gen=[o for o in obs if o['type']=='GENERATION']
    print(f"  trace={t.get('name')} id={t['id'][:12]} svc={svc} obs={len(obs)} gen_models={[g.get('model') for g in gen]}")
