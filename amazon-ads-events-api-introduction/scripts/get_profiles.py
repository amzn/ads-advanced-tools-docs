import requests
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_profiles(access_token, client_id):
    """
    Get Amazon Advertising profiles
    
    Args:
        access_token: Bearer token for authorization
        client_id: Amazon Advertising API Client ID
    
    Returns:
        JSON response with profiles data
    """
    url = 'https://advertising-api.amazon.com/v2/profiles'
    
    headers = {
        'Amazon-Advertising-API-ClientId': client_id,
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        print("Success! Profiles retrieved:")
        print(json.dumps(response.json(), indent=2))
        return response.json()
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
        return None

if __name__ == "__main__":
    # Use the latest access token you provided
    CLIENT_ID = "<YOUR_CLIENT_ID>"
    ACCESS_TOKEN = "<YOUR_ACCESS_TOKEN>"
    
    get_profiles(ACCESS_TOKEN, CLIENT_ID)
