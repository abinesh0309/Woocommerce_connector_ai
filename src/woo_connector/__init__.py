"""Read-only WooCommerce connector for AI agents (MCP server)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("woo-connector")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]
