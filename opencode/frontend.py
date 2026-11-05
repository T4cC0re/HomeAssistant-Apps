"""Rewrite OpenCode response URLs for Home Assistant ingress."""

import hashlib
from html import escape
import re
from urllib.parse import urlsplit, urlunsplit


MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_SCRIPT_BYTES = 1024 * 1024
SHIM_PATH = "/_ha/ingress.js"
_INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]+\Z")
_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_ENTRY = re.compile(r"/_assets/index-[A-Za-z0-9_-]+\.js")
_BUNDLE = re.compile(r"/_assets/(?:index|current)-[A-Za-z0-9_-]+\.js\Z")
_RESOURCE = re.compile(
    r"(?P<attribute>(?<![\w:-])(?:src|href|action|poster)\s*=\s*)"
    r"(?P<quote>[\"'])(?P<path>/(?!/)[^\"']*)(?P=quote)",
    re.IGNORECASE,
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_INDEX = "/_assets/index-BDq5uAY5.js"
_CURRENT = "/_assets/current-CS21lgrW.js"
_BUNDLES = {
    _INDEX: (
        "eb530039f18c528f73d1e48e1a967b59388e16c6f6ab1d54129f50cd087ce0ef",
        (
            (
                b"get component(){return e.router??ss},root:t,",
                b"get component(){return e.router??ss},root:t,get base(){return new URL(document.baseURI).pathname.replace(/\\/$/,``)},",
            ),
            (b"navigator.serviceWorker.register(`/sw.js`)", b"Promise.resolve()"),
        ),
    ),
    _CURRENT: (
        "a1027a0658e7b8307bef9a6fc2e023da1fd3f732a77770bbf65059ec7414576d",
        (
            (
                b"xn=()=>yn().location",
                b"xn=()=>new Proxy(yn().location,{get(e,t){let n=Reflect.get(e,t);if(t!==`pathname`)return n;let r=new URL(document.baseURI).pathname.replace(/\\/$/,``);return n===r?`/`:r&&n.startsWith(r+`/`)?n.slice(r.length):n}})",
            ),
            (
                b"return o(new Request(n,{signal:p.signal})).finally(",
                b"return o(e,{...t,signal:p.signal}).finally(",
            ),
        ),
    ),
}


INGRESS_SCRIPT = r"""(() => {
  const prefix = new URL(document.baseURI).pathname.replace(/\/$/, "");
  if (!/^\/api\/hassio_ingress\/[A-Za-z0-9_-]+$/.test(prefix)) return;
  const rewrite = (value, websocket = false) => {
    let url;
    try { url = new URL(value, document.baseURI); } catch { return value; }
    if (url.host !== location.host) return value;
    if (websocket ? !["ws:", "wss:"].includes(url.protocol) : url.origin !== location.origin) return value;
    if (url.pathname === prefix || url.pathname.startsWith(prefix + "/") ||
        url.pathname.startsWith("/api/hassio_ingress/")) return value;
    url.pathname = prefix + url.pathname;
    return url.href;
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const original = input instanceof Request ? input.url : input;
    const url = rewrite(original);
    let ingress = false;
    try {
      const target = new URL(url, document.baseURI);
      ingress = target.origin === location.origin &&
        (target.pathname === prefix || target.pathname.startsWith(prefix + "/"));
    } catch {}
    const options = ingress ? { ...init, credentials: "same-origin" } : init;
    if (input instanceof Request) {
      return nativeFetch(url === input.url ? input : new Request(url, input), options);
    }
    return nativeFetch(url, options);
  };
  for (const name of ["WebSocket", "EventSource"]) {
    const Native = window[name];
    if (!Native) continue;
    window[name] = class extends Native {
      constructor(url, options) {
        const routed = rewrite(url, name === "WebSocket");
        if (options === undefined) super(routed);
        else super(routed, options);
      }
    };
  }
  const NativeWorker = window.Worker;
  if (NativeWorker) {
    window.Worker = class extends NativeWorker {
      constructor(url, options) {
        const routed = rewrite(url);
        if (options === undefined) super(routed);
        else super(routed, options);
      }
    };
  }
  const nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...options) {
    return nativeOpen.call(this, method, rewrite(url), ...options);
  };
  const linkHref = Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype, "href");
  Object.defineProperty(HTMLLinkElement.prototype, "href", {
    ...linkHref,
    set(value) { linkHref.set.call(this, rewrite(value)); }
  });
  document.addEventListener("load", event => {
    if (!(event.target instanceof HTMLLinkElement) || !event.target.sheet) return;
    try {
      for (const rule of event.target.sheet.cssRules) {
        if (rule.type !== CSSRule.FONT_FACE_RULE) continue;
        const source = rule.style.getPropertyValue("src");
        const routed = source.replace(/url\(([^)]+)\)/g, (match, raw) => {
          const value = raw.trim().replace(/^["']|["']$/g, "");
          return 'url("' + rewrite(value) + '")';
        });
        if (routed !== source) rule.style.setProperty("src", routed);
      }
    } catch { /* Ignore style sheets that the browser does not expose. */ }
  }, true);
})();
""".encode("utf-8")


def is_bundle_candidate(path: str) -> bool:
    """Select the two OpenCode UI bundles by their asset names."""
    return bool(_BUNDLE.fullmatch(path)) and not path.startswith("/_assets/current-tool-state-")


def adapt_bundle(path: str, body: bytes) -> bytes:
    """Adapt only the checked OpenCode 2.2.5 UI bundles."""
    if path not in _BUNDLES or len(body) > MAX_SCRIPT_BYTES:
        raise ValueError("The OpenCode UI bundle name or size changed.")
    expected_hash, replacements = _BUNDLES[path]
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise ValueError("The OpenCode UI bundle hash changed.")
    for original, replacement in replacements:
        if body.count(original) != 1:
            raise ValueError("The OpenCode UI bundle structure changed.")
        body = body.replace(original, replacement, 1)
    return body


def rewrite_html(body: bytes, prefix: str) -> bytes:
    """Add the current ingress path to HTML links and relative URLs."""
    if not _INGRESS_PATH.fullmatch(prefix):
        raise ValueError("The ingress path is not valid.")
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("The OpenCode HTML document is too large.")
    html = body.decode("utf-8")
    entries = set(_ENTRY.findall(html))
    if entries and entries != {_INDEX}:
        raise ValueError("The OpenCode UI entry bundle changed.")

    def resource(match: re.Match) -> str:
        path = match["path"]
        if path == prefix or path.startswith(prefix + "/"):
            return match[0]
        return match["attribute"] + match["quote"] + prefix + path + match["quote"]

    html = _RESOURCE.sub(resource, html)
    base = '<base href="' + escape(prefix + "/", quote=True) + '">'
    script = '<script src="' + escape(prefix + SHIM_PATH, quote=True) + '"></script>'
    html, count = _HEAD.subn(lambda match: match[0] + base + script, html, count=1)
    if count != 1:
        raise ValueError("The OpenCode HTML document has no head element.")
    return html.encode("utf-8")


def rewrite_location(location: str, prefix: str, origin: str, backend_port: int) -> str:
    """Keep app redirects under the current Home Assistant ingress path."""
    if not _INGRESS_PATH.fullmatch(prefix):
        raise ValueError("The ingress path is not valid.")
    try:
        target = urlsplit(location)
        public = urlsplit(origin)
        is_loopback = (
            target.scheme == "http" and target.hostname in _LOOPBACK_HOSTS
            and target.port == backend_port
        )
    except ValueError:
        return location

    is_public = (
        target.scheme == public.scheme and target.netloc.lower() == public.netloc.lower()
    )
    is_root_relative = location.startswith("/") and not location.startswith("//")
    if not (is_loopback or is_public or is_root_relative):
        return location

    path = target.path or "/"
    if path == prefix or path.startswith(prefix + "/"):
        if is_loopback:
            return origin + urlunsplit(("", "", path, target.query, target.fragment))
        return location
    path = prefix + path
    suffix = urlunsplit(("", "", path, target.query, target.fragment))
    return suffix if is_root_relative else origin + suffix
