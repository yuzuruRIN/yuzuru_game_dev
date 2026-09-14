"""
ทดสอบ logic ของ Device Login โดยไม่ต้องต่อ Supabase จริง

ดึงเฉพาะบล็อก Device Login ท้าย main.py มา exec ในสภาพแวดล้อมจำลอง
(stub app/supabase แต่ใช้ jose/hmac/hashlib ของจริง) จึงรันได้โดยไม่ต้องมี
SUPABASE_URL / SUPABASE_SERVICE_KEY และไม่แตะข้อมูลจริงเลย

วิธีรัน:   python tests/test_auth_logic.py
ต้องมี:    pip install python-jose
"""
import io
import os
import sys
import hmac
import hashlib
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(HERE), "main.py")
MARKER = "# Device Login — ระบบล็อกอินหน้าแรกของเกม"

src = io.open(MAIN, encoding="utf-8").read()
block = src[src.index(MARKER):]


class _App:
    """stub FastAPI: decorator คืนฟังก์ชันเดิม"""
    def post(self, *a, **k):
        return lambda fn: fn

    def get(self, *a, **k):
        return lambda fn: fn


def _as_text(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


ns = {
    "os": os, "hmac": hmac, "hashlib": hashlib, "requests": None,
    "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
    "jwt": jwt, "JWTError": JWTError,
    "app": _App(), "supabase": None,
    "JWT_SECRET": "test-secret-abc123", "JWT_ALGORITHM": "HS256",
    "SYNC_TOKEN": "admintoken",
    "_as_text": _as_text, "_as_int": _as_int,
    "_get_member": lambda e: None, "_log": lambda w, e: None,
    "now_iso": lambda: datetime.now(timezone.utc).isoformat(),
}
exec(compile(block, MAIN, "exec"), ns)

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}: {got!r}")


print("── _gen_otp ────────────────────────────────────────────")
codes = {ns["_gen_otp"]() for _ in range(200)}
check("ยาว 6 หลักทุกตัว", all(len(c) == 6 and c.isdigit() for c in codes), True)
check("สุ่มจริง (ไม่ซ้ำกันเกินไป)", len(codes) > 150, True)

print("\n── _hash_otp ───────────────────────────────────────────")
h1 = ns["_hash_otp"]("a@b.com", "123456")
check("ค่าเดิมได้ hash เดิม", ns["_hash_otp"]("a@b.com", "123456") == h1, True)
check("ไม่สนตัวพิมพ์ของอีเมล", ns["_hash_otp"]("A@B.COM", "123456") == h1, True)
check("คนละอีเมล -> คนละ hash", ns["_hash_otp"]("c@d.com", "123456") != h1, True)
check("คนละรหัส -> คนละ hash", ns["_hash_otp"]("a@b.com", "123457") != h1, True)
check("ไม่ใช่เลขดิบ (เก็บเป็น hash)", "123456" not in h1, True)

print("\n── _is_placeholder_email ───────────────────────────────")
check("handle@donator.discord", ns["_is_placeholder_email"]("Yuzu@donator.discord"), True)
check("อีเมลจริง", ns["_is_placeholder_email"]("somchai@gmail.com"), False)

print("\n── _parse_iso ──────────────────────────────────────────")
check("รูปแบบ +00:00", ns["_parse_iso"]("2026-09-14T10:00:00+00:00").year, 2026)
check("รูปแบบ Z", ns["_parse_iso"]("2026-09-14T10:00:00Z").tzinfo is not None, True)
check("ไม่มี tz -> เติม UTC", ns["_parse_iso"]("2026-09-14T10:00:00").tzinfo, timezone.utc)
check("ค่าพัง -> None", ns["_parse_iso"]("ไม่ใช่วันที่"), None)
check("ค่าว่าง -> None", ns["_parse_iso"](None), None)

print("\n── _device_slots ──────────────────────────────────────")
check("ค่าปกติ", ns["_device_slots"]({"device_slots": 3}), 3)
check("NULL -> default 2", ns["_device_slots"]({"device_slots": None}), 2)
check("0 -> default 2 (ไม่ล็อกตัวเอง)", ns["_device_slots"]({"device_slots": 0}), 2)
check("ไม่มีคอลัมน์ -> default 2", ns["_device_slots"]({}), 2)

print("\n── token: ผูกเครื่อง (ส่วนที่สำคัญที่สุด) ──────────────────")
tok = ns["_create_game_token"]("Player@Mail.com", "HWID-AAA", 42)
good = ns["_decode_game_token"](tok, "HWID-AAA")
check("เครื่องเดิม -> ผ่าน", good is not None, True)
check("อีเมลถูกเก็บเป็นตัวพิมพ์เล็ก", good["sub"], "player@mail.com")
check("device_id ติดไปใน token", good["did"], 42)
check("เครื่องอื่น -> ถูกปฏิเสธ", ns["_decode_game_token"](tok, "HWID-BBB"), None)
check("ไม่ส่ง hwid -> ถูกปฏิเสธ", ns["_decode_game_token"](tok, ""), None)

cheat_tok = jwt.encode(
    {"sub": "player@mail.com", "exp": datetime.now(timezone.utc) + timedelta(days=7)},
    "test-secret-abc123", algorithm="HS256")
check("token ระบบโกงเดิมใช้แทนไม่ได้", ns["_decode_game_token"](cheat_tok, "HWID-AAA"), None)

forged = jwt.encode({"sub": "x@y.com", "hwid": "HWID-AAA", "did": 1, "typ": "game",
                     "exp": datetime.now(timezone.utc) + timedelta(days=30)},
                    "wrong-secret", algorithm="HS256")
check("เซ็นด้วย secret ผิด -> ถูกปฏิเสธ", ns["_decode_game_token"](forged, "HWID-AAA"), None)

expired = jwt.encode({"sub": "x@y.com", "hwid": "HWID-AAA", "did": 1, "typ": "game",
                      "exp": datetime.now(timezone.utc) - timedelta(days=1)},
                     "test-secret-abc123", algorithm="HS256")
check("token หมดอายุ -> ถูกปฏิเสธ", ns["_decode_game_token"](expired, "HWID-AAA"), None)

print("\n── _device_public ─────────────────────────────────────")
pub = ns["_device_public"]({"id": 1, "platform": "windows", "label": None,
                            "hwid_hash": "ความลับ", "activated_at": "x", "last_seen_at": "y"})
check("ไม่หลุด hwid_hash ออกไป", "hwid_hash" not in pub, True)
check("คืนคีย์ครบ", sorted(pub), ["activated_at", "id", "label", "last_seen_at", "platform"])

print("\n" + "=" * 56)
print("ล้มเหลว 0 เคส — ผ่านทั้งหมด" if not fails else f"ล้มเหลว {len(fails)}: {fails}")
sys.exit(1 if fails else 0)
