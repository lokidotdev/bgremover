"""HTTP layer: the public removal endpoint, the admin API and the admin panel."""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from .access import Decision, DomainPolicy, DuplicateRule, InvalidRule, RuleNotFound
from .auth import COOKIE, SESSION_SECONDS, AdminAuth
from .config import Settings
from .db import Database
from .matting import InvalidImage, MaskModel, Remover, RemoveOptions
from .usage import UsageLog

log = logging.getLogger("bgremover")
UI_DIR = Path(__file__).parent / "admin_ui"
_EXPOSE = "X-Image-Width, X-Image-Height, X-Processing-Ms"


class Login(BaseModel):
    password: str

class NewRule(BaseModel):
    pattern: str = Field(max_length=300)
    note: str = Field(default="", max_length=200)

class RulePatch(BaseModel):
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=200)

class PolicySettings(BaseModel):
    allow_no_origin: bool


def create_app(settings: Settings, model: MaskModel | None = None) -> FastAPI:
    """Builds the app. Pass `model` to skip loading BiRefNet (tests, alternative models)."""
    db = Database(settings.database_path)
    policy = DomainPolicy(db)
    usage = UsageLog(db)

    password = settings.admin_password
    if not password:
        password = secrets.token_urlsafe(12)
        log.warning("BGR_ADMIN_PASSWORD not set; generated admin password for this run: %s", password)
    auth = AdminAuth(password, settings.secret_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mask_model = model
        if mask_model is None:
            from .matting.birefnet import BiRefNet

            mask_model = BiRefNet(
                settings.model_path,
                model_url=settings.model_url,
                fp16=settings.fp16,
                use_tensorrt=settings.use_tensorrt,
            )
            app.state.device = mask_model.device
        app.state.remover = Remover(mask_model, max_pixels=settings.max_pixels, workers=settings.workers)
        yield
        app.state.remover.close()
        db.close()

    app = FastAPI(title="BG Remover", lifespan=lifespan, docs_url="/docs", redoc_url=None)
    app.state.device = "custom"

    # ---------------------------------------------------------------- public API

    def cors_headers(decision: Decision) -> dict[str, str]:
        if not (decision.allowed and decision.origin):
            return {"Vary": "Origin"}
        return {
            "Access-Control-Allow-Origin": decision.origin,
            "Access-Control-Expose-Headers": _EXPOSE,
            "Vary": "Origin",
        }

    @app.options("/v1/remove", include_in_schema=False)
    async def preflight(request: Request) -> Response:
        decision = policy.check(request.headers.get("origin"))
        if not decision.allowed:
            return Response(status_code=403, headers={"Vary": "Origin"})
        return Response(
            status_code=204,
            headers={
                **cors_headers(decision),
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "*"),
                "Access-Control-Max-Age": "600",
            },
        )

    @app.post(
        "/v1/remove",
        summary="Remove the background from an image",
        description=(
            "Send the image as multipart field `image` or as the raw request body. "
            "Options (query string or form fields): `format` = png | webp | jpg | mask, "
            "`background` = hex colour, `crop` = true|false, `refine_edges` = true|false. "
            "Only callers from allowed domains are served."
        ),
        responses={200: {"content": {"image/png": {}}}, 403: {}, 413: {}, 422: {}},
    )
    async def remove(request: Request) -> Response:
        started = time.perf_counter()
        decision = policy.check(request.headers.get("origin"), request.headers.get("referer"))
        headers = cors_headers(decision)

        def done(status: int, response: Response) -> Response:
            usage.record(decision.host, status, int((time.perf_counter() - started) * 1000))
            response.headers.update(headers)
            return response

        if not decision.allowed:
            who = decision.host or "Requests without an Origin header"
            verb = "is" if decision.host else "are"
            return done(403, _error(403, f"{who} {verb} not allowed to use this API"))

        try:
            data, options = await _read_request(request, settings.max_upload_bytes)
            cutout = await request.app.state.remover.remove(data, options)
        except HTTPException as exc:
            return done(exc.status_code, _error(exc.status_code, exc.detail))
        except InvalidImage as exc:
            return done(422, _error(422, str(exc)))
        except Exception:
            log.exception("removal failed")
            return done(500, _error(500, "internal error"))

        ms = int((time.perf_counter() - started) * 1000)
        return done(
            200,
            Response(
                cutout.content,
                media_type=cutout.media_type,
                headers={
                    "X-Image-Width": str(cutout.width),
                    "X-Image-Height": str(cutout.height),
                    "X-Processing-Ms": str(ms),
                },
            ),
        )

    @app.get("/health", include_in_schema=False)
    async def health(request: Request) -> dict:
        return {"ok": hasattr(request.app.state, "remover"), "device": request.app.state.device}

    # ---------------------------------------------------------------- admin API

    def require_admin(request: Request) -> None:
        if not auth.verify(request.cookies.get(COOKIE)):
            raise HTTPException(401, "not signed in")

    admin = Depends(require_admin)

    @app.post("/admin/api/login", include_in_schema=False)
    def login(body: Login, request: Request) -> Response:
        token = auth.login(body.password)
        if not token:
            raise HTTPException(401, "wrong password")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
            secure=request.url.scheme == "https", path="/admin",
        )
        return response

    @app.post("/admin/api/logout", include_in_schema=False)
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/admin")
        return response

    @app.get("/admin/api/overview", dependencies=[admin], include_in_schema=False)
    def overview(request: Request) -> dict:
        return {
            "device": request.app.state.device,
            "rules": [r.__dict__ for r in policy.rules()],
            "settings": {"allow_no_origin": policy.allow_no_origin},
            "usage": usage.summary(7),
        }

    @app.get("/admin/api/domains", dependencies=[admin], include_in_schema=False)
    def list_domains() -> list[dict]:
        return [r.__dict__ for r in policy.rules()]

    @app.post("/admin/api/domains", dependencies=[admin], status_code=201, include_in_schema=False)
    def add_domain(body: NewRule) -> dict:
        try:
            return policy.add_rule(body.pattern, body.note).__dict__
        except InvalidRule as exc:
            raise HTTPException(422, str(exc))
        except DuplicateRule as exc:
            raise HTTPException(409, str(exc))

    @app.patch("/admin/api/domains/{rule_id}", dependencies=[admin], include_in_schema=False)
    def update_domain(rule_id: int, body: RulePatch) -> dict:
        try:
            return policy.update_rule(rule_id, enabled=body.enabled, note=body.note).__dict__
        except RuleNotFound:
            raise HTTPException(404, "no such domain")

    @app.delete("/admin/api/domains/{rule_id}", dependencies=[admin], status_code=204, include_in_schema=False)
    def delete_domain(rule_id: int) -> Response:
        try:
            policy.delete_rule(rule_id)
        except RuleNotFound:
            raise HTTPException(404, "no such domain")
        return Response(status_code=204)

    @app.put("/admin/api/settings", dependencies=[admin], include_in_schema=False)
    def put_settings(body: PolicySettings) -> dict:
        policy.set_allow_no_origin(body.allow_no_origin)
        return {"allow_no_origin": policy.allow_no_origin}

    @app.get("/admin/api/usage", dependencies=[admin], include_in_schema=False)
    def get_usage(days: int = 7) -> dict:
        return usage.summary(days)

    @app.post("/admin/api/try", dependencies=[admin], include_in_schema=False)
    async def try_it(request: Request) -> Response:
        """Playground: same pipeline as /v1/remove, but bypasses the domain policy and isn't logged."""
        started = time.perf_counter()
        try:
            data, options = await _read_request(request, settings.max_upload_bytes)
            cutout = await request.app.state.remover.remove(data, options)
        except InvalidImage as exc:
            raise HTTPException(422, str(exc))
        ms = int((time.perf_counter() - started) * 1000)
        return Response(cutout.content, media_type=cutout.media_type, headers={"X-Processing-Ms": str(ms)})

    # ---------------------------------------------------------------- admin UI

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/admin")

    @app.get("/admin", include_in_schema=False)
    def admin_page() -> Response:
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/admin/static", StaticFiles(directory=UI_DIR), name="admin-static")
    return app


async def _read_request(request: Request, max_bytes: int) -> tuple[bytes, RemoveOptions]:
    """Extracts image bytes and options from a multipart form or a raw body."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + 64 * 1024:
        raise HTTPException(413, f"image larger than {max_bytes // (1024 * 1024)} MB")

    fields: dict[str, str] = dict(request.query_params)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form(max_files=1, max_fields=10, max_part_size=max_bytes)
        upload = form.get("image")
        if not isinstance(upload, UploadFile):
            raise HTTPException(422, "multipart field 'image' is required")
        data = await upload.read()
        fields.update({k: v for k, v in form.items() if isinstance(v, str)})
    else:
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > max_bytes:
                raise HTTPException(413, f"image larger than {max_bytes // (1024 * 1024)} MB")
        data = bytes(data)

    if len(data) > max_bytes:
        raise HTTPException(413, f"image larger than {max_bytes // (1024 * 1024)} MB")

    def flag(name: str, default: bool) -> bool:
        value = fields.get(name)
        return default if value is None else value.lower() in ("1", "true", "yes", "on")

    background = fields.get("background") or fields.get("bg_color") or None
    options = RemoveOptions(
        format=fields.get("format", "png").lower(),  # type: ignore[arg-type]
        background=background,
        crop=flag("crop", False),
        refine_edges=flag("refine_edges", True),
    )
    return data, options


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
