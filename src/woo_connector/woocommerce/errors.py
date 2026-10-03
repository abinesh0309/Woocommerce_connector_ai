"""Typed errors whose messages tell the agent (and the merchant) what to do next."""

from woo_connector.auth.credentials import NotConnected

__all__ = ["AuthError", "BadRequest", "NotConnected", "NotFound", "NotJson", "RateLimited", "RedirectRefused", "UpstreamError", "WooError"]


class WooError(Exception):
    """Base class. ``str(error)`` is safe to show to an agent: no stack traces, no secrets."""


class NotFound(WooError):
    """HTTP 404. ``code`` is WooCommerce's error code (``rest_no_route`` = endpoint missing)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code

    @property
    def route_missing(self) -> bool:
        return self.code == "rest_no_route"


class AuthError(WooError):
    """401/403, or a signature problem."""


class RateLimited(WooError):
    """The store (or its host/CDN/WAF) is throttling us and we gave up waiting."""


class UpstreamError(WooError):
    """5xx or network failure after retries."""


class BadRequest(WooError):
    """4xx other than auth/404: usually a bad parameter."""


class RedirectRefused(WooError):
    """3xx. We never follow redirects, so credentials cannot reach another origin."""


class NotJson(WooError):
    """The response was not JSON (WAF challenge page, maintenance page, wrong URL)."""
