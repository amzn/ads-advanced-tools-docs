import requests
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_advertisers(access_token, client_id, scope):
    """
    Get Amazon DSP advertisers
    
    Args:
        access_token: Bearer token for authorization
        client_id: Amazon Advertising API Client ID
        scope: Amazon Advertising API Scope (profile ID)
    
    Returns:
        JSON response with advertisers data
    """
    url = 'https://advertising-api.amazon.com/dsp/advertisers'
    
    headers = {
        'Amazon-Advertising-API-ClientId': client_id,
        'Authorization': f'Bearer {access_token}',
        'Amazon-Advertising-API-Scope': scope
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        print("Success! Advertisers retrieved:")
        print(json.dumps(response.json(), indent=2))
        return response.json()
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
        return None

if __name__ == "__main__":
    # Load credentials from environment variables
    CLIENT_ID = os.getenv("CLIENT_ID")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    SCOPE = os.getenv("SCOPE")
    
    if not CLIENT_ID or not ACCESS_TOKEN or not SCOPE:
        print("Error: Please set CLIENT_ID, ACCESS_TOKEN, and SCOPE in your .env file")
        exit(1)
    
    get_advertisers(ACCESS_TOKEN, CLIENT_ID, SCOPE)
