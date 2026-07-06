"""
Example: Lead Scoring & Predictive Value

Use case: Automotive, financial, and B2B advertisers sending lead quality
signals to optimize bids toward high-value prospects.

How it works:
    When a potential customer submits a lead form (loan application, test drive
    request, etc.), your CRM scores the lead based on qualification criteria.
    This script sends that lead score as a custom attribute alongside the
    conversion event, enabling the ad platform to learn which audiences
    produce the highest-quality leads — not just the most leads.

Custom attributes sent:
    - lead_score: Qualitative tier (e.g., "high_intent", "medium", "low")
    - predicted_value: Estimated dollar value of the lead (as integer cents)
    - lead_source: Where the lead originated (e.g., "website_form")
    - customer_segment: Business classification (e.g., "enterprise", "smb")

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import os
import sys

# Add project root to path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.send_event import build_event_payload, hash_email, send_conversion_event


def main():
    """Send a lead scoring conversion event with custom attributes."""

    # Step 1: Build match keys from hashed PII
    # The API requires email addresses to be SHA-256 hashed for privacy.
    # hash_email() normalizes (lowercase, strip) then hashes the email.
    match_keys = [
        {
            "type": "EMAIL",
            "values": [hash_email("customer@example.com")],
        }
    ]

    # Step 2: Define custom attributes for lead scoring
    # These attributes tell the ad platform about the QUALITY of this lead,
    # not just that a lead occurred. This enables value-based bidding.
    # Note: Values must be alphanumeric + underscore only (no dots or special chars).
    # For monetary values, use integer representation (e.g., "450" for $450).
    custom_data = [
        {"name": "lead_score", "dataType": "STRING", "value": "high_intent"},
        {"name": "predicted_value", "dataType": "INTEGER", "value": "450"},
        {"name": "lead_source", "dataType": "STRING", "value": "website_form"},
        {"name": "customer_segment", "dataType": "STRING", "value": "enterprise"},
    ]

    # Step 3: Build the event payload
    # conversion_type="LEAD" indicates this is a lead generation event.
    # The value field represents the predicted monetary value of this lead.
    payload = build_event_payload(
        event_name="LEAD_SUBMISSION",
        conversion_type="LEAD",
        dataset_name="LeadGenCampaign",
        country_code="US",
        match_keys=match_keys,
        value=450.00,
        custom_data=custom_data,
    )

    # Step 4: Send the event to Amazon Ads Events API
    # The response contains "success" and "error" arrays indicating
    # which events were accepted or rejected.
    response = send_conversion_event(payload)

    # Step 5: Check the response
    if "success" in response and response["success"]:
        print(f"\nLead scoring event sent successfully! "
              f"Index: {response['success'][0]['index']}")
    if "error" in response and response["error"]:
        print(f"\nEvent had errors: {response['error']}")


if __name__ == "__main__":
    main()
