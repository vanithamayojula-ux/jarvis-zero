"""Live check of JARVIS's Edge control: runs real spoken-style commands against the real browser.

Usage: python scripts/edge_check.py   (opens a visible Edge window with JARVIS's own profile)
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.browser import EdgeController  # noqa: E402
from jarvis.components import OllamaLLM  # noqa: E402
from jarvis.config import DATA_DIR, Settings  # noqa: E402
from jarvis.conversation import Conversation  # noqa: E402
from jarvis.tools import Reminders, ToolBox  # noqa: E402

STEPS = [
    ("open youtube and search for anime fights", 4),
    ("play the first video", 6),
    ("pause the video", 1),
    ("what's on this page", 1),
    ("go back", 3),
    ("click on the search box", 1),
    ("type lofi music and search", 4),
    ("scroll down", 1),
    ("open google", 3),
    ("type weather in mumbai and press enter", 4),
    ("summarize this page", 1),
    ("open whatsapp", 5),
    ("type hello from jarvis and send it", 1),
]


async def main() -> None:
    settings = Settings()
    edge = EdgeController(DATA_DIR / settings.get("edge_profile"), settings.get("edge_debug_port"))
    tools = ToolBox(settings, Reminders(DATA_DIR / "reminders-check.json"), edge=edge)
    llm = OllamaLLM(settings.get("ollama_url"), settings.get("llm_model"), settings.get("llm_keep_alive"))
    conv = Conversation(settings, llm, tools)
    for text, wait in STEPS:
        t = time.perf_counter()
        reply = " ".join([s async for s in conv.reply(text)])
        page = edge.current() if edge.running() else {}
        print(f"YOU:    {text}\nJARVIS: {reply}  ({time.perf_counter() - t:.1f}s)\nPAGE:   {page.get('title', '')[:70]} | {page.get('url', '')[:80]}\n")
        await asyncio.sleep(wait)


asyncio.run(main())
