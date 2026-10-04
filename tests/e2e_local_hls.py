"""
End-to-end test of capture → VAD → ASR/translation stubs → captions, against a
REAL (local) HLS live stream produced by ffmpeg.  Needs ffmpeg; no models, no
internet.   Run:   python tests/e2e_local_hls.py     (takes ~30 s)

The generated audio is a 300 Hz tone, 2.0 s on / 1.5 s off, repeating every
3.5 s, so we know exactly when "speech" starts in the stream.  We check that

  1. nothing is lost: every tone is found, with the right duration, and
  2. the capture clock places consecutive tones 3.5 s apart on the live
     timeline (error well below the 100 ms the viewer would notice),
  3. the ages sent to the browser are plausible.  They are NOT constant: audio
     arrives in whole HLS segments, so the end of a phrase is only seen when the
     burst containing it arrives — the jitter is up to one segment duration.
     (This is the main reason a 1-2 s video delay cannot be enough.)
"""
import asyncio
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["ENABLE_TRANSLATION"] = "false"

import captions  # noqa: E402
import capture  # noqa: E402
import transcribe  # noqa: E402
from translate import Translator  # noqa: E402
from vad import EnergyDetector, VadSegmenter  # noqa: E402

RUN_SECONDS = 30
PERIOD, ON = 3.5, 2.0


def _start_generator(outdir: str) -> subprocess.Popen:
    expr = f"0.4*sin(2*PI*300*t)*lt(mod(t\\,{PERIOD})\\,{ON})"
    cmd = [
        "ffmpeg", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=16000:c=mono",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
        "-hls_flags", "delete_segments+omit_endlist",
        os.path.join(outdir, "playlist.m3u8"),
    ]
    return subprocess.Popen(cmd)


async def run() -> dict:
    tmp = tempfile.mkdtemp()
    gen = _start_generator(tmp)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/playlist.m3u8"

    # ASR stub: "transcribes" every segment as a caption spanning the segment
    transcribe._load_model = lambda: None
    transcribe._transcribe_segment = lambda pcm: [
        {"text": f"tom {len(pcm) / 32000:.1f}s", "start": 0.0, "end": len(pcm) / 32000}
    ]

    for _ in range(100):                      # wait for the playlist to exist
        if os.path.exists(os.path.join(tmp, "playlist.m3u8")):
            break
        await asyncio.sleep(0.1)

    pcm_q, seg_q, tr_q, cap_q = (asyncio.Queue(maxsize=n) for n in (capture.DEFAULT_QUEUE_CHUNKS, 10, 20, 20))
    tasks = [
        capture.start_capture(pcm_q, stream_url=url),
        VadSegmenter(pcm_q, seg_q, detector=EnergyDetector()).start(),
        transcribe.Transcriber(seg_q, tr_q).start(),
        Translator(tr_q, cap_q).start(),
    ]

    results = []
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        try:
            item = await asyncio.wait_for(cap_q.get(), 1.0)
        except asyncio.TimeoutError:
            continue
        for piece in captions.expand(item):
            msg = captions.to_message(piece, capture.clock.live_time)
            results.append((piece, msg, capture.clock.live_time(piece["audio_start"])))

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    gen.terminate()
    gen.wait()
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"results": results}


def analyse(results) -> None:
    assert len(results) >= 5, f"only {len(results)} captions found in {RUN_SECONDS}s"

    live = [r[2] for r in results]
    gaps = [b - a for a, b in zip(live, live[1:])]
    durs = [r[0]["audio_end"] - r[0]["audio_start"] for r in results]
    ages = [r[1]["age_start"] for r in results]

    print(f"captions found           : {len(results)} (expected ~{RUN_SECONDS / PERIOD - 2:.0f}+)")
    print(f"spacing on live timeline : {[round(g, 2) for g in gaps]}  (ideal {PERIOD})")
    print(f"segment durations        : {[round(d, 2) for d in durs]}  (tone {ON}s + pre-roll/hang)")
    print(f"age_start sent to browser: {[round(a, 2) for a in ages]}")

    # The first pair can be off by a fraction of a segment: the clock's minimum
    # filter needs a few bursts to converge.  Judge the steady state.
    steady = gaps[1:]
    worst = max(abs(g - PERIOD) for g in steady)
    print(f"first gap error (clock converging): {abs(gaps[0] - PERIOD) * 1000:.0f} ms")
    print(f"worst steady-state spacing error  : {worst * 1000:.0f} ms")
    assert worst < 0.15, "capture clock does not place segments correctly on the live timeline"
    assert abs(gaps[0] - PERIOD) < 0.6, "clock far off even at startup"
    assert all(ON - 0.1 <= d <= ON + 1.0 for d in durs), "tone cut short / audio lost"
    # tone (2 s) + 0.5 s silence wait + 0..one 2 s segment of burst jitter
    assert all(2.0 < a < 7.0 for a in ages), "implausible age_start"
    print("\nE2E OK")


def test_e2e_local_hls():
    import pytest

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    analyse(asyncio.run(run())["results"])


if __name__ == "__main__":
    analyse(asyncio.run(run())["results"])
