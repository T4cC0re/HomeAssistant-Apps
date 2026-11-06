# Configuration

The app has two options:

```yaml
lazy_start: true
litellm_discovery: true
```

| Option | Default | Function |
| --- | --- | --- |
| `lazy_start` | `true` | Start a user's server on the first valid ingress request. If `false`, start all users in the database when the app starts. |
| `litellm_discovery` | `true` | Read the LiteLLM app settings and enable the model discovery plugin. |

When discovery is enabled, the app reads the installed LiteLLM app settings.
If LiteLLM is unavailable, OpenCode starts without the generated connection.

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
currently restricts this endpoint to Core. The gateway recognizes only the
exact Core or role restriction response after a successful authenticated
installed-app list request. In that case,
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

When a request includes a browser `Origin` header, it must match the current
public Home Assistant origin. The gateway then uses the local OpenCode
origin for the backend request. This permits terminal connection tokens and
WebSocket connections even when the instance starts before ingress is used
or the public Home Assistant URL changes. A different browser origin is
rejected before the gateway contacts OpenCode.

The app stores each user's files at `/data/<username>`. Inside the sandbox,
the same directory is `/home/opencode`, with `HOME=/home/opencode`.
The app creates private configuration, cache, state, and temporary
directories in that home. It runs the process
with numeric user ID `10000 + id` and group `0`. It passes the parent
environment except the Supervisor token. App-generated user and server
settings take precedence over the parent environment.

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

Each OpenCode process has a private mount namespace and an AppArmor child
profile. Its home is mounted at `/home/opencode`. The original `/data`
tree, the gateway's root home, and `/run` are hidden. Each instance also
has private `/tmp`, `/var/tmp`, and `/dev/shm` filesystems. Mount changes
do not affect the gateway or other instances.

System tools, mise installations, and configuration are mounted read-only.
The child profile permits writes in the user's home, temporary directories,
and `/homeassistant`. It permits development tools and network access.
Normal file permissions still apply. User mise caches and state are stored
in the private home. All child processes inherit the restrictions.

The launcher needs Linux mount namespaces, `mount_setattr`, the app's
enforced AppArmor profile, and the `SYS_ADMIN` capability declared in the
manifest. It drops all capabilities and sets the user's numeric identity
before it enters the child profile. It then prevents new privileges.
If any step fails, the OpenCode instance does not start. The app does not
use Landlock or create AppArmor profiles at runtime.

If sandbox setup fails, the app log shows the original error followed by
a `Sandbox diagnostics` record. This record contains the active AppArmor
label, effective user ID, `SYS_ADMIN` availability, seccomp state, and the
failed mount target and flags. Unavailable process details are marked
`unavailable`. Credentials and environment contents are not included.

After an AppArmor profile change, use the normal Home Assistant app
update. A rebuild does not reload the AppArmor profile. A mount denial
can have other causes; the diagnostics do not confirm a stale profile.

Home Assistant mounts its configuration at `/homeassistant`. All users
share this directory. Normal file permissions apply to each numeric user
ID and group `0`. Files that only root can write remain read-only for
OpenCode. The app does not change permissions on the Home Assistant
configuration.

You can keep an OpenCode configuration at
`/data/<username>/.config/opencode/opencode.json`. Inside the sandbox,
this file is `/home/opencode/.config/opencode/opencode.json`. The app supplies its
server and LiteLLM settings through `OPENCODE_CONFIG_CONTENT`, which
OpenCode applies after file configuration. The app adds the LiteLLM plugin
when discovery succeeds. Set `litellm_discovery: false` to disable this
connection.

## LiteLLM and discovery

The app reads the installed app list and the LiteLLM app information from
Supervisor. It uses the LiteLLM hostname, port `4000`, `server_root_path`,
and `master_key`. It passes the key as `LITELLM_MASTER_KEY` to each user
process. Every OpenCode user can access this key and its LiteLLM privileges.
The app does not log the key or Supervisor responses.

The `opencode-plugin-litellm` npm plugin discovers models and pricing. It
uses the LiteLLM model information endpoint when available. The app starts
without the generated connection if LiteLLM is absent, its settings are
invalid, or Supervisor access fails. If more than one LiteLLM app is
installed, discovery stops and the app logs a warning.

The plugin normalizes URLs to a `/v1` API endpoint. LiteLLM also exposes its
routes without the configured root prefix. Check discovery after changing
`server_root_path`, especially when it does not end in `/v1`.

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
