"""FastAPI application: inbound endpoints → Dispatcher → Pushover."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile

from . import __version__, status
from .config import Config, Source, config_path, load_config
from .dispatcher import Dispatcher
from .models import Attachment, Result
from .pushover import PushoverClient
from .reload import ConfigWatcher
from .sources import apprise, generic, github, pushover, slack

log = logging.getLogger("pushmore")


STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon-16x16.png": "image/png",
    "favicon-32x32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "android-chrome-192x192.png": "image/png",
    "android-chrome-512x512.png": "image/png",
    "site.webmanifest": "application/manifest+json",
}


def _static_route(name: str, media_type: str):
    path = STATIC_DIR / name

    async def serve() -> FileResponse:
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    return serve


class BodyTooLarge(Exception):
    pass


class BadRequest(Exception):
    pass


class Unauthorized(Exception):
    pass


def _eq(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-pushmore-token") or request.query_params.get("token")


async def read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # let request.form() reuse the buffered body
    return body


async def parse_fields(
    request: Request, body: bytes, max_attachment: int
) -> tuple[dict[str, Any], list[Attachment]]:
    """Parse JSON, urlencoded or multipart into a flat dict plus uploaded files."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    data: dict[str, Any] = dict(request.query_params)
    files: list[Attachment] = []
    if ctype == "multipart/form-data":
        form = await request.form(max_files=10, max_fields=100)
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                content = await value.read(max_attachment + 1)
                if len(content) > max_attachment:
                    raise BadRequest("attachment is too large")
                files.append(Attachment(value.filename or key, content, value.content_type or ""))
            else:
                data[key] = value
    elif ctype == "application/x-www-form-urlencoded":
        data.update(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    elif body.strip():
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise BadRequest("body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise BadRequest("JSON body must be an object")
        data.update(parsed)
    return data, files


def _source_changes(old: Config, new: Config) -> str:
    added = sorted(set(new.sources) - set(old.sources))
    removed = sorted(set(old.sources) - set(new.sources))
    changed = sorted(
        sid for sid in set(old.sources) & set(new.sources) if old.sources[sid] != new.sources[sid]
    )
    parts = [
        f"{label}={','.join(ids)}"
        for label, ids in (("added", added), ("removed", removed), ("changed", changed))
        if ids
    ]
    return " ".join(parts) or "no source changes"


def create_app(
    config: Config | None = None,
    http_client: httpx.AsyncClient | None = None,
    config_file: str | os.PathLike[str] | None = None,
    reload_interval: float | None = None,
) -> FastAPI:
    """Build the app.

    Pass ``config`` for a fixed config (no reload), or let it be loaded from
    ``config_file`` / ``$PUSHMORE_CONFIG``, in which case the file is polled for
    changes every ``reload_interval`` seconds (``$PUSHMORE_RELOAD_INTERVAL``,
    default 10, 0 disables).
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        path = Path(config_file) if config_file else config_path()
        cfg = config or load_config(path)
        client = http_client or httpx.AsyncClient(headers={"User-Agent": f"PushMore/{__version__}"})

        def make_pushover(c: Config) -> PushoverClient:
            return PushoverClient(c.pushover_api_url, c.pushover_user, c.pushover_token, client)

        app.state.config = cfg
        app.state.config_loaded_at = datetime.now(UTC)
        app.state.config_file = None if config else str(path)
        app.state.dispatcher = Dispatcher(cfg, make_pushover(cfg))
        log.info(
            "PushMore %s started with %d source(s)%s",
            __version__,
            len(cfg.sources),
            " [dry-run]" if cfg.dry_run else "",
        )

        def apply(new: Config) -> None:
            old = app.state.config
            app.state.dispatcher.reconfigure(new, make_pushover(new))
            app.state.config = new
            app.state.config_loaded_at = datetime.now(UTC)
            log.info(
                "Config reloaded: %d source(s), %s%s",
                len(new.sources),
                _source_changes(old, new),
                " [dry-run]" if new.dry_run else "",
            )

        interval = reload_interval
        if interval is None:
            interval = float(os.environ.get("PUSHMORE_RELOAD_INTERVAL", "10"))
        task = None
        app.state.reload_interval = interval if config is None else 0
        if config is None and interval > 0:
            watcher = ConfigWatcher(path, apply, interval)
            watcher.prime()
            app.state.watcher = watcher
            task = asyncio.create_task(watcher.run())
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if http_client is None:
                await client.aclose()

    app = FastAPI(
        title="PushMore",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(BodyTooLarge)
    async def _too_large(request: Request, exc: BodyTooLarge) -> JSONResponse:
        return JSONResponse({"status": 0, "errors": ["request body too large"]}, status_code=413)

    @app.exception_handler(BadRequest)
    async def _bad(request: Request, exc: BadRequest) -> JSONResponse:
        return JSONResponse({"status": 0, "errors": [str(exc)]}, status_code=400)

    @app.exception_handler(Unauthorized)
    async def _unauth(request: Request, exc: Unauthorized) -> JSONResponse:
        log.warning(
            "Unauthorized request to %s from %s",
            request.url.path,
            request.client.host if request.client else "?",
        )
        return JSONResponse({"status": 0, "errors": ["unauthorized"]}, status_code=401)

    def cfg(request: Request) -> Config:
        return request.app.state.config

    def dispatcher(request: Request) -> Dispatcher:
        return request.app.state.dispatcher

    def token_source(request: Request, source_id: str, stype: str) -> Source:
        src = cfg(request).source(source_id, stype)
        if src is None or not _eq(request_token(request), src.token):
            raise Unauthorized
        return src

    def result_response(result: Result) -> JSONResponse:
        code = 502 if result.status == "error" else 200
        return JSONResponse(
            {"status": "ok" if result.ok else "error", "result": result.status, "detail": result.detail},
            status_code=code,
        )

    # ---- config page ------------------------------------------------------
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def config_page(request: Request) -> HTMLResponse:
        state = request.app.state
        watcher = getattr(state, "watcher", None)
        page = status.render(
            state.config,
            loaded_at=state.config_loaded_at,
            config_file=state.config_file,
            reload_interval=state.reload_interval,
            reload_error=watcher.last_error if watcher else None,
        )
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
                "X-Frame-Options": "DENY",
            },
        )

    # ---- meta -------------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---- icons ------------------------------------------------------------
    for name, media_type in STATIC_FILES.items():
        app.add_api_route(
            f"/{name}", _static_route(name, media_type), methods=["GET", "HEAD"], include_in_schema=False
        )
        if name.endswith(".png"):
            app.add_api_route(
                f"/icons/{name}",
                _static_route(name, media_type),
                methods=["GET", "HEAD"],
                include_in_schema=False,
            )
    app.add_api_route(
        "/icon.png",
        _static_route("android-chrome-512x512.png", "image/png"),
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )

    # ---- Pushover compatible ----------------------------------------------
    @app.post("/1/messages.json")
    async def pushover_messages(request: Request) -> JSONResponse:
        c = cfg(request)
        body = await read_body(request, c.max_body_bytes)
        data, files = await parse_fields(request, body, c.max_attachment_bytes)
        token = data.get("token") or request_token(request)
        src = next((s for s in c.sources_of_type("pushover") if _eq(token, s.token)), None)
        if src is None:
            return JSONResponse(
                {
                    "token": "invalid",
                    "status": 0,
                    "request": str(uuid.uuid4()),
                    "errors": ["application token is invalid"],
                },
                status_code=400,
            )
        upload = next((f for f in files), None)
        try:
            n = pushover.to_notification(data, src, upload, c.max_attachment_bytes)
        except pushover.PushoverRequestError as exc:
            return JSONResponse(
                {"status": 0, "request": str(uuid.uuid4()), "errors": [str(exc)]}, status_code=400
            )
        safe = {k: v for k, v in data.items() if k not in ("token", "user", "attachment_base64")}
        result = await dispatcher(request).dispatch(
            src, n, endpoint="pushover", payload=json.dumps(safe).encode()
        )
        request_id = result.request_id or str(uuid.uuid4())
        if result.status == "error":
            return JSONResponse(
                {"status": 0, "request": request_id, "errors": [result.detail]}, status_code=502
            )
        return JSONResponse({"status": 1, "request": request_id})

    # ---- GitHub -----------------------------------------------------------
    @app.post("/hooks/github/{source_id}")
    async def github_hook(request: Request, source_id: str) -> JSONResponse:
        c = cfg(request)
        src = c.source(source_id, "github")
        if src is None:
            raise Unauthorized
        body = await read_body(request, c.max_body_bytes)
        if src.secret:
            if not github.verify_signature(src.secret, body, request.headers.get("x-hub-signature-256")):
                raise Unauthorized
        elif not src.allow_unsigned:
            raise Unauthorized
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return JSONResponse({"status": "ok", "result": "pong"})
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("application/x-www-form-urlencoded"):
                payload = json.loads(dict(parse_qsl(body.decode())).get("payload", "{}"))
            else:
                payload = json.loads(body)
        except ValueError as exc:
            raise BadRequest("body is not valid JSON") from exc
        n = github.to_notification(event, payload, src)
        result = await dispatcher(request).dispatch(src, n, endpoint=f"github:{event}", payload=body)
        return result_response(result)

    # ---- Slack-style ------------------------------------------------------
    @app.post("/hooks/slack/{source_id}")
    async def slack_hook(request: Request, source_id: str) -> PlainTextResponse:
        src = token_source(request, source_id, "slack")
        c = cfg(request)
        body = await read_body(request, c.max_body_bytes)
        data, _ = await parse_fields(request, body, c.max_attachment_bytes)
        if isinstance(data.get("payload"), str):  # legacy form-encoded `payload=` field
            try:
                data = json.loads(data["payload"])
            except ValueError as exc:
                raise BadRequest("payload is not valid JSON") from exc
        result = await dispatcher(request).dispatch(
            src, slack.to_notification(data, src), endpoint="slack", payload=body
        )
        # Slack clients expect a plain "ok"
        if result.status == "error":
            return PlainTextResponse(result.detail or "error", status_code=502)
        return PlainTextResponse("ok")

    # ---- Generic JSON -----------------------------------------------------
    @app.post("/hooks/generic/{source_id}")
    async def generic_hook(request: Request, source_id: str) -> JSONResponse:
        src = token_source(request, source_id, "generic")
        c = cfg(request)
        body = await read_body(request, c.max_body_bytes)
        try:
            payload: Any = json.loads(body) if body.strip() else {}
        except ValueError:
            payload = {"message": body.decode("utf-8", errors="replace")}
        try:
            n = generic.to_notification(payload, src)
        except generic.TemplateRenderError as exc:
            raise BadRequest(str(exc)) from exc
        result = await dispatcher(request).dispatch(src, n, endpoint="generic", payload=body)
        return result_response(result)

    # ---- Apprise ----------------------------------------------------------
    async def _apprise_api(request: Request, src: Source) -> PlainTextResponse:
        c = cfg(request)
        body = await read_body(request, c.max_body_bytes)
        data, files = await parse_fields(request, body, c.max_attachment_bytes)
        n = apprise.from_apprise_api(data, src, files, c.max_attachment_bytes)
        if n is None:
            raise BadRequest("body or title is required")
        safe = {k: v for k, v in data.items() if k not in ("urls", "token")}
        result = await dispatcher(request).dispatch(
            src, n, endpoint="apprise", payload=json.dumps(safe).encode()
        )
        if result.status == "error":
            return PlainTextResponse(result.detail or "error", status_code=424)
        return PlainTextResponse("Notification(s) sent.")

    @app.post("/notify/{key}")
    async def apprise_notify_key(request: Request, key: str) -> PlainTextResponse:
        src = next((s for s in cfg(request).sources_of_type("apprise") if _eq(key, s.token)), None)
        if src is None:
            raise Unauthorized
        return await _apprise_api(request, src)

    @app.post("/notify")
    @app.post("/notify/")
    async def apprise_notify(request: Request) -> PlainTextResponse:
        # Stateless apprise-api call: `urls` in the body is ignored; the token decides the source.
        token = request_token(request)
        src = next((s for s in cfg(request).sources_of_type("apprise") if _eq(token, s.token)), None)
        if src is None:
            raise Unauthorized
        return await _apprise_api(request, src)

    @app.post("/hooks/apprise-json/{source_id}")
    async def apprise_json_hook(request: Request, source_id: str) -> JSONResponse:
        src = token_source(request, source_id, "apprise_json")
        c = cfg(request)
        body = await read_body(request, c.max_body_bytes)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise BadRequest("body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise BadRequest("JSON body must be an object")
        n = apprise.from_apprise_json(data, src, c.max_attachment_bytes)
        logged = {k: v for k, v in data.items() if k != "attachments"}
        result = await dispatcher(request).dispatch(
            src, n, endpoint="apprise_json", payload=json.dumps(logged).encode()
        )
        return result_response(result)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "pushmore.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - container default
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        access_log=False,
    )


if __name__ == "__main__":
    main()
