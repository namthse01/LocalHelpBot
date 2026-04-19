import json, urllib.request
msg = (
    'can you read content from this : '
    '"D:\\Code\\yolo\\source_claude\\How_AI_Learn\\The-Complete-Guide-to-Building-Skill-for-Claude.pdf" '
    'Then summarize it as: guilde_Claude.md at this location folder: '
    '"D:\\Code\\yolo\\source_claude"'
)
p = {"model":"code-agent","stream":False,"messages":[{"role":"user","content":msg}]}
req = urllib.request.Request(
    "http://localhost:11435/api/chat",
    data=json.dumps(p).encode("utf-8"),
    headers={"Content-Type":"application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=600) as r:
    raw = r.read().decode("utf-8")
parts=[]
for line in raw.splitlines():
    line=line.strip()
    if not line: continue
    try:
        d=json.loads(line)
    except: continue
    parts.append(d.get("message",{}).get("content","") or d.get("error",""))
print("".join(parts)[:6000])
