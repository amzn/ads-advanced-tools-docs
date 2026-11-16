import requests
import webbrowser
from urllib.parse import urlencode
from dotenv import load_dotenv
import os

load_dotenv()

# Configuration (loaded from .env)
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# Step 1: Generate and open the authorization URL
print("=== Step 1: Authorization ===")
params = {
    "client_id": CLIENT_ID,
    "scope": "advertising::campaign_management",
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
}
auth_url = "https://www.amazon.com/ap/oa?" + urlencode(params)
print(f"Opening browser to:\n{auth_url}\n")
webbrowser.open(auth_url)

# Step 2: Get the code from the user
print("After authorizing, copy the 'code' parameter from the redirect URL.")
authorization_code = input("Paste your authorization code here: ")

# Step 3: Exchange code for tokens
print("\n=== Step 2: Token Exchange ===")
token_response = requests.post("https://api.amazon.com/auth/o2/token", data={
    "grant_type": "authorization_code",
    "code": authorization_code,
    "redirect_uri": REDIRECT_URI,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
})

result = token_response.json()
if "access_token" in result:
    print(f"\nAccess Token: {result['access_token']}")
    print(f"Refresh Token: {result['refresh_token']}")
    print(f"Expires In: {result.get('expires_in')} seconds")
else:
    print(f"\nError: {result}")
