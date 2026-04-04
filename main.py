from fastapi import FastAPI, HTTPException, Query
from supabase import create_client
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import requests
import os

# =====================
# Environment
# =====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_NOW")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7

PATREON_ACCESS_TOKEN = os.getenv("PATREON_ACCESS_TOKEN")
PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")
SYNC_TOKEN = os.getenv("SYNC_TOKEN")

DEV_EMAILS = ["lxpetitprixce@gmail.com", "devthelastyear@yuzuru.rin"]

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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# =====================
# Patreon Sync Utils
# =====================
def build_included_map(included):
    return {(item["type"], item["id"]): item for item in included}


def is_member_active(patron_status, tier_titles, last_charge_status):
    # If patron_status is specifically reported by Patreon
    if patron_status and patron_status != "active_patron":
        return False

    # Check last charge status if available
    if last_charge_status:
        normalized = str(last_charge_status).strip().lower()
        if normalized not in ["paid", "pending"]:
            return False

    # We removed the mandatory tier_titles check to avoid blacklisting
    # patrons who might not have a specific tier title assigned.
    
    return True


def parse_patreon_member(member, included_map):
    attrs = member.get("attributes", {})
    rels = member.get("relationships", {})

    user_obj = None
    user_rel = rels.get("user", {}).get("data")
    if user_rel:
        user_obj = included_map.get((user_rel["type"], user_rel["id"]))

    tier_titles = []
    entitled_tiers = rels.get("currently_entitled_tiers", {}).get("data", [])
    for tier_ref in entitled_tiers:
        tier_obj = included_map.get((tier_ref["type"], tier_ref["id"]))
        if tier_obj:
            title = tier_obj.get("attributes", {}).get("title")
            if title:
                tier_titles.append(title)

    email = None
    username = ""
    patreon_user_id = None

    if user_obj:
        user_attrs = user_obj.get("attributes", {})
        email = user_attrs.get("email")
        username = user_attrs.get("full_name") or user_attrs.get("vanity") or ""
        patreon_user_id = user_obj.get("id")

    if not email:
        return None

    patron_status = attrs.get("patron_status")
    last_charge_status = attrs.get("last_charge_status")
    next_charge_date = attrs.get("next_charge_date")

    active = is_member_active(
        patron_status=patron_status,
        tier_titles=tier_titles,
        last_charge_status=last_charge_status
    )

    return {
        "username": username,
        "email": email.lower().strip(),
        "tier": ", ".join(tier_titles) if tier_titles else "",
        "blacklist": not active,
        "patreon_user_id": patreon_user_id,
        "patron_status": patron_status,
        "last_charge_status": last_charge_status,
        "next_charge_date": next_charge_date,
        "updated_at": now_iso()
    }


def fetch_patreon_members():
    if not PATREON_ACCESS_TOKEN:
        raise Exception("Missing PATREON_ACCESS_TOKEN")

    if not PATREON_CAMPAIGN_ID:
        raise Exception("Missing PATREON_CAMPAIGN_ID")

    url = f"https://www.patreon.com/api/oauth2/v2/campaigns/{PATREON_CAMPAIGN_ID}/members"
    headers = {
        "Authorization": f"Bearer {PATREON_ACCESS_TOKEN}",
        "User-Agent": "PatreonSyncApp/1.0"
    }
    params = {
        "include": "user,currently_entitled_tiers",
        "fields[member]": "patron_status,last_charge_status,next_charge_date",
        "fields[user]": "email,full_name,vanity",
        "fields[tier]": "title",
        "page[count]": 100
    }

    parsed_members = []

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        data = payload.get("data", [])
        included = payload.get("included", [])
        included_map = build_included_map(included)

        for member in data:
            row = parse_patreon_member(member, included_map)
            if row:
                parsed_members.append(row)

        next_link = payload.get("links", {}).get("next")
        if not next_link:
            break

        url = next_link
        params = None

    return parsed_members


def upsert_member(member_row):
    (
        supabase
        .table("member_list")
        .upsert(member_row, on_conflict="email")
        .execute()
    )


def mark_missing_members_blacklisted(active_emails):
    result = (
        supabase
        .table("member_list")
        .select("email, tier")
        .execute()
    )

    rows = result.data or []
    updated_count = 0

    for row in rows:
        email = (row.get("email") or "").lower().strip()
        tier = row.get("tier") or ""
        
        if not email:
            continue

        # Skip developer emails
        if email in DEV_EMAILS:
            continue
            
        # Skip members with Donator tier
        if "Donator" in tier:
            continue

        if email not in active_emails:
            (
                supabase
                .table("member_list")
                .update({
                    "blacklist": True,
                    # We no longer clear the 'tier' column here
                    "updated_at": now_iso()
                })
                .eq("email", email)
                .execute()
            )
            updated_count += 1

    return updated_count


def run_patreon_sync():
    members = fetch_patreon_members()

    active_emails = set()
    upserted = 0
    blacklisted_from_feed = 0

    for member in members:
        email = member["email"]
        
        # If this is a developer email, skip updating them but mark as active
        if email in DEV_EMAILS:
            active_emails.add(email)
            continue
            
        upsert_member(member)
        upserted += 1

        if member["blacklist"] is False:
            active_emails.add(email)
        else:
            blacklisted_from_feed += 1

    missing_blacklisted = mark_missing_members_blacklisted(active_emails)

    return {
        "fetched_members": len(members),
        "upserted": upserted,
        "blacklisted_from_feed": blacklisted_from_feed,
        "missing_blacklisted": missing_blacklisted,
        "active_emails_count": len(active_emails),
        "synced_at": now_iso()
    }


# =====================
# Root
# =====================
@app.get("/")
def root():
    return {"status": "ok"}


# =====================
# Login
# =====================
@app.post("/login")
def login(data: dict):
    email = data.get("email")

    if not email:
        return {"result": "fail"}

    email = email.lower().strip()

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
        "token": token,
        "username": member.get("username", "Supporter"),
        "tier": member.get("tier", "Free")
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
# Get User History
# =====================
@app.post("/get-history")
def get_history(data: dict):
    token = data.get("token")

    if not token:
        return {"result": "unauthorized"}

    email = verify_token(token)
    if not email:
        return {"result": "unauthorized"}

    member_res = (
        supabase
        .table("member_list")
        .select("blacklist, username, tier")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if not member_res.data:
        return {"result": "unauthorized"}

    member = member_res.data[0]

    if member.get("blacklist") is True:
        return {"result": "banned"}

    usage_res = (
        supabase
        .table("cheatcode_usage")
        .select("cheat_id, used_count")
        .eq("member_email", email)
        .execute()
    )

    history = []
    if usage_res.data:
        for usage in usage_res.data:
            cheat_id = usage.get("cheat_id")
            used_count = usage.get("used_count", 0)

            cheat_res = (
                supabase
                .table("cheatcode_check_list")
                .select("code, effect, amount_limit")
                .eq("id", cheat_id)
                .limit(1)
                .execute()
            )

            if cheat_res.data:
                cheat = cheat_res.data[0]
                history.append({
                    "code": cheat.get("code"),
                    "effect": cheat.get("effect"),
                    "used_count": used_count,
                    "amount_limit": cheat.get("amount_limit", 0)
                })

    return {
        "result": "ok",
        "email": email,
        "username": member.get("username", "Supporter"),
        "tier": member.get("tier", "Free"),
        "history": history
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

    email = verify_token(token)
    if not email:
        return {"result": "unauthorized"}

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
    cheat_id = cheat.get("id")

    if cheat.get("is_active") is not True:
        return {"result": "code_disabled"}

    allowed_tiers = cheat.get("allowed_tiers", [])

    if allowed_tiers and member_tier not in allowed_tiers:
        return {"result": "tier_not_allowed"}

    amount_limit = cheat.get("amount_limit", 0)

    usage_res = (
        supabase
        .table("cheatcode_usage")
        .select("*")
        .eq("member_email", email)
        .eq("cheat_id", cheat_id)
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
        }).eq("member_email", email).eq("cheat_id", cheat_id).execute()

    else:
        if amount_limit > 0:
            supabase.table("cheatcode_usage").insert({
                "member_email": email,
                "cheat_id": cheat_id,
                "used_count": 1
            }).execute()
        else:
            return {"result": "limit_reached"}

    return {
        "result": "ok",
        "effect": cheat.get("effect"),
        "payload": cheat.get("payload")
    }


# =====================
# Patreon Sync Endpoint
# =====================
@app.get("/sync-patreon-members")
def sync_patreon_members(token: str = Query(...)):
    if not SYNC_TOKEN or token != SYNC_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        result = run_patreon_sync()
        return {
            "status": "ok",
            "result": result
        }
    except requests.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Patreon HTTP error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")