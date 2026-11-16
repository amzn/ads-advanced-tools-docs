import requests
from dotenv import load_dotenv
import os
import json

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")

# Step 1: Get a fresh access token
token_response = requests.post("https://api.amazon.com/auth/o2/token", data={
    "grant_type": "refresh_token",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "refresh_token": REFRESH_TOKEN,
})

access_token = token_response.json()["access_token"]

# Step 2: Query advertiser accounts
url = "https://advertising-api.amazon.com/adsApi/v1/query/advertiserAccounts"

headers = {
    "Authorization": f"Bearer {access_token}",
    "Amazon-Advertising-API-ClientId": CLIENT_ID,
    "Content-Type": "application/json",
}

payload = {
    "isGlobalAccountFilter": {"include": [True]},
    "maxResults": 100,
}

all_accounts = []
next_token = None

while True:
    if next_token:
        payload["nextToken"] = next_token

    response = requests.post(url, headers=headers, json=payload)
    data = response.json()

    accounts = data.get("advertiserAccounts", [])
    all_accounts.extend(accounts)

    next_token = data.get("nextToken")
    if not next_token:
        break

# Step 3: Filter to DSP-enabled accounts only
dsp_accounts = [
    acct for acct in all_accounts
    if any(alt.get("dspAdvertiserId") for alt in acct.get("alternateIds", []))
]

print(f"\nFound {len(dsp_accounts)} DSP-enabled accounts (out of {len(all_accounts)} total):\n")
for acct in dsp_accounts:
    acct_id = acct.get("advertiserAccountId", "N/A")
    name = acct.get("displayName", "N/A")
    currency = acct.get("currencyCode", "N/A")
    is_global = acct.get("isGlobalAccount", False)

    # Find US alternate ID if available
    us_profile = None
    dsp_id = None
    for alt in acct.get("alternateIds", []):
        if alt.get("countryCode") == "US":
            us_profile = alt.get("profileId")
        if alt.get("region") == "NA":
            dsp_id = alt.get("dspAdvertiserId")

    print(f"Account ID: {acct_id}")
    print(f"  Name: {name}")
    print(f"  Currency: {currency}")
    print(f"  Global: {is_global}")
    if dsp_id:
        print(f"  DSP Advertiser ID (NA): {dsp_id}")
    if us_profile:
        print(f"  US Profile ID: {us_profile}")
    print()
    print("
=== For your .env file ===")
print(f"ACCOUNT_ID={dsp_id}   # Use this DSP Advertiser ID as ACCOUNT_ID in create_events.py")
