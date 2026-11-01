import hashlib
import os
import time

import httpx
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PLANE_BASE_URL = "https://api.plane.so"

# HTTP status codes
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429

# How long (seconds) a successful validation result is cached before we
# re-check with the Plane API. Keeps burst tool calls from hitting the
# rate-limited /users/me/ endpoint on every request.
DEFAULT_CACHE_TTL_SECONDS = 3600


class _RateLimitedError(Exception):
    """Raised when the Plane API returns 429 Too Many Requests during key validation."""


class PlaneHeaderAuthProvider(TokenVerifier):
    def __init__(
        self,
        required_scopes: list[str] | None = None,
        timeout_seconds: int = 10,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        super().__init__(required_scopes=required_scopes)
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        # Maps sha256(token) → expiry_timestamp. Stores only the hash so the
        # raw token is never retained in memory beyond the call stack.
        self._valid_token_cache: dict[str, float] = {}

    def _token_cache_key(self, token: str) -> str:
        """Return a sha256 hex digest of the token — never store the raw value."""
        return hashlib.sha256(token.encode()).hexdigest()

    def _is_cached_valid(self, token: str) -> bool:
        """Return True if this token was recently validated and the cache entry is still fresh."""
        key = self._token_cache_key(token)
        expiry = self._valid_token_cache.get(key)
        return expiry is not None and time.time() < expiry

    def _cache_token(self, token: str) -> None:
        """Record a successful validation so subsequent requests skip the Plane API call."""
        key = self._token_cache_key(token)
        self._valid_token_cache[key] = time.time() + self.cache_ttl_seconds

    async def _validate_api_key(self, token: str) -> bool:
        """Validate the API key by calling the Plane API.

        Returns:
            True if the key is valid.

        Raises:
            _RateLimitedError: when the Plane API returns 429. The key may be
                valid; callers should surface this as a transient error rather
                than an authentication failure.
        """
        base_url = (os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", DEFAULT_PLANE_BASE_URL)).rstrip(
            "/"
        )
        user_url = f"{base_url}/api/v1/users/me/"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    user_url,
                    headers={
                        "x-api-key": token,
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code == HTTP_TOO_MANY_REQUESTS:
                    logger.warning("API key validation rate-limited by Plane API (429)")
                    raise _RateLimitedError("Plane API returned 429 during key validation")
                if response.status_code != HTTP_OK:
                    logger.warning("API key validation failed: %s", response.status_code)
                    return False
                return True
        except httpx.RequestError as e:
            logger.warning("API key validation request failed: %s", e)
            return False

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            from fastmcp.server.dependencies import get_http_headers

            headers = get_http_headers()

            if token:
                workspace_slug = headers.get("x-workspace-slug")
                if not workspace_slug:
                    logger.warning(
                        "x-api-key header found but x-workspace-slug is missing; "
                        "ensure the client sends the 'x-workspace-slug' header alongside 'x-api-key'"
                    )
                    return None

                if self._is_cached_valid(token):
                    logger.debug("API key validation served from cache")
                else:
                    try:
                        valid = await self._validate_api_key(token)
                    except _RateLimitedError:
                        logger.warning(
                            "API key validation skipped due to Plane API rate-limiting (429); "
                            "the key may be valid — please retry after backing off"
                        )
                        return None

                    if not valid:
                        logger.warning("API key validation against Plane API failed")
                        return None

                    self._cache_token(token)
                    logger.info("API key validated successfully via Plane API")

                expires_at = int(time.time() + 3600)
                return AccessToken(
                    token=token,
                    client_id="api_key_header_user",
                    scopes=["read", "write"],
                    expires_at=expires_at,
                    claims={
                        "auth_method": "api_key_header",
                        "workspace_slug": workspace_slug,
                    },
                )
        except RuntimeError:
            # No active HTTP request available (e.g., stdio transport)
            logger.debug("No active HTTP request available for header check")
