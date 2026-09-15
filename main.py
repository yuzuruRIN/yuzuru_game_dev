from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
import csv
import io
import hmac
import hashlib
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

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7

# Never boot with a guessable secret: anyone who knows it can forge a token for
# any account. The old default ("CHANGE_ME_NOW") let the server start silently
# insecure whenever the env var was missing.
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is required")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")

PATREON_ACCESS_TOKEN = os.getenv("PATREON_ACCESS_TOKEN")
PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")
SYNC_TOKEN = os.getenv("SYNC_TOKEN")
PATREON_WEBHOOK_SECRET = os.getenv("PATREON_WEBHOOK_SECRET")

# Discord bot (Reina) webhook — forward Patreon events so the bot posts a confirm card
# e.g. http://<oracle-ip>:8080/patreon/webhook  (leave unset to disable forwarding)
BOT_WEBHOOK_URL = os.getenv("BOT_WEBHOOK_URL")

# Minimum gap between webhook-triggered full syncs (hours)
AUTO_SYNC_MIN_INTERVAL_HOURS = 6
last_auto_sync_at = None

DEV_EMAILS = ["lxpetitprixce@gmail.com", "devthelastyear@yuzuru.rin"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# =====================
# JWT Utils
# =====================
def create_token(email: str):
    # datetime.utcnow() is deprecated from Python 3.12 on
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "exp": now + timedelta(days=JWT_EXPIRE_DAYS),
        "iat": now
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


# =====================
# Shared helpers
# =====================
#  amount_limit convention, used consistently everywhere below:
#     amount_limit <= 0 or NULL  = unlimited uses
#     amount_limit  > 0          = at most that many uses per account
# ---------------------

def _as_int(value, default=0):
    """Always return an int - a NULL column would otherwise blow up on `None > 0`."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value, default=""):
    """
    Guard against NULL columns.
    dict.get(key, default) does not help here: when the key exists but holds None
    it returns None, not the default - that is how "Welcome back, None" reached players.
    """
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _is_dev(email):
    """Case-insensitive DEV_EMAILS check - the stored address may be mixed case."""
    return _as_text(email).lower() in DEV_EMAILS


def _tier_list(raw):
    """
    allowed_tiers may be a jsonb array or plain text.
    Using `tier not in raw` directly on text degrades into a substring test
    (tier "old" would satisfy allowed_tiers "Gold"), so normalise to a list first.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("[", "").replace("]", "").replace('"', "").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    return [t for t in (_as_text(i).lower() for i in items) if t]


def _log(where, exc):
    import traceback
    print(f"[ERROR] {where}: {exc}")
    traceback.print_exc()


def _get_member(email):
    """
    Look a member up by email, ignoring case.

    /login used to lowercase the input and then match with .eq(), so any member
    whose stored address has capitals (the dev account among them) could not log
    in at all. Falls back to ilike, then re-checks in Python because "_" and "%"
    are LIKE wildcards and appear in real addresses - the extra check keeps a
    pattern from matching the wrong person.
    """
    raw = _as_text(email)
    if not raw:
        return None

    res = (
        supabase
        .table("member_list")
        .select("*")
        .eq("email", raw)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]

    res = (
        supabase
        .table("member_list")
        .select("*")
        .ilike("email", raw)
        .limit(20)
        .execute()
    )
    target = raw.lower()
    for row in (res.data or []):
        if _as_text(row.get("email")).lower() == target:
            return row

    return None


def _get_cheat(code):
    """Look a cheat code up ignoring case - codes are CamelCase and players mistype it."""
    raw = _as_text(code)
    if not raw:
        return None

    res = (
        supabase
        .table("cheatcode_check_list")
        .select("*")
        .eq("code", raw)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]

    res = (
        supabase
        .table("cheatcode_check_list")
        .select("*")
        .ilike("code", raw)
        .limit(20)
        .execute()
    )
    target = raw.lower()
    for row in (res.data or []):
        if _as_text(row.get("code")).lower() == target:
            return row

    return None


def _consume_usage(email, cheat_id, amount_limit):
    """
    Increment used_count once, using compare-and-swap.

    The previous read-then-write was not atomic: concurrent requests all read the
    same used_count and overwrote each other, letting a code exceed amount_limit.
    The .eq("used_count", ...) guard makes a losing writer update no rows, so it
    re-reads and tries again.

    Returns (ok: bool, reason: str|None)
    """
    for _ in range(3):
        usage_res = (
            supabase
            .table("cheatcode_usage")
            .select("used_count")
            .eq("member_email", email)
            .eq("cheat_id", cheat_id)
            .limit(1)
            .execute()
        )

        if usage_res.data:
            raw_used = usage_res.data[0].get("used_count")
            used_count = _as_int(raw_used)

            if amount_limit > 0 and used_count >= amount_limit:
                return False, "limit_reached"

            guard = (
                supabase
                .table("cheatcode_usage")
                .update({"used_count": used_count + 1})
                .eq("member_email", email)
                .eq("cheat_id", cheat_id)
            )

            # The CAS guard must compare against the raw stored value, not the
            # normalised one: .eq("used_count", 0) matches no row when it is NULL.
            if raw_used is None:
                guard = guard.is_("used_count", None)
            else:
                guard = guard.eq("used_count", raw_used)

            upd = guard.execute()

            if getattr(upd, "data", None):
                return True, None

            # Some supabase-py versions do not return the updated rows - confirm by re-reading.
            recheck = (
                supabase
                .table("cheatcode_usage")
                .select("used_count")
                .eq("member_email", email)
                .eq("cheat_id", cheat_id)
                .limit(1)
                .execute()
            )
            if recheck.data and _as_int(recheck.data[0].get("used_count")) == used_count + 1:
                return True, None

            # Lost the race - re-read and retry.
            continue

        # First use of this code: create the row.
        # The old code returned "limit_reached" here whenever amount_limit <= 0,
        # which made unlimited codes unusable for anyone who had not used them yet.
        try:
            supabase.table("cheatcode_usage").insert({
                "member_email": email,
                "cheat_id": cheat_id,
                "used_count": 1
            }).execute()
            return True, None
        except Exception:
            # Another request created the row at the same time - re-read and retry.
            continue

    return False, "server_error"


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

    tier_infos = []  # (title, amount_cents)
    entitled_tiers = rels.get("currently_entitled_tiers", {}).get("data", [])
    for tier_ref in entitled_tiers:
        tier_obj = included_map.get((tier_ref["type"], tier_ref["id"]))
        if tier_obj:
            t_attrs = tier_obj.get("attributes", {})
            title = t_attrs.get("title")
            if title:
                tier_infos.append((title, t_attrs.get("amount_cents") or 0))

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
    active_tiers = [(t, a) for t, a in tier_infos if t.lower().strip() != "free"]

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
        tier_titles=[t for t, _ in active_tiers],
        last_charge_status=last_charge_status
    )

    return {
        "username": username,
        "email": email.lower().strip(),
        # The API does not guarantee tier order, and an upgraded member is
        # entitled to several tiers until the period rolls over -- pick the
        # most expensive one, never just the last in the list
        "tier": max(active_tiers, key=lambda t: t[1])[0],
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
        "fields[tier]": "title,amount_cents",
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
        if _is_dev(email) or "Donator" in tier:
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

        if _is_dev(email):
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


def auto_sync_if_stale():
    """Full sync piggybacked on webhook wake-ups.

    Runs AFTER the webhook response is sent, so Patreon is not kept waiting.
    Catches anything a single webhook event can't: members who left while
    the server was asleep, expired charges, missed events, etc.
    Throttled so bursts of webhook events don't trigger repeated full syncs.
    """
    global last_auto_sync_at
    now = datetime.now(timezone.utc)
    if last_auto_sync_at and (now - last_auto_sync_at) < timedelta(hours=AUTO_SYNC_MIN_INTERVAL_HOURS):
        return
    last_auto_sync_at = now
    try:
        result = run_patreon_sync()
        print(
            f"[Auto Sync] done: new={result.get('new_subscribers')} "
            f"updated={result.get('updated_members')} "
            f"blacklisted={result.get('new_blacklisted_members')}"
        )
    except Exception as e:
        print(f"[Auto Sync] failed: {e}")


# =====================
# Root
# =====================
@app.get("/")
@app.get("/health")
def root():
    # The game pings this to wake the Render free-tier instance before a login.
    return {"status": "ok", "result": "ok"}


# =====================
# Login
# =====================
@app.post("/login")
def login(data: dict):
    try:
        email = _as_text(data.get("email"))

        if not email:
            return {"result": "fail"}

        # Discord handles are public: anyone who can read the member list can
        # guess "<handle>@donator.discord" and log in as that supporter, because
        # this endpoint asks for nothing but an address. Flip
        # BLOCK_PLACEHOLDER_LOGIN=1 once donors have had time to run /linkemail
        # on the bot -- until then, blocking here would lock them out of cheats.
        # (both names are defined in the Device Login section at the end of file)
        if BLOCK_PLACEHOLDER_LOGIN and _is_placeholder_email(email):
            return {"result": "use_linkemail"}

        member = _get_member(email)

        if not member:
            return {"result": "fail"}

        if member.get("blacklist") is True:
            return {"result": "banned"}

        # Use the address exactly as stored, not as typed, so every endpoint and
        # every cheatcode_usage row keys off the same string. Lowercasing here
        # instead would orphan the usage history of mixed-case addresses.
        canonical_email = _as_text(member.get("email"), email)

        token = create_token(canonical_email)

        return {
            "result": "ok",
            "token": token,
            "username": _as_text(member.get("username"), "Supporter"),
            "tier": _as_text(member.get("tier"), "Free")
        }

    except Exception as exc:
        _log("login", exc)
        return {"result": "server_error"}


# =====================
# Verify Token
# =====================
@app.post("/verify-token")
def verify(data: dict):
    try:
        token = data.get("token")

        if not token:
            return {"result": "invalid"}

        email = verify_token(token)
        if not email:
            return {"result": "invalid"}

        # This used to only decode the JWT, so an account that had been removed
        # or lost its entitlement still looked logged in until the token expired
        # (up to 7 days). Returning username/tier also lets the game refresh them
        # without making the player log out and back in.
        member = _get_member(email)
        if not member:
            return {"result": "invalid"}

        if member.get("blacklist") is True:
            return {"result": "banned"}

        return {
            "result": "ok",
            "email": email,
            "username": _as_text(member.get("username"), "Supporter"),
            "tier": _as_text(member.get("tier"), "Free")
        }

    except Exception as exc:
        _log("verify-token", exc)
        return {"result": "server_error"}


# =====================
# Get User History
# =====================
@app.post("/get-history")
def get_history(data: dict):
    try:
        token = data.get("token")

        if not token:
            return {"result": "unauthorized"}

        email = verify_token(token)
        if not email:
            return {"result": "unauthorized"}

        member = _get_member(email)
        if not member:
            return {"result": "unauthorized"}

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
        usage_rows = usage_res.data or []

        if usage_rows:
            # This used to run one query per usage row (N+1), which is slow on the
            # Render free tier. Fetch every code's details in a single .in_() call.
            cheat_ids = [u.get("cheat_id") for u in usage_rows if u.get("cheat_id") is not None]

            cheat_map = {}
            if cheat_ids:
                cheat_res = (
                    supabase
                    .table("cheatcode_check_list")
                    .select("id, code, effect, amount_limit")
                    .in_("id", cheat_ids)
                    .execute()
                )
                for cheat in (cheat_res.data or []):
                    cheat_map[cheat.get("id")] = cheat

            for usage in usage_rows:
                cheat = cheat_map.get(usage.get("cheat_id"))
                if not cheat:
                    continue
                history.append({
                    "code": cheat.get("code"),
                    "effect": cheat.get("effect"),
                    "used_count": _as_int(usage.get("used_count")),
                    "amount_limit": _as_int(cheat.get("amount_limit"))
                })

        return {
            "result": "ok",
            "email": email,
            "username": _as_text(member.get("username"), "Supporter"),
            "tier": _as_text(member.get("tier"), "Free"),
            "history": history
        }

    except Exception as exc:
        _log("get-history", exc)
        return {"result": "server_error"}


# =====================
# Use Cheat Code
# =====================
@app.post("/use-cheat")
def use_cheat(data: dict):
    try:
        token = data.get("token")
        cheat_code = _as_text(data.get("cheat_code"))

        if not token or not cheat_code:
            return {"result": "fail"}

        # 1. Verify token
        email = verify_token(token)
        if not email:
            return {"result": "unauthorized"}

        # 2. Check member, blacklist, tier
        member = _get_member(email)
        if not member:
            return {"result": "unauthorized"}

        if member.get("blacklist") is True:
            return {"result": "banned"}

        member_tier = _as_text(member.get("tier"), "Free").lower()

        # 3. Check cheat code exists & active
        cheat = _get_cheat(cheat_code)
        if not cheat:
            return {"result": "invalid_code"}

        cheat_id = cheat.get("id")

        if cheat.get("is_active") is not True:
            return {"result": "code_disabled"}

        # 4. Check tier permission
        allowed_tiers = _tier_list(cheat.get("allowed_tiers"))

        if allowed_tiers and member_tier not in allowed_tiers:
            return {"result": "tier_not_allowed"}

        # 5. Check & consume usage
        amount_limit = _as_int(cheat.get("amount_limit"))

        ok, reason = _consume_usage(email, cheat_id, amount_limit)
        if not ok:
            return {"result": reason}

        return {
            "result": "ok",
            "effect": cheat.get("effect"),
            "payload": cheat.get("payload")
        }

    except Exception as exc:
        _log("use-cheat", exc)
        return {"result": "server_error"}


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
# Patreon Webhook (real-time, push-based)
# =====================
def verify_patreon_signature(raw_body: bytes, signature: str) -> bool:
    if not PATREON_WEBHOOK_SECRET or not signature:
        return False
    # Patreon signs the raw request body with HMAC-MD5 using the webhook secret
    expected = hmac.new(
        PATREON_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.md5
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_email_from_webhook(payload: dict):
    # Get email even when the member has no active tier (used for delete events)
    member = payload.get("data", {})
    included_map = build_included_map(payload.get("included", []))
    attrs = member.get("attributes", {})
    email = attrs.get("email")
    if not email:
        user_rel = member.get("relationships", {}).get("user", {}).get("data")
        if user_rel:
            user_obj = included_map.get((user_rel["type"], user_rel["id"]))
            if user_obj:
                email = user_obj.get("attributes", {}).get("email")
    return email.lower().strip() if email else None


def forward_to_bot(raw_body: bytes, signature: str, event: str):
    """Forward the untouched Patreon request (same body + signature) to the
    Discord bot, which verifies it with the shared secret and posts a
    confirmation card. Runs after the response is sent; failure here never
    affects the reply to Patreon."""
    if not BOT_WEBHOOK_URL:
        return
    try:
        r = requests.post(
            BOT_WEBHOOK_URL,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Patreon-Signature": signature,
                "X-Patreon-Event": event,
            },
            timeout=10,
        )
        print(f"[Bot Forward] {event} -> {r.status_code}")
    except Exception as e:
        print(f"[Bot Forward] failed: {e}")


@app.post("/webhook/patreon")
async def patreon_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    signature = request.headers.get("X-Patreon-Signature", "")
    event = request.headers.get("X-Patreon-Event", "")

    # 1. Make sure the request really came from Patreon
    if not verify_patreon_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Forward a copy to the Discord bot so admins get a confirm card
    background_tasks.add_task(forward_to_bot, raw_body, signature, event)

    # While the server is awake anyway, sweep for expired/left members
    # in the background (throttled, runs after the response is sent)
    background_tasks.add_task(auto_sync_if_stale)

    payload = await request.json()
    member_obj = payload.get("data", {})
    included_map = build_included_map(payload.get("included", []))

    # 2. Member left / pledge deleted -> defer to the sync sweep.
    #    Patreon keeps a cancelled member's benefits (and reports them as
    #    active with their tier) until the paid period ends, so blacklisting
    #    here is premature -- and the next full sync would revert it anyway.
    #    The sweep blacklists them automatically once entitlement lapses.
    if event in ("members:delete", "members:pledge:delete"):
        email = extract_email_from_webhook(payload)
        return {"status": "ok", "event": event, "action": "deferred_to_sync", "email": email}

    # 3. Create / update / pledge created -> upsert using the existing parser
    row = parse_patreon_member(member_obj, included_map)
    if not row:
        # No paid tier (Free / removed) -> nothing to store
        return {"status": "ok", "event": event, "action": "skipped"}

    if _is_dev(row["email"]):
        return {"status": "ok", "event": event, "action": "skipped_dev"}

    upsert_member(row)
    return {
        "status": "ok",
        "event": event,
        "action": "upserted",
        "email": row["email"],
        "tier": row["tier"],
        "blacklist": row["blacklist"],
    }


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
        next_charge_date = row.get("Next Charge Date") or None
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

        if _is_dev(email):
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


def process_gsheet_csv(csv_content: str):
    today = datetime.now(timezone.utc).date()

    res = (
        supabase
        .table("member_list")
        .select("email, blacklist")
        .execute()
    )
    db_map = {row["email"].lower().strip(): row for row in res.data} if res.data else {}

    f = io.StringIO(csv_content)
    reader = csv.DictReader(f)

    new_subscribers = 0
    newly_blacklisted = 0
    reactivated_members = 0
    skipped_members = 0
    to_insert = []
    to_blacklist = []
    to_reactivate = []

    for row in reader:
        name = (row.get("Name") or "").strip()
        end_date_str = (row.get("End Date") or "").strip()

        if not name or not end_date_str:
            continue

        email = f"{name}@donator.discord".lower()

        try:
            end_date = datetime.strptime(end_date_str, "%d/%m/%Y").date()
        except ValueError:
            continue

        is_expired = end_date < today
        existing = db_map.get(email)

        if not existing:
            new_subscribers += 1
            to_insert.append({
                "username": name,
                "email": email,
                "tier": "Donator",
                "blacklist": is_expired,
                "updated_at": now_iso()
            })
        else:
            currently_blacklisted = existing.get("blacklist") is True
            if is_expired and not currently_blacklisted:
                newly_blacklisted += 1
                to_blacklist.append(email)
            elif not is_expired and currently_blacklisted:
                reactivated_members += 1
                to_reactivate.append(email)
            else:
                skipped_members += 1

    now = now_iso()

    if to_insert:
        supabase.table("member_list").insert(to_insert).execute()

    if to_blacklist:
        supabase.table("member_list").update({"blacklist": True, "updated_at": now}).in_("email", to_blacklist).execute()

    if to_reactivate:
        supabase.table("member_list").update({"blacklist": False, "updated_at": now}).in_("email", to_reactivate).execute()

    return {
        "new_subscribers": new_subscribers,
        "newly_blacklisted": newly_blacklisted,
        "reactivated_members": reactivated_members,
        "skipped_members": skipped_members,
        "synced_at": now
    }


@app.post("/import-gsheet-csv")
async def import_gsheet_csv(token: str = Query(...), file: UploadFile = File(...)):
    if not SYNC_TOKEN or token != SYNC_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")

    try:
        content = await file.read()
        decoded_content = content.decode("utf-8-sig")
        result = process_gsheet_csv(decoded_content)
        return {
            "status": "ok",
            "result": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GSheet CSV import failed: {str(e)}")


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
        <title>Member CSV Import</title>
        <style>
            :root {
                --primary: #FF424D;
                --green: #34D399;
                --bg: #0F172A;
                --card: #1E293B;
                --text: #F8FAFC;
                --accent: #38BDF8;
                --tab-active-patreon: #FF424D;
                --tab-active-gsheet: #34D399;
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
                max-width: 520px;
                text-align: center;
                border: 1px solid rgba(255,255,255,0.1);
            }
            h2 { margin-bottom: 0.25rem; font-size: 1.8rem; }
            .subtitle { color: #94A3B8; font-size: 0.9rem; margin-bottom: 1.75rem; line-height: 1.5; }

            .tabs {
                display: flex;
                border-radius: 0.75rem;
                background: #0F172A;
                padding: 0.25rem;
                margin-bottom: 2rem;
                gap: 0.25rem;
            }
            .tab-btn {
                flex: 1;
                padding: 0.7rem;
                border: none;
                border-radius: 0.6rem;
                font-weight: 700;
                font-size: 0.85rem;
                cursor: pointer;
                background: transparent;
                color: #64748B;
                transition: all 0.25s;
                margin-top: 0;
                box-shadow: none;
                width: auto;
            }
            .tab-btn.active-patreon {
                background: var(--tab-active-patreon);
                color: white;
                box-shadow: 0 2px 10px rgba(255,66,77,0.4);
            }
            .tab-btn.active-gsheet {
                background: var(--tab-active-gsheet);
                color: white;
                box-shadow: 0 2px 10px rgba(52,211,153,0.4);
            }

            .tab-panel { display: none; }
            .tab-panel.active { display: block; }

            .form-group { margin-bottom: 1.5rem; text-align: left; }
            label { display: block; margin-bottom: 0.6rem; font-weight: 600; color: #CBD5E1; font-size: 0.85rem; letter-spacing: 0.05rem; }

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

            .hint { font-size: 0.78rem; color: #64748B; margin-top: 0.4rem; }

            button.submit-btn {
                color: white;
                border: none;
                padding: 1.1rem;
                border-radius: 0.75rem;
                font-weight: 700;
                font-size: 1rem;
                cursor: pointer;
                width: 100%;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                margin-top: 0.5rem;
            }
            button.submit-btn.patreon-btn {
                background: var(--primary);
                box-shadow: 0 4px 14px rgba(255,66,77,0.4);
            }
            button.submit-btn.gsheet-btn {
                background: var(--green);
                color: #0F172A;
                box-shadow: 0 4px 14px rgba(52,211,153,0.4);
            }
            button.submit-btn:hover { opacity: 0.9; transform: translateY(-3px); }
            button.submit-btn:active { transform: translateY(-1px); }
            button.submit-btn:disabled { background: #475569; color: #94A3B8; cursor: not-allowed; box-shadow: none; transform: none; }

            .result-box {
                margin-top: 2rem;
                text-align: left;
                padding: 1.25rem;
                border-radius: 1rem;
                background: rgba(0,0,0,0.4);
                display: none;
                font-size: 0.88rem;
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
            .tier-item { background: rgba(252,211,77,0.1); padding: 0.5rem; border-radius: 0.5rem; font-size: 0.8rem; margin-bottom: 0.5rem; color: #FDE68A; border: 1px solid rgba(252,211,77,0.2); }
        </style>
    </head>
    <body>
        <div class="container">
            <h2 id="page-title" style="color: var(--primary);">Patreon CSV Import</h2>
            <p class="subtitle" id="page-subtitle">Upload members CSV from Relationship Manager<br>to sync with Supabase</p>

            <div class="tabs">
                <button class="tab-btn active-patreon" onclick="switchTab('patreon')">🎖 PATREON</button>
                <button class="tab-btn" onclick="switchTab('gsheet')">📊 GOOGLE SHEET</button>
            </div>

            <!-- Patreon Tab -->
            <div id="tab-patreon" class="tab-panel active">
                <div class="form-group">
                    <label>SYNC TOKEN</label>
                    <input type="text" id="patreon-token" placeholder="Enter sync token...">
                </div>
                <div class="form-group">
                    <label>CSV FILE</label>
                    <input type="file" id="patreon-file" accept=".csv">
                    <p class="hint">Export from Patreon Relationship Manager</p>
                </div>
                <button class="submit-btn patreon-btn" id="patreon-btn" onclick="handlePatreonUpload()">SYNC MEMBERS</button>
                <div class="result-box" id="patreon-result"></div>
            </div>

            <!-- Google Sheet Tab -->
            <div id="tab-gsheet" class="tab-panel">
                <div class="form-group">
                    <label>SYNC TOKEN</label>
                    <input type="text" id="gsheet-token" placeholder="Enter sync token...">
                </div>
                <div class="form-group">
                    <label>CSV FILE</label>
                    <input type="file" id="gsheet-file" accept=".csv">
                    <p class="hint">Export Google Sheet as CSV (File → Download → CSV)</p>
                </div>
                <button class="submit-btn gsheet-btn" id="gsheet-btn" onclick="handleGSheetUpload()">SYNC DONATORS</button>
                <div class="result-box" id="gsheet-result"></div>
            </div>
        </div>

        <script>
            const tabs = ['patreon', 'gsheet'];
            const tabMeta = {
                patreon: { title: 'Patreon CSV Import', subtitle: 'Upload members CSV from Relationship Manager<br>to sync with Supabase', color: 'var(--primary)', activeClass: 'active-patreon' },
                gsheet:  { title: 'Google Sheet Import', subtitle: 'Upload donator list exported from Google Sheet<br>Email will be set as name@donator.discord', color: 'var(--green)', activeClass: 'active-gsheet' }
            };

            function switchTab(tab) {
                tabs.forEach(t => {
                    document.getElementById('tab-' + t).classList.remove('active');
                    document.querySelectorAll('.tab-btn')[tabs.indexOf(t)].className = 'tab-btn';
                });
                document.getElementById('tab-' + tab).classList.add('active');
                const btn = document.querySelectorAll('.tab-btn')[tabs.indexOf(tab)];
                btn.classList.add(tabMeta[tab].activeClass);
                document.getElementById('page-title').textContent = tabMeta[tab].title;
                document.getElementById('page-title').style.color = tabMeta[tab].color;
                document.getElementById('page-subtitle').innerHTML = tabMeta[tab].subtitle;
            }

            async function handlePatreonUpload() {
                const token = document.getElementById('patreon-token').value;
                const fileInput = document.getElementById('patreon-file');
                const resultDiv = document.getElementById('patreon-result');
                const btn = document.getElementById('patreon-btn');

                if (!token || !fileInput.files[0]) { alert('Please provide Token and File'); return; }

                btn.disabled = true; btn.innerText = 'SYNCING...'; resultDiv.style.display = 'none';
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);

                try {
                    const response = await fetch('/import-patreon-csv?token=' + encodeURIComponent(token), { method: 'POST', body: formData });
                    const data = await response.json();
                    btn.disabled = false; btn.innerText = 'SYNC MEMBERS'; resultDiv.style.display = 'block';

                    if (response.ok) {
                        const res = data.result;
                        let html = '<div style="color:#4ADE80;font-weight:bold;margin-bottom:1rem;">✅ SYNC COMPLETED</div>';
                        html += statLine('New Subscribers', res.new_subscribers);
                        html += statLine('Updated Members', res.updated_members);
                        html += statLine('Unchanged', res.skipped_members);
                        html += statLine('Blacklisted (Gone)', res.new_blacklisted_members);
                        if (res.changed_details && res.changed_details.length > 0) {
                            html += '<div class="tier-list"><b>⚠️ TIER CHANGES:</b>';
                            res.changed_details.forEach(i => { html += '<div class="tier-item"><b>' + i.username + '</b>: ' + i.old_tier + ' ➔ ' + i.new_tier + '</div>'; });
                            html += '</div>';
                        }
                        resultDiv.innerHTML = html;
                    } else {
                        resultDiv.innerHTML = '<span style="color:#F87171">❌ Error: ' + (data.detail || 'Sync Failed') + '</span>';
                    }
                } catch (e) {
                    btn.disabled = false; btn.innerText = 'SYNC MEMBERS'; resultDiv.style.display = 'block';
                    resultDiv.innerHTML = '<span style="color:#F87171">❌ Connection Error: ' + e.message + '</span>';
                }
            }

            async function handleGSheetUpload() {
                const token = document.getElementById('gsheet-token').value;
                const fileInput = document.getElementById('gsheet-file');
                const resultDiv = document.getElementById('gsheet-result');
                const btn = document.getElementById('gsheet-btn');

                if (!token || !fileInput.files[0]) { alert('Please provide Token and File'); return; }

                btn.disabled = true; btn.innerText = 'SYNCING...'; resultDiv.style.display = 'none';
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);

                try {
                    const response = await fetch('/import-gsheet-csv?token=' + encodeURIComponent(token), { method: 'POST', body: formData });
                    const data = await response.json();
                    btn.disabled = false; btn.innerText = 'SYNC DONATORS'; resultDiv.style.display = 'block';

                    if (response.ok) {
                        const res = data.result;
                        let html = '<div style="color:#4ADE80;font-weight:bold;margin-bottom:1rem;">✅ SYNC COMPLETED</div>';
                        html += statLine('New Donators', res.new_subscribers);
                        html += statLine('Newly Blacklisted (expired)', res.newly_blacklisted);
                        html += statLine('Reactivated (re-subscribed)', res.reactivated_members);
                        html += statLine('Unchanged', res.skipped_members);
                        resultDiv.innerHTML = html;
                    } else {
                        resultDiv.innerHTML = '<span style="color:#F87171">❌ Error: ' + (data.detail || 'Sync Failed') + '</span>';
                    }
                } catch (e) {
                    btn.disabled = false; btn.innerText = 'SYNC DONATORS'; resultDiv.style.display = 'block';
                    resultDiv.innerHTML = '<span style="color:#F87171">❌ Connection Error: ' + e.message + '</span>';
                }
            }

            function statLine(label, val) {
                return '<div class="stat-line"><span>' + label + '</span><span class="stat-val">' + val + '</span></div>';
            }
        </script>
    </body>
    </html>
    """
    return html_content


# ============================================================================
# Device Login — ระบบล็อกอินหน้าแรกของเกม
#
# Flow:
#   1. /auth/request-otp  อีเมล + รหัสเครื่อง -> ส่งรหัส 6 หลักไปทางอีเมล
#   2. /auth/activate     อีเมล + OTP + รหัสเครื่อง -> ผูกเครื่อง + คืน token
#   3. /auth/verify       token + รหัสเครื่อง -> ต่ออายุ (เรียกทุกครั้งที่เปิดเกม)
#
# หลักการที่ยึดไว้:
#   • ตรวจ hwid ฝั่ง server เสมอ และฝังไว้ใน JWT — ไฟล์ persistent ของ Ren'Py
#     ก๊อปข้ามเครื่องได้ ถ้าเช็คแต่ฝั่งเกม ก๊อปไปเครื่องอื่นก็เล่นได้เลย
#   • เก็บ OTP เป็น hash (HMAC) ไม่เก็บเลขตรง ๆ — DB หลุดก็ย้อนกลับไม่ได้
#   • ไม่คืน hwid_hash กลับไปให้ client เด็ดขาด
#   • อีเมลใน devices/otp_codes/auth_log เป็นตัวพิมพ์เล็กล้วนเสมอ
# ============================================================================

import secrets

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

# ── เพดานการขอ OTP ────────────────────────────────────────────────────────
# นับเฉพาะคำขอที่ "ส่งเมลออกไปจริง" — คำขอที่โดนปฏิเสธไม่สร้างแถว จึงไม่ต่อ
# เวลาการแบนของตัวเอง และไม่กินโควตาอีเมล
#
# ต่ออีเมล  = ปกป้องกล่องจดหมายของเจ้าของอีเมลจากการถูกยิงรัว
# ต่อ IP    = ปกป้องโควตาอีเมลรวม และกันการไล่ยิงหลายอีเมลจากเครื่องเดียว
#
# ไม่มีเพดานต่อ hwid_hash เพราะค่านั้นฝั่งเกมส่งมาเอง คนยิงสคริปต์สุ่มค่าใหม่
# ทุกครั้งได้ ด่านนั้นจึงกันได้แค่เกมที่วนลูปผิดพลาด ไม่ใช่คนที่ตั้งใจ
OTP_RATE_WINDOW_MINUTES = 15
OTP_RATE_LIMIT = 5               # ต่ออีเมล ต่อ 15 นาที
OTP_DAILY_LIMIT_EMAIL = 10       # ต่ออีเมล ต่อ 24 ชม.
OTP_RATE_LIMIT_IP = 10           # ต่อ IP ต่อ 15 นาที
OTP_DAILY_LIMIT_IP = 30          # ต่อ IP ต่อ 24 ชม.

# ── โหมดยืนยันตัวตน ───────────────────────────────────────────────────────
# ตั้ง OTP_REQUIRED=1 เพื่อบังคับยืนยันด้วยรหัสทางอีเมล (ต้องตั้ง Resend ให้
# เรียบร้อยก่อน ไม่งั้นจะไม่มีใครล็อกอินได้เลย)
#
# ค่าเริ่มต้นคือ "ปิด" = ใช้อีเมลอย่างเดียว ตามที่ตัดสินใจกันไว้:
# อีเมลเป็นข้อมูลของผู้สนับสนุนเอง ถ้าเจ้าตัวเต็มใจแบ่งให้คนอื่นก็เป็นสิทธิ์ของเขา
# ด่านที่เหลือจึงเป็นการจำกัดจำนวนเครื่อง + ให้เจ้าตัวเห็นและปลดเครื่องเองได้
#
# สลับค่านี้ได้ทุกเมื่อโดยไม่ต้องให้ผู้เล่นอัปเดตเกม — เกมถามเซิร์ฟเวอร์ทุกครั้ง
OTP_REQUIRED = os.getenv("OTP_REQUIRED") == "1"

# เมื่อไม่มี OTP อีเมลคือหลักฐานชิ้นเดียว จึงต้องกันคนไล่ยิงอีเมลมั่ว ๆ
# นับต่อ IP เพราะ hwid_hash ฝั่งเกมส่งมาเอง ปลอมได้ไม่จำกัด
ACTIVATE_RATE_LIMIT_IP = 10      # พยายามผูกเครื่อง ต่อ IP ต่อ 15 นาที
ACTIVATE_DAILY_LIMIT_IP = 40     # พยายามผูกเครื่อง ต่อ IP ต่อ 24 ชม.

DEVICE_SLOTS_DEFAULT = 2
DEVICE_RELEASE_COOLDOWN_DAYS = 30
GAME_TOKEN_EXPIRE_DAYS = 30
GRACE_PERIOD_DAYS = 14           # เล่นออฟไลน์ได้กี่วันหลัง verify ครั้งล่าสุด

PLACEHOLDER_EMAIL_SUFFIX = "@donator.discord"

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
OTP_FROM_EMAIL = os.getenv("OTP_FROM_EMAIL")
# ตั้ง OTP_DEBUG=1 เพื่อ print OTP ลง log แทนการส่งอีเมลจริง (ใช้ตอนเทสต์เท่านั้น)
OTP_DEBUG = os.getenv("OTP_DEBUG") == "1"
# ตั้ง FREE_MODE=1 ตอนปล่อยเกมให้เล่นฟรี -> เกมจะข้ามหน้าล็อกอิน
FREE_MODE = os.getenv("FREE_MODE") == "0"
# ตั้ง BLOCK_PLACEHOLDER_LOGIN=1 เมื่อประกาศให้ผู้โดเนทผูกอีเมลครบแล้ว
BLOCK_PLACEHOLDER_LOGIN = os.getenv("BLOCK_PLACEHOLDER_LOGIN") == "1"


# =====================
# Device Login helpers
# =====================
def _norm_email(value):
    """อีเมลในตารางใหม่เป็นตัวพิมพ์เล็กล้วนเสมอ — ต่างจาก member_list ที่ปนกัน"""
    return _as_text(value).lower()


def _is_placeholder_email(email):
    """อีเมลสังเคราะห์จาก Discord handle — ส่ง OTP ไปไม่ถึงเพราะไม่ใช่โดเมนจริง"""
    return _norm_email(email).endswith(PLACEHOLDER_EMAIL_SUFFIX)


def _parse_iso(value):
    """แปลง timestamptz จาก Supabase เป็น datetime ที่มี tz เสมอ"""
    text = _as_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _client_ip(request):
    """
    IP จริงของผู้ขอ

    แอปอยู่หลัง proxy ของ Render ดังนั้น request.client.host จะเป็น IP ของ proxy
    เสมอ ต้องอ่านจาก X-Forwarded-For

    ⚠️ ต้องเอา "ตัวสุดท้าย" ไม่ใช่ตัวแรก:
        proxy แต่ละชั้นจะ *ต่อท้าย* IP ที่ตัวเองรับมา ถ้าผู้ยิงแนบ header
        X-Forwarded-For ปลอมมาเอง ค่าปลอมนั้นจะไปอยู่ "ข้างหน้า" และ IP จริงที่
        Render เติมจะอยู่ท้ายสุด — ถ้าอ่านตัวแรกจะโดนหลอกได้ทุกครั้งด้วยการ
        สุ่ม header ใหม่ เท่ากับด่าน IP ไร้ผลไปเลย
    """
    fwd = _as_text(request.headers.get("x-forwarded-for"))
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1][:64]
    return _as_text(getattr(request.client, "host", ""))[:64]


def _count_since(rows, cutoff):
    """นับแถวที่ created_at ใหม่กว่า cutoff (แถวมาจากช่วง 24 ชม. อยู่แล้ว)"""
    total = 0
    for row in rows:
        stamp = _parse_iso(row.get("created_at"))
        if stamp and stamp >= cutoff:
            total += 1
    return total


def _otp_rows_since(column, value, cutoff):
    if not value:
        return []
    res = (
        supabase
        .table("otp_codes")
        .select("created_at")
        .eq(column, value)
        .gte("created_at", cutoff.isoformat())
        .limit(500)
        .execute()
    )
    return res.data or []


def _activate_rows_since(ip, cutoff):
    """
    ครั้งที่พยายามผูกเครื่องจาก IP นี้ นับทั้งที่สำเร็จและถูกปฏิเสธ

    ต้องนับที่ล้มเหลวด้วย ไม่งั้นคนไล่เดาอีเมลจะยิงได้ไม่จำกัด เพราะการเดาผิด
    ไม่เคยสร้างแถวใน devices
    """
    if not ip:
        return []
    res = (
        supabase
        .table("auth_log")
        .select("created_at")
        .eq("request_ip", ip)
        .in_("event", ["activated", "activate_denied"])
        .gte("created_at", cutoff.isoformat())
        .limit(500)
        .execute()
    )
    return res.data or []


def _gen_otp():
    """secrets ไม่ใช่ random — เลข OTP ต้องเดาไม่ได้"""
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def _hash_otp(email, code):
    """
    HMAC ด้วย JWT_SECRET

    เก็บ hash แทนเลขจริง เพื่อว่าถ้าตาราง otp_codes หลุดออกไป ก็เอาไปใช้ยืนยัน
    ตัวตนต่อไม่ได้ ผูก email เข้าไปใน message ด้วยกันเอา hash ของคนหนึ่งไปใช้
    กับอีกคน
    """
    msg = f"{_norm_email(email)}:{_as_text(code)}".encode()
    return hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _auth_log(email, event, hwid_hash=None, detail=None, ip=None):
    """
    บันทึกทุกเหตุการณ์สำคัญ — ตัวนี้จะช่วยมากตอนผู้เล่นทักมาว่า "เข้าไม่ได้"

    ห้าม raise ออกไป: log พังไม่ควรทำให้ผู้เล่นล็อกอินไม่ได้
    """
    try:
        supabase.table("auth_log").insert({
            "email": _norm_email(email) or None,
            "event": event,
            "hwid_hash": hwid_hash or None,
            "detail": detail,
            "request_ip": ip or None,
        }).execute()
    except Exception as exc:
        print(f"[auth_log] {event} ({email}): {exc}")


def _hwid_fingerprint(hwid_hash):
    """
    ค่าที่ฝังใน JWT แทนรหัสเครื่องดิบ

    payload ของ JWT เป็นแค่ base64 ที่ใครก็ถอดอ่านได้ (ส่วนที่เข้ารหัสคือลายเซ็น
    เท่านั้น) ถ้าเก็บ hwid ตรง ๆ คนที่ได้ไฟล์ persistent ของผู้เล่นคนอื่นไปจะเปิด
    token อ่านได้ทันทีว่าต้องปลอมตัวเป็นรหัสอะไร แล้วแก้เกมให้ส่งค่านั้นก็สวม
    สิทธิ์ได้เลย

    เก็บเป็น HMAC แทน -> token ที่หลุดออกไปไม่บอกใบ้ว่าต้องปลอมเป็นอะไร และ
    ย้อนกลับไม่ได้เพราะไม่มี JWT_SECRET
    """
    return hmac.new(
        JWT_SECRET.encode(), _as_text(hwid_hash).encode(), hashlib.sha256
    ).hexdigest()


def _create_game_token(email, hwid_hash, device_id):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": _norm_email(email),
        "hwid": _hwid_fingerprint(hwid_hash),
        "did": device_id,
        # แยกจาก token ของระบบโกงเดิม (create_token) ที่ไม่ได้ผูกเครื่อง
        "typ": "game",
        "exp": now + timedelta(days=GAME_TOKEN_EXPIRE_DAYS),
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_game_token(token, hwid_hash):
    """คืน payload เมื่อ token ใช้ได้ 'และ' มาจากเครื่องเดิมเท่านั้น"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if payload.get("typ") != "game":
        return None
    # กันเคสส่ง hwid ว่างมา: fingerprint ของค่าว่างก็ยังเป็นสตริงที่เทียบได้
    if not _as_text(hwid_hash):
        return None
    if not hmac.compare_digest(
        _as_text(payload.get("hwid")), _hwid_fingerprint(hwid_hash)
    ):
        return None
    return payload


def _device_slots(member):
    slots = _as_int(member.get("device_slots"), DEVICE_SLOTS_DEFAULT)
    return slots if slots > 0 else DEVICE_SLOTS_DEFAULT


def _active_devices(email):
    res = (
        supabase
        .table("devices")
        .select("*")
        .eq("email", _norm_email(email))
        .is_("released_at", "null")
        .order("activated_at")
        .execute()
    )
    return res.data or []


def _device_public(row):
    """ตัด hwid_hash ออกเสมอ — ไม่มีเหตุผลให้ client รู้รหัสเครื่องของเครื่องอื่น"""
    return {
        "id": row.get("id"),
        "platform": _as_text(row.get("platform"), "unknown"),
        "label": _as_text(row.get("label")) or None,
        "activated_at": row.get("activated_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


def _send_otp_email(email, code):
    """
    ส่ง OTP ผ่าน Resend

    ⚠️ credential ของผู้ให้บริการอีเมลต้องอยู่ที่นี่เท่านั้น ห้ามฝังในตัวเกม —
    ถ้าหลุดจะถูกเอาไปส่งสแปมจนโดเมนโดนแบล็กลิสต์ เกมส่งมาแค่อีเมลปลายทาง
    """
    if not RESEND_API_KEY or not OTP_FROM_EMAIL:
        if OTP_DEBUG:
            print(f"[OTP-DEBUG] {email} -> {code}")
            return True
        print("[OTP] ยังไม่ได้ตั้ง RESEND_API_KEY / OTP_FROM_EMAIL")
        return False

    html = (
        '<div style="font-family:sans-serif;max-width:420px">'
        "<h2>รหัสยืนยันการเข้าเกม</h2>"
        "<p>รหัสยืนยันของคุณคือ</p>"
        f'<p style="font-size:32px;letter-spacing:6px;font-weight:bold">{code}</p>'
        f"<p>รหัสนี้ใช้ได้ภายใน {OTP_TTL_MINUTES} นาที และใช้ได้เฉพาะเครื่องที่ขอเท่านั้น</p>"
        "<p style=\"color:#888;font-size:13px\">ถ้าคุณไม่ได้เป็นคนขอรหัสนี้ ไม่ต้องทำอะไรค่ะ</p>"
        "</div>"
    )

    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": OTP_FROM_EMAIL,
                "to": [email],
                "subject": f"รหัสยืนยัน The Last Year: {code}",
                "html": html,
            },
            timeout=15,
        )
        if res.status_code >= 400:
            print(f"[OTP] Resend ตอบ {res.status_code}: {res.text[:200]}")
            return False
        return True
    except Exception as exc:
        _log("send_otp_email", exc)
        return False


def _member_or_error(email):
    """
    หาสมาชิก + ตรวจสิทธิ์พื้นฐาน

    คืน (member, error_dict) — ถ้า error_dict ไม่ใช่ None ให้ endpoint คืนค่านั้นทันที
    """
    if _is_placeholder_email(email):
        # ยังไม่ได้ผูกอีเมลจริงผ่าน /linkemail ในบอท Discord
        return None, {"result": "use_linkemail"}

    member = _get_member(email)
    if not member:
        return None, {"result": "not_member"}
    if member.get("blacklist") is True:
        return None, {"result": "banned"}
    return member, None


# =====================
# Auth: Request OTP
# =====================
@app.post("/auth/request-otp")
def auth_request_otp(data: dict, request: Request):
    try:
        email = _norm_email(data.get("email"))
        hwid = _as_text(data.get("hwid_hash"))
        ip = _client_ip(request)

        if not email or not hwid:
            return {"result": "fail"}

        member, err = _member_or_error(email)
        if err:
            return err

        canon = _norm_email(member.get("email")) or email

        # โหมดอีเมลอย่างเดียว — บอกเกมให้ข้ามไปผูกเครื่องได้เลย
        #
        # ยังตรวจสมาชิกให้ครบก่อนถึงจะตอบ เพราะเกมต้องแยกให้ออกว่าเป็น
        # not_member / banned / use_linkemail ไม่ใช่ปล่อยผ่านทุกอีเมล
        if not OTP_REQUIRED:
            return {"result": "not_required", "email": canon}

        # ── เพดานการขอรหัส ────────────────────────────────────────────────
        # ดึงมาทีเดียวช่วง 24 ชม. แล้วนับช่วง 15 นาทีเอาใน Python
        # -> 2 query แทนที่จะเป็น 4
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=OTP_RATE_WINDOW_MINUTES)
        day_start = now - timedelta(hours=24)

        def _deny(scope, retry_minutes):
            _auth_log(canon, "otp_rate_limited", hwid, {"scope": scope, "ip": ip})
            return {"result": "rate_limited", "retry_after_minutes": retry_minutes}

        email_rows = _otp_rows_since("email", canon, day_start)
        if len(email_rows) >= OTP_DAILY_LIMIT_EMAIL:
            return _deny("email_day", 60 * 24)
        if _count_since(email_rows, window_start) >= OTP_RATE_LIMIT:
            return _deny("email_window", OTP_RATE_WINDOW_MINUTES)

        # IP เป็นด่านเดียวที่ผู้ยิงปลอมไม่ได้ (hwid ฝั่งเกมส่งมาเอง)
        # ถ้าอ่าน IP ไม่ได้ ก็ปล่อยผ่านด่านนี้ ดีกว่าบล็อกผู้เล่นจริงทิ้ง
        if ip:
            ip_rows = _otp_rows_since("request_ip", ip, day_start)
            if len(ip_rows) >= OTP_DAILY_LIMIT_IP:
                return _deny("ip_day", 60 * 24)
            if _count_since(ip_rows, window_start) >= OTP_RATE_LIMIT_IP:
                return _deny("ip_window", OTP_RATE_WINDOW_MINUTES)

        code = _gen_otp()
        expires = now + timedelta(minutes=OTP_TTL_MINUTES)

        supabase.table("otp_codes").insert({
            "email": canon,
            "code_hash": _hash_otp(canon, code),
            "purpose": "activate",
            "hwid_hash": hwid,
            "request_ip": ip or None,
            "expires_at": expires.isoformat(),
        }).execute()

        if not _send_otp_email(canon, code):
            _auth_log(canon, "otp_send_failed", hwid)
            return {"result": "email_error"}

        _auth_log(canon, "otp_sent", hwid)
        return {"result": "ok", "expires_in_minutes": OTP_TTL_MINUTES}

    except Exception as exc:
        _log("auth/request-otp", exc)
        return {"result": "server_error"}


# =====================
# Auth: Activate device
# =====================
@app.post("/auth/activate")
def auth_activate(data: dict, request: Request):
    try:
        email = _norm_email(data.get("email"))
        code = _as_text(data.get("otp"))
        hwid = _as_text(data.get("hwid_hash"))
        platform = _as_text(data.get("platform"), "unknown")[:32]
        label = _as_text(data.get("label"))[:60] or None
        ip = _client_ip(request)

        if not email or not hwid:
            return {"result": "fail"}
        if OTP_REQUIRED and not code:
            return {"result": "fail"}

        now = datetime.now(timezone.utc)

        # ── จำกัดอัตราการพยายามผูกเครื่องต่อ IP ───────────────────────────
        # สำคัญเป็นพิเศษในโหมดไม่ใช้ OTP: อีเมลเป็นหลักฐานชิ้นเดียว ถ้าไม่มี
        # ด่านนี้ ก็ไล่เดาอีเมลสมาชิกได้ไม่จำกัดโดยไม่มีอะไรขวาง
        ip_rows = _activate_rows_since(ip, now - timedelta(hours=24))
        if len(ip_rows) >= ACTIVATE_DAILY_LIMIT_IP:
            _auth_log(email, "activate_denied", hwid, {"scope": "ip_day"}, ip)
            return {"result": "rate_limited", "retry_after_minutes": 60 * 24}
        if _count_since(ip_rows, now - timedelta(minutes=OTP_RATE_WINDOW_MINUTES)) >= ACTIVATE_RATE_LIMIT_IP:
            _auth_log(email, "activate_denied", hwid, {"scope": "ip_window"}, ip)
            return {"result": "rate_limited", "retry_after_minutes": OTP_RATE_WINDOW_MINUTES}

        member, err = _member_or_error(email)
        if err:
            # บันทึกไว้ด้วย ไม่งั้นการไล่เดาอีเมลจะไม่ถูกนับเข้าเพดานข้างบน
            _auth_log(email, "activate_denied", hwid, {"reason": err.get("result")}, ip)
            return err

        canon = _norm_email(member.get("email")) or email

        # ── ตรวจ OTP (เฉพาะตอนเปิดโหมดยืนยันทางอีเมล) ────────────────────
        if OTP_REQUIRED:
            res = (
                supabase
                .table("otp_codes")
                .select("*")
                .eq("email", canon)
                .eq("purpose", "activate")
                .is_("consumed_at", "null")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                _auth_log(canon, "activate_denied", hwid, {"reason": "no_otp"}, ip)
                return {"result": "no_otp"}

            otp_row = rows[0]
            attempts = _as_int(otp_row.get("attempts"))

            if attempts >= OTP_MAX_ATTEMPTS:
                return {"result": "too_many_attempts"}

            expires_at = _parse_iso(otp_row.get("expires_at"))
            if expires_at is None or expires_at < now:
                return {"result": "expired"}

            # OTP ผูกกับเครื่องที่ขอ -> อ่านรหัสจากอีเมลแล้วเอาไปกรอกบนเครื่องอื่นไม่ได้
            if _as_text(otp_row.get("hwid_hash")) != hwid:
                return {"result": "wrong_device"}

            if not hmac.compare_digest(_as_text(otp_row.get("code_hash")), _hash_otp(canon, code)):
                supabase.table("otp_codes").update({"attempts": attempts + 1}).eq("id", otp_row["id"]).execute()
                _auth_log(canon, "otp_failed", hwid, {"attempts": attempts + 1}, ip)
                return {"result": "bad_otp", "attempts_left": max(0, OTP_MAX_ATTEMPTS - attempts - 1)}

            supabase.table("otp_codes").update({"consumed_at": now_iso()}).eq("id", otp_row["id"]).execute()

        # ── ผ่านการตรวจแล้ว ─────────────────────────────────────────────
        devices = _active_devices(canon)
        mine = next((d for d in devices if _as_text(d.get("hwid_hash")) == hwid), None)
        slots = _device_slots(member)

        if mine:
            # เครื่องเดิมยืนยันซ้ำ (เช่นลงเกมใหม่) — ไม่กิน slot เพิ่ม
            device_id = mine.get("id")
            patch = {"last_seen_at": now_iso(), "platform": platform}
            if label:
                patch["label"] = label
            supabase.table("devices").update(patch).eq("id", device_id).execute()
            slots_used = len(devices)
        else:
            if len(devices) >= slots:
                _auth_log(canon, "denied_no_slot", hwid, {"slots": slots}, ip)
                return {
                    "result": "no_slot",
                    "slots": slots,
                    "devices": [_device_public(d) for d in devices],
                    # บอกเกมว่าปลดเครื่องได้เองจากหน้านี้เลยไหม
                    "can_release": not OTP_REQUIRED,
                    "release_cooldown_days": DEVICE_RELEASE_COOLDOWN_DAYS,
                }
            ins = supabase.table("devices").insert({
                "email": canon,
                "hwid_hash": hwid,
                "platform": platform,
                "label": label,
            }).execute()
            device_id = (ins.data or [{}])[0].get("id")
            slots_used = len(devices) + 1

        # ตั้ง email_verified เฉพาะตอนผ่าน OTP จริงเท่านั้น
        # โหมดอีเมลอย่างเดียวไม่ได้พิสูจน์ว่าใครเป็นเจ้าของอีเมล การตั้งธงนี้
        # จะทำให้ข้อมูลโกหก และถ้าวันหลังเปิด OTP ขึ้นมาจะแยกไม่ออกว่าใคร
        # ยืนยันจริงแล้วบ้าง
        if OTP_REQUIRED and member.get("email_verified") is not True:
            supabase.table("member_list").update(
                {"email_verified": True}
            ).eq("email", member.get("email")).execute()

        _auth_log(canon, "activated", hwid, {"device_id": device_id, "otp": OTP_REQUIRED}, ip)

        return {
            "result": "ok",
            "token": _create_game_token(canon, hwid, device_id),
            "email": canon,
            "username": _as_text(member.get("username"), "Supporter"),
            "tier": _as_text(member.get("tier"), "Free"),
            "device_id": device_id,
            "slots": slots,
            "slots_used": slots_used,
            "free_mode": FREE_MODE,
            "grace_days": GRACE_PERIOD_DAYS,
        }

    except Exception as exc:
        _log("auth/activate", exc)
        return {"result": "server_error"}


# =====================
# Auth: Verify (เรียกทุกครั้งที่เปิดเกม)
# =====================
@app.post("/auth/verify")
def auth_verify(data: dict):
    try:
        token = _as_text(data.get("token"))
        hwid = _as_text(data.get("hwid_hash"))

        if not token or not hwid:
            return {"result": "invalid"}

        payload = _decode_game_token(token, hwid)
        if not payload:
            return {"result": "invalid"}

        email = _norm_email(payload.get("sub"))
        member = _get_member(email)
        if not member:
            return {"result": "invalid"}
        if member.get("blacklist") is True:
            return {"result": "banned"}

        device_id = payload.get("did")
        res = supabase.table("devices").select("*").eq("id", device_id).limit(1).execute()
        rows = res.data or []
        if not rows:
            return {"result": "device_revoked"}

        device = rows[0]
        # เครื่องถูกปลดไปแล้ว (ผู้เล่นปลดเอง หรือ dev รีเซ็ตให้) -> ต้องยืนยันใหม่
        if device.get("released_at") is not None:
            return {"result": "device_revoked"}
        if _as_text(device.get("hwid_hash")) != hwid:
            return {"result": "device_revoked"}

        supabase.table("devices").update({"last_seen_at": now_iso()}).eq("id", device_id).execute()

        # ต่ออายุแบบเลื่อนไปเรื่อย ๆ — คนที่เปิดเกมสม่ำเสมอจะไม่โดนเด้งออกกลางคัน
        return {
            "result": "ok",
            "token": _create_game_token(email, hwid, device_id),
            "email": email,
            "username": _as_text(member.get("username"), "Supporter"),
            "tier": _as_text(member.get("tier"), "Free"),
            "free_mode": FREE_MODE,
            "grace_days": GRACE_PERIOD_DAYS,
        }

    except Exception as exc:
        _log("auth/verify", exc)
        return {"result": "server_error"}


# =====================
# Auth: My devices
# =====================
@app.post("/auth/devices")
def auth_devices(data: dict):
    try:
        token = _as_text(data.get("token"))
        hwid = _as_text(data.get("hwid_hash"))

        payload = _decode_game_token(token, hwid) if token and hwid else None
        if not payload:
            return {"result": "invalid"}

        email = _norm_email(payload.get("sub"))
        member = _get_member(email)
        if not member:
            return {"result": "invalid"}

        devices = _active_devices(email)

        # เหลืออีกกี่วันถึงจะปลดเครื่องเองได้
        cooldown_left = 0
        since = datetime.now(timezone.utc) - timedelta(days=DEVICE_RELEASE_COOLDOWN_DAYS)
        recent = (
            supabase
            .table("devices")
            .select("released_at")
            .eq("email", email)
            .eq("released_by", "self")
            .gte("released_at", since.isoformat())
            .order("released_at", desc=True)
            .limit(1)
            .execute()
        )
        if recent.data:
            last = _parse_iso(recent.data[0].get("released_at"))
            if last:
                elapsed = (datetime.now(timezone.utc) - last).days
                cooldown_left = max(0, DEVICE_RELEASE_COOLDOWN_DAYS - elapsed)

        return {
            "result": "ok",
            "slots": _device_slots(member),
            "devices": [_device_public(d) for d in devices],
            "current_device_id": payload.get("did"),
            "release_cooldown_days_left": cooldown_left,
        }

    except Exception as exc:
        _log("auth/devices", exc)
        return {"result": "server_error"}


# =====================
# Auth: Release a device (ผู้เล่นปลดเอง)
# =====================
@app.post("/auth/release-device")
def auth_release_device(data: dict, request: Request):
    try:
        token = _as_text(data.get("token"))
        hwid = _as_text(data.get("hwid_hash"))
        device_id = data.get("device_id")
        ip = _client_ip(request)

        if device_id is None or not hwid:
            return {"result": "fail"}

        # ── ทางที่ 1: มี token อยู่แล้ว (เครื่องที่ล็อกอินผ่าน) ────────────
        payload = _decode_game_token(token, hwid) if token else None
        email = _norm_email(payload.get("sub")) if payload else ""
        current_device_id = payload.get("did") if payload else None

        # ── ทางที่ 2: ยังไม่มี token เพราะ slot เต็ม -> ยืนยันด้วยอีเมล ────
        #
        # ถ้าไม่มีทางนี้ คนที่เปลี่ยนเครื่องจะติดตาย: ปลด slot ต้องใช้ token
        # แต่จะได้ token ต้องมี slot ว่างก่อน
        #
        # เปิดเฉพาะโหมดไม่ใช้ OTP เท่านั้น — โหมดนั้นอีเมลเป็นหลักฐานระดับ
        # เดียวกับการล็อกอินอยู่แล้ว จึงไม่ได้ลดความปลอดภัยลง แต่ถ้าเปิด OTP
        # ไว้ ช่องนี้จะกลายเป็นทางลัดข้าม OTP ทันที
        if not email and not OTP_REQUIRED:
            member, err = _member_or_error(_norm_email(data.get("email")))
            if err:
                return err
            email = _norm_email(member.get("email"))

        if not email:
            return {"result": "invalid"}

        # ── cooldown: ปลดเองได้ทุก DEVICE_RELEASE_COOLDOWN_DAYS วัน ────────────
        # ไม่งั้นจะกลายเป็นช่องให้เวียนเครื่องไปเรื่อย ๆ จนไม่ต่างกับไม่มี slot
        since = datetime.now(timezone.utc) - timedelta(days=DEVICE_RELEASE_COOLDOWN_DAYS)
        recent = (
            supabase
            .table("devices")
            .select("released_at")
            .eq("email", email)
            .eq("released_by", "self")
            .gte("released_at", since.isoformat())
            .limit(1)
            .execute()
        )
        if recent.data:
            return {"result": "cooldown", "cooldown_days": DEVICE_RELEASE_COOLDOWN_DAYS}

        # เครื่องต้องเป็นของคนนี้ และยังไม่ถูกปลด
        target = [d for d in _active_devices(email) if str(d.get("id")) == str(device_id)]
        if not target:
            return {"result": "not_found"}

        supabase.table("devices").update({
            "released_at": now_iso(),
            "released_by": "self",
        }).eq("id", device_id).execute()

        _auth_log(email, "released", hwid, {"device_id": device_id, "by": "self"}, ip)

        return {
            "result": "ok",
            # ถ้าปลดเครื่องที่กำลังเล่นอยู่ token ปัจจุบันจะใช้ไม่ได้ทันที
            # (current_device_id เป็น None เมื่อปลดผ่านอีเมลจากเครื่องใหม่)
            "self_revoked": current_device_id is not None
                            and str(current_device_id) == str(device_id),
            "next_release_in_days": DEVICE_RELEASE_COOLDOWN_DAYS,
        }

    except Exception as exc:
        _log("auth/release-device", exc)
        return {"result": "server_error"}


# =====================
# Admin: reset devices (ใช้ตอนผู้เล่นทักมาว่าเปลี่ยนเครื่อง)
# =====================
@app.post("/admin/reset-devices")
def admin_reset_devices(data: dict):
    """
    รับ admin_token ทาง body ไม่ใช่ query string — query string จะไปโผล่ใน
    access log ของ proxy/เซิร์ฟเวอร์ ส่วน body ไม่ถูกบันทึก

    ส่ง dry_run=true มาเพื่อดูรายการเครื่องก่อนโดยยังไม่ปลดจริง
    """
    try:
        if not SYNC_TOKEN or not hmac.compare_digest(_as_text(data.get("admin_token")), SYNC_TOKEN):
            return {"result": "unauthorized"}

        email = _norm_email(data.get("email"))
        if not email:
            return {"result": "fail"}

        member = _get_member(email)
        if not member:
            return {"result": "not_member"}

        canon = _norm_email(member.get("email")) or email
        devices = _active_devices(canon)

        if data.get("dry_run"):
            return {
                "result": "ok",
                "dry_run": True,
                "slots": _device_slots(member),
                "devices": [_device_public(d) for d in devices],
            }

        if devices:
            supabase.table("devices").update({
                "released_at": now_iso(),
                "released_by": "dev",
            }).eq("email", canon).is_("released_at", "null").execute()

        _auth_log(canon, "released", None, {"count": len(devices), "by": "dev"})

        return {
            "result": "ok",
            "released": len(devices),
            "devices": [_device_public(d) for d in devices],
        }

    except Exception as exc:
        _log("admin/reset-devices", exc)
        return {"result": "server_error"}
