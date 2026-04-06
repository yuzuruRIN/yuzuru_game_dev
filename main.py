from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse
import csv
import io
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

    # Try to get email and username from member attributes first
    email = attrs.get("email")
    username = attrs.get("full_name") or ""
    patreon_user_id = None

    if user_obj:
        user_attrs = user_obj.get("attributes", {})
        if not email:
            email = user_attrs.get("email")
        if not username:
            username = user_attrs.get("full_name") or user_attrs.get("vanity") or ""
        patreon_user_id = user_obj.get("id")

    # Filter out "Free" tiers and ensure common casing
    active_tiers = [t for t in tier_titles if t.lower().strip() != "free"]

    # If they only have "Free" or no tiers, skip them
    if not active_tiers:
        return None

    if not email:
        return None

    patron_status = attrs.get("patron_status")
    last_charge_status = attrs.get("last_charge_status")
    next_charge_date = attrs.get("next_charge_date")

    active = is_member_active(
        patron_status=patron_status,
        tier_titles=active_tiers,
        last_charge_status=last_charge_status
    )

    return {
        "username": username,
        "email": email.lower().strip(),
        "tier": active_tiers[-1],
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
        "fields[member]": "email,full_name,patron_status,last_charge_status,next_charge_date",
        "fields[user]": "email,full_name,vanity",
        "fields[tier]": "title",
        "page[count]": 100
    }

    parsed_members = []
    page_index = 0

    raw_count = 0
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()

        if "errors" in payload:
            raise Exception(f"Patreon API errors: {payload['errors']}")

        data = payload.get("data", [])
        raw_count += len(data)
        included = payload.get("included", [])
        included_map = build_included_map(included)

        print(f"[Patreon Sync] page={page_index} members_on_page={len(data)} included={len(included)}")

        for member in data:
            row = parse_patreon_member(member, included_map)
            if row:
                parsed_members.append(row)

        next_link = payload.get("links", {}).get("next")
        if not next_link:
            break

        url = next_link
        params = None
        page_index += 1

    if len(parsed_members) == 0:
        print(f"[Patreon Sync] WARNING: No members were parsed (Total raw from API: {raw_count})")

    return parsed_members, raw_count


def upsert_member(member_row):
    (
        supabase
        .table("member_list")
        .upsert(member_row, on_conflict="email")
        .execute()
    )


def mark_missing_members_blacklisted(active_emails, db_map):
    emails_to_blacklist = []

    for email, row in db_map.items():
        email = email.lower().strip()
        tier = row.get("tier") or ""
        
        # Skip dev/donator
        if email in DEV_EMAILS or "Donator" in tier:
            continue

        # Collect emails of those who should be blacklisted
        if email not in active_emails and row.get("blacklist") is False:
            emails_to_blacklist.append(email)

    if emails_to_blacklist:
        (
            supabase
            .table("member_list")
            .update({
                "blacklist": True,
                "updated_at": now_iso()
            })
            .in_("email", emails_to_blacklist)
            .execute()
        )

    return len(emails_to_blacklist)


def run_patreon_sync():
    # 1. Fetch from Patreon
    members, total_raw = fetch_patreon_members()

    # 2. Fetch only necessary columns for comparison
    res = (
        supabase
        .table("member_list")
        .select("email, username, tier, blacklist, patron_status, last_charge_status, next_charge_date")
        .execute()
    )
    db_map = {row["email"].lower().strip(): row for row in res.data} if res.data else {}

    # Warning if Patreon returns nothing
    if total_raw == 0:
        return {
            "total_raw_from_api": 0,
            "fetched_members": 0,
            "upserted": 0,
            "blacklisted_from_feed": 0,
            "missing_blacklisted": 0,
            "active_emails_count": 0,
            "synced_at": now_iso(),
            "warning": "No members returned from Patreon API. Check if CAMPAIGN_ID is correct."
        }

    active_emails = set()
    new_subscribers = 0
    updated_members = 0
    skipped_members = 0
    blacklisted_from_feed = 0

    to_upsert = []
    changed_details = []

    for member in members:
        email = member["email"]
        active_emails.add(email)

        if email in DEV_EMAILS:
            continue

        # Comparison Logic
        existing = db_map.get(email)
        should_update = False
        
        if not existing:
            should_update = True
            new_subscribers += 1
        else:
            # Check for changes in key fields
            check_fields = ["username", "tier", "blacklist", "patron_status", "last_charge_status", "next_charge_date"]
            changes = {}
            for f in check_fields:
                val_new = member.get(f)
                val_old = existing.get(f)
                
                # Treat None as empty string for string fields to avoid false positive changes
                if val_new is None and isinstance(val_old, str) and val_old == "":
                    val_new = ""
                if val_old is None and isinstance(val_new, str) and val_new == "":
                    val_old = ""

                if str(val_new) != str(val_old):
                    should_update = True
                    changes[f] = {"old": val_old, "new": val_new}
            
            if should_update:
                updated_members += 1
                # Only log details if the TIER specifically changed
                if "tier" in changes:
                    changed_details.append({
                        "username": member.get("username", ""),
                        "email": email,
                        "old_tier": changes["tier"]["old"],
                        "new_tier": changes["tier"]["new"]
                    })
            else:
                skipped_members += 1

        if should_update:
            to_upsert.append(member)

        # Count those who are already blacklisted in the feed
        if member["blacklist"] is True:
            blacklisted_from_feed += 1

    # Bulk Upsert for new/updated members
    if to_upsert:
        (
            supabase
            .table("member_list")
            .upsert(to_upsert, on_conflict="email")
            .execute()
        )

    missing_blacklisted = mark_missing_members_blacklisted(active_emails, db_map)

    return {
        "total_raw_from_api": total_raw,
        "new_subscribers": new_subscribers,
        "updated_members": updated_members,
        "skipped_members": skipped_members,
        "blacklisted_from_feed": blacklisted_from_feed,
        "new_blacklisted_members": missing_blacklisted,
        "active_emails_count": len(members), # Total in Patreon feed
        "changed_details": changed_details,
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


# =====================
# CSV Import Utils
# =====================

def process_patreon_csv(csv_content: str):
    # Fetch existing members for comparison
    res = (
        supabase
        .table("member_list")
        .select("email, username, tier, blacklist, patron_status, last_charge_status, next_charge_date")
        .execute()
    )
    db_map = {row["email"].lower().strip(): row for row in res.data} if res.data else {}

    f = io.StringIO(csv_content)
    reader = csv.DictReader(f)
    
    active_emails = set()
    new_subscribers = 0
    updated_members = 0
    skipped_members = 0
    to_upsert = []
    changed_details = []

    for row in reader:
        # Headers from Patreon Audience CSV
        email = (row.get("Email") or "").lower().strip()
        name = row.get("Name") or ""
        tier = row.get("Tier") or ""
        patron_status_raw = row.get("Patron Status") or ""
        last_charge_status = row.get("Last Charge Status") or ""
        next_charge_date = row.get("Next Charge Date") or ""
        is_free_member = (row.get("Free Member") or "").lower() == "yes"

        # Filter out Free members or missing data
        if not email or is_free_member or not tier or tier.lower() == "free":
            continue

        active_emails.add(email)
        
        # Convert "Active patron" to "active_patron" etc.
        patron_status = patron_status_raw.lower().replace(" ", "_").strip()

        is_active = is_member_active(
            patron_status=patron_status,
            tier_titles=[tier],
            last_charge_status=last_charge_status
        )

        member_data = {
            "username": name,
            "email": email,
            "tier": tier,
            "blacklist": not is_active,
            "patron_status": patron_status,
            "last_charge_status": last_charge_status,
            "next_charge_date": next_charge_date,
            "updated_at": now_iso()
        }

        if email in DEV_EMAILS:
            continue

        # Comparison Logic
        existing = db_map.get(email)
        should_update = False
        
        if not existing:
            should_update = True
            new_subscribers += 1
        else:
            check_fields = ["username", "tier", "blacklist", "patron_status", "last_charge_status", "next_charge_date"]
            changes = {}
            for f_name in check_fields:
                val_new = member_data.get(f_name)
                val_old = existing.get(f_name)
                
                if val_new is None and isinstance(val_old, str) and val_old == "":
                    val_new = ""
                if val_old is None and isinstance(val_new, str) and val_new == "":
                    val_old = ""

                if str(val_new) != str(val_old):
                    should_update = True
                    changes[f_name] = {"old": val_old, "new": val_new}
            
            if should_update:
                updated_members += 1
                if "tier" in changes:
                    changed_details.append({
                        "username": name,
                        "email": email,
                        "old_tier": changes["tier"]["old"],
                        "new_tier": changes["tier"]["new"]
                    })
            else:
                skipped_members += 1

        if should_update:
            to_upsert.append(member_data)

    if to_upsert:
        supabase.table("member_list").upsert(to_upsert, on_conflict="email").execute()

    missing_blacklisted = mark_missing_members_blacklisted(active_emails, db_map)

    return {
        "new_subscribers": new_subscribers,
        "updated_members": updated_members,
        "skipped_members": skipped_members,
        "new_blacklisted_members": missing_blacklisted,
        "changed_details": changed_details,
        "synced_at": now_iso()
    }


@app.post("/import-patreon-csv")
async def import_patreon_csv(token: str = Query(...), file: UploadFile = File(...)):
    if not SYNC_TOKEN or token != SYNC_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")

    try:
        content = await file.read()
        decoded_content = content.decode("utf-8")
        result = process_patreon_csv(decoded_content)
        return {
            "status": "ok",
            "result": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CSV import failed: {str(e)}")


# =====================
# UI Upload Page
# =====================

@app.get("/upload", response_class=HTMLResponse)
async def upload_page():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Patreon CSV Import</title>
        <style>
            :root {
                --primary: #FF424D;
                --bg: #0F172A;
                --card: #1E293B;
                --text: #F8FAFC;
                --accent: #38BDF8;
            }
            body { 
                font-family: 'Inter', -apple-system, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }
            .container {
                background: var(--card);
                padding: 2.5rem;
                border-radius: 1.5rem;
                box-shadow: 0 10px 25px rgba(0,0,0,0.5);
                width: 90%;
                max-width: 500px;
                text-align: center;
                border: 1px solid rgba(255,255,255,0.1);
            }
            h2 { color: var(--primary); margin-bottom: 0.5rem; font-size: 1.8rem; }
            p { color: #94A3B8; font-size: 0.95rem; margin-bottom: 2.5rem; line-height: 1.5; }
            
            .form-group { margin-bottom: 2rem; text-align: left; }
            label { display: block; margin-bottom: 0.75rem; font-weight: 600; color: #CBD5E1; font-size: 0.9rem; letter-spacing: 0.05rem; }
            
            input[type="text"], input[type="file"] {
                width: 100%;
                padding: 0.85rem;
                border-radius: 0.75rem;
                border: 2px solid #334155;
                background: #0F172A;
                color: white;
                box-sizing: border-box;
                transition: border-color 0.2s;
            }
            input[type="text"]:focus { outline: none; border-color: var(--primary); }
            
            button {
                background: var(--primary);
                color: white;
                border: none;
                padding: 1.1rem;
                border-radius: 0.75rem;
                font-weight: 700;
                font-size: 1rem;
                cursor: pointer;
                width: 100%;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                margin-top: 1rem;
                box-shadow: 0 4px 14px rgba(255, 66, 77, 0.4);
            }
            button:hover { opacity: 0.9; transform: translateY(-3px); box-shadow: 0 6px 20px rgba(255, 66, 77, 0.5); }
            button:active { transform: translateY(-1px); }
            button:disabled { background: #475569; cursor: not-allowed; box-shadow: none; transform: none; }
            
            #result {
                margin-top: 2.5rem;
                text-align: left;
                padding: 1.25rem;
                border-radius: 1rem;
                background: rgba(0,0,0,0.4);
                display: none;
                font-size: 0.9rem;
                line-height: 1.7;
                max-height: 300px;
                overflow-y: auto;
                border: 1px solid #334155;
                animation: fadeIn 0.4s ease-out;
            }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
            
            .stat-line { display: flex; justify-content: space-between; margin-bottom: 0.5rem; padding-bottom: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.05); }
            .stat-val { color: var(--accent); font-weight: 700; }
            .tier-list { margin-top: 1rem; }
            .tier-item { background: rgba(252, 211, 77, 0.1); padding: 0.5rem; border-radius: 0.5rem; font-size: 0.8rem; margin-bottom: 0.5rem; color: #FDE68A; border: 1px solid rgba(252, 211, 77, 0.2); }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Patreon CSV Import</h2>
            <p>Upload members CSV from Relationship Manager<br>to sync with Supabase</p>
            
            <div class="form-group">
                <label>SYNC TOKEN</label>
                <input type="text" id="token" placeholder="Enter sync token...">
            </div>
            
            <div class="form-group">
                <label>CSV FILE</label>
                <input type="file" id="csvFile" accept=".csv">
            </div>
            
            <button id="uploadBtn" onclick="handleUpload()">SYNC MEMBERS</button>
            
            <div id="result"></div>
        </div>

        <script>
            async function handleUpload() {
                const token = document.getElementById('token').value;
                const fileInput = document.getElementById('csvFile');
                const resultDiv = document.getElementById('result');
                const btn = document.getElementById('uploadBtn');

                if (!token || !fileInput.files[0]) {
                    alert('Please provide Token and File');
                    return;
                }

                btn.disabled = true;
                btn.innerText = 'SYNCING DATA...';
                resultDiv.style.display = 'none';

                const formData = new FormData();
                formData.append('file', fileInput.files[0]);

                try {
                    const response = await fetch(`/import-patreon-csv?token=${token}`, {
                        method: 'POST',
                        body: formData
                    });

                    const data = await response.json();
                    btn.disabled = false;
                    btn.innerText = 'SYNC MEMBERS';
                    resultDiv.style.display = 'block';

                    if (response.ok) {
                        const res = data.result;
                        let logHtml = `<div style="color:#4ADE80; font-weight:bold; margin-bottom:1rem;">✅ SYNC COMPLETED</div>`;
                        logHtml += `<div class="stat-line"><span>New Subscribers</span><span class="stat-val">${res.new_subscribers}</span></div>`;
                        logHtml += `<div class="stat-line"><span>Updated Members</span><span class="stat-val">${res.updated_members}</span></div>`;
                        logHtml += `<div class="stat-line"><span>Unchanged</span><span class="stat-val">${res.skipped_members}</span></div>`;
                        logHtml += `<div class="stat-line"><span>Blacklisted (Gone)</span><span class="stat-val">${res.new_blacklisted_members}</span></div>`;
                        
                        if (res.changed_details.length > 0) {
                            logHtml += `<div class="tier-list"><b>⚠️ TIER CHANGES DETECTED:</b>`;
                            res.changed_details.forEach(item => {
                                logHtml += `<div class="tier-item"><b>${item.username}</b>: ${item.old_tier} ➔ ${item.new_tier}</div>`;
                            });
                            logHtml += `</div>`;
                        }
                        resultDiv.innerHTML = logHtml;
                    } else {
                        resultDiv.innerHTML = `<span style="color:#F87171">❌ Error: ${data.detail || 'Sync Failed'}</span>`;
                    }
                } catch (error) {
                    btn.disabled = false;
                    btn.innerText = 'SYNC MEMBERS';
                    resultDiv.style.display = 'block';
                    resultDiv.innerHTML = `<span style="color:#F87171">❌ Connection Error: ${error.message}</span>`;
                }
            }
        </script>
    </body>
    </html>
    """
    return html_content