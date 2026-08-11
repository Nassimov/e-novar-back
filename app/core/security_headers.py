from __future__ import annotations

"""
Secure response headers — Competitive Arena Phase 16 Security Audit finding.
Purely additive (response headers only, no request/behavior changes) — safe
to add without touching any business logic. HSTS is conditional on HTTPS
being terminated upstream (Railway does this) — sending it over plain HTTP
would be a no-op/harmless per spec, but we still gate it on request.url.scheme
to avoid a misleading header in local dev.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response
