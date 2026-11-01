"""Unit tests for PlaneHeaderAuthProvider.

Covers:
- 429 rate-limit from Plane API is treated as a transient error (not a
  permanent auth failure), so callers get a clear message and can retry.
- Missing x-workspace-slug header is rejected with an actionable log message.
- 200 OK from Plane API yields a valid AccessToken with expected claims.
- Non-200/non-429 responses are treated as an invalid key.
- Network errors are handled gracefully.
- Successful validations are cached so burst tool calls do not hammer the
  rate-limited /users/me/ endpoint on every request.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from plane_mcp.auth.plane_header_auth_provider import (
    DEFAULT_CACHE_TTL_SECONDS,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    _RateLimitedError,
    PlaneHeaderAuthProvider,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    return response


def _make_async_client_ctx(status_code: int) -> MagicMock:
    """Return an async context manager whose .get() returns *response*."""
    response = _make_response(status_code)
    async_client = AsyncMock()
    async_client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=async_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _validate_api_key
# ---------------------------------------------------------------------------


class TestValidateApiKey:
    """Direct tests for the private _validate_api_key helper."""

    @pytest.fixture
    def provider(self):
        return PlaneHeaderAuthProvider()

    async def test_returns_true_on_200(self, provider):
        ctx = _make_async_client_ctx(HTTP_OK)
        with patch("httpx.AsyncClient", return_value=ctx):
            result = await provider._validate_api_key("valid-token")
        assert result is True

    async def test_raises_rate_limited_error_on_429(self, provider):
        ctx = _make_async_client_ctx(HTTP_TOO_MANY_REQUESTS)
        with patch("httpx.AsyncClient", return_value=ctx):
            with pytest.raises(_RateLimitedError):
                await provider._validate_api_key("any-token")

    @pytest.mark.parametrize("status_code", [401, 403, 500])
    async def test_returns_false_on_non_200_non_429(self, provider, status_code):
        ctx = _make_async_client_ctx(status_code)
        with patch("httpx.AsyncClient", return_value=ctx):
            result = await provider._validate_api_key("bad-token")
        assert result is False

    async def test_returns_false_on_network_error(self, provider):
        async_client = AsyncMock()
        async_client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=async_client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=ctx):
            result = await provider._validate_api_key("any-token")
        assert result is False


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


def _make_headers_ctx(headers: dict):
    """Patch get_http_headers to return *headers*."""
    return patch(
        "plane_mcp.auth.plane_header_auth_provider.PlaneHeaderAuthProvider.verify_token.__globals__"
        # We patch via the module path instead:
    )


class TestVerifyToken:
    """Integration tests for verify_token using mocked dependencies."""

    @pytest.fixture
    def provider(self):
        return PlaneHeaderAuthProvider()

    def _patch_headers(self, headers: dict):
        return patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value=headers,
        )

    def _patch_validate(self, result):
        """Patch _validate_api_key to return *result* or raise if it's an exception."""
        if isinstance(result, Exception):
            return patch.object(
                PlaneHeaderAuthProvider,
                "_validate_api_key",
                new=AsyncMock(side_effect=result),
            )
        return patch.object(
            PlaneHeaderAuthProvider,
            "_validate_api_key",
            new=AsyncMock(return_value=result),
        )

    async def test_returns_access_token_on_valid_key(self, provider):
        with self._patch_headers({"x-workspace-slug": "my-workspace"}):
            with self._patch_validate(True):
                token = await provider.verify_token("good-token")

        assert token is not None
        assert token.claims["workspace_slug"] == "my-workspace"
        assert token.claims["auth_method"] == "api_key_header"
        assert "read" in token.scopes
        assert "write" in token.scopes

    async def test_returns_none_when_workspace_slug_missing(self, provider):
        # No x-workspace-slug header at all
        with self._patch_headers({}):
            with self._patch_validate(True):
                token = await provider.verify_token("good-token")

        assert token is None

    async def test_returns_none_when_workspace_slug_empty_string(self, provider):
        with self._patch_headers({"x-workspace-slug": ""}):
            with self._patch_validate(True):
                token = await provider.verify_token("good-token")

        assert token is None

    async def test_returns_none_when_api_key_invalid(self, provider):
        with self._patch_headers({"x-workspace-slug": "my-workspace"}):
            with self._patch_validate(False):
                token = await provider.verify_token("bad-token")

        assert token is None

    async def test_returns_none_on_rate_limit_does_not_raise(self, provider):
        """429 from Plane must NOT propagate as an unhandled exception.

        The caller (FastMCP middleware) expects None for auth failure; any
        exception would be an internal server error. Verify we return None and
        do not raise.
        """
        with self._patch_headers({"x-workspace-slug": "my-workspace"}):
            with self._patch_validate(_RateLimitedError("rate limited")):
                token = await provider.verify_token("rate-limited-token")

        assert token is None

    async def test_returns_none_when_token_is_empty(self, provider):
        with self._patch_headers({"x-workspace-slug": "my-workspace"}):
            with self._patch_validate(True):
                token = await provider.verify_token("")

        assert token is None

    async def test_returns_none_on_runtime_error_no_http_context(self, provider):
        """Handles stdio transport (no active HTTP request) gracefully."""
        with patch(
            "fastmcp.server.dependencies.get_http_headers",
            side_effect=RuntimeError("no active request"),
        ):
            token = await provider.verify_token("any-token")

        assert token is None



# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestTokenCache:
    """Verify the in-process cache prevents repeated Plane API calls for
    the same token within the TTL window — the root cause of the 429 spiral
    when many MCP tool calls are made in quick succession."""

    @pytest.fixture
    def provider(self):
        # Short TTL so expiry tests are fast
        return PlaneHeaderAuthProvider(cache_ttl_seconds=60)

    def _patch_headers(self, headers: dict):
        return patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value=headers,
        )

    async def test_second_call_does_not_hit_plane_api(self, provider):
        """After a successful validation, burst calls must use the cache."""
        validate_mock = AsyncMock(return_value=True)
        with self._patch_headers({"x-workspace-slug": "ws"}):
            with patch.object(PlaneHeaderAuthProvider, "_validate_api_key", validate_mock):
                await provider.verify_token("my-token")
                await provider.verify_token("my-token")
                await provider.verify_token("my-token")

        # Plane API called exactly once despite three verify_token calls
        assert validate_mock.call_count == 1

    async def test_different_tokens_each_validated(self, provider):
        """Two different tokens must each be validated independently."""
        validate_mock = AsyncMock(return_value=True)
        with self._patch_headers({"x-workspace-slug": "ws"}):
            with patch.object(PlaneHeaderAuthProvider, "_validate_api_key", validate_mock):
                await provider.verify_token("token-a")
                await provider.verify_token("token-b")

        assert validate_mock.call_count == 2

    async def test_expired_cache_entry_revalidates(self, provider):
        """Once the TTL expires, the next call must re-validate with Plane."""
        validate_mock = AsyncMock(return_value=True)
        with self._patch_headers({"x-workspace-slug": "ws"}):
            with patch.object(PlaneHeaderAuthProvider, "_validate_api_key", validate_mock):
                await provider.verify_token("my-token")

                # Manually expire the cache entry
                import time
                for key in provider._valid_token_cache:
                    provider._valid_token_cache[key] = time.time() - 1

                await provider.verify_token("my-token")

        assert validate_mock.call_count == 2

    async def test_failed_validation_not_cached(self, provider):
        """An invalid token must not be cached; each call must re-check."""
        validate_mock = AsyncMock(return_value=False)
        with self._patch_headers({"x-workspace-slug": "ws"}):
            with patch.object(PlaneHeaderAuthProvider, "_validate_api_key", validate_mock):
                await provider.verify_token("bad-token")
                await provider.verify_token("bad-token")

        assert validate_mock.call_count == 2

    async def test_raw_token_not_stored_in_cache(self, provider):
        """The cache must store only the sha256 hash, never the raw token string."""
        validate_mock = AsyncMock(return_value=True)
        raw_token = "super-secret-api-key"
        with self._patch_headers({"x-workspace-slug": "ws"}):
            with patch.object(PlaneHeaderAuthProvider, "_validate_api_key", validate_mock):
                await provider.verify_token(raw_token)

        for cache_key in provider._valid_token_cache:
            assert raw_token not in cache_key, "Raw token must not be stored in cache"
        assert len(provider._valid_token_cache) == 1

    def test_default_cache_ttl_is_one_hour(self):
        assert DEFAULT_CACHE_TTL_SECONDS == 3600

    def test_cache_ttl_configurable(self):
        provider = PlaneHeaderAuthProvider(cache_ttl_seconds=120)
        assert provider.cache_ttl_seconds == 120
