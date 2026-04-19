"""Authentication and authorization helpers for Musubi Core."""

from musubi.auth.middleware import AuthHTTPError, AuthRequirement, authenticate_request
from musubi.auth.scopes import ScopeError, ScopeGrant, resolve_namespace_scope
from musubi.auth.tokens import AuthContext, ExpiredTokenError, InvalidTokenError, validate_token

__all__ = [
    "AuthContext",
    "AuthHTTPError",
    "AuthRequirement",
    "ExpiredTokenError",
    "InvalidTokenError",
    "ScopeError",
    "ScopeGrant",
    "authenticate_request",
    "resolve_namespace_scope",
    "validate_token",
]
