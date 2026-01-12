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
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except JWTError:
        return None

# =====================
# Login
# =====================
@app.post("/login")
def login(data: dict):
    email = data.get("email")
    code = data.get("code")  # เผื่อใช้ตรวจในอนาคต

    if not email:
        return {"result": "fail"}

    res = supabase.table("member_list") \
        .select("*") \
        .eq("email", email) \
        .execute()

    if not res.data:
        return {"result": "fail"}

    member = res.data[0]

    if member.get("blacklist"):
        return {"result": "banned"}

    token = create_token(email)

    return {
        "result": "ok",
        "token": token
    }
