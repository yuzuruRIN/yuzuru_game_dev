"""
inspect_patreon_member.py — ส่องข้อมูลดิบของสมาชิกจาก Patreon API

ใช้ตรวจว่า Patreon รายงาน tier / สถานะอะไรจริงๆ (ก่อนโทษฐานข้อมูล):
  python inspect_patreon_member.py --token <PATREON_ACCESS_TOKEN> --campaign <CAMPAIGN_ID> --email someone@gmail.com
ไม่ใส่ --email = แสดงทุกคน
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.getenv("PATREON_ACCESS_TOKEN"))
    p.add_argument("--campaign", default=os.getenv("PATREON_CAMPAIGN_ID"))
    p.add_argument("--email", help="กรองเฉพาะอีเมลนี้")
    args = p.parse_args()
    if not args.token or not args.campaign:
        sys.exit("ต้องใส่ --token และ --campaign (ค่าเดียวกับ env บน Render)")

    url = (f"https://www.patreon.com/api/oauth2/v2/campaigns/{args.campaign}/members?"
           + urllib.parse.urlencode({
               "include": "user,currently_entitled_tiers",
               "fields[member]": "email,full_name,patron_status,last_charge_status,"
                                 "next_charge_date,currently_entitled_amount_cents,pledge_relationship_start",
               "fields[user]": "email,full_name",
               "fields[tier]": "title,amount_cents",
               "page[count]": 100,
           }))
    target = args.email.lower().strip() if args.email else None

    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {args.token}", "User-Agent": "PatreonInspect/1.0"})
        payload = json.load(urllib.request.urlopen(req, timeout=60))
        tiers = {i["id"]: i.get("attributes", {}) for i in payload.get("included", [])
                 if i.get("type") == "tier"}

        for m in payload.get("data", []):
            a = m.get("attributes", {})
            email = (a.get("email") or "").lower().strip()
            if target and email != target:
                continue
            print(f"── {a.get('full_name')} <{email}> ──")
            print(f"   patron_status      : {a.get('patron_status')}")
            print(f"   last_charge_status : {a.get('last_charge_status')}")
            print(f"   next_charge_date   : {a.get('next_charge_date')}")
            print(f"   entitled ตอนนี้     : ${(a.get('currently_entitled_amount_cents') or 0)/100:.2f}")
            refs = m.get("relationships", {}).get("currently_entitled_tiers", {}).get("data", [])
            if not refs:
                print("   currently_entitled_tiers: (ว่าง)")
            for i, r in enumerate(refs):
                t = tiers.get(r.get("id"), {})
                print(f"   tier[{i}] id={r.get('id')}: {t.get('title')} "
                      f"(${(t.get('amount_cents') or 0)/100:.2f})")
            print()
        url = payload.get("links", {}).get("next")


if __name__ == "__main__":
    main()
