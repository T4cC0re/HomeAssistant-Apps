"""Check the built web interface with a real OpenCode server and browser."""

import argparse
import asyncio
import ipaddress
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


async def run(args):
    with tempfile.TemporaryDirectory(prefix="opencode-browser-smoke-") as directory:
        data = Path(directory)
        home = data / "Smoke User"
        home.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            backend_port = listener.getsockname()[1]
        password = secrets.token_urlsafe(32)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "OPENCODE_SERVER_PASSWORD": password, "OPENCODE_DISABLE_AUTOUPDATE": "true",
        }
        process = await asyncio.create_subprocess_exec(
            str(args.opencode.resolve()), "serve", "--hostname", "127.0.0.1", "--port", str(backend_port),
            cwd=home, env=environment, stdout=asyncio.subprocess.DEVNULL,
        )
        store = main.UserStore(data)
        try:
            async with aiohttp.ClientSession(auto_decompress=False) as client:
                for _ in range(300):
                    if process.returncode is not None:
                        raise RuntimeError("The OpenCode server stopped during startup")
                    try:
                        async with client.get(
                            f"http://127.0.0.1:{backend_port}/global/health",
                            auth=aiohttp.BasicAuth("opencode", password),
                            timeout=aiohttp.ClientTimeout(total=1),
                        ) as response:
                            if response.status == 200:
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError("The OpenCode server did not become ready")

                async def validate_session(session):
                    return session == "smoke-session"

                async def ensure(*_args):
                    return SimpleNamespace(port=backend_port, password=password)

                manager = SimpleNamespace(store=store, ensure=ensure, seen_users={})
                gateway = main.Gateway(SimpleNamespace(validate_session=validate_session), manager, client, args.web_dir)
                app = web.Application(client_max_size=0)
                app.router.add_route("*", "/{path:.*}", gateway.handle)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner, "127.0.0.1", 0).start()
                    origin = "http://127.0.0.1:" + str(runner.addresses[0][1])
                    browser_environment = {
                        **os.environ, "OPENCODE_SMOKE_ORIGIN": origin,
                        "OPENCODE_SMOKE_DIRECTORY": str(home),
                        "PLAYWRIGHT_MODULE": str(args.playwright.resolve()),
                    }
                    if args.browser:
                        browser_environment["OPENCODE_SMOKE_BROWSER"] = str(args.browser.resolve())
                    browser = await asyncio.create_subprocess_exec(
                        shutil.which("node") or "node", str(Path(__file__).with_suffix(".mjs")),
                        env=browser_environment,
                    )
                    if await browser.wait():
                        raise RuntimeError("The browser smoke test failed")
                finally:
                    await runner.cleanup()
        finally:
            store.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-dir", type=Path, required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--playwright", type=Path, required=True, help="Path to the Playwright index.mjs file")
    parser.add_argument("--browser", type=Path, help="Path to the browser executable")
    main.TRUSTED_INGRESS = ipaddress.ip_address("127.0.0.1")
    asyncio.run(run(parser.parse_args()))
