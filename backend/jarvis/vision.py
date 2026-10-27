"""Looking at ONE window, on this laptop only: a one-off in-memory picture of it, then Windows' own offline OCR.

  * capture_window(hwnd): PrintWindow into a memory bitmap -> a Pillow image. It is never saved (BytesIO only), is capped at
    1920 px on its longest side, and is refused when the window asks not to be captured (display affinity) or comes out
    completely black (protected video, DRM). One look at a time, and at most one every two seconds.
  * ocr(image): Windows.Media.Ocr, the OCR built into Windows (no download, no cloud). By default a short-lived fixed
    PowerShell script reads the PNG bytes from its input and prints JSON lines; no user text is ever put into it. It has a
    size cap on what it may print and a time limit, after which it is killed. Optional in-process route: the free `winrt`
    packages, used only when JARVIS_OCR_WINRT=1 and they import.

Pixels never leave the laptop and nothing here writes them anywhere. Heavy imports (Pillow, ctypes structures) are lazy, so
nothing is loaded or running until the person asks JARVIS to look.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time

MAX_SIDE = 1920
MAX_PIXELS = 7680 * 4320
OCR_TIMEOUT_S = 10.0
OCR_OUT_CAP = 200_000
DEBOUNCE_S = 2.0

SAY_TOO_SOON = "I just looked a moment ago. Give me a couple of seconds, then ask again."
SAY_PROTECTED = "That window is protected from capture, so I can't read it as an image."
SAY_NO_LANG = ("Windows has no OCR language installed; add one in Settings > Time & language > Language "
               "(I never download one by myself).")
SAY_OCR_TIMEOUT = "Reading the picture took too long, so I stopped."
SAY_OCR_FAILED = "I couldn't read the picture on this laptop."
SAY_NO_GRAB = "I couldn't take a look at that window."


class VisionError(Exception):
    """Written to be spoken."""


# The whole OCR program. A fixed text: nothing the person or the screen said is ever put into it.
OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}
$ms = New-Object System.IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($ms)
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter($stream)
$writer.WriteBytes($ms.ToArray())
$null = Await ($writer.StoreAsync()) ([uint32])
$stream.Seek(0)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language('en-US')))
}
if (-not $engine) { [Console]::Out.WriteLine('{"error":"no_language"}'); exit 3 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) {
    $x0 = [double]::MaxValue; $y0 = [double]::MaxValue; $x1 = 0; $y1 = 0
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        if ($r.X -lt $x0) { $x0 = $r.X }
        if ($r.Y -lt $y0) { $y0 = $r.Y }
        if ($r.X + $r.Width -gt $x1) { $x1 = $r.X + $r.Width }
        if ($r.Y + $r.Height -gt $y1) { $y1 = $r.Y + $r.Height }
    }
    if ($x0 -eq [double]::MaxValue) { $x0 = 0; $y0 = 0 }
    $o = @{ text = $line.Text; x = [int]$x0; y = [int]$y0; w = [int]($x1 - $x0); h = [int]($y1 - $y0) }
    [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress))
}
"""


def powershell_argv() -> list[str]:
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(OCR_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded]


def run_limited(argv: list[str], data: bytes, timeout: float = OCR_TIMEOUT_S, cap: int = OCR_OUT_CAP) -> str:
    """Run a fixed program with `data` on its input; return at most `cap` bytes of its output as text. After `timeout`
    seconds the program is killed. No shell, no temporary files."""
    flags = 0x08000000 if sys.platform == "win32" else 0            # CREATE_NO_WINDOW: no console flashes up
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
                            creationflags=flags)
    expired = threading.Event()

    def stop() -> None:
        expired.set()
        try:
            proc.kill()
        except OSError:
            pass

    def feed() -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        out = proc.stdout.read(cap)
    finally:
        timer.cancel()
        if proc.poll() is None:                                      # more output than the cap allows, or still running
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdout, proc.stdin):
            try:
                pipe.close()
            except OSError:
                pass
    if expired.is_set():
        raise VisionError(SAY_OCR_TIMEOUT)
    return out.decode("utf-8", errors="replace")


def parse_ocr(raw: str) -> list[dict]:
    """JSON lines from the OCR program -> [{text, x, y, w, h}]; anything malformed is skipped, and size is capped."""
    from .untrusted import clean
    items: list[dict] = []
    for line in (raw or "")[:OCR_OUT_CAP].lstrip(chr(0xFEFF)).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("error") == "no_language":
            raise VisionError(SAY_NO_LANG)
        text = clean(str(e.get("text") or "")).strip()[:400]
        if not text:
            continue
        box = {}
        for k in ("x", "y", "w", "h"):
            v = e.get(k, 0)
            box[k] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 100_000 else 0
        items.append({"text": text, **box})
        if len(items) >= 3000:
            break
    return items


def ocr_text(items: list[dict]) -> str:
    """The recognised lines, top to bottom."""
    rows = sorted(items, key=lambda i: (i["y"] // 8, i["x"]))
    return "\n".join(i["text"] for i in rows)


def _all_black(img) -> bool:
    small = img.convert("RGB").resize((24, 24))
    return all(hi == 0 for _lo, hi in small.getextrema())


def _cap_size(img):
    w, h = img.size
    if max(w, h) <= MAX_SIDE:
        return img
    from PIL import Image
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _grab_win32(hwnd: int):
    """PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT) into a memory bitmap. Nothing touches the disk."""
    import ctypes
    from ctypes import wintypes
    from PIL import Image
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    u.GetWindowDC.restype = vp
    u.GetWindowDC.argtypes = [vp]
    u.ReleaseDC.argtypes = [vp, vp]
    u.PrintWindow.argtypes = [vp, vp, wintypes.UINT]
    u.GetWindowDisplayAffinity.argtypes = [vp, ctypes.POINTER(wintypes.DWORD)]
    u.IsIconic.argtypes = [vp]
    u.GetWindowRect.argtypes = [vp, ctypes.POINTER(wintypes.RECT)]
    g.CreateCompatibleDC.restype = vp
    g.CreateCompatibleDC.argtypes = [vp]
    g.CreateCompatibleBitmap.restype = vp
    g.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = vp
    g.SelectObject.argtypes = [vp, vp]
    g.DeleteObject.argtypes = [vp]
    g.DeleteDC.argtypes = [vp]
    g.GetDIBits.argtypes = [vp, vp, wintypes.UINT, wintypes.UINT, vp, vp, wintypes.UINT]
    h = vp(hwnd)
    affinity = wintypes.DWORD(0)
    if not u.GetWindowDisplayAffinity(h, ctypes.byref(affinity)):
        raise VisionError(SAY_NO_GRAB)                               # can't tell whether it is protected: don't look
    if affinity.value != 0:                                          # WDA_MONITOR / WDA_EXCLUDEFROMCAPTURE
        raise VisionError(SAY_PROTECTED)
    if u.IsIconic(h):
        raise VisionError("That window is minimised, so there's nothing for me to look at.")
    rect = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(rect))
    w, ht = rect.right - rect.left, rect.bottom - rect.top
    if w < 8 or ht < 8 or w * ht > MAX_PIXELS:
        raise VisionError(SAY_NO_GRAB)

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    wdc = u.GetWindowDC(h)
    mem = g.CreateCompatibleDC(wdc) if wdc else None
    bmp = g.CreateCompatibleBitmap(wdc, w, ht) if mem else None
    try:
        if not (wdc and mem and bmp):
            raise VisionError(SAY_NO_GRAB)
        old = g.SelectObject(mem, bmp)
        ok = u.PrintWindow(h, mem, 2)                               # 2 = PW_RENDERFULLCONTENT
        g.SelectObject(mem, old)
        if not ok:
            raise VisionError(SAY_NO_GRAB)
        info = BMIH(ctypes.sizeof(BMIH), w, -ht, 1, 32, 0, 0, 0, 0, 0, 0)     # negative height: top-down rows
        buf = (ctypes.c_ubyte * (w * ht * 4))()
        if not g.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(info), 0):
            raise VisionError(SAY_NO_GRAB)
        return Image.frombuffer("RGB", (w, ht), bytes(buf), "raw", "BGRX", 0, 1)
    finally:
        if bmp:
            g.DeleteObject(bmp)
        if mem:
            g.DeleteDC(mem)
        if wdc:
            u.ReleaseDC(h, wdc)


def _ocr_winrt(png: bytes) -> list[dict]:                            # pragma: no cover (optional packages, off by default)
    import asyncio
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    async def go():
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png)
        await writer.store_async()
        stream.seek(0)
        bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise VisionError(SAY_NO_LANG)
        result = await engine.recognize_async(bitmap)
        return [{"text": ln.text, "x": 0, "y": i * 20, "w": 0, "h": 0} for i, ln in enumerate(result.lines)]
    return asyncio.run(go())


class Vision:
    """Everything is injectable so tests never touch a real window: grab(hwnd) -> image, runner(png bytes) -> JSON lines."""

    def __init__(self, grab=None, runner=None, clock=time.monotonic, debounce: float = DEBOUNCE_S):
        self._grab = grab or _grab_win32
        self._runner = runner
        self.clock, self.debounce = clock, debounce
        self._lock = threading.Lock()
        self._last: float | None = None

    def capture_window(self, hwnd: int):
        """A Pillow image of that one window, in memory only. Raises VisionError (spoken) when it must not be looked at."""
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < self.debounce:
                raise VisionError(SAY_TOO_SOON)
            self._last = now
            img = self._grab(hwnd)
            if _all_black(img):
                raise VisionError(SAY_PROTECTED)                    # a blank frame: protected content, not an empty window
            return _cap_size(img.convert("RGB"))

    def ocr(self, image) -> list[dict]:
        """[{text, x, y, w, h}] for the lines Windows' OCR finds in the picture."""
        buf = io.BytesIO()
        image.save(buf, "PNG")
        png = buf.getvalue()
        if self._runner is None and os.environ.get("JARVIS_OCR_WINRT") == "1":
            try:
                return _ocr_winrt(png)
            except VisionError:
                raise
            except Exception:
                pass                                                 # not installed or failed: the PowerShell route below
        raw = (self._runner or (lambda data: run_limited(powershell_argv(), data)))(png)
        return parse_ocr(raw)

    def ocr_text(self, image) -> str:
        return ocr_text(self.ocr(image))
