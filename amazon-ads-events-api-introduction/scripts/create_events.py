import requests
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def create_events(access_token, client_id, account_id, events_data):
    """
    Create Amazon Ads conversion events
    
    Args:
        access_token: Bearer token for authorization
        client_id: Amazon Ads Client ID
        account_id: Amazon Ads Account ID (advertiser ID)
        events_data: Dictionary containing events data
    
    Returns:
        JSON response with created events
    """
    url = 'https://advertising-api.amazon.com/adsApi/v1/create/events'
    
    headers = {
        'Amazon-Ads-AccountId': account_id,
        'Amazon-Ads-ClientId': client_id,
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    
    response = requests.post(url, headers=headers, json=events_data)
    
    if response.status_code == 200:
        print("Success! Events created:")
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
    ACCOUNT_ID = os.getenv("ACCOUNT_ID")
    
    if not CLIENT_ID or not ACCESS_TOKEN or not ACCOUNT_ID:
        print("Error: Please set CLIENT_ID, ACCESS_TOKEN, and ACCOUNT_ID in your .env file")
        exit(1)
    
    # Example event data
    events_payload = {
        "events": [
            {
                "eventDescription": {
                    "name": "Add_To_Cart_test",
                    "conversionType": "ADD_TO_SHOPPING_CART",
                    "eventSource": "WEBSITE",
                    "eventIngestionMethod": "SERVER_TO_SERVER",
                    "dataSetName": "dataset_1211"
                },
                "countryCode": "GB",
                "eventTime": "2025-11-12T14:15:22Z",
                "matchKeys": [
                    {
                        "type": "EMAIL",
                        "values": ["e84c32d63d75e0d725c0357b91dcb0566f3c6bc68ada48b8927f22622de8b1e8"]
                    }
                ],
                "consent": {
                    "tcf": "XXXXCP7aT0AP7aT0AEsACBENArEoAP_gAEPgAAwIINJD7D7FbSFCwHpzaLsAMAhHRsCAQoQAAASBAmABQAKQIAQCgkAQFASgBAACAAAAAAAIIAAAgAEAAAAIAAACAAAAEAAIAAAAEAAAmAgAAIIACAAAhAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAAQOhQD2F2K2kKFkPCmQWYAQBCijYEAhQAAAAkCBIAAgAUgQAgFIIAgAIFAAAAAAAAAQEgCQAAQABAAAIACgAAAAAAIAAAAAAAQQAAAAAIAAAAAAAAEAAAAAAAQAAAAIAABEhCAAQQAEAAAAAAAQAAAAAAAAAAABAAA"
                }
            }
        ]
    }
    
    create_events(ACCESS_TOKEN, CLIENT_ID, ACCOUNT_ID, events_payload)
