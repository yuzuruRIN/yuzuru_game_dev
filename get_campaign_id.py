import requests

PATREON_ACCESS_TOKEN = "ใส่ access token ของคุณตรงนี้"

url = "https://www.patreon.com/api/oauth2/v2/campaigns"
headers = {
    "Authorization": f"Bearer {PATREON_ACCESS_TOKEN}",
    "User-Agent": "PatreonCampaignCheck/1.0"
}

resp = requests.get(url, headers=headers, timeout=30)
resp.raise_for_status()

print(resp.json())