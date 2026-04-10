# -*- coding: utf-8 -*-
import json
import re
import os
import sys
import urllib.request
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://tms.tgl-cloud.com/"
AVATAR_DIR = "personal_avatar"

with open("raw_data.json", "r", encoding="utf-8") as f:
    content = f.read()

data = json.loads(content[re.search(r"\[", content).start():])

seen = set()
users = []

def extract(nodes):
    if isinstance(nodes, dict):
        nodes = [nodes]
    for n in nodes:
        for u in n.get("users", []):
            code = u.get("tglCode")
            if code and code not in seen:
                seen.add(code)
                users.append(u)
        extract(n.get("children", []))

extract(data)
os.makedirs(AVATAR_DIR, exist_ok=True)

ok = skip = fail = 0

for u in users:
    img = u.get("profileImageUrl")
    name = u.get("fullName", "?")

    if not img:
        skip += 1
        continue

    dest = os.path.join(AVATAR_DIR, img)
    if os.path.exists(dest):
        skip += 1
        continue

    # URL-encode filename để xử lý tiếng Việt
    url = BASE + quote(img, safe="")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        ok += 1
        print(f"[{ok}] {name} — {img}")
    except Exception as e:
        fail += 1
        print(f"FAIL: {img} — {e}")

print(f"\nXong! Tải được: {ok} | Bỏ qua: {skip} | Lỗi: {fail}")
