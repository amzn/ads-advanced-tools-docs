"""OAuth 2.0 authentication for Amazon Ads API.

This module handles the Login with Amazon (LWA) OAuth 2.0 token lifecycle.
It exchanges a long-lived refresh token for short-lived access tokens,
automatically refreshing when tokens expire.

Flow:
    1. On first API call, the refresh token is exchanged for an access token
    2. Access tokens are cached in memory and reused until they expire
    3. Tokens are refreshed automatically 60 seconds before expiry to avoid
       failed requests due to token expiration during transit

Prerequisites:
    - CLIENT_ID: Your LWA application's client ID
    - CLIENT_SECRET: Your LWA application's client secret
    - REFRESH_TOKEN: A long-lived token obtained via the OAuth authorization flow

Author: Chintan Sanghavi
Date: July 6, 2026
"""

import time
import logging
import requests
import os

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Amazon's OAuth 2.0 token endpoint for Login with Amazon (LWA)
TOKEN_URL = "https://api.amazon.com/auth/o2/token"


class AmazonAdsAuth:
    """Manages OAuth 2.0 token lifecycle for Amazon Ads API.

    This class encapsulates the token refresh logic so that calling code
    can simply access `self.access_token` without worrying about expiry.

    Usage:
        auth = AmazonAdsAuth()
        headers = {"Authorization": f"Bearer {auth.access_token}"}
    """

    def __init__(self):
        # Load credentials from environment variables
        self.client_id = os.getenv("CLIENT_ID")
        self.client_secret = os.getenv("CLIENT_SECRET")
        self.refresh_token = os.getenv("REFRESH_TOKEN")

        # Internal state for token caching
        self._access_token = None
        self._token_expiry = 0  # Unix timestamp when the token expires

        # Validate that all required credentials are present
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            raise ValueError(
                "Missing required credentials. "
                "Ensure CLIENT_ID, CLIENT_SECRET, "
                "and REFRESH_TOKEN are set in your .env file."
            )

    @property
    def access_token(self) -> str:
        """Returns a valid access token, refreshing if expired.

        This property is the main interface for getting a usable token.
        It transparently handles refresh logic — callers don't need to
        worry about token expiry.
        """
        if self._is_token_expired():
            self._refresh_access_token()
        return self._access_token

    def _is_token_expired(self) -> bool:
        """Check if the current token is expired or about to expire.

        We refresh 60 seconds before actual expiry to provide a buffer
        and avoid race conditions where a token expires mid-request.
        """
        return time.time() >= (self._token_expiry - 60)

    def _refresh_access_token(self):
        """Exchange the refresh token for a new access token.

        Makes a POST request to Amazon's OAuth token endpoint with
        the refresh_token grant type. The response includes a new
        access token and its TTL (typically 3600 seconds / 1 hour).

        Raises:
            requests.HTTPError: If the token refresh request fails
                (e.g., invalid credentials, revoked refresh token).
        """
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        # POST to LWA token endpoint with TLS verification enabled
        response = requests.post(
            TOKEN_URL, data=payload, timeout=30, verify=True
        )
        response.raise_for_status()

        # Extract the new access token and compute expiry time
        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expiry = time.time() + token_data.get("expires_in", 3600)

        logger.info("Access token refreshed successfully.")
