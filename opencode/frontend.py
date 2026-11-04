"""Set the public URL for the OpenCode web interface."""

from html import escape
import json
import re
from urllib.parse import urlsplit, urlunsplit


MAX_HTML_BYTES = 2 * 1024 * 1024
_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BASE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_RESOURCE = re.compile(
    r"(?P<attribute>(?<![\w:-])(?:src|href|poster|action)\s*=\s*)(?P<quote>[\"'])(?P<path>/(?!/)[^\"']*)(?P=quote)",
    re.IGNORECASE,
)


_SCRIPT = r"""
(() => {
  const config = __CONFIG__;
  window.__OPENCODE_INGRESS__ = config;
  if (!config.prefix) return;
  const route = (value) => {
    const url = new URL(value, document.baseURI);
    if (!["http:", "https:", "ws:", "wss:"].includes(url.protocol)) return String(value);
    if (url.host !== location.host) return String(value);
    if (url.pathname !== config.prefix && !url.pathname.startsWith(config.prefix + "/")) {
      url.pathname = config.prefix + url.pathname;
    }
    return url.href;
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (input instanceof Request) {
      const url = route(input.url);
      return nativeFetch(url === input.url ? input : new Request(url, input), init);
    }
    return nativeFetch(route(input), init);
  };
  for (const name of ["WebSocket", "EventSource"]) {
    const Native = window[name];
    if (!Native) continue;
    window[name] = class extends Native {
      constructor(url, options) {
        if (options === undefined) super(route(url));
        else super(route(url), options);
      }
    };
  }
  const nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...options) {
    return nativeOpen.call(this, method, route(url), ...options);
  };
})();
""".strip()


def transform_html(body: bytes, prefix: str, public_url: str) -> bytes:
    """Add the ingress path before the web interface starts."""
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("The OpenCode HTML document is too large")
    if (
        not prefix.startswith("/")
        or prefix.startswith("//")
        or any(character in prefix for character in "?#\\")
        or any(ord(character) < 32 for character in prefix)
        or any(part in {".", ".."} for part in prefix.split("/"))
    ):
        raise ValueError("The ingress path is not valid")
    prefix = prefix.rstrip("/")
    url = urlsplit(public_url)
    if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
        raise ValueError("The public URL is not valid")
    config = json.dumps(
        {"prefix": prefix, "url": urlunsplit((url.scheme, url.netloc, prefix, "", ""))},
        separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = _BASE.sub("", body.decode("utf-8"))

    def resource(match: re.Match) -> str:
        path = match["path"]
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            path = prefix + path
        return match["attribute"] + match["quote"] + path + match["quote"]

    html = _RESOURCE.sub(resource, html)
    insertion = (
        '<base href="' + escape(prefix + "/", quote=True) + '">'
        '<script id="opencode-ingress">' + _SCRIPT.replace("__CONFIG__", config) + "</script>"
    )
    html, count = _HEAD.subn(lambda match: match[0] + insertion, html, count=1)
    if count != 1:
        raise ValueError("The OpenCode HTML document has no head element")
    return html.encode("utf-8")
