from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppException(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "APP_ERROR"):
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class NotFoundError(AppException):
    def __init__(self, resource: str = "Resource"):
        super().__init__(
            message=f"{resource} not found",
            status_code=404,
            code="NOT_FOUND",
        )


class UnauthorizedError(AppException):
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message=message, status_code=401, code="UNAUTHORIZED")


class ForbiddenError(AppException):
    def __init__(self, message: str = "Access denied"):
        super().__init__(message=message, status_code=403, code="FORBIDDEN")


class ValidationError(AppException):
    def __init__(self, message: str = "Validation failed"):
        super().__init__(message=message, status_code=422, code="VALIDATION_ERROR")


class ConflictError(AppException):
    def __init__(self, message: str = "Conflict"):
        super().__init__(message=message, status_code=409, code="CONFLICT")


class PaymentError(AppException):
    def __init__(self, message: str = "Payment failed"):
        super().__init__(message=message, status_code=402, code="PAYMENT_ERROR")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(UnauthorizedError)
    async def unauthorized_handler(request: Request, exc: UnauthorizedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(ForbiddenError)
    async def forbidden_handler(request: Request, exc: ForbiddenError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        import logging, os, re
        from fastapi import HTTPException as _HTTPException
        from fastapi.exceptions import RequestValidationError as _RVE
        # Let FastAPI's own handlers deal with HTTPException / RequestValidationError
        if isinstance(exc, (_HTTPException, _RVE)):
            raise exc
        logging.getLogger("app.exceptions").exception("Unhandled server error: %s", exc)

        # This handler runs inside ServerErrorMiddleware, which is ABOVE CORSMiddleware.
        # Without manually adding CORS headers here, cross-origin browsers see
        # "Failed to fetch" (TypeError) instead of a meaningful error message.
        _CORS_RE = (
            r"^https?://localhost(:\d+)?$"
            r"|^https://e-novar\.com$"
            r"|^https://[a-z0-9-]+\.e-novar\.com$"
            r"|^https://[a-z0-9-]+\.nacimmessi1010\.workers\.dev$"
            r"|^https://[a-z0-9-]+\.workers\.dev$"
            r"|^https://[a-z0-9-]+\.pages\.dev$"
        )
        headers: dict = {}
        origin = request.headers.get("origin", "")
        if origin and re.match(_CORS_RE, origin):
            headers["access-control-allow-origin"] = origin
            headers["access-control-allow-credentials"] = "true"
            headers["vary"] = "Origin"

        # In development, surface the real error so we can diagnose it.
        detail: str
        if os.getenv("APP_ENV", "production") == "development":
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = "Une erreur interne est survenue. Veuillez réessayer."

        return JSONResponse(
            status_code=500,
            content={"detail": detail},
            headers=headers,
        )
