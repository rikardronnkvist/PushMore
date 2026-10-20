<p align="center"><img src="docs/icon-128.png" width="128" alt="PushMore"></p>

# PushMore

Even more push notifications to Pushover.

PushMore is a small self-hosted gateway that receives notifications in the formats your tools already speak and forwards them to [Pushover](https://pushover.net). Pushover does the delivery to phones; PushMore adds more sources, filtering and throttling in front of it.

```
GitHub webhooks ─────┐
Slack-style hooks ───┤
Apprise (API/json) ──┼──► PushMore ──► Pushover API ──► 📱
Generic JSON ────────┤   filter · dedup · rate limit
Pushover API clients ┘
```

## Endpoints

| Endpoint | Source type | Auth |
|---|---|---|
| `POST /hooks/github/<id>` | `github` | `X-Hub-Signature-256` (HMAC) |
| `POST /hooks/slack/<id>` | `slack` | token¹ |
| `POST /notify/<token>` | `apprise` | token in path |
| `POST /notify` | `apprise` | token¹ |
| `POST /hooks/apprise-json/<id>` | `apprise_json` | token¹ |
| `POST /hooks/generic/<id>` | `generic` | token¹ |
| `POST /1/messages.json` | `pushover` | `token` field |
| `GET /` | – | none (keep internal) |
| `GET /healthz` | – | none |

¹ `Authorization: Bearer <token>`, `X-PushMore-Token: <token>` or `?token=<token>`. Unknown sources and wrong tokens return the same `401`.

## Configuration

The config file (`/config/config.yaml`) has a global Pushover section and a list of sources. Each source has a unique id (used in the URL), a type and its own token. Values can reference `${ENV_VAR}` or `${file:/run/secrets/name}` (Docker secrets).

```yaml
pushover:
  user_key: ${file:/run/secrets/pushover_user_key}
  app_token: ${file:/run/secrets/pushover_app_token}   # default app for all sources

server:
  dry_run: false              # true = log what would be sent, never call Pushover

sources:
  my-source:
    type: slack
    token: ${file:/run/secrets/pushmore_my_source}
```

Tokens must be unique and at least 16 characters. Generate them with `openssl rand -hex 24`.

### Options for every source

All optional.

```yaml
    pushover_token: azGDORePK8gMaC0QOYAMyEEuzJnyUi   # send as another Pushover app (own icon/sound), else pushover.app_token
    priority: -1              # default priority when the mapping has none (-2 … 2)
    sound: bike
    device: iphone
    title_prefix: "[Kuma]"
    min_priority: 0           # drop anything below this priority
    exclude: ["^maintenance"] # drop if title/message matches a regex (case-insensitive)
    dedup_seconds: 300        # drop identical title+message within this window
    rate_limit: 10/min        # also /s, /h
    enabled: true
```

### `github`: GitHub webhooks

```yaml
  github-main:
    type: github
    secret: ${file:/run/secrets/pushmore_github_secret}
    events: [push, release, workflow_run, pull_request]   # default: all supported
    only_failures: false      # true = only failed workflow runs, dependabot alerts, pushes and releases
```

GitHub → Settings → Webhooks: Payload URL `https://pushmore.example.com/hooks/github/github-main`, content type `application/json`, same secret.

Supported events: `push`, `release`, `workflow_run`, `pull_request`, `issues`, `star`, `fork`, `dependabot_alert`. Failed workflow runs and high/critical Dependabot alerts get priority 1; stars and forks get -1. `ping` is answered without a notification.

### `slack`: Slack incoming-webhook format

For tools that can post to a Slack webhook: Uptime Kuma, Grafana, Sonarr/Radarr, Komodo, Semaphore, Authentik (Slack/Discord mode) …

```yaml
  uptime-kuma:
    type: slack
    token: ${file:/run/secrets/pushmore_kuma}
    danger_priority: 1        # priority for attachments with color "danger"/red (default 1)
```

URL: `https://pushmore.example.com/hooks/slack/uptime-kuma?token=<token>`

Handles `text`, legacy `attachments` (title, link, text, fields, color) and Block Kit. Slack mrkdwn is converted to Pushover HTML.

### `apprise`: apprise-api compatible

For `apprise://` URLs and tools with an "Apprise API" integration, e.g. cross-seed's `notificationWebhookUrls`.

```yaml
  cross-seed:
    type: apprise
    token: ${file:/run/secrets/pushmore_crossseed}
    type_priority: { info: -1, success: -1, warning: 0, failure: 1 }   # default: 0, 0, 0, 1
```

URL: `http://pushmore:8080/notify/<token>` or `apprise://pushmore.example.com/<token>`

Apprise `type` decides the priority (missing `type` = `info`). `format: markdown|html` is converted to Pushover HTML. The first image attachment that fits is forwarded. `urls` in the request is ignored, so a caller cannot redirect delivery.

### `apprise_json`: Apprise `json://` plugin

For tools that embed the Apprise library and can only use a `json://` URL.

```yaml
  scripts:
    type: apprise_json
    token: ${file:/run/secrets/pushmore_scripts}
```

URL: `json://pushmore.example.com/hooks/apprise-json/scripts?-token=<token>`

Same `type_priority` behaviour as `apprise`; base64 image attachments are supported.

### `generic`: any JSON webhook

Map an arbitrary JSON payload with sandboxed Jinja2 templates. Payload keys are available directly and as `payload`.

```yaml
  alertmanager:
    type: generic
    token: ${file:/run/secrets/pushmore_am}
    title_template: "{{ commonLabels.alertname }} ({{ status }})"
    message_template: |
      {% for a in alerts %}{{ a.annotations.summary }}
      {% endfor %}
    url_template: "{{ externalURL }}"
    priority_template: "{{ 1 if status == 'firing' else -1 }}"
```

URL: `https://pushmore.example.com/hooks/generic/alertmanager` with `Authorization: Bearer <token>`

Without templates PushMore uses the payload's `message`/`text`/`body` and `title` fields. A template error returns `400`.

### `pushover`: drop-in for api.pushover.net

For tools that only speak the Pushover API. Point them at `https://pushmore.example.com/1/messages.json` and give them this token as the app token; the real Pushover credentials stay in PushMore.

```yaml
  legacy-tools:
    type: pushover
    token: ${file:/run/secrets/pushmore_legacy}
    allow_user_override: false   # ignore the client's `user` (default)
```

All normal message parameters are supported, including `attachment` and `attachment_base64`.

## Config page

`GET /` shows the running configuration: global settings, runtime settings and every source with its endpoint. Values not set in the file are shown with their default and marked `default`. Secrets are masked, and a failed reload is shown at the top. The page has no authentication, so only expose it internally.

## Logging and reload

Logs go to stdout (`docker service logs`), one line per request with `status` = `sent`, `ignored`, `filtered`, `deduped`, `ratelimited`, `dry_run` or `error`. `LOG_LEVEL=DEBUG` also logs the received payload, which helps when writing mappings.

The config file is checked every 10 seconds and applied without a restart. An invalid file is logged and the running config is kept.

| Variable | Default | |
|---|---|---|
| `PUSHMORE_CONFIG` | `/config/config.yaml` | Config file |
| `PUSHMORE_RELOAD_INTERVAL` | `10` | Seconds between checks, `0` disables |
| `LOG_LEVEL` | `INFO` | |
| `PORT` | `8080` | |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Set to `*` behind a reverse proxy to log real client IPs |

## Running

### Docker Swarm + Traefik

```yaml
services:
  pushmore:
    image: ghcr.io/rikardronnkvist/pushmore:0.1
    environment:
      FORWARDED_ALLOW_IPS: "*"
    volumes:
      - /srv/pushmore/config.yaml:/config/config.yaml:ro
    secrets: [pushover_user_key, pushover_app_token, pushmore_github_secret]
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

secrets:
  pushover_user_key: { external: true }
  pushover_app_token: { external: true }
  pushmore_github_secret: { external: true }
```

Only expose what must be reachable from the internet (usually just `/hooks/github/`). The container runs as uid 10001, so the config file must be readable by that user.

### Docker

```bash
docker run -d --name pushmore -p 8080:8080 \
  -v ./config.yaml:/config/config.yaml:ro \
  -e PUSHOVER_USER_KEY=... -e PUSHOVER_APP_TOKEN=... \
  ghcr.io/rikardronnkvist/pushmore:0.1
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## Releases

Publishing a GitHub release tagged `vX.Y.Z` (no dot after the `v`) builds a multi-arch image (`linux/amd64`, `linux/arm64`) and pushes it to `ghcr.io/rikardronnkvist/pushmore` as `X.Y.Z`, `X.Y`, `X` (from 1.0) and `latest` (not for pre-releases).

## License

MIT
