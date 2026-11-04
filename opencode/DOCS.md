# Configuration

The app has two options:

```yaml
lazy_start: true
litellm_discovery: true
```

| Option | Default | Function |
| --- | --- | --- |
| `lazy_start` | `true` | Start a user's server on the first valid ingress request. If `false`, start all users in the database when the app starts. |
| `litellm_discovery` | `true` | Read the LiteLLM connection data and add an OpenCode provider. If `false`, do not add this provider. |

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

The gateway logs the `X-Remote-User-Id` header for each request. This is
the Home Assistant user ID from ingress, not the numeric ID in `users.db`.
Requests without the header get a missing marker in the log. The gateway
does not log cookies or query strings.

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
request. It supplies them to the mise task with the OpenCode configuration.
If the URL or prefix changes, it restarts that user's server before it
forwards the request. An eagerly started server gets this configuration
on the user's first ingress request.

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
OpenCode applies after file configuration. Set `litellm_discovery: false`
if you want to manage that provider yourself.

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

The provider uses `@ai-sdk/openai-compatible`. Its URL uses the published
host, port, and API path. Its key uses `master_key`. The app reads the
LiteLLM `/models` response to fill the OpenCode model list. If that request
fails, the app logs a warning. You can supply `provider.litellm.models` in
your OpenCode configuration or restart the app after LiteLLM is ready.

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

The Docker build uses the same OpenCode version for the server and web
interface. `prepare_frontend.py` adds the ingress prefix to the web
interface's router and API URL. It stops the build if the expected source
code changes. `frontend.py` supplies the current prefix when it serves the
HTML page. This keeps assets, navigation, HTTP requests, events, and
terminal WebSockets under Home Assistant ingress.
