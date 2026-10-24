#!/usr/bin/env python3
import os
import json
import sys
import requests
from dotenv import load_dotenv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

TOKEN = os.getenv("FB_ACCESS_TOKEN", "").strip()
if not TOKEN:
    print("FB_ACCESS_TOKEN not set. Add it to auto_upload/.env or env.")
    sys.exit(1)

resp = requests.get(
    "https://graph.facebook.com/v26.0/me/accounts",
    params={
        "access_token": TOKEN,
        "fields": "id,name,access_token,instagram_business_account{id,username}",
    },
    timeout=15,
)
if resp.status_code != 200:
    print("ERROR", resp.status_code, resp.text[:2000])
    sys.exit(1)
data = resp.json()

pages = []
for page in data.get("data", []):
    ig = page.get("instagram_business_account", {})
    pages.append({
        "page_id": page["id"],
        "name": page.get("name", ""),
        "ig_user_id": ig.get("id", ""),
        "ig_username": ig.get("username", ""),
    })

print(json.dumps(pages, indent=2, ensure_ascii=False))
print(f"\nTotal pages: {len(pages)}")
