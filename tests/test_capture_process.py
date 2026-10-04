"""FfmpegProcess / Chunker / _reader_loop, driven by a fake ffmpeg (no network, no ffmpeg)."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import capture  # noqa: E402
from capture import Chunker, FfmpegProcess, StallError  # noqa: E402

FAKE_SRC = r'''
import sys, time
mode = sys.argv[1]
if mode == "noisy":
    for _ in range(100):
        sys.stderr.write("w" * 4096 + "\n"); sys.stderr.flush()
        sys.stdout.buffer.write(b"\0" * 3200); sys.stdout.buffer.flush(); time.sleep(0.01)
elif mode == "noisy_forever":
    while True:
        sys.stderr.write("w" * 4096 + "\n"); sys.stderr.flush()
        sys.stdout.buffer.write(b"\0" * 3200); sys.stdout.buffer.flush(); time.sleep(0.01)
elif mode == "stall":
    sys.stdout.buffer.write(b"\0" * 6400); sys.stdout.buffer.flush(); time.sleep(120)
elif mode == "crash":
    sys.stderr.write("Server returned 404 Not Found\n"); sys.exit(1)
'''


@pytest.fixture()
def fake(tmp_path):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(FAKE_SRC)
    return lambda mode: [sys.executable, str(script), mode]


def test_chunker_positions_are_contiguous():
    c = Chunker(4)
    out = c.feed(b"ab") + c.feed(b"cdefghij")
    assert out == [(0, b"abcd"), (4, b"efgh")] and c.received_end == 10
    c.discard_partial()
    assert c.emitted == 8 and c.received_end == 8


def test_noisy_stderr_does_not_stall_stdout(fake):
    """Regression: undrained stderr used to freeze stdout after ~130 KB."""
    async def go():
        total = 0
        async with FfmpegProcess(fake("noisy")) as ff:
            while raw := await ff.read(25600, 10):
                total += len(raw)
            return total, await ff.exit_code()
    assert asyncio.run(asyncio.wait_for(go(), 30)) == (320_000, 0)


def test_stop_returns_promptly_while_child_floods_stderr(fake):
    async def go():
        t = time.monotonic()
        async with FfmpegProcess(fake("noisy_forever")) as ff:
            await ff.read(25600, 5)
            await asyncio.sleep(1.0)
        return time.monotonic() - t
    assert asyncio.run(asyncio.wait_for(go(), 30)) < 5


def test_stalled_child_raises_and_is_reaped(fake):
    async def go():
        with pytest.raises(StallError):
            async with FfmpegProcess(fake("stall")) as ff:
                await ff.read(25600, 2)
                await ff.read(25600, 0.5)
        return ff._proc.returncode
    assert asyncio.run(asyncio.wait_for(go(), 30)) is not None


def test_reader_loop_keeps_positions_contiguous_under_stderr_flood(fake, monkeypatch):
    monkeypatch.setattr(capture, "_build_ffmpeg_cmd", lambda url: fake("noisy_forever"))

    async def go():
        q = asyncio.Queue(maxsize=capture.DEFAULT_QUEUE_CHUNKS)
        task = capture.start_capture(q, stream_url="x", chunk_ms=100)
        await asyncio.sleep(2)
        await capture.stop_capture(task)
        return [q.get_nowait() for _ in range(q.qsize())]
    items = asyncio.run(asyncio.wait_for(go(), 30))
    chunks = [i for i in items if i[1] is not None]
    assert len(chunks) > 50
    assert {b[0] - a[0] for a, b in zip(chunks, chunks[1:])} == {3200}


def test_missing_ffmpeg_binary_does_not_kill_capture(monkeypatch):
    monkeypatch.setattr(capture, "_build_ffmpeg_cmd", lambda url: ["/nonexistent/ffmpeg"])

    async def go():
        task = capture.start_capture(asyncio.Queue(), stream_url="x", chunk_ms=100)
        await asyncio.sleep(0.5)
        alive = not task.done()
        await capture.stop_capture(task)
        return alive
    assert asyncio.run(asyncio.wait_for(go(), 30)) is True
