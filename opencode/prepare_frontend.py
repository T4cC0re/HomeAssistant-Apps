"""Prepare the OpenCode 1.19.5 web interface for Home Assistant ingress."""

import json
from pathlib import Path
import re
import sys


VERSION = "1.19.5"
LOCATION_FILES = (
    "components/debug-bar.tsx",
    "components/titlebar.tsx",
    "context/layout.tsx",
    "context/tabs.tsx",
    "pages/directory-layout.tsx",
    "pages/session.tsx",
    "pages/session/use-session-hash-scroll.ts",
)
LOCATION_SOURCE = '''import { useLocation as useRouterLocation } from "@solidjs/router"

export function useLocation() {
  const location = useRouterLocation()
  return new Proxy(location, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, receiver)
      const prefix = window.__OPENCODE_INGRESS__?.prefix
      if (key !== "pathname" || !prefix) return value
      if (value === prefix) return "/"
      return value.startsWith(prefix + "/") ? value.slice(prefix.length) : value
    },
  })
}
'''


def replace_once(path: Path, before: str, after: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(before) != 1:
        raise ValueError(f"The OpenCode source does not match the expected text: {path}")
    path.write_text(source.replace(before, after, 1), encoding="utf-8")


def prepare_frontend(root: Path) -> None:
    """Set the router base and API URL from the ingress configuration."""
    app = root / "packages/app"
    package = json.loads((app / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != VERSION:
        raise ValueError(f"The OpenCode web interface must have version {VERSION}")
    replace_once(
        app / "src/entry.tsx",
        'const DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:defaultServerUrl"',
        'declare global {\n'
        '  interface Window {\n'
        '    __OPENCODE_INGRESS__?: { prefix: string; url: string }\n'
        '  }\n'
        '}\n\n'
        'const DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:defaultServerUrl"',
    )
    replace_once(
        app / "src/entry.tsx",
        "  return location.origin\n",
        "  return window.__OPENCODE_INGRESS__?.url ?? location.origin\n",
    )
    replace_once(
        app / "src/app.tsx",
        "                component={props.router ?? Router}\n",
        '                component={props.router ?? Router}\n'
        '                base={window.__OPENCODE_INGRESS__?.prefix ?? ""}\n',
    )
    for name in LOCATION_FILES:
        path = app / "src" / name
        source = path.read_text(encoding="utf-8")

        def location_import(match: re.Match) -> str:
            names = [name.strip() for name in match[1].split(",")]
            if names.count("useLocation") != 1:
                raise ValueError(f"The OpenCode location import does not match the expected text: {path}")
            names.remove("useLocation")
            router = 'import { ' + ", ".join(names) + ' } from "@solidjs/router"\n' if names else ""
            return router + 'import { useLocation } from "@/utils/ingress-location"'

        source, count = re.subn(r'import \{([^}]+)\} from "@solidjs/router"', location_import, source)
        if count != 1:
            raise ValueError(f"The OpenCode location import does not match the expected text: {path}")
        path.write_text(source, encoding="utf-8")
    (app / "src/utils/ingress-location.ts").write_text(LOCATION_SOURCE, encoding="utf-8")
    favicon = root / "packages/ui/src/components/favicon.tsx"
    source = favicon.read_text(encoding="utf-8")
    if source.count('href="/') != 4:
        raise ValueError("The OpenCode favicon source does not match the expected text")
    favicon.write_text(source.replace('href="/', 'href="./'), encoding="utf-8")
    manifest = app / "public/site.webmanifest"
    settings = json.loads(manifest.read_text(encoding="utf-8"))
    for key in ("id", "start_url", "scope"):
        settings[key] = "./"
    for icon in settings["icons"]:
        icon["src"] = "./" + icon["src"].lstrip("/")
    manifest.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: prepare_frontend.py SOURCE_DIRECTORY")
    prepare_frontend(Path(sys.argv[1]))
