"""Connector-specific bindings for the shared Google OAuth implementation."""

from . import ga_api as api
from .auth import GoogleAnalyticsAuthError as AuthError

__all__ = ["AuthError", "api"]
