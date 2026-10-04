"""
Unit tests — no models, no network.   Run:  python -m pytest tests -q
"""
import asyncio
import http.server
import os
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
    assert len(segs) >= 4                          # 20 s with the 5 s default cap → >= 4 segments
    assert all(len(s[0]) / BYTES_PER_SEC <= 5.2 for s in segs)
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
        def __init__(self, api_key, **_kw):
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


# ── main.py: .env handling and --fast ────────────────────────────────────────

def _clean_env(monkeypatch):
    for k in ("GROQ_API_KEY", "TRANSLATION_BACKEND", "WHISPER_MODEL",
              "VAD_MAX_SEGMENT_MS", "ENABLE_TRANSLATION", "STREAM_URL"):
        monkeypatch.delenv(k, raising=False)


def test_env_placeholder_groq_key_is_ignored(monkeypatch, tmp_path):
    import main
    _clean_env(monkeypatch)
    envfile = tmp_path / ".env"
    envfile.write_text("GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx\n")
    monkeypatch.setattr(main, "_ENV_FILE", envfile)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_xxxxxxxxxxxxxxxxxxxx")   # as load_dotenv would
    notes = main._normalize_env()
    assert "GROQ_API_KEY" not in os.environ and len(notes) == 1


def test_env_real_groq_key_is_kept(monkeypatch, tmp_path):
    import main
    _clean_env(monkeypatch)
    envfile = tmp_path / ".env"
    envfile.write_text("GROQ_API_KEY=gsk_AbC123realkey\n")
    monkeypatch.setattr(main, "_ENV_FILE", envfile)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_AbC123realkey")
    assert main._normalize_env() == [] and os.environ["GROQ_API_KEY"] == "gsk_AbC123realkey"


def test_env_blank_values_fall_back_to_defaults(monkeypatch, tmp_path):
    import main
    import translate
    _clean_env(monkeypatch)
    envfile = tmp_path / ".env"
    envfile.write_text("TRANSLATION_BACKEND=\nWHISPER_MODEL=small\n")
    monkeypatch.setattr(main, "_ENV_FILE", envfile)
    monkeypatch.setenv("TRANSLATION_BACKEND", "")      # what load_dotenv leaves behind
    monkeypatch.setenv("WHISPER_MODEL", "small")
    main._normalize_env()
    assert "TRANSLATION_BACKEND" not in os.environ and os.environ["WHISPER_MODEL"] == "small"
    assert translate._active_backend() == "nllb"


def test_env_blank_in_file_does_not_clobber_shell_value(monkeypatch, tmp_path):
    import main
    _clean_env(monkeypatch)
    envfile = tmp_path / ".env"
    envfile.write_text("TRANSLATION_BACKEND=\n")
    monkeypatch.setattr(main, "_ENV_FILE", envfile)
    monkeypatch.setenv("TRANSLATION_BACKEND", "marian")   # exported in the shell
    main._normalize_env()
    assert os.environ["TRANSLATION_BACKEND"] == "marian"


def test_translate_blank_backend_uses_default(monkeypatch):
    import translate
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("TRANSLATION_BACKEND", "")
    assert translate._active_backend() == "nllb"


def test_fast_overrides_env_but_model_flag_wins(monkeypatch):
    import argparse
    import main
    _clean_env(monkeypatch)
    monkeypatch.setenv("WHISPER_MODEL", "small")          # as .env.example sets it
    monkeypatch.setenv("VAD_MAX_SEGMENT_MS", "5000")
    ns = lambda **kw: argparse.Namespace(**{"stream_url": None, "model": None,
                                            "no_translate": False, "fast": False, **kw})
    main._apply_cli_overrides(ns(fast=True))
    assert os.environ["WHISPER_MODEL"] == "tiny"
    assert os.environ["VAD_MAX_SEGMENT_MS"] == "3000"
    assert os.environ["ENABLE_TRANSLATION"] == "false"
    monkeypatch.setenv("WHISPER_MODEL", "small")
    main._apply_cli_overrides(ns(fast=True, model="medium"))
    assert os.environ["WHISPER_MODEL"] == "medium"


# ── server.py: slow WebSocket clients ────────────────────────────────────────

class _FakeWS:
    def __init__(self, hang_send=False, hang_close=False):
        self.hang_send, self.hang_close = hang_send, hang_close
        self.sent, self.closed = [], None

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.hang_send:
            await asyncio.sleep(3600)
        self.sent.append(text)

    async def close(self, code=1000):
        if self.hang_close:
            await asyncio.sleep(3600)
        self.closed = code


def test_broadcast_closes_slow_client_and_keeps_others(monkeypatch):
    import server
    monkeypatch.setattr(server, "_SEND_TIMEOUT", 0.05)

    async def run():
        mgr = server.ConnectionManager()
        good, slow = _FakeWS(), _FakeWS(hang_send=True)
        await mgr.connect(good)
        await mgr.connect(slow)
        await mgr.broadcast({"pt": "oi"})
        await asyncio.sleep(0.05)                # let the background close run
        return mgr, good, slow

    mgr, good, slow = asyncio.run(run())
    assert mgr.count == 1 and len(good.sent) == 1
    assert slow.closed == 1011                   # socket really closed, so the page reconnects


def test_broadcast_not_delayed_by_client_that_cannot_close(monkeypatch):
    import server, time
    monkeypatch.setattr(server, "_SEND_TIMEOUT", 0.05)
    monkeypatch.setattr(server, "_CLOSE_TIMEOUT", 0.05)

    async def run():
        mgr = server.ConnectionManager()
        await mgr.connect(_FakeWS(hang_send=True, hang_close=True))
        t0 = time.monotonic()
        await mgr.broadcast({"pt": "oi"})
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.1)                 # background close times out cleanly
        return mgr, elapsed

    mgr, elapsed = asyncio.run(run())
    assert mgr.count == 0 and elapsed < 0.5 and not mgr._closing


# ── groq_client.py / translate.py: shared client, timeouts, reasoning effort ──

def _fake_groq_class(created):
    class FakeGroq:
        def __init__(self, api_key, **kw):
            created.append({"api_key": api_key, **kw})
    return FakeGroq


def test_groq_client_is_reused_and_configured(monkeypatch):
    import types
    import groq_client
    created = []
    monkeypatch.setitem(sys.modules, "groq", types.SimpleNamespace(Groq=_fake_groq_class(created)))
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.delenv("GROQ_TIMEOUT_S", raising=False)
    monkeypatch.delenv("GROQ_MAX_RETRIES", raising=False)
    a, b = groq_client.get_client(), groq_client.get_client()
    assert a is b and len(created) == 1                     # one client, not one per request
    assert created[0] == {"api_key": "k1", "timeout": 8.0, "max_retries": 1}
    monkeypatch.setenv("GROQ_TIMEOUT_S", "3")
    assert groq_client.get_client() is not a and created[-1]["timeout"] == 3.0
    monkeypatch.setenv("GROQ_TIMEOUT_S", "garbage")          # bad value -> default, no crash
    assert groq_client.get_client() and created[-1]["timeout"] == 8.0


def test_groq_transient_error_classification():
    import groq_client

    class RateLimitError(Exception): pass
    class APIConnectionError(Exception): pass
    class APITimeoutError(APIConnectionError): pass
    assert groq_client.is_transient(RateLimitError())
    assert groq_client.is_transient(APITimeoutError())      # via base class
    assert not groq_client.is_transient(ValueError("bug"))


def test_reasoning_effort_only_for_gpt_oss(monkeypatch):
    import translate
    monkeypatch.delenv("GROQ_REASONING_EFFORT", raising=False)
    # When env var not set, uses Groq's default (no param passed)
    assert translate._reasoning_kwargs("openai/gpt-oss-20b") == {}
    assert translate._reasoning_kwargs("llama-3.1-8b-instant") == {}     # would be a 400
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "medium")
    # reasoning_effort passed directly when set
    assert translate._reasoning_kwargs("openai/gpt-oss-120b") == {"reasoning_effort": "medium"}
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "off")
    # off disables reasoning entirely via include_reasoning=False
    assert translate._reasoning_kwargs("openai/gpt-oss-20b") == {"include_reasoning": False}


def test_groq_translation_sends_reasoning_effort(monkeypatch):
    import types
    import translate
    seen = {}

    def create(**kw):
        seen.update(kw)
        msg = types.SimpleNamespace(content=" hola ")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class FakeGroq:
        def __init__(self, api_key, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))

    monkeypatch.setitem(sys.modules, "groq", types.SimpleNamespace(Groq=FakeGroq))
    monkeypatch.setenv("GROQ_API_KEY", "k-translate")
    monkeypatch.delenv("GROQ_TRANSLATE_MODEL", raising=False)
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "low")  # explicitly set to test it's passed
    assert translate._translate_groq_sync("olá") == "hola"
    assert seen["model"] == "openai/gpt-oss-20b"
    # reasoning_effort is now passed directly, not via extra_body
    assert seen["reasoning_effort"] == "low"


def test_vad_detects_gap_wherever_it_falls():
    # With 100 ms chunks the window buffer is empty at every 8th chunk boundary
    # (3200 B chunk vs 1024 B window); a gap there used to go unnoticed and the
    # segment silently spanned the missing audio (timestamps then drift).
    for k in (15, 16, 17, 24):
        gap_start, gap_end = k * 0.1, (k + 2) * 0.1
        segs = asyncio.run(_run_vad(_tone(4), drop_chunks=(k, k + 1)))
        for data, start_s, _ in segs:
            end_s = start_s + len(data) / BYTES_PER_SEC
            assert not (start_s < gap_start - 0.02 and end_s > gap_end + 0.02), \
                f"segment {start_s:.2f}-{end_s:.2f} s spans the gap {gap_start:.1f}-{gap_end:.1f} s (k={k})"
        assert len(segs) >= 2, f"gap at chunk {k} not detected"


def test_vad_marker_then_stream_continues_normally():
    pcm = _silence(0.5) + _tone(2) + _silence(1)
    segs = asyncio.run(_run_vad(pcm, markers=(30,)))     # marker during the silence tail
    assert len(segs) == 1 and 2.0 <= len(segs[0][0]) / BYTES_PER_SEC <= 2.9


# ── Viewer gate ───────────────────────────────────────────────────────────────

def test_transcriber_skips_asr_when_no_viewers():
    """When has_viewers() returns False, the Transcriber must discard segments
    without calling the ASR backend at all."""
    from transcribe import Transcriber

    asr_calls = []

    async def _run():
        inq: asyncio.Queue = asyncio.Queue()
        outq: asyncio.Queue = asyncio.Queue()

        # Stub: record every ASR call, return empty
        import transcribe
        orig = transcribe._transcribe_segment
        transcribe._transcribe_segment = lambda pcm: (asr_calls.append(len(pcm)) or [])

        try:
            t = Transcriber(inq, outq, has_viewers=lambda: False)
            task = t.start()

            # Feed three segments
            for _ in range(3):
                await inq.put((b"\x00" * 16000, 0.0, 0.0))

            # Give the task a moment to consume them
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            transcribe._transcribe_segment = orig

        return outq.qsize()

    out_items = asyncio.run(_run())
    assert asr_calls == [], "ASR should not be called when no viewers are connected"
    assert out_items == 0, "No transcripts should reach the output queue"


def test_transcriber_resumes_asr_when_viewer_connects():
    """When has_viewers() flips from False to True, the next segment is processed."""
    from transcribe import Transcriber

    asr_calls = []
    viewer_flag = [False]  # mutable so the lambda sees updates

    async def _run():
        inq: asyncio.Queue = asyncio.Queue()
        outq: asyncio.Queue = asyncio.Queue()

        import transcribe
        orig = transcribe._transcribe_segment
        transcribe._transcribe_segment = lambda pcm: (
            asr_calls.append(len(pcm)) or [{"text": "ok", "start": 0.0, "end": 0.5}]
        )

        try:
            t = Transcriber(inq, outq, has_viewers=lambda: viewer_flag[0])
            task = t.start()

            # First segment — no viewer
            await inq.put((b"\x00" * 16000, 0.0, 0.0))
            await asyncio.sleep(0.05)
            assert asr_calls == [], "should skip with no viewer"

            # Second segment — viewer arrives
            viewer_flag[0] = True
            await inq.put((b"\x00" * 16000, 1.0, 0.0))
            await asyncio.sleep(0.05)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            transcribe._transcribe_segment = orig

        return outq.qsize()

    out_items = asyncio.run(_run())
    assert len(asr_calls) == 1, "ASR should be called exactly once after viewer connects"
    assert out_items == 1, "One transcript should reach the output queue"
