"""
Example: Subscription Renewals & LTV Optimization

Use case: SaaS, media, and telecom brands sending renewal and retention
signals to maximize Customer Lifetime Value optimization.

How it works:
    When a subscriber renews their subscription, this script sends the
    renewal event enriched with subscription tier, renewal count, and
    lifetime revenue. This teaches the ad platform what a "retainable"
    subscriber looks like at acquisition time, shifting bid optimization
    from "cheapest sign-up" to "longest-retained customer."

Custom attributes sent:
    - subscription_type: Plan tier (e.g., "premium", "standard", "free")
    - renewal_cycle: How many times this customer has renewed (integer)
    - is_active_subscriber: Current subscription status
    - lifetime_revenue: Total revenue from this customer in cents (integer)
    - months_as_customer: Tenure length in months (integer)

API: POST /adsApi/v1/create/events
Docs: https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import os
import sys

# Add project root to path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.send_event import build_event_payload, hash_email, send_conversion_event


def main():
    """Send a subscription renewal event with LTV custom attributes."""

    # Step 1: Build match keys from hashed PII
    # Per API spec: match key values must be SHA-256 hashed for PII types.
    # Each match key type can have exactly 1 value in the "values" array.
    match_keys = [
        {
            "type": "EMAIL",
            "values": [hash_email("subscriber@example.com")],
        }
    ]

    # Step 2: Define custom attributes (max 13 per event)
    # These attributes provide the ad platform with customer lifetime value
    # signals, enabling optimization toward high-retention subscribers.
    #
    # Per API spec:
    #   - name: only letters, numbers, and underscore (snake_case)
    #   - dataType: STRING | INTEGER | TIMESTAMP
    #   - value: only letters, numbers, and underscore
    #
    # For monetary values, use cents as INTEGER (e.g., 71988 = $719.88)
    custom_data = [
        {"name": "subscription_type", "dataType": "STRING", "value": "premium"},
        {"name": "renewal_cycle", "dataType": "INTEGER", "value": "12"},
        {"name": "is_active_subscriber", "dataType": "STRING", "value": "true"},
        {"name": "lifetime_revenue", "dataType": "INTEGER", "value": "71988"},
        {"name": "months_as_customer", "dataType": "INTEGER", "value": "36"},
    ]

    # Step 3: Build the event payload
    # conversion_type="OFF_AMAZON_PURCHASES" since this is a paid transaction.
    # event_id provides deduplication — if this event is accidentally sent twice,
    # the API will only process it once.
    payload = build_event_payload(
        event_name="SUBSCRIPTION_RENEWAL",
        conversion_type="OFF_AMAZON_PURCHASES",
        dataset_name="SubscriptionEvents",
        country_code="US",
        match_keys=match_keys,
        custom_data=custom_data,
        value=59.99,
        currency_code="USD",
        event_source="WEBSITE",
        event_id="renewal_evt_20260706_001",  # Deduplication ID
    )

    # Step 4: Send to Amazon Ads Events API (NA region)
    response = send_conversion_event(payload, region="NA")

    # Step 5: Check response — API returns 207 with success/error arrays
    # A 207 Multi-Status response means the request was received; check
    # individual event results in the success/error arrays.
    if "success" in response and response["success"]:
        print(f"\nEvent sent successfully! Index: {response['success'][0]['index']}")
    if "error" in response and response["error"]:
        print(f"\nEvent had errors: {response['error']}")


if __name__ == "__main__":
    main()
