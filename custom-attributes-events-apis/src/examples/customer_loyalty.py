"""
Example: Customer Loyalty & Retention Optimization

Use case: Brands with loyalty programs sending membership tier and
retention context to support loyalty-focused campaign strategies.

How it works:
    When a loyalty member makes a purchase, this script sends their
    membership tier, tenure, and retention segment as custom attributes.
    This helps the ad platform distinguish between:
    - True new customer acquisition (growing the customer base)
    - Existing loyal customers who would have bought anyway
    - Lapsed members who need reactivation

    Without these signals, 70%+ of ad-driven purchases may come from
    existing members — meaning you're paying to acquire customers you
    already have.

Custom attributes sent:
    - loyalty_status: Whether the buyer is a member or non-member
    - loyalty_tier: Membership level (e.g., "gold", "platinum", "silver")
    - retention_segment: Business classification of the customer
    - lifetime_revenue: Total historical revenue in cents (integer)
    - months_as_member: How long they've been a member (integer)

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import os
import sys

# Add project root to path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.send_event import build_event_payload, hash_email, send_conversion_event


def main():
    """Send a purchase event with loyalty/retention custom attributes."""

    # Step 1: Build match keys from hashed PII
    match_keys = [
        {
            "type": "EMAIL",
            "values": [hash_email("loyalmember@example.com")],
        }
    ]

    # Step 2: Define custom attributes for loyalty segmentation
    # These attributes enable the ad platform to:
    #   - Separate acquisition from retention in reporting
    #   - Build lookalike audiences from your best customers
    #   - Avoid spending budget on customers who'd convert organically
    #
    # lifetime_revenue uses cents as INTEGER (245000 = $2,450.00)
    # to comply with the API's alphanumeric-only value constraint.
    custom_data = [
        {"name": "loyalty_status", "dataType": "STRING", "value": "member"},
        {"name": "loyalty_tier", "dataType": "STRING", "value": "gold"},
        {"name": "retention_segment", "dataType": "STRING", "value": "high_value_existing"},
        {"name": "lifetime_revenue", "dataType": "INTEGER", "value": "245000"},
        {"name": "months_as_member", "dataType": "INTEGER", "value": "24"},
    ]

    # Step 3: Build the event payload
    payload = build_event_payload(
        event_name="PURCHASE",
        conversion_type="OFF_AMAZON_PURCHASES",
        dataset_name="LoyaltyProgram",
        country_code="US",
        match_keys=match_keys,
        value=129.99,
        currency_code="USD",
        custom_data=custom_data,
    )

    # Step 4: Send the event
    response = send_conversion_event(payload)

    # Step 5: Check the response
    if "success" in response and response["success"]:
        print(f"\nCustomer loyalty event sent successfully! "
              f"Index: {response['success'][0]['index']}")
    if "error" in response and response["error"]:
        print(f"\nEvent had errors: {response['error']}")


if __name__ == "__main__":
    main()
