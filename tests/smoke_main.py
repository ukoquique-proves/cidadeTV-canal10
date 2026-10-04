"""
Smoke test of main.py: real uvicorn server + real WebSocket client + SIGINT shutdown,
with only the two ML models stubbed.  Needs ffmpeg and `pip install websockets`.
    python tests/smoke_main.py
"""
import asyncio, functools, http.server, json, shutil, signal, subprocess, sys, tempfile, textwrap, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import e2e_local_hls as e2e  # reuse the HLS generator

LAUNCHER = textwrap.dedent("""
    import sys
    sys.path.insert(0, %r)
    import transcribe, vad
    transcribe._load_model = lambda: None
    transcribe._transcribe_segment = lambda pcm: [{"text": "Isto é uma frase de teste que fala sobre o tempo.", "start": 0.0, "end": len(pcm)/32000}]
    vad.SileroDetector = lambda thr: vad.EnergyDetector()
    import main
    sys.argv = ["main.py", "--no-translate", "--port", "8765", "--stream-url", sys.argv[1]]
    main.main()
""" % str(ROOT))


async def client():
    import websockets
    got = []
    async with websockets.connect("ws://127.0.0.1:8765/captions") as ws:
        end = time.time() + 20
        while time.time() < end and len(got) < 2:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            except asyncio.TimeoutError:
                continue
            if not m.get("ping"):
                got.append(m)
    return got


def main():
    tmp = tempfile.mkdtemp()
    gen = e2e._start_generator(tmp)
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
    h.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(2.5)
    url = f"http://127.0.0.1:{httpd.server_port}/playlist.m3u8"

    p = subprocess.Popen([sys.executable, "-c", LAUNCHER, url], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    try:
        got = asyncio.run(client())
        print("WS messages:", got)
        assert len(got) >= 1 and {"pt", "es", "age_start", "age_end"} <= set(got[0])
    finally:
        t0 = time.time()
        p.send_signal(signal.SIGINT)
        try:
            out, _ = p.communicate(timeout=15)
            print(f"exit code {p.returncode} after {time.time() - t0:.1f}s of SIGINT")
            print("\n".join(l for l in out.splitlines() if "caption ready" in l or "Shutting" in l or "Bye" in l or "Traceback" in l)[-1200:])
            assert p.returncode == 0, "unclean exit"
        except subprocess.TimeoutExpired:
            p.kill(); print("HUNG on SIGINT"); raise
        finally:
            gen.terminate(); gen.wait(); httpd.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
