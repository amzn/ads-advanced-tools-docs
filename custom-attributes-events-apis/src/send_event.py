"""Core event submission logic for Amazon Ads Events API (Ads API v1).

This module provides the main building blocks for constructing and sending
conversion events with custom attributes. It includes:
    - build_event_payload(): Constructs a valid API request payload
    - hash_email() / hash_phone(): PII hashing utilities for match keys
    - send_conversion_event(): High-level function to send an event

The module enforces API constraints at build time (before the request is sent)
to provide clear error messages and prevent malformed requests.

Author: Chintan Sanghavi
Date: July 6, 2026

API Reference: POST /adsApi/v1/create/events
Schema: EventCreate object

Key constraints from the OpenAPI spec:
- events array: 1-500 items per request
- customData array: 0-13 items per event
- customData.name: Only letters, numbers, and underscore allowed
- customData.dataType: STRING | INTEGER | TIMESTAMP
- customData.value: Only letters, numbers, and underscore allowed
- matchKeys array: 1-14 items per event
- matchKeys.values: exactly 1 value per match key
- countryCode: required (ISO 3166-1 alpha-2)
- eventTime: required (ISO 8601 datetime)
- eventDescription: required (conversionType, eventIngestionMethod, eventSource, name)
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from src.client import CAPIClient

logger = logging.getLogger(__name__)

# Validation pattern for custom attribute names and values per API spec.
# The API only accepts alphanumeric characters and underscores.
# Values like "49.99" (with dots) or "high-intent" (with hyphens) will be rejected.
VALID_ATTRIBUTE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def build_event_payload(
    event_name: str,
    conversion_type: str,
    dataset_name: str,
    country_code: str,
    match_keys: list[dict],
    custom_data: list[dict],
    value: Optional[float] = None,
    currency_code: Optional[str] = None,
    event_source: str = "WEBSITE",
    event_time: Optional[str] = None,
    event_id: Optional[str] = None,
    units_sold: Optional[int] = None,
) -> dict:
    """
    Build a complete Events API payload conforming to the Ads API v1 spec.

    This function constructs the JSON payload that will be sent to the API.
    It validates inputs against API constraints before building the payload,
    so errors are caught early with clear messages.

    Args:
        event_name: The name of the imported event (e.g., "SUBSCRIPTION_RENEWAL").
            This identifies the type of action the customer took.
        conversion_type: The category of conversion. Must be one of:
            ADD_TO_SHOPPING_CART, APPLICATION, CHECKOUT, CONTACT, LEAD,
            OFF_AMAZON_PURCHASES, MOBILE_APP_FIRST_START, PAGE_VIEW,
            SEARCH, SIGN_UP, SUBSCRIBE, OTHER.
        dataset_name: Event DataSet name that groups related events.
            Pattern: [A-Za-z][A-Za-z0-9_-]* (must start with a letter).
        country_code: ISO 3166-1 alpha-2 country code (e.g., "US", "GB", "DE").
        match_keys: List of match key objects used to identify the user.
            Each object has "type" (e.g., "EMAIL") and "values" (list with
            one SHA-256 hashed value). Used to match conversions to ad exposures.
        custom_data: List of custom attribute dicts. Each must have:
            - "name": Attribute identifier (alphanumeric + underscore only)
            - "dataType": One of "STRING", "INTEGER", "TIMESTAMP"
            - "value": The attribute value (alphanumeric + underscore only)
        value: Monetary value for OFF_AMAZON_PURCHASES (in the currency unit,
            e.g., 59.99 for $59.99). Non-monetary for other conversion types.
        currency_code: ISO 4217 currency code (e.g., "USD", "EUR").
            Required when value represents a monetary amount.
        event_source: Where the conversion occurred. One of: WEBSITE, ANDROID,
            IOS, FIRE_TV, OFFLINE, MEASUREMENT_ATTRIBUTION_PARTNER.
            Defaults to "WEBSITE".
        event_time: ISO 8601 timestamp of when the conversion happened.
            Defaults to current UTC time if not provided.
            Must be within 24-48 hours for optimal attribution matching.
        event_id: Client-specified deduplication ID. If the same event_id
            is sent multiple times, the API will deduplicate it.
        units_sold: Number of items purchased (only for OFF_AMAZON_PURCHASES).

    Returns:
        A dictionary representing the complete API request payload,
        ready to be sent as JSON to the Events API endpoint.

    Raises:
        ValueError: If custom_data exceeds 13 attributes, match_keys exceeds 14,
            or attribute names/values contain invalid characters.

    Example:
        payload = build_event_payload(
            event_name="PURCHASE",
            conversion_type="OFF_AMAZON_PURCHASES",
            dataset_name="RetailOrders",
            country_code="US",
            match_keys=[{"type": "EMAIL", "values": ["<sha256_hash>"]}],
            custom_data=[
                {"name": "loyalty_tier", "dataType": "STRING", "value": "gold"}
            ],
            value=49.99,
            currency_code="USD",
        )
    """
    # Default to current UTC time if no event time provided
    if event_time is None:
        event_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Input validation against API constraints ---

    # The API allows a maximum of 13 custom attributes per event
    # (10 fully custom + 3 reserved: brand, product, category)
    if len(custom_data) > 13:
        raise ValueError(
            f"Maximum 13 custom attributes allowed per event, got {len(custom_data)}."
        )

    # The API allows a maximum of 14 match keys per event
    if len(match_keys) > 14:
        raise ValueError(
            f"Maximum 14 match keys allowed per event, got {len(match_keys)}."
        )

    # Validate each custom attribute against the API's character constraints
    for attr in custom_data:
        # Attribute names must be alphanumeric + underscore (snake_case)
        if not VALID_ATTRIBUTE_PATTERN.match(attr["name"]):
            raise ValueError(
                f"Invalid custom attribute name '{attr['name']}'. "
                "Only letters, numbers, and underscores are allowed."
            )
        # Attribute values must also be alphanumeric + underscore
        # For monetary values, use cents as integers (e.g., "4999" for $49.99)
        if not VALID_ATTRIBUTE_PATTERN.match(attr["value"]):
            raise ValueError(
                f"Invalid custom attribute value '{attr['value']}' for '{attr['name']}'. "
                "Only letters, numbers, and underscores are allowed."
            )
        # Validate dataType is one of the three supported types
        if attr["dataType"] not in ("STRING", "INTEGER", "TIMESTAMP"):
            raise ValueError(
                f"Invalid dataType '{attr['dataType']}' for '{attr['name']}'. "
                "Must be STRING, INTEGER, or TIMESTAMP."
            )

    # --- Build the event object ---

    # eventDescription contains metadata about the type of event
    event = {
        "eventDescription": {
            "name": event_name,
            "conversionType": conversion_type,
            "eventSource": event_source,
            "dataSetName": dataset_name,
            "eventIngestionMethod": "SERVER_TO_SERVER",  # Always server-side for CAPI
        },
        "countryCode": country_code,
        "eventTime": event_time,
        "matchKeys": match_keys,
    }

    # Add custom attributes (the core feature of this sample code)
    if custom_data:
        event["customData"] = custom_data

    # Add optional monetary value (e.g., purchase amount)
    if value is not None:
        event["value"] = value

    # Add currency code (required when value is monetary)
    if currency_code is not None:
        event["currencyCode"] = currency_code

    # Add deduplication ID to prevent duplicate event processing
    if event_id is not None:
        event["eventId"] = event_id

    # Add units sold (only applicable for purchase events)
    if units_sold is not None:
        event["unitsSold"] = units_sold

    # Wrap in the top-level "events" array (API accepts 1-500 events per request)
    return {"events": [event]}


def hash_email(email: str) -> str:
    """
    Normalize and hash an email address with SHA-256 for use as a match key.

    The Amazon Ads API requires PII match keys to be SHA-256 hashed before
    sending. This function applies the normalization rules from the API docs
    before hashing to ensure consistent matching.

    Normalization rules (per API docs):
    - Lowercase the entire email
    - Remove leading/trailing whitespace

    Args:
        email: The raw email address (e.g., "User@Example.COM")

    Returns:
        A 64-character hex string representing the SHA-256 hash of the
        normalized email (e.g., "a1b2c3d4...").

    Example:
        hashed = hash_email("customer@example.com")
        match_key = {"type": "EMAIL", "values": [hashed]}
    """
    normalized = email.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_phone(phone: str) -> str:
    """
    Normalize and hash a phone number with SHA-256 for use as a match key.

    Strips all characters except digits and the leading '+' sign,
    then hashes the result. This ensures consistent matching regardless
    of how the phone number was formatted (parentheses, dashes, spaces).

    Args:
        phone: The raw phone number (e.g., "+1 (555) 123-4567")

    Returns:
        A 64-character hex string representing the SHA-256 hash.

    Example:
        hashed = hash_phone("+15551234567")
        match_key = {"type": "PHONE", "values": [hashed]}
    """
    # Keep only digits and the leading + sign
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def send_conversion_event(payload: dict, region: str = "NA") -> dict:
    """
    Send a conversion event to the Amazon Ads Events API.

    This is the high-level convenience function that:
    1. Creates an authenticated API client for the specified region
    2. Sends the payload to the Events API endpoint
    3. Returns the parsed response

    Args:
        payload: The complete event payload built by build_event_payload().
        region: API region - "NA" (North America), "EU" (Europe),
                or "FE" (Far East). Defaults to "NA".

    Returns:
        The API response dictionary (207 multi-status) containing:
        - "success": List of accepted events with their index
        - "error": List of rejected events with error details

    Example:
        payload = build_event_payload(...)
        response = send_conversion_event(payload, region="NA")
        if response["success"]:
            print(f"Event accepted at index {response['success'][0]['index']}")
    """
    client = CAPIClient(region=region)
    logger.info("Sending event to %s endpoint...", region)
    logger.debug(
        "Payload (event count: %d)", len(payload.get("events", []))
    )
    response = client.send_conversion_events(payload)
    logger.info("Response received (success: %d, errors: %d)",
                len(response.get("success", [])),
                len(response.get("error", [])))
    return response
