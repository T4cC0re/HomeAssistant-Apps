# Configuration

The app has two options:

```yaml
lazy_start: true
litellm_discovery: true
```

| Option | Default | Function |
| --- | --- | --- |
| `lazy_start` | `true` | Start a user's server on the first valid ingress request. If `false`, start all users in the database when the app starts. |
| `litellm_discovery` | `true` | Read the LiteLLM model list. If `false`, use the fixed provider without discovered models. |

The app checks whether LiteLLM is installed for both settings. If LiteLLM
is absent, the app logs a warning and continues. OpenCode can use other
providers that you configure in its web interface or user configuration.

## Request authentication

The entrypoint is `main.py`. It listens on `0.0.0.0:8080`. Home Assistant
uses this port for ingress. The app also publishes port `8080` on the host.
You can change that host port in the Network settings if it is in use.

Each request must come from the Supervisor ingress proxy at `172.30.32.2`.
The gateway does not use `X-Forwarded-For` to trust a client. It requires
the `ingress_session` cookie, `X-Ingress-Path`, and `X-Remote-User-Name`.
If the user name is empty, it returns HTTP `500` with an error message.
Other missing or invalid fields cause the request to fail before a user
or process is created.

The gateway does not log each ingress request. It logs process starts,
errors, and watchdog events. It uses the user name to select a process.
The `X-Remote-User-Id` header and the session cookie do not select a process.

The gateway calls `POST /ingress/validate_session`. Home Assistant
currently restricts this endpoint to Core, even when an app has the admin
role. The gateway recognizes only the exact Core restriction response
after a successful authenticated app information request. In that case,
it uses the session validation that Supervisor completed before it sent
the ingress request. Other API errors cause the request to fail.

The gateway checks a WebSocket session again every 30 seconds when the API
permits this check. With the Core restriction, Supervisor validates the
session when it opens the connection; an open WebSocket cannot get a new
session check until it reconnects. Direct host access is denied because
it has no trusted Supervisor user identity.

See the [Supervisor service API](https://contributors.home-assistant.io/apps/supervisor-api/services/)
and [ingress guide](https://contributors.home-assistant.io/apps/guides/ingress/).

## Users and processes

The gateway keeps users that it has seen in memory. It stores the user
name in `/data/users.db` before it starts or selects the user's server.
The `users` table has these columns:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
username TEXT NOT NULL UNIQUE
```

The server port is `8080 + id`. Only the gateway port is published. Each
server listens on `127.0.0.1` and has a separate random password. The
password is not sent to the browser. The gateway removes ingress cookies,
user identity headers, and client authorization before it sends a
request to OpenCode.

Each user has `HOME=/data/<username>`. The app creates private configuration,
cache, state, and temporary directories in that home. It runs the process
with numeric user ID `10000 + id` and group `0`. It does not pass the
Supervisor token or the parent environment to the process.

The gateway detects the public URL and ingress prefix from each trusted
request. It uses them to forward that request.
A new session cookie, URL, or prefix does not restart a live server.
The server receives the URL and prefix from its first start, including empty
values when `lazy_start` is `false`. If the server stops, the next request
starts a replacement.

The proxy streams HTTP request and response bodies. It supports server
sent events and WebSocket upgrades without an app request size limit.
Limits in Home Assistant or an external proxy can still apply.

## Filesystem access

The `opencode` mise task limits the environment passed to each process.
It does not apply a Landlock file sandbox. Home Assistant OS does not
include Landlock in the host kernel. The process starts in its own home
directory. Its numeric user ID and directory permissions prevent access
to `/data/users.db`, `/data/options.json`, and other users' homes.
It can access other paths that normal file permissions allow. Network
access stays enabled for model APIs and tools.

Home Assistant mounts its configuration at `/homeassistant`. All users
share this directory. Normal file permissions apply to each numeric user
ID and group `0`. Files that only root can write remain read-only for
OpenCode. The app does not change permissions on the Home Assistant
configuration.

You can keep an OpenCode configuration at
`/data/<username>/.config/opencode/opencode.json`. The app supplies its
server and LiteLLM settings through `OPENCODE_CONFIG_CONTENT`, which
OpenCode applies after file configuration. The app always sets its LiteLLM
provider URL and name. Set `litellm_discovery: false` to skip model discovery.

## LiteLLM and discovery

On startup, the app calls `GET /discovery` and logs the response without
private configuration values. Master keys, tokens, and unknown
configuration values are redacted.

The app also probes `http://supervisor/addons/self/info`,
`http://supervisor/info`,
`http://supervisor/core/api/hassio/discovery`, and
`http://homeassistant/api/hassio/discover`. It sends the app's
`SUPERVISOR_TOKEN` on each request. It logs each HTTP status and the
response body, or a connection error. These response bodies can contain
private data. The app never logs the request token. A denied or unreachable
probe does not stop startup. If a probe returns valid discovery data, the
app can use it for the LiteLLM provider.

Home Assistant currently restricts this endpoint to Core. If the call is
denied, the app checks `http://192.168.178.147:8123/api/hassio/discover`
and `/api/hassio/discovery` with its admin-role app token. It logs each
HTTP status and a redacted response. The Core proxy can also deny these
paths. If all calls fail, OpenCode starts without the LiteLLM provider.
The app does not read LiteLLM options. It still checks whether LiteLLM is
installed and logs a warning when it is absent.

The provider uses `@ai-sdk/openai-compatible`, the name
`homeassistant-litellm`, and the URL
`http://aca25ede-litellm:4000/inference/v1`. OpenCode receives no API key
for this provider. The app can still use discovered LiteLLM credentials to
read `/models` and fill the model list. If that request fails, the app logs
a warning. You can supply `provider.litellm.models` in your OpenCode
configuration or restart the app after LiteLLM is ready.

## Watchdog

The app exposes `/_supervisor/watchdog` on port `8080` for the Supervisor
watchdog. Only requests from the Supervisor address can use it. A watchdog
request does not create a user or start OpenCode. The gateway logs each
request and records its time in memory. If the next request does not arrive
within five minutes, it logs one warning. It logs recovery when requests
resume. If no watchdog request arrives, it does not expect one.

A missed request shows that Supervisor-to-app checks stopped. It does not
prove that Home Assistant Core is unhealthy. If you turn off the watchdog
after it has sent a request, the app warns until you restart it.

## Web interface

OpenCode v2 serves its own web interface. The gateway removes the Home
Assistant ingress prefix from each request path and forwards the request
to the user's OpenCode server. For each HTML response, it adds a base URL
and the current `X-Ingress-Path` value to root-relative links. It adds a
same-origin script before OpenCode's module script. This script adds the
current ingress path to unprefixed `fetch`, XHR, EventSource, WebSocket, and
Worker URLs, and to dynamic asset links. It adjusts font URLs in loaded style
sheets. The gateway also adapts the two pinned OpenCode 2.2.5 UI bundles
in transit. The adapter gives the router the current ingress base, removes
the prefix before OpenCode parses its route, and skips service worker
registration. It also keeps the original body when the request queue sends
an API request. This lets JSON requests work on an HTTP/1.1 connection.
The script sends the ingress session cookie with same-origin app requests.
It does not add that cookie to external requests. The adapter checks each
bundle's name, SHA-256 hash, and replacement count. If a bundle changes,
the gateway returns HTTP `502` with an error.

The gateway rewrites app redirects to the current ingress path. It gets
the path from each authenticated request. A new ingress URL does not
restart a user's server. The script derives its path from the page's base
tag. It does not contain a session token. Changed HTML and JavaScript
responses are not cached. Other assets, uploads, events, and WebSockets
continue to stream through the gateway. The OpenCode binary and source
files are not changed.
