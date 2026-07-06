"""
Example: Promotion & Discount Efficiency

Use case: Brands separating full-price demand from discount-subsidized
demand to understand true campaign effectiveness.

How it works:
    When a purchase occurs, this script flags whether the buyer used a
    coupon, received a discount, or paid full price. This prevents the
    ad platform from training exclusively on discount-driven buyers
    during sales events, which can crater post-promotion performance.

Custom attributes sent:
    - coupon_used: Whether any coupon was applied ("true" or "false")
    - discount_status: Discount category ("no_discount", "seasonal", "clearance")
    - full_price_flag: Whether the purchase was at full price
    - promo_type: Type of promotion active ("none", "bogo", "percentage_off")
    - deal_event: Whether tied to a deal event ("none", "prime_day", "black_friday")

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import os
import sys

# Add project root to path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.send_event import build_event_payload, hash_email, send_conversion_event


def main():
    """Send a purchase event with promotion/discount context attributes."""

    # Step 1: Build match keys from hashed PII
    match_keys = [
        {
            "type": "EMAIL",
            "values": [hash_email("buyer@example.com")],
        }
    ]

    # Step 2: Define custom attributes for promotion tracking
    # This example shows a FULL PRICE purchase (no discount applied).
    # For discounted purchases, you'd change these values accordingly:
    #   coupon_used="true", discount_status="seasonal", full_price_flag="false"
    #
    # This data helps the ad platform distinguish between:
    #   - Customers with genuine brand affinity (buy at full price)
    #   - Discount-seekers (only buy during sales)
    custom_data = [
        {"name": "coupon_used", "dataType": "STRING", "value": "false"},
        {"name": "discount_status", "dataType": "STRING", "value": "no_discount"},
        {"name": "full_price_flag", "dataType": "STRING", "value": "true"},
        {"name": "promo_type", "dataType": "STRING", "value": "none"},
        {"name": "deal_event", "dataType": "STRING", "value": "none"},
    ]

    # Step 3: Build the event payload
    payload = build_event_payload(
        event_name="PURCHASE",
        conversion_type="OFF_AMAZON_PURCHASES",
        dataset_name="RetailOrders",
        country_code="US",
        match_keys=match_keys,
        value=89.99,
        currency_code="USD",
        custom_data=custom_data,
    )

    # Step 4: Send the event
    response = send_conversion_event(payload)

    # Step 5: Check the response
    if "success" in response and response["success"]:
        print(f"\nPromotion efficiency event sent successfully! "
              f"Index: {response['success'][0]['index']}")
    if "error" in response and response["error"]:
        print(f"\nEvent had errors: {response['error']}")


if __name__ == "__main__":
    main()
