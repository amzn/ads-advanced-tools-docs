"""
Example: Retail Margin Optimization

Use case: E-commerce brands sending margin and profitability data
to optimize for net-profit ROAS rather than raw revenue.

How it works:
    When a purchase occurs, this script sends the product's margin data
    as custom attributes. This allows the ad platform to optimize toward
    profitable products rather than just expensive ones. A $50 accessory
    with 65% margin is more valuable than a $500 TV with 3% margin.

Custom attributes sent:
    - price_of_unit: Unit price in cents (integer, e.g., 4999 = $49.99)
    - product_margin_percentage: Margin as integer percentage (e.g., 65 = 65%)
    - product_category: Product category for segmentation
    - is_private_label: Whether this is a higher-margin private label product

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import os
import sys

# Add project root to path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.send_event import build_event_payload, hash_email, send_conversion_event


def main():
    """Send a purchase event with margin/profitability custom attributes."""

    # Step 1: Build match keys from hashed PII
    match_keys = [
        {
            "type": "EMAIL",
            "values": [hash_email("shopper@example.com")],
        }
    ]

    # Step 2: Define custom attributes for margin optimization
    # Key insight: Use INTEGER type for numeric values.
    # Represent monetary values in cents (4999 = $49.99) and
    # percentages as whole numbers (65 = 65%) to comply with the
    # API constraint that values can only contain [A-Za-z0-9_].
    custom_data = [
        {"name": "price_of_unit", "dataType": "INTEGER", "value": "4999"},
        {"name": "product_margin_percentage", "dataType": "INTEGER", "value": "65"},
        {"name": "product_category", "dataType": "STRING", "value": "electronics"},
        {"name": "is_private_label", "dataType": "STRING", "value": "true"},
    ]

    # Step 3: Build the event payload
    # The top-level "value" field is the purchase amount (used for ROAS calculation).
    # The custom attributes provide the MARGIN context that turns revenue-based
    # optimization into profit-based optimization.
    payload = build_event_payload(
        event_name="PURCHASE",
        conversion_type="OFF_AMAZON_PURCHASES",
        dataset_name="RetailOrders",
        country_code="US",
        match_keys=match_keys,
        value=49.99,
        currency_code="USD",
        custom_data=custom_data,
    )

    # Step 4: Send the event
    response = send_conversion_event(payload)

    # Step 5: Check the response
    if "success" in response and response["success"]:
        print(f"\nRetail margin event sent successfully! "
              f"Index: {response['success'][0]['index']}")
    if "error" in response and response["error"]:
        print(f"\nEvent had errors: {response['error']}")


if __name__ == "__main__":
    main()
