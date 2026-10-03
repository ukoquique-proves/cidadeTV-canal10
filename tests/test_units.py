"""
Unit tests — no models, no network.   Run:  python -m pytest tests -q
"""
import asyncio
import http.server
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import captions  # noqa: E402
from capture import BYTES_PER_SEC, AudioClock  # noqa: E402
from vad import EnergyDetector, VadSegmenter, WINDOW_BYTES  # noqa: E402


# ── AudioClock ───────────────────────────────────────────────────────────────

def test_clock_uses_minimum_offset_of_bursty_arrivals():
    """A 6 s segment arrives at once. Reading its first bytes looks 'late';
    the last byte gives the true offset, and the min filter picks it."""
    c = AudioClock()
    true_offset = 1000.0                       # wall = audio + 1000
    for seg in range(5):
        seg_end = (seg + 1) * 6.0
        wall = seg_end + true_offset           # whole segment arrives when it ends
        for frac in (0.25, 0.5, 0.75, 1.0):    # several reads of the same burst
            c.observe(seg_end - 6.0 + 6.0 * frac, wall=wall)
    assert c.offset == pytest.approx(true_offset)
    assert c.live_time(10.0) == pytest.approx(1010.0)


def test_clock_empty_and_reset():
    c = AudioClock()
    assert c.offset is None and c.live_time(5) is None
    c.observe(1.0, wall=100.0)
    c.reset()
    assert c.offset is None


# ── VAD on synthetic audio ───────────────────────────────────────────────────

def _tone(seconds, amp=0.3):
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 300 * t) * amp * 32767).astype(np.int16).tobytes()


def _silence(seconds):
    return b"\x00\x00" * int(16000 * seconds)


async def _run_vad(pcm: bytes, chunk_ms=100, drop_chunks=(), markers=()):
    inq, outq = asyncio.Queue(), asyncio.Queue()
    vad = VadSegmenter(inq, outq, detector=EnergyDetector())
    task = vad.start()
    n = 3200 * chunk_ms // 100
    for i, off in enumerate(range(0, len(pcm), n)):
        if i in markers:
            await inq.put((off, None))
        if i in drop_chunks:
            continue
        await inq.put((off, pcm[off:off + n]))
    await asyncio.sleep(0.3)
    await vad.stop()
    items = []
    while not outq.empty():
        items.append(outq.get_nowait())
    return items


def test_vad_segments_start_times_and_no_audio_loss():
    # 1 s silence | 2 s speech | 1.5 s silence | 3 s speech | 2 s silence
    pcm = _silence(1) + _tone(2) + _silence(1.5) + _tone(3) + _silence(2)
    segs = asyncio.run(_run_vad(pcm))
    assert len(segs) == 2
    (a, a_start, _), (b, b_start, _) = segs
    # onset at 1.0 s and 4.5 s; pre-roll adds up to 300 ms before the onset
    assert 0.65 <= a_start <= 1.05
    assert 4.15 <= b_start <= 4.55
    # whole 2 s / 3 s of speech present (+ pre-roll, + <=0.5 s hang)
    assert 2.0 <= len(a) / BYTES_PER_SEC <= 2.9
    assert 3.0 <= len(b) / BYTES_PER_SEC <= 3.9


def test_vad_forces_cut_on_long_speech():
    pcm = _silence(0.5) + _tone(20) + _silence(1)
    segs = asyncio.run(_run_vad(pcm))
    assert len(segs) >= 3                          # 20 s with 8 s max → >= 3 segments
    assert all(len(s[0]) / BYTES_PER_SEC <= 8.2 for s in segs)
    covered = sum(len(s[0]) for s in segs) / BYTES_PER_SEC
    assert covered >= 19.5                         # nothing lost at the cuts


def test_vad_handles_gap_in_audio_and_marker():
    pcm = _silence(0.5) + _tone(3) + _silence(1) + _tone(3) + _silence(1)
    # drop 2 chunks in the middle of the first tone (queue overflow) → segment ends there
    segs = asyncio.run(_run_vad(pcm, drop_chunks=(10, 11)))
    assert len(segs) >= 2
    # a restart marker flushes and resets without crashing
    segs2 = asyncio.run(_run_vad(pcm, markers=(25,)))
    assert len(segs2) >= 1


# ── captions.py ──────────────────────────────────────────────────────────────

def test_expand_splits_long_text_and_distributes_time():
    pt = "Esta é uma frase bastante longa que o apresentador diz sem parar, e que precisa ser dividida em legendas menores."
    es = "Esta es una frase bastante larga que el presentador dice sin parar, y que necesita dividirse en subtítulos más pequeños."
    item = {"pt": pt, "es": es, "audio_start": 10.0, "audio_end": 16.0}
    parts = captions.expand(item)
    assert len(parts) >= 2
    assert " ".join(p["pt"] for p in parts) == pt        # no words lost / reordered
    assert " ".join(p["es"] for p in parts) == es
    assert parts[0]["audio_start"] == pytest.approx(10.0)
    assert parts[-1]["audio_end"] == pytest.approx(16.0)
    for a, b in zip(parts, parts[1:]):
        assert a["audio_end"] == pytest.approx(b["audio_start"])
    assert all(len(p["pt"]) <= captions.MAX_CHARS_PER_CAPTION + 15 for p in parts)


def test_expand_short_text_is_untouched():
    item = {"pt": "Bom dia.", "es": "Buenos días.", "audio_start": 1.0, "audio_end": 2.0}
    assert captions.expand(item) == [item]


def test_to_message_ages():
    item = {"pt": "Olá", "es": "Hola", "audio_start": 100.0, "audio_end": 103.0}
    live = lambda t: t + 5000.0                  # wall = audio + 5000
    msg = captions.to_message(item, live, now=5107.0)
    assert msg["age_start"] == pytest.approx(7.0)
    assert msg["age_end"] == pytest.approx(4.0)
    # no clock yet → show immediately
    assert captions.to_message(item, lambda t: None)["age_start"] == 0.0


# ── server: playlist rewriting + proxy security ──────────────────────────────

from fastapi.testclient import TestClient  # noqa: E402
import server  # noqa: E402


def test_rewrite_playlist_urljoin_and_uri_attributes():
    base = "https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8"
    pl = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-MAP:URI="/init.mp4"\n'
          "#EXTINF:6,\n/tvcidade10/seg1.ts\n#EXTINF:6,\n../x/seg2.ts?token=abc\n#EXTINF:6,\nseg3.ts\n")
    out = server.rewrite_playlist(pl, base)
    from urllib.parse import parse_qs, urlparse
    urls = [parse_qs(urlparse(l.split('URI="')[-1].rstrip('"')).query)["url"][0]
            for l in out.splitlines() if "/proxy/segment" in l]
    assert urls == [
        "https://video10.logicahost.com.br/tvcidade10/tvcidade10/key.bin",
        "https://video10.logicahost.com.br/init.mp4",
        "https://video10.logicahost.com.br/tvcidade10/seg1.ts",
        "https://video10.logicahost.com.br/tvcidade10/x/seg2.ts?token=abc",
        "https://video10.logicahost.com.br/tvcidade10/tvcidade10/seg3.ts",
    ]


@pytest.fixture()
def upstream():
    """Tiny 'internal' HTTP server on 127.0.0.1 that also redirects to localhost."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/secret.txt":
                body = b"INTERNAL-ONLY"
            elif self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/secret.txt")
                self.end_headers()
                return
            elif self.path == "/playlist.m3u8":
                body = b"#EXTM3U\n#EXTINF:2,\nseg1.ts\n"
            else:
                body = b"SEGMENT"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def test_proxy_blocks_open_relay_ssrf(upstream, monkeypatch):
    monkeypatch.setenv("STREAM_URL", "https://video10.logicahost.com.br/a/playlist.m3u8")
    monkeypatch.delenv("PROXY_ALLOWED_HOSTS", raising=False)
    c = TestClient(server.create_app())
    url = f"http://127.0.0.1:{upstream}/secret.txt"
    # 1) unsigned → rejected
    assert c.get("/proxy/segment", params={"url": url}).status_code == 403
    # 2) forged signature → rejected
    assert c.get("/proxy/segment", params={"url": url, "sig": "0" * 32}).status_code == 403
    # 3) correctly signed but host not allowed → rejected (defence in depth)
    r = c.get("/proxy/segment", params={"url": url, "sig": server._sign(url)})
    assert r.status_code == 403 and b"INTERNAL" not in r.content


def test_proxy_serves_allowed_host_and_blocks_redirect_to_other_host(upstream, monkeypatch):
    monkeypatch.setenv("STREAM_URL", f"http://127.0.0.1:{upstream}/playlist.m3u8")
    with TestClient(server.create_app()) as c:
        # playlist is served, with signed proxy URLs
        r = c.get("/proxy/playlist")
        assert r.status_code == 200 and "/proxy/segment?url=" in r.text
        # the old ?url= parameter is ignored: it must NOT fetch arbitrary URLs
        r2 = c.get("/proxy/playlist", params={"url": f"http://127.0.0.1:{upstream}/secret.txt"})
        assert b"INTERNAL-ONLY" not in r2.content and "#EXTM3U" in r2.text
        seg_line = [l for l in r.text.splitlines() if l.startswith("/proxy/segment")][0]
        assert c.get(seg_line).content == b"SEGMENT"
        # redirect from the allowed host to 'localhost' (a different hostname) is blocked
        url = f"http://127.0.0.1:{upstream}/redir"
        r = c.get("/proxy/segment", params={"url": url, "sig": server._sign(url)})
        assert r.status_code == 502 and b"INTERNAL" not in r.content


def test_websocket_receives_pushed_caption(monkeypatch):
    with TestClient(server.create_app()) as c:
        with c.websocket_connect("/captions") as ws:
            msg = {"pt": "Olá", "es": "Hola", "age_start": 6.0, "age_end": 4.0}
            c.portal.call(server.push_caption, msg)
            assert ws.receive_json() == msg


# ── transcribe.py: Groq hallucination filter ─────────────────────────────────

def _fake_groq(monkeypatch, response):
    import types
    import transcribe

    class FakeGroq:
        def __init__(self, api_key):
            self.audio = types.SimpleNamespace(
                transcriptions=types.SimpleNamespace(create=lambda **kw: response))

    monkeypatch.setitem(sys.modules, "groq", types.SimpleNamespace(Groq=FakeGroq))
    monkeypatch.setenv("GROQ_API_KEY", "test")
    return transcribe


def test_groq_filtered_segments_are_not_resurrected_by_text_fallback(monkeypatch):
    import types
    resp = types.SimpleNamespace(
        text=" Obrigado.",
        segments=[{"text": " Obrigado.", "start": 0, "end": 1,
                   "no_speech_prob": 0.9, "avg_logprob": -1.2, "compression_ratio": 1.0}])
    tr = _fake_groq(monkeypatch, resp)
    assert tr._transcribe_segment_groq(b"\0" * 32000) == []


def test_groq_text_fallback_still_works_without_segments(monkeypatch):
    import types
    tr = _fake_groq(monkeypatch, types.SimpleNamespace(text=" Bom dia.", segments=None))
    out = tr._transcribe_segment_groq(b"\0" * 32000)
    assert [o["text"] for o in out] == ["Bom dia."]
    tr = _fake_groq(monkeypatch, types.SimpleNamespace(text=" legendado por x", segments=[]))
    assert tr._transcribe_segment_groq(b"\0" * 32000) == []


def test_expand_never_loses_or_misaligns_text():
    import random
    rnd = random.Random(1)
    vocab = "a de que o uma casa bem rua muito dia, fim. sim".split()
    for _ in range(3000):
        pt = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 40)))
        es = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(0, 60)))
        parts = captions.expand({"pt": pt, "es": es, "audio_start": 0.0, "audio_end": 5.0})
        assert " ".join(p["pt"] for p in parts).split() == pt.split()
        assert " ".join(p["es"] for p in parts).split() == es.split()
        assert parts[0]["audio_start"] == 0.0 and abs(parts[-1]["audio_end"] - 5.0) < 1e-9


def test_split_text_returns_exactly_n_pieces():
    assert len(captions.split_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa b c d", 4)) == 4
    assert len(captions.split_text("um dois", 5)) == 2      # capped by word count
