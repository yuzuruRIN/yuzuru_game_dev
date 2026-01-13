from fastapi import FastAPI
from supabase import create_client
from jose import jwt, JWTError
from datetime import datetime, timedelta
import os

# =====================
# Environment
# =====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_NOW")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# =====================
# JWT Utils
# =====================
def create_token(email: str):
    payload = {
        "sub": email,
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

# =====================
# Login
# =====================
@app.post("/login")
def login(data: dict):
    email = data.get("email")

    if not email:
        return {"result": "fail"}

    res = (
        supabase
        .table("member_list")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if not res.data:
        return {"result": "fail"}

    member = res.data[0]

    if member.get("blacklist") is True:
        return {"result": "banned"}

    token = create_token(email)

    return {
        "result": "ok",
        "token": token
    }

# =====================
# Verify Token
# =====================
@app.post("/verify-token")
def verify(data: dict):
    token = data.get("token")

    if not token:
        return {"result": "invalid"}

    email = verify_token(token)
    if not email:
        return {"result": "invalid"}

    return {
        "result": "ok",
        "email": email
    }

# =====================
# Use Cheat Code
# =====================
@app.post("/use-cheat")
def use_cheat(data: dict):
    token = data.get("token")
    cheat_code = data.get("cheat_code")

    if not token or not cheat_code:
        return {"result": "fail"}

    # 1. Verify token
    email = verify_token(token)
    if not email:
        return {"result": "unauthorized"}

    # 2. Check member, blacklist, tier
    member_res = (
        supabase
        .table("member_list")
        .select("blacklist, tier")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if not member_res.data:
        return {"result": "unauthorized"}

    member = member_res.data[0]

    if member.get("blacklist") is True:
        return {"result": "banned"}

    member_tier = member.get("tier")

    # 3. Check cheat code exists & active
    cheat_res = (
        supabase
        .table("cheatcode_check_list")
        .select("*")
        .eq("code", cheat_code)
        .limit(1)
        .execute()
    )

    if not cheat_res.data:
        return {"result": "invalid_code"}

    cheat = cheat_res.data[0]

    if cheat.get("is_active") is not True:
        return {"result": "code_disabled"}

    # 4. Check tier permission
    allowed_tiers = cheat.get("allowed_tiers", [])

    if allowed_tiers and member_tier not in allowed_tiers:
        return {
            "result": "tier_not_allowed"
        }

    amount_limit = cheat.get("amount_limit", 0)

    # 5. Check usage
    usage_res = (
        supabase
        .table("cheatcode_usage")
        .select("*")
        .eq("member_email", email)
        .eq("code", cheat_code)
        .limit(1)
        .execute()
    )

    if usage_res.data:
        usage = usage_res.data[0]
        used_count = usage.get("used_count", 0)

        if amount_limit > 0 and used_count >= amount_limit:
            return {"result": "limit_reached"}

        supabase.table("cheatcode_usage").update({
            "used_count": used_count + 1
        }).eq("member_email", email).eq("code", cheat_code).execute()

    else:
        if amount_limit > 0:
            supabase.table("cheatcode_usage").insert({
                "member_email": email,
                "code": cheat_code,
                "used_count": 1
            }).execute()
        else:
            return {"result": "limit_reached"}

    return {
        "result": "ok",
        "effect": cheat.get("effect"),
        "payload": cheat.get("payload")
    }
