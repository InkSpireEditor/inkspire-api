# -*- coding: utf-8 -*-
"""The ASGI application.

Run it with: poetry run uvicorn inkspire_api.main:app --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, files, llm
from .deps import CurrentUser, current_user
from .settings import Settings, get_settings
from .fs import Conflict, NotFound, StorageError
from .throttle import LoginThrottle, RateLimiter

#: How a failure to read or change the stories on disk is answered.
STORAGE_STATUS = {
    NotFound: status.HTTP_404_NOT_FOUND,
    Conflict: status.HTTP_409_CONFLICT,
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Builds the application. Called once at import, and once per test."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        # Checked on the way up, so a missing secret stops the server starting rather
        # than answering every login with a 500 once it is already serving. Checked
        # here rather than in this function's body because importing this module must
        # not require a configured installation.
        settings.jwt_secret_or_raise()
        yield

    application = FastAPI(title="InkSpire API", lifespan=lifespan)
    application.state.login_throttle = LoginThrottle(
        settings.login_max_attempts, settings.login_interval
    )
    application.state.llm_limiter = RateLimiter(settings.llm_limit, settings.llm_interval)
    application.state.llm_service = None
    application.state.scanner = None
    application.state.notes_scanner = None

    # The frontend is served from a different port, so every request to this API is
    # cross-origin. allow_credentials is what lets the browser attach the auth
    # cookies, and it is incompatible with a wildcard origin — hence the regex.
    application.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_methods=["GET", "OPTIONS", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
        max_age=3600,
    )

    @application.exception_handler(HTTPException)
    def _message_body(_request: Request, exc: HTTPException) -> JSONResponse:
        """Errors are `{"code", "message"}` throughout, which is what clients read."""
        return JSONResponse(
            {"code": exc.status_code, "message": exc.detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @application.exception_handler(RequestValidationError)
    def _malformed_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """A malformed request body answers 400 in the same shape as every other error.

        FastAPI's default is a 422 whose `detail` is a list of objects; clients here
        display `message`, so the first problem is flattened into one string.
        """
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", ())[1:])
        detail = first.get("msg", "Invalid request body")
        return JSONResponse(
            {"code": 400, "message": f"{field}: {detail}" if field else detail},
            status_code=400,
        )

    @application.exception_handler(StorageError)
    def _storage(_request: Request, exc: StorageError) -> JSONResponse:
        """The routes let storage errors out, and each becomes the status that fits it.

        Anything not listed is a 500: the repository is not in the state the API needs,
        and that is not something the client did.
        """
        code = STORAGE_STATUS.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
        return JSONResponse({"code": code, "message": str(exc)}, status_code=code)

    application.include_router(auth.router)

    # Everything under /api requires a valid token. Declaring it on the router rather
    # than on each route means a new route is private unless it is put elsewhere.
    api = APIRouter(prefix="/api", dependencies=[Depends(current_user)])

    @api.get("/me")
    def me(user: CurrentUser) -> dict:
        """The account the request is authenticated as."""
        return {"email": user.email, "roles": user.all_roles()}

    api.include_router(files.stories_router)
    api.include_router(files.notes_router)
    api.include_router(llm.router)
    application.include_router(api)
    return application


app = create_app()
