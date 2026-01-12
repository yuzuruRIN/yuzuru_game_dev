from fastapi import FastAPI
from supabase import create_client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

@app.post("/login")
def login(data: dict):
    email = data.get("email")

    res = supabase.table("member_list").select("*").eq("email", email).execute()
    if not res.data:
        return {"result": "fail"}

    member = res.data[0]
    if member["blacklist"]:
        return {"result": "banned"}

    token = "DUMMY_TOKEN_FOR_NOW"  # เดี๋ยวเปลี่ยนเป็น JWT
    return {
        "result": "ok",
        "token": token
    }
