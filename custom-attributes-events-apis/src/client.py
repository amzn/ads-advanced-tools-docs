"""Amazon Ads Events API (CAPI v1) HTTP client.

This module provides a robust HTTP client for sending conversion events
to the Amazon Ads Events API. It handles:
    - Authentication via OAuth 2.0 (delegated to AmazonAdsAuth)
    - Regional endpoint selection (NA, EU, FE)
    - Automatic retries with exponential backoff for rate limits (429)
      and server errors (5xx)
    - Request timeouts to prevent hanging connections

Endpoint: POST /adsApi/v1/create/events
Docs: https://advertising.amazon.com/API/docs/en-us/amazon-ads/1-0/betas#tag/Events

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import time
import json
import logging
import requests
import os

from dotenv import load_dotenv
from src.auth import AmazonAdsAuth

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Regional API endpoints — choose based on where your advertiser account is located
# NA = North America, EU = Europe, FE = Far East (Japan, Australia)
API_ENDPOINTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}

# Retry configuration for transient failures
MAX_RETRIES = 3                  # Total attempts before giving up
INITIAL_BACKOFF_SECONDS = 1      # First retry waits 1s, then 2s, then 4s


class CAPIClient:
    """HTTP client for the Amazon Ads Events API (Ads API v1).

    Creates events via: POST /adsApi/v1/create/events

    Required headers:
        - Authorization: Bearer <access_token>
        - Amazon-Ads-AccountId: The advertiser account ID
        - Amazon-Ads-ClientId: The LWA client ID
        - Content-Type: application/json

    Usage:
        client = CAPIClient(region="NA")
        response = client.send_conversion_events(payload)
    """

    def __init__(self, region: str = "NA"):
        """Initialize the client with authentication and region settings.

        Args:
            region: The API region to target. One of "NA", "EU", or "FE".
                   Defaults to "NA" (North America).
        """
        # Initialize OAuth authentication (will validate credentials)
        self.auth = AmazonAdsAuth()

        # Load advertiser ID from environment
        self.advertiser_id = os.getenv("ADVERTISER_ID")
        self.region = region

        # Select the appropriate regional endpoint
        self.base_url = API_ENDPOINTS.get(region, API_ENDPOINTS["NA"])

        if not self.advertiser_id:
            raise ValueError(
                "ADVERTISER_ID is required. Set it in your .env file."
            )

    def send_conversion_events(self, payload: dict) -> dict:
        """
        Send conversion events to the Amazon Ads Events API.

        This method handles the full request lifecycle:
        1. Constructs the request URL and headers
        2. Sends the POST request with the event payload
        3. Retries on rate limits (429) and server errors (5xx)
        4. Returns the parsed response on success (207)

        POST /adsApi/v1/create/events

        Args:
            payload: The complete event payload. Must contain an "events" array
                     with 1-500 EventCreate objects.

        Returns:
            The API response as a dictionary. The 207 multi-status response
            contains two arrays:
                - "success": Events that were accepted
                - "error": Events that failed with error details

        Raises:
            requests.HTTPError: If the request fails after all retries.
            requests.exceptions.Timeout: If all attempts time out.
        """
        url = f"{self.base_url}/adsApi/v1/create/events"
        headers = self._build_headers()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Send the POST request with JSON payload
                # verify=True ensures TLS certificate validation
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=30,
                    verify=True,
                )

                # Handle rate limiting — wait and retry with exponential backoff
                if response.status_code == 429:
                    wait_time = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Rate limited (429). Retrying in %ds (attempt %d/%d)...",
                        wait_time, attempt, MAX_RETRIES,
                    )
                    time.sleep(wait_time)
                    continue

                # Handle server errors — these are usually transient
                if response.status_code >= 500:
                    wait_time = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Server error (%d). Retrying in %ds (attempt %d/%d)...",
                        response.status_code, wait_time, attempt, MAX_RETRIES,
                    )
                    time.sleep(wait_time)
                    continue

                # 207 Multi-Status is the expected success response
                # It contains per-event success/error details
                if response.status_code == 207:
                    return response.json()

                # For any other status code, raise an exception
                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout:
                logger.warning(
                    "Request timed out (attempt %d/%d)...",
                    attempt, MAX_RETRIES,
                )
                # Only raise on the final attempt; earlier attempts loop back
                if attempt == MAX_RETRIES:
                    raise

        # If we exhausted all retries without returning, raise the last error
        response.raise_for_status()
        return response.json()

    def _build_headers(self) -> dict:
        """Construct request headers per API spec.

        The Amazon Ads Events API requires four headers:
        - Authorization: OAuth 2.0 Bearer token
        - Amazon-Ads-AccountId: Identifies which advertiser account to use
        - Amazon-Ads-ClientId: Identifies the calling application
        - Content-Type: Must be application/json
        """
        return {
            "Authorization": f"Bearer {self.auth.access_token}",
            "Amazon-Ads-AccountId": self.advertiser_id,
            "Amazon-Ads-ClientId": self.auth.client_id,
            "Content-Type": "application/json",
        }
