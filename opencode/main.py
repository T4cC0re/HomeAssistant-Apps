"""Start one OpenCode server for each authenticated Home Assistant user."""

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web, WSMsgType
from multidict import CIMultiDict
from yarl import URL

from frontend import transform_html
from supervisor import Supervisor, SupervisorCoreOnly, SupervisorError


LOG = logging.getLogger("opencode")
PORT = 8080
USER_UID_BASE = 10000
APP_DIR = Path(__file__).resolve().parent
TRUSTED_INGRESS = ipaddress.ip_address("172.30.32.2")
WATCHDOG_PATH = "/_supervisor/watchdog"
WATCHDOG_TIMEOUT = 300
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
PRIVATE_HEADERS = {"authorization", "cookie", "host", "forwarded", "x-supervisor-token"}


class WatchdogMonitor:
    """Warn when Supervisor stops calling the app watchdog."""

    def __init__(self, timeout: int = WATCHDOG_TIMEOUT):
        self.timeout = timeout
        self.last_seen: float | None = None
        self.warned = False
        self.timer: asyncio.TimerHandle | None = None

    def received(self):
        loop = asyncio.get_running_loop()
        if self.warned:
            LOG.info("Supervisor watchdog requests resumed.")
        LOG.info("Supervisor watchdog request received.")
        self.last_seen = loop.time()
        self.warned = False
        if self.timer is not None:
            self.timer.cancel()
        self.timer = loop.call_later(self.timeout, self.missed)

    def missed(self):
        if self.last_seen is not None and not self.warned:
            LOG.warning(
                "A watchdog request was expected but not received within %s seconds. "
                "Supervisor-to-app health checks may be unhealthy.", self.timeout,
            )
            self.warned = True

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


def read_options(path: Path) -> dict:
    """Read and check the two app options."""
    options = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(options, dict):
        raise ValueError("The app options must contain a JSON object.")
    result = {}
    for name in ("lazy_start", "litellm_discovery"):
        value = options.get(name, True)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean.")
        result[name] = value
    return result


def check_username(username: str) -> str:
    """Accept one directory name without path separators or control characters."""
    if (not username or not username.strip() or len(username.encode("utf-8")) > 200
            or username in {".", "..", "users.db", "users.db-wal", "users.db-shm", "options.json"}
            or any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in username)):
        raise web.HTTPBadRequest(text="The Home Assistant user name is not a valid directory name.")
    return username


class UserStore:
    """Keep stable user IDs in the app data directory."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o711)
        self.connection = sqlite3.connect(data_dir / "users.db")
        os.chmod(data_dir / "users.db", 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE)"
        )
        self.connection.commit()

    def upsert(self, username: str) -> tuple[int, str]:
        username = check_username(username)
        row = self.connection.execute(
            "SELECT id, username FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            with self.connection:
                self.connection.execute("INSERT INTO users (username) VALUES (?)", (username,))
            row = self.connection.execute(
                "SELECT id, username FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not 0 < row[0] <= 65535 - PORT:
            raise web.HTTPServiceUnavailable(text="The OpenCode user port range is full.")
        return row

    def all(self) -> list[tuple[int, str]]:
        return self.connection.execute("SELECT id, username FROM users ORDER BY id").fetchall()

    def close(self):
        self.connection.close()


@dataclass
class Instance:
    user_id: int
    username: str
    process: asyncio.subprocess.Process
    password: str

    @property
    def port(self) -> int:
        return PORT + self.user_id


class Instances:
    """Start, check, and stop the servers for registered users."""

    def __init__(self, store: UserStore, client: aiohttp.ClientSession, overlay: dict,
                 app_dir: Path = APP_DIR):
        self.store = store
        self.client = client
        self.overlay = overlay
        self.app_dir = app_dir
        self.running: dict[int, Instance] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.seen_users: dict[str, int] = {}

    def home(self, user_id: int, username: str) -> Path:
        check_username(username)
        home = self.store.data_dir / username

        def open_directory(parent_fd, name):
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError:
                raise ValueError("A user runtime directory must be a directory without symbolic links.") from None
            try:
                if os.geteuid() == 0:
                    os.fchown(descriptor, USER_UID_BASE + user_id, 0)
                os.fchmod(descriptor, 0o700)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor

        data_fd = os.open(self.store.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            home_fd = open_directory(data_fd, username)
            try:
                for name in (".config", ".cache", ".tmp"):
                    os.close(open_directory(home_fd, name))
                local_fd = open_directory(home_fd, ".local")
                try:
                    for name in ("share", "state"):
                        os.close(open_directory(local_fd, name))
                finally:
                    os.close(local_fd)
            finally:
                os.close(home_fd)
        finally:
            os.close(data_fd)
        return home

    def environment(self, user_id: int, username: str, home: Path,
                    prefix: str, origin: str, password: str) -> dict[str, str]:
        config = json.loads(json.dumps(self.overlay))
        config["server"] = {"hostname": "127.0.0.1", "port": PORT + user_id,
                            "cors": [origin] if origin else []}
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": str(home), "USER": username, "SHELL": "/bin/sh",
            "MISE_DATA_DIR": "/opt/mise", "MISE_CONFIG_DIR": "/opt/mise-config",
            "MISE_CACHE_DIR": str(home / ".cache/mise"),
            "MISE_STATE_DIR": str(home / ".local/state/mise"),
            "MISE_TRUSTED_CONFIG_PATHS": str(self.app_dir),
            "MISE_YES": "1", "MISE_AUTO_INSTALL": "0",
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "TMPDIR": str(home / ".tmp"),
            "OPENCODE_USERNAME": username, "OPENCODE_PORT": str(PORT + user_id),
            "OPENCODE_PUBLIC_URL": origin + prefix,
            "OPENCODE_INGRESS_PATH": prefix,
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_SERVER_USERNAME": "opencode",
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
        }

    async def ensure(self, user_id: int, username: str, prefix: str, origin: str) -> Instance:
        if not 0 < user_id <= 65535 - PORT:
            raise ValueError("The saved user ID is outside the port range.")
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            old = self.running.get(user_id)
            if old and old.process.returncode is None:
                return old
            home = self.home(user_id, username)
            password = secrets.token_urlsafe(32)
            kwargs = {}
            if os.geteuid() == 0:
                kwargs = {"user": USER_UID_BASE + user_id, "group": 0, "extra_groups": []}
            process = await asyncio.create_subprocess_exec(
                "mise", "-C", str(self.app_dir), "run", "opencode",
                cwd=home, env=self.environment(user_id, username, home, prefix, origin, password),
                start_new_session=True, **kwargs,
            )
            instance = Instance(user_id, username, process, password)
            self.running[user_id] = instance
            try:
                await self.ready(instance)
            except BaseException:
                await self.stop(instance)
                self.running.pop(user_id, None)
                raise
            LOG.info("OpenCode is ready for user %s on port %s.", username, instance.port)
            return instance

    async def ready(self, instance: Instance):
        try:
            async with asyncio.timeout(60):
                while True:
                    if instance.process.returncode is not None:
                        raise RuntimeError("The OpenCode process stopped during startup. Check the app log.")
                    try:
                        async with self.client.get(
                            f"http://127.0.0.1:{instance.port}/global/health",
                            headers={"Authorization": aiohttp.encode_basic_auth("opencode", instance.password)},
                            timeout=aiohttp.ClientTimeout(total=1),
                        ) as response:
                            if response.status == 200:
                                return
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.2)
        except asyncio.TimeoutError:
            raise RuntimeError("OpenCode did not become ready within 60 seconds.") from None

    async def stop(self, instance: Instance):
        try:
            os.killpg(instance.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(instance.process.wait(), 10)
        except asyncio.TimeoutError:
            try:
                os.killpg(instance.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await instance.process.wait()

    async def close(self):
        await asyncio.gather(*(self.stop(item) for item in self.running.values()))


def ingress_context(request: web.Request) -> tuple[str, str, str, str]:
    """Accept user headers only from the Supervisor ingress proxy."""
    try:
        peer = ipaddress.ip_address(request.remote or "")
    except ValueError:
        raise web.HTTPForbidden(text="Use Home Assistant ingress to open OpenCode.") from None
    if peer != TRUSTED_INGRESS:
        raise web.HTTPForbidden(text="Use Home Assistant ingress to open OpenCode.")
    username = request.headers.get("X-Remote-User-Name", "")
    if not username.strip():
        raise web.HTTPInternalServerError(text="X-Remote-User-Name is empty. Home Assistant must supply a user name.")
    check_username(username)
    prefix = request.headers.get("X-Ingress-Path", "")
    if not re.fullmatch(r"/api/hassio_ingress/[A-Za-z0-9_-]+", prefix):
        raise web.HTTPBadRequest(text="X-Ingress-Path must contain a Home Assistant ingress path.")
    session = request.cookies.get("ingress_session", "")
    if not session:
        raise web.HTTPUnauthorized(text="The Home Assistant ingress session is missing.")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).lower()
    host = request.headers.get("X-Forwarded-Host", request.host)
    origin = f"{scheme}://{host}"
    parsed = urlsplit(origin)
    if (scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or any(char.isspace() for char in host)):
        raise web.HTTPBadRequest(text="The public Home Assistant URL is invalid.")
    return username, prefix, origin, session


def proxy_headers(headers, *, response=False) -> CIMultiDict:
    connection = {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    blocked = HOP_HEADERS | connection
    if response:
        blocked |= {"set-cookie", "www-authenticate"}
    else:
        blocked |= PRIVATE_HEADERS | {"accept-encoding", "content-length"}
    result = CIMultiDict()
    for name, value in headers.items():
        lower = name.lower()
        if lower in blocked or (not response and lower.startswith(("x-remote-", "x-forwarded-", "x-ingress-"))):
            continue
        result.add(name, value)
    return result


class Gateway:
    """Validate ingress requests and stream them to each user's server."""

    def __init__(self, supervisor: Supervisor, instances: Instances,
                 client: aiohttp.ClientSession, web_dir: Path = APP_DIR / "web"):
        self.supervisor = supervisor
        self.instances = instances
        self.client = client
        self.web_dir = web_dir
        self.warned_core_only = False

    async def validate(self, session: str):
        try:
            valid = await self.supervisor.validate_session(session)
        except SupervisorCoreOnly:
            if not self.warned_core_only:
                LOG.warning("Supervisor restricts session checks to Core. Use the session check from trusted ingress.")
                self.warned_core_only = True
            return
        except SupervisorError:
            raise web.HTTPServiceUnavailable(text="The Home Assistant session check is unavailable.") from None
        if not valid:
            raise web.HTTPUnauthorized(text="The Home Assistant ingress session is invalid.")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        username, prefix, origin, session = ingress_context(request)
        await self.validate(session)
        user_id, username = self.instances.store.upsert(username)
        self.instances.seen_users[username] = user_id
        try:
            instance = await self.instances.ensure(user_id, username, prefix, origin)
        except (OSError, ValueError, RuntimeError):
            LOG.exception("Cannot start OpenCode for user %s.", username)
            raise web.HTTPServiceUnavailable(text="Cannot start OpenCode. Check the app log and sandbox support.") from None
        raw_path = request.rel_url.raw_path
        if raw_path == prefix:
            raise web.HTTPPermanentRedirect(location=prefix + "/")
        if raw_path.startswith(prefix + "/"):
            raw_path = raw_path[len(prefix):]
        if request.rel_url.raw_query_string:
            raw_path += "?" + request.rel_url.raw_query_string
        target = URL(f"http://127.0.0.1:{instance.port}" + raw_path, encoded=True)
        headers = proxy_headers(request.headers)
        headers["Accept-Encoding"] = "identity"
        headers["Authorization"] = aiohttp.encode_basic_auth("opencode", instance.password)
        headers["X-Forwarded-Prefix"] = prefix
        headers["X-Forwarded-Proto"] = urlsplit(origin).scheme
        headers["X-Forwarded-Host"] = urlsplit(origin).netloc
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request, target, headers, instance, session)
        if request.method in {"GET", "HEAD"}:
            local = self.static_response(request, raw_path.split("?", 1)[0], prefix, origin)
            if local is not None:
                return local
        return await self.http(request, target, headers, instance, prefix, origin)

    def static_response(self, request: web.Request, path: str, prefix: str, origin: str):
        relative = path.lstrip("/")
        candidate = (self.web_dir / relative).resolve()
        if candidate.is_relative_to(self.web_dir.resolve()) and candidate.is_file():
            if candidate.name != "index.html":
                return web.FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        elif "text/html" not in request.headers.get("Accept", "") and path not in {"/", "/index.html"}:
            return None
        index = self.web_dir / "index.html"
        if not index.is_file():
            raise web.HTTPServiceUnavailable(text="The OpenCode web interface is missing from the image.")
        return web.Response(body=transform_html(index.read_bytes(), prefix, origin + prefix),
                            content_type="text/html", headers={"Cache-Control": "no-store"})

    async def http(self, request, target, headers, instance, prefix, origin):
        response = None
        try:
            async with self.client.request(
                request.method, target, headers=headers,
                data=request.content.iter_chunked(256 * 1024) if request.can_read_body else None,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            ) as upstream:
                outgoing = proxy_headers(upstream.headers, response=True)
                location = outgoing.get("Location")
                if location:
                    internal = f"http://127.0.0.1:{instance.port}"
                    if location.startswith(internal + "/"):
                        outgoing["Location"] = origin + prefix + location[len(internal):]
                    elif location.startswith("/") and not location.startswith("//"):
                        outgoing["Location"] = prefix + location
                response = web.StreamResponse(status=upstream.status, headers=outgoing)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            raise web.HTTPBadGateway(text="The OpenCode server connection failed.") from None

    async def websocket(self, request, target, headers, instance, session):
        protocols = [part.strip() for part in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if part.strip()]
        for name in list(headers):
            if name.lower().startswith("sec-websocket-"):
                del headers[name]
        try:
            upstream = await self.client.ws_connect(
                target, headers=headers,
                protocols=protocols, max_msg_size=0, autoping=True,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text="The OpenCode WebSocket connection failed.") from None
        downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else (),
                                            max_msg_size=0, autoping=True)
        try:
            await downstream.prepare(request)
        except BaseException:
            await upstream.close()
            raise

        async def relay(source, destination):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await destination.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await destination.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break

        async def check_session():
            while True:
                await asyncio.sleep(30)
                try:
                    await self.validate(session)
                except web.HTTPException:
                    await downstream.close(code=1008, message=b"The ingress session is no longer valid.")
                    return

        tasks = [asyncio.create_task(relay(downstream, upstream)),
                 asyncio.create_task(relay(upstream, downstream)),
                 asyncio.create_task(check_session())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
        return downstream


GATEWAY_KEY = web.AppKey("gateway", Gateway)
WATCHDOG_KEY = web.AppKey("watchdog", WatchdogMonitor)


async def create_app(options_path=Path("/data/options.json"), data_dir=Path("/data")):
    options = read_options(options_path)
    token = os.environ.pop("SUPERVISOR_TOKEN", "")
    if not token:
        raise ValueError("SUPERVISOR_TOKEN is required. Start this app through Home Assistant.")
    app = web.Application(client_max_size=0)
    app[WATCHDOG_KEY] = WatchdogMonitor()

    async def lifecycle(app):
        store = UserStore(data_dir)
        client = aiohttp.ClientSession(auto_decompress=False, trust_env=False,
                                       cookie_jar=aiohttp.DummyCookieJar())
        supervisor = Supervisor(client, token)
        instances = None
        try:
            overlay = await supervisor.startup(options["litellm_discovery"])
            instances = Instances(store, client, overlay)
            app[GATEWAY_KEY] = Gateway(supervisor, instances, client)
            if not options["lazy_start"]:
                for user_id, username in store.all():
                    try:
                        await instances.ensure(user_id, username, "", "")
                    except (OSError, ValueError, RuntimeError):
                        LOG.exception("Cannot start the saved OpenCode user %s.", username)
            yield
        finally:
            app[WATCHDOG_KEY].close()
            if instances:
                await instances.close()
            await client.close()
            store.close()

    async def handle(request):
        return await app[GATEWAY_KEY].handle(request)

    async def watchdog(request):
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        try:
            peer = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden(text="Only Supervisor can call the app watchdog.") from None
        if peer != TRUSTED_INGRESS or "X-Ingress-Path" in request.headers:
            raise web.HTTPForbidden(text="Only Supervisor can call the app watchdog.")
        app[WATCHDOG_KEY].received()
        return web.Response(text="ok")

    app.cleanup_ctx.append(lifecycle)
    app.router.add_route("*", WATCHDOG_PATH, watchdog)
    app.router.add_route("*", "/{path:.*}", handle)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    os.umask(0o077)
    try:
        web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)
    except (OSError, ValueError, SupervisorError):
        LOG.exception("Cannot start the OpenCode app.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
