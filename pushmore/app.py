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
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import UploadFile

from . import __version__
from .config import Config, Source, load_config
from .dispatcher import Dispatcher
from .models import Attachment, Result
from .pushover import PushoverClient
from .sources import apprise, generic, github, pushover, slack
from .store import EventStore

log = logging.getLogger("pushmore")


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


def create_app(config: Config | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = config or load_config()
        client = http_client or httpx.AsyncClient(headers={"User-Agent": f"PushMore/{__version__}"})
        store = EventStore(cfg.db_path, cfg.store_payloads)
        app.state.config = cfg
        app.state.store = store
        app.state.dispatcher = Dispatcher(
            cfg, PushoverClient(cfg.pushover_api_url, cfg.pushover_user, cfg.pushover_token, client), store
        )
        log.info(
            "PushMore %s started with %d source(s)%s",
            __version__,
            len(cfg.sources),
            " [dry-run]" if cfg.dry_run else "",
        )

        async def pruner() -> None:
            while True:
                with contextlib.suppress(Exception):
                    store.prune(cfg.retention_days)
                await asyncio.sleep(3600)

        task = asyncio.create_task(pruner())
        try:
            yield
        finally:
            task.cancel()
            if http_client is None:
                await client.aclose()
            store.close()

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

    # ---- meta -------------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/events")
    async def events(
        request: Request, limit: int = 50, source: str | None = None, payload: bool = False
    ) -> list[dict[str, Any]]:
        if not _eq(request_token(request), cfg(request).admin_token):
            raise Unauthorized
        return request.app.state.store.recent(limit, source, payload)

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
