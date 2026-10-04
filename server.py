"""
server.py — FastAPI application: WebSocket caption server + HLS proxy.

Endpoints
---------
GET  /                  → static/index.html
GET  /static/{path}     → static files
WS   /captions          → pushes caption JSON to every connected browser
GET  /proxy/playlist    → the configured stream's playlist (URLs rewritten)
GET  /proxy/segment     → segments / sub-playlists referenced by that playlist
GET  /health            → health check

WebSocket message format (server → client)::

    {"pt": "...", "es": "...", "age_start": 7.4, "age_end": 4.1}

(ages are seconds before "now" at the live edge — see captions.py)

HLS proxy security
------------------
The stream host may not send CORS headers, so the browser fetches everything
through this server.  A proxy that fetches arbitrary URLs would be an open
relay into your network (SSRF), so:

* /proxy/playlist takes NO url parameter — it always serves STREAM_URL.
* Every URL the playlist rewriter emits carries an HMAC signature made with a
  random per-process secret; /proxy/segment rejects anything unsigned.
* Independently, every outgoing request (including each redirect hop) must go
  to the stream's host or to a host in PROXY_ALLOWED_HOSTS (comma separated;
  an entry starting with "." matches subdomains, e.g. ".logicahost.com.br").
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from capture import DEFAULT_STREAM_URL  # noqa: E402  (single definition; re-exported here)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"

_SECRET = secrets.token_bytes(32)


def stream_url() -> str:
    """Read at request time so --stream-url / env changes are honoured."""
    return os.environ.get("STREAM_URL", DEFAULT_STREAM_URL)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

_SEND_TIMEOUT = 2.0    # a client that can't take a message within this is "slow"
_CLOSE_TIMEOUT = 2.0   # ...and we don't wait longer than this for it to close


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._closing: set[asyncio.Task] = set()   # keeps background closes alive

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.info("WS client connected (total=%d)", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.discard(ws)
            logger.info("WS client disconnected (total=%d)", len(self._connections))

    async def _close_quietly(self, ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.close(code=1011), timeout=_CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001 — already closed / hung: nothing more to do
            pass

    def _drop(self, ws: WebSocket) -> None:
        """Forget a client that failed a send AND close its socket.

        Removing it from the set alone would leave the connection open: its handler
        keeps pinging it, so the browser shows "connected" but never receives
        another caption.  Closing makes the page's onclose handler reconnect.
        Closing runs in the background so a hung client can't delay the broadcast.
        """
        self.disconnect(ws)
        task = asyncio.ensure_future(self._close_quietly(ws))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return
        text = json.dumps(message, ensure_ascii=False)

        async def _send(ws: WebSocket):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=_SEND_TIMEOUT)
                return None
            except Exception:  # noqa: BLE001
                return ws

        # concurrently, so one slow client can't delay the others
        for dead in await asyncio.gather(*(_send(ws) for ws in list(self._connections))):
            if dead is not None:
                self._drop(dead)

    @property
    def count(self) -> int:
        return len(self._connections)


_manager = ConnectionManager()


async def push_caption(caption: dict) -> None:
    await _manager.broadcast(caption)


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

def _sign(url: str) -> str:
    return hmac.new(_SECRET, url.encode(), hashlib.sha256).hexdigest()[:32]


def _allowed_hosts() -> list[str]:
    hosts = [h.strip().lower() for h in os.environ.get("PROXY_ALLOWED_HOSTS", "").split(",") if h.strip()]
    stream_host = urllib.parse.urlparse(stream_url()).hostname
    if stream_host:
        hosts.append(stream_host.lower())
    return hosts


def host_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == a or (a.startswith(".") and host.endswith(a)) for a in _allowed_hosts())


def _proxied(abs_url: str) -> str:
    return "/proxy/segment?url={}&sig={}".format(urllib.parse.quote(abs_url, safe=""), _sign(abs_url))


_URI_ATTR = re.compile(r'URI="([^"]*)"')


def rewrite_playlist(content: str, original_url: str) -> str:
    """Rewrite every URL in an M3U8 (URI lines *and* URI="..." attributes such
    as EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA) to a signed /proxy/segment URL."""
    out = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(_URI_ATTR.sub(
                lambda m: 'URI="{}"'.format(_proxied(urllib.parse.urljoin(original_url, m.group(1)))),
                line,
            ))
        else:
            out.append(_proxied(urllib.parse.urljoin(original_url, stripped)))
    return "\n".join(out) + "\n"


async def _block_foreign_hosts(request: httpx.Request) -> None:
    """httpx request hook: runs for the first request AND every redirect hop."""
    if not host_allowed(str(request.url)):
        raise httpx.RequestError(f"host not allowed: {request.url.host}", request=request)


_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TVCidade10SubtitleProxy/1.0)"},
            event_hooks={"request": [_block_foreign_hosts]},
        )
    return _http_client


def _cors(extra: dict | None = None) -> dict:
    return {"Access-Control-Allow-Origin": "*", **(extra or {})}


async def _fetch(url: str) -> httpx.Response | Response:
    """GET `url` through the guarded client; returns an error Response on failure."""
    if not host_allowed(url):
        logger.warning("Proxy blocked host for %s", url)
        return Response(status_code=403, content=b"Host not allowed (see PROXY_ALLOWED_HOSTS)")
    try:
        resp = await _client().get(url)
        resp.raise_for_status()
        return resp
    except httpx.HTTPStatusError as exc:
        sc = exc.response.status_code
        if sc == 404:
            # Return 503 so hls.js keeps retrying (404 makes it give up entirely).
            # Include a recognisable body so the page can show a clear message.
            return Response(status_code=503, content=b"Stream offline")
        return Response(status_code=sc, content=b"Upstream error")
    except httpx.HTTPError as exc:
        logger.error("Proxy fetch error: %s", exc)
        return Response(status_code=502, content=b"Bad Gateway")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        global _http_client
        yield
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None

    app = FastAPI(title="TV Cidade 10 — Live Subtitles", lifespan=lifespan)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        path = STATIC_DIR / "index.html"
        if path.exists():
            return HTMLResponse(content=path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Player not found — check static/index.html</h1>", status_code=404)

    @app.websocket("/captions")
    async def captions_ws(ws: WebSocket):
        await _manager.connect(ws)
        try:
            while True:
                try:
                    # we only send, but reading lets us notice disconnects at once
                    await asyncio.wait_for(ws.receive_text(), timeout=30)
                except asyncio.TimeoutError:
                    # bounded: a stuck client must not park this handler forever
                    await asyncio.wait_for(ws.send_text('{"ping":true}'), timeout=_SEND_TIMEOUT)
        except Exception:  # noqa: BLE001  (also covers WebSocketDisconnect)
            pass
        finally:
            _manager.disconnect(ws)

    @app.get("/proxy/playlist")
    async def proxy_playlist():
        target = stream_url()
        resp = await _fetch(target)
        if isinstance(resp, Response):
            return resp
        return Response(
            content=rewrite_playlist(resp.text, str(resp.url)).encode(),
            media_type=PLAYLIST_TYPE,
            headers=_cors({"Cache-Control": "no-cache"}),
        )

    @app.get("/proxy/segment")
    async def proxy_segment(url: str, sig: str = ""):
        if not hmac.compare_digest(sig.encode(), _sign(url).encode()):
            return Response(status_code=403, content=b"Bad signature")
        resp = await _fetch(url)
        if isinstance(resp, Response):
            return resp

        ctype = resp.headers.get("content-type", "video/mp2t")
        is_playlist = "mpegurl" in ctype.lower() or urllib.parse.urlparse(url).path.endswith(".m3u8")
        if is_playlist:
            return Response(
                content=rewrite_playlist(resp.text, str(resp.url)).encode(),
                media_type=PLAYLIST_TYPE,
                headers=_cors({"Cache-Control": "no-cache"}),
            )
        return Response(content=resp.content, media_type=ctype, headers=_cors())

    @app.get("/health")
    async def health():
        return {"status": "ok", "ws_clients": _manager.count}

    return app
