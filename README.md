<p align="center"><img src="docs/icon-128.png" width="128" alt="PushMore"></p>

# PushMore

Even more push notifications to Pushover.

PushMore is a small self-hosted gateway that receives notifications in the formats your tools already speak and forwards them to [Pushover](https://pushover.net). Pushover keeps doing the hard part (delivery to phones); PushMore adds more sources, filtering and throttling in front of it.

```
GitHub webhooks ─────┐
Slack-style hooks ───┤
Apprise (API/json) ──┼──► PushMore ──► Pushover API ──► 📱
Generic JSON ────────┤   filter · dedup · rate limit
Pushover API clients ┘
```

## Endpoints

| Endpoint | Source type | Auth | Typical clients |
|---|---|---|---|
| `POST /1/messages.json` | `pushover` | `token` field (form/JSON/query) | Anything that already speaks Pushover |
| `POST /hooks/github/<id>` | `github` | `X-Hub-Signature-256` (HMAC) | GitHub repo/org webhooks |
| `POST /hooks/slack/<id>` | `slack` | token¹ | Uptime Kuma, Grafana, *arr, Komodo, Semaphore … |
| `POST /notify/<key>` | `apprise` | key in path | `apprise://host/<key>`, "Apprise API" integrations |
| `POST /notify` | `apprise` | token¹ | Stateless apprise-api calls (`urls` is ignored) |
| `POST /hooks/apprise-json/<id>` | `apprise_json` | token¹ | Apprise `json://` plugin |
| `POST /hooks/generic/<id>` | `generic` | token¹ | Any JSON webhook + Jinja2 templates |
| `GET /healthz` | – | none | Health checks |
| `GET /favicon.ico`, `/icon.png` … | – | none | Icons for dashboards and other services (see below) |

¹ `Authorization: Bearer <token>`, `X-PushMore-Token: <token>` or `?token=<token>`. Unknown sources and wrong tokens return the same `401`.

### What gets mapped

- **GitHub**: `push`, `release`, `workflow_run` (failures get priority 1), `pull_request`, `issues`, `star`, `fork`, `dependabot_alert` (high/critical gets priority 1). `ping` is answered without notifying. Use `events:` to allow-list and `only_failures: true` for a CI-only source.
- **Slack**: `text`, legacy `attachments` (title, title_link, text, fields, color) and Block Kit (`header`, `section`, `context`, `rich_text`, button URLs). mrkdwn is converted to Pushover HTML. `color: danger` (or red) gets `danger_priority`.
- **Apprise**: `type` → priority via `type_priority` (default `failure` = 1). `format: markdown|html` is converted to Pushover HTML. The first image attachment that fits is forwarded (multipart for apprise-api, base64 for `json://`); remote attachment URLs are never fetched.
- **Generic**: `title_template`, `message_template`, `url_template`, `priority_template` (sandboxed Jinja2, payload keys available directly and as `payload`). Without templates PushMore uses `message`/`text`/`body` and `title`.
- **Pushover**: All normal message parameters, including `attachment` (multipart) and `attachment_base64`. The client's `user` is ignored unless `allow_user_override: true`.

### Icons

PushMore serves its own icons so other services (dashboards, Pushover app icon, Home Assistant, Homepage …) can link to them:

| Path | Size |
|---|---|
| `/favicon.ico` | 16/32/48 multi-size |
| `/favicon-16x16.png`, `/favicon-32x32.png` | 16, 32 |
| `/apple-touch-icon.png` | 180 |
| `/android-chrome-192x192.png`, `/android-chrome-512x512.png` | 192, 512 |
| `/icon.png` | 512 (alias) |
| `/site.webmanifest` | Web app manifest |

PNG files are also available under `/icons/<name>`.

## Logging

Everything is logged to stdout, so it ends up in `docker logs` / `docker service logs`. There is no database and no log API. Each request produces one line:

```
INFO pushmore.dispatcher: source=uptime-kuma endpoint=slack status=sent title='[Kuma] nas01 down'
```

`status` is one of `sent`, `ignored`, `filtered`, `deduped`, `ratelimited`, `dry_run` or `error` (logged as WARNING). With `LOG_LEVEL=DEBUG` the received payload is logged too (truncated to 4 KB, Pushover credentials removed), which is useful when writing mappings.

## Configuration

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml`. Values can reference `${ENV_VAR}` or `${file:/run/secrets/name}`.

Per-source options (all optional): `priority` (default when the mapping has no opinion), `sound`, `device`, `title_prefix`, `pushover_token` / `pushover_user`, `min_priority`, `exclude` (regex list), `dedup_seconds`, `rate_limit` (`10/min`, `100/h`), `enabled`.

Tokens must be unique and at least 16 characters; GitHub sources require a `secret`. Generate tokens with `openssl rand -hex 24`.

Environment: `PUSHMORE_CONFIG` (default `/config/config.yaml`), `PORT` (8080), `LOG_LEVEL`, `FORWARDED_ALLOW_IPS` (set to your reverse proxy network so client IPs in logs are correct).

## Running

```bash
docker run -d --name pushmore -p 8080:8080 \
  -v ./config.yaml:/config/config.yaml:ro \
  -e PUSHOVER_USER_KEY=... -e PUSHOVER_APP_TOKEN=... \
  ghcr.io/rikardronnkvist/pushmore:latest
```

### Docker Swarm + Traefik

```yaml
services:
  pushmore:
    image: ghcr.io/rikardronnkvist/pushmore:latest
    environment:
      FORWARDED_ALLOW_IPS: "*"
    configs:
      - source: pushmore_config
        target: /config/config.yaml
    # reference these in config.yaml as ${file:/run/secrets/<name>}
    secrets: [pushover_user_key, pushover_app_token, pushmore_kuma_token]
    networks: [traefik]
    deploy:
      replicas: 1   # dedup and rate limiting are in-memory
      labels:
        - traefik.enable=true
        - traefik.http.services.pushmore.loadbalancer.server.port=8080
        # Internal: everything
        - traefik.http.routers.pushmore.rule=Host(`pushmore.example.com`)
        - traefik.http.routers.pushmore.entrypoints=websecure
        # Public: only GitHub webhooks
        - traefik.http.routers.pushmore-gh.rule=Host(`pushmore.example.com`) && PathPrefix(`/hooks/github/`)
        - traefik.http.routers.pushmore-gh.entrypoints=websecure
```

Only expose what must be reachable from the internet (usually just `/hooks/github/`), and keep the rest internal.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Set `server.dry_run: true` to see what would be sent without calling Pushover.

## Releases

Publishing a GitHub release (tag `vX.Y.Z`) builds a multi-arch image (`linux/amd64`, `linux/arm64`) and pushes it to `ghcr.io/rikardronnkvist/pushmore` with the tags `X.Y.Z`, `X.Y`, `X` (from 1.0) and `latest` (not for pre-releases).

## License

MIT
