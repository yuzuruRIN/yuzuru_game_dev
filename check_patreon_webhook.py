"""
check_patreon_webhook.py — ตรวจสุขภาพ Webhook ฝั่ง Patreon

ใช้หาสาเหตุ event หาย เช่น กรณี "คนอัปเกรด Tier แล้ว Supabase ไม่อัปเดต":
  • paused = True                    -> Patreon หยุดส่งเพราะยิงไม่สำเร็จติดกันหลายครั้ง
  • num_consecutive_times_failed > 0 -> มี event ค้างอยู่ในคิว ยังไม่ถูกส่ง
  • triggers ไม่ครบ                  -> เช่น ไม่ได้ติ๊ก members:pledge:update ไว้

การใช้:
  # ดูสถานะ (token = Creator's Access Token ตัวเดียวกับ PATREON_ACCESS_TOKEN บน Render)
  python check_patreon_webhook.py --token <PATREON_ACCESS_TOKEN>

  # สั่งปลด pause + ให้ Patreon ทยอยส่ง event ที่ค้างมาใหม่
  python check_patreon_webhook.py --token <TOKEN> --unpause <WEBHOOK_ID>
"""

import argparse
import json
import os
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://www.patreon.com/api/oauth2/v2/webhooks"
FIELDS = "?fields%5Bwebhook%5D=uri,triggers,paused,last_attempted_at,num_consecutive_times_failed"


def call(url, token, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/vnd.api+json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.getenv("PATREON_ACCESS_TOKEN"),
                   help="Creator's Access Token (ค่าเดียวกับ PATREON_ACCESS_TOKEN บน Render)")
    p.add_argument("--unpause", metavar="WEBHOOK_ID",
                   help="ปลด pause ของ webhook id นี้ เพื่อให้ Patreon ส่ง event ที่ค้างมาใหม่")
    args = p.parse_args()

    if not args.token:
        sys.exit("ต้องใส่ --token หรือ set PATREON_ACCESS_TOKEN ก่อนครับ")

    if args.unpause:
        body = {"data": {"type": "webhook", "id": args.unpause,
                         "attributes": {"paused": False}}}
        call(f"{API}/{args.unpause}", args.token, method="PATCH", body=body)
        print(f"✅ ปลด pause webhook {args.unpause} แล้ว — Patreon จะทยอยส่ง event ที่ค้างมาใหม่")
        return

    result = call(API + FIELDS, args.token)
    webhooks = result.get("data", [])
    if not webhooks:
        print("⚠️ token นี้ไม่มี webhook เลย — แปลว่า webhook ที่ใช้อยู่ถูกสร้างผ่านหน้าเว็บ")
        print("   ให้เช็คที่ https://www.patreon.com/portal/registration/register-webhooks แทน")
        return

    for wh in webhooks:
        a = wh.get("attributes", {})
        failed = a.get("num_consecutive_times_failed", 0)
        paused = a.get("paused", False)
        print(f"── Webhook {wh.get('id')} ──")
        print(f"   URL          : {a.get('uri')}")
        print(f"   triggers     : {', '.join(a.get('triggers', []))}")
        print(f"   last attempt : {a.get('last_attempted_at')}")
        print(f"   fail streak  : {failed}")
        print(f"   paused       : {paused}")
        if paused or failed > 0:
            print(f"   🔴 มี event ค้าง! รัน: python check_patreon_webhook.py --token <TOKEN> --unpause {wh.get('id')}")
        else:
            print("   🟢 ปกติดี")
        print()


if __name__ == "__main__":
    main()
