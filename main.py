"""
main.py — Entry point for the TV Cidade 10 live subtitle system.

    python main.py                   # full pipeline + web player
    python main.py --capture-only    # just print transcripts to the terminal
    python main.py --no-translate    # skip PT→ES translation
    python main.py --help

Settings come from the environment / .env (see .env.example).
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")  # before importing the pipeline modules

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

from capture import DEFAULT_QUEUE_CHUNKS, clock, start_capture, stop_capture  # noqa: E402
from captions import expand, to_message  # noqa: E402
from server import create_app, push_caption  # noqa: E402
from transcribe import Transcriber, _resolve_config as _whisper_config  # noqa: E402
from translate import Translator  # noqa: E402
from vad import VadSegmenter  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TV Cidade 10 — live subtitle pipeline")
    p.add_argument("--capture-only", action="store_true",
                   help="Print transcripts to the terminal; no web server.")
    p.add_argument("--no-translate", action="store_true", help="Skip the PT→ES step.")
    p.add_argument("--stream-url", default=None, help="Override STREAM_URL.")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                   help="Interface to bind (default 127.0.0.1; use 0.0.0.0 to expose on your LAN).")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--model", default=None, help="Whisper model (tiny/small/medium/large-v3…).")
    p.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Lowest-latency profile: tiny Whisper model, no translation, "
            "VAD_MAX_SEGMENT_MS=3000. Good for testing sync before accuracy matters."
        ),
    )
    return p.parse_args()


# ── Output stages ───────────────────────────────────────────────────────────

def _log_latency(item: dict, msg: dict) -> None:
    asr = item.get("t_asr", 0) - item.get("t_emit", 0)
    mt = item.get("t_mt", 0) - item.get("t_asr", 0)
    seg_dur = item.get("audio_end", 0) - item.get("audio_start", 0)
    logger.info(
        "caption ready: speech started %.1fs ago at the live edge "
        "(seg %.1fs, ASR %.1fs, translation %.1fs) | %s",
        msg["age_start"], seg_dur, asr, max(mt, 0.0), msg["pt"][:70],
    )


async def _print_captions(queue: asyncio.Queue) -> None:
    print("\n" + "─" * 60 + "\n  capture-only mode — transcripts appear below\n" + "─" * 60 + "\n")
    while True:
        item = await queue.get()
        for piece in expand(item):
            msg = to_message(piece, clock.live_time)
            print(f"[{piece['audio_start']:7.1f}s | age {msg['age_start']:4.1f}s]  PT: {msg['pt']}")
            if msg["es"]:
                print(f"{'':22}ES: {msg['es']}")
        print()


async def _push_captions(queue: asyncio.Queue) -> None:
    while True:
        item = await queue.get()
        for piece in expand(item):
            msg = to_message(piece, clock.live_time)
            await push_caption(msg)
            _log_latency(piece, msg)


# ── Pipeline ────────────────────────────────────────────────────────────────

def build_pipeline(args: argparse.Namespace) -> list[asyncio.Task]:
    stream = os.environ.get(
        "STREAM_URL", "https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8"
    )
    logger.info("Stream URL: %s", stream)

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        logger.info("ASR backend   : Groq (whisper-large-v3-turbo) ✓")
        logger.info("Translation   : Groq LLM (%s) ✓",
                    os.environ.get("GROQ_TRANSLATE_MODEL", "llama-3.1-8b-instant"))
    else:
        _dev, _cmp, _mdl = _whisper_config()
        logger.info("ASR backend   : local faster-whisper %s on %s (%s)", _mdl, _dev, _cmp)
        logger.info("Translation   : %s (local)", os.environ.get("TRANSLATION_BACKEND", "nllb"))
        logger.warning(
            "GROQ_API_KEY not set — using local models. "
            "Add GROQ_API_KEY to .env for much lower latency on CPU hardware."
        )
    if os.environ.get("WHISPER_PROMPT"):
        logger.info("Whisper prompt: %s", os.environ.get("WHISPER_PROMPT"))

    # Queues.  The audio queue is big (~2 min) because HLS delivers audio in
    # bursts; the later ones are small and drop the oldest item when full.
    pcm_queue = asyncio.Queue(maxsize=DEFAULT_QUEUE_CHUNKS)
    segment_queue = asyncio.Queue(maxsize=10)
    transcript_queue = asyncio.Queue(maxsize=20)
    caption_queue = asyncio.Queue(maxsize=20)

    tasks = [
        start_capture(pcm_queue, stream_url=stream, chunk_ms=100),
        VadSegmenter(pcm_queue, segment_queue).start(),
        Transcriber(segment_queue, transcript_queue).start(),
        Translator(transcript_queue, caption_queue).start(),
    ]
    out = _print_captions if args.capture_only else _push_captions
    tasks.append(asyncio.create_task(out(caption_queue), name="output"))
    return tasks


async def run_server(host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(create_app(), host=host, port=port,
                            log_level=LOG_LEVEL.lower(), access_log=False)
    await uvicorn.Server(config).serve()


async def _main() -> None:
    args = _parse_args()
    # CLI overrides go into the environment so every module sees them
    if args.stream_url:
        os.environ["STREAM_URL"] = args.stream_url
    if args.model:
        os.environ["WHISPER_MODEL"] = args.model
    if args.no_translate:
        os.environ["ENABLE_TRANSLATION"] = "false"
    if args.fast:
        # Override only what isn't already explicitly set
        os.environ.setdefault("WHISPER_MODEL", "tiny")
        os.environ["ENABLE_TRANSLATION"] = "false"
        os.environ.setdefault("VAD_MAX_SEGMENT_MS", "3000")
        logger.info("--fast mode: tiny model, no translation, VAD_MAX_SEGMENT_MS=3000")

    tasks = build_pipeline(args)
    if not args.capture_only:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning("Binding to %s — anyone on your network can open the player "
                           "and re-stream the channel through it.", args.host)
        logger.info("Player: http://%s:%d", "localhost" if args.host == "127.0.0.1" else args.host, args.port)
        tasks.append(asyncio.create_task(run_server(args.host, args.port), name="server"))

    logger.info("Pipeline running — press Ctrl+C to stop")
    try:
        # If ANY stage ends (crash, or uvicorn handling Ctrl+C), stop everything.
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if not t.cancelled() and t.exception():
                raise t.exception()
    finally:
        logger.info("Shutting down…")
        capture = tasks[0]
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        await stop_capture(capture)
        logger.info("Bye")


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
