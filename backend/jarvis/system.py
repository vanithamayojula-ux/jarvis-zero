"""Laptop volume and screen brightness, through Windows' own controls.

Volume: the default speaker's master volume (pycaw → Windows Core Audio), so "set the volume to 40" is exact.
Brightness: the built-in screen via WMI (WmiMonitorBrightnessMethods). External monitors usually don't support it.
"""
from __future__ import annotations

import subprocess


class SystemError_(RuntimeError):
    """Written to be spoken."""


def _clamp(n: float) -> int:
    return int(max(0, min(100, round(n))))


class SystemControls:
    def _endpoint(self):
        try:
            import comtypes
            comtypes.CoInitialize()                       # tools run on worker threads
        except Exception:
            pass
        try:
            from pycaw.pycaw import AudioUtilities
            return AudioUtilities.GetSpeakers().EndpointVolume
        except Exception as exc:
            raise SystemError_("I can't reach the speaker volume right now.") from exc

    # ---- volume ----
    def volume(self) -> int:
        return _clamp(self._endpoint().GetMasterVolumeLevelScalar() * 100)

    def set_volume(self, pct: float) -> int:
        ep = self._endpoint()
        ep.SetMasterVolumeLevelScalar(_clamp(pct) / 100, None)
        if pct > 0:
            ep.SetMute(0, None)                           # "volume to 50" while muted should be heard
        return self.volume()

    def muted(self) -> bool:
        return bool(self._endpoint().GetMute())

    def set_mute(self, on: bool) -> None:
        self._endpoint().SetMute(1 if on else 0, None)

    # ---- brightness ----
    @staticmethod
    def _ps(script: str) -> str:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()

    def brightness(self) -> int:
        out = self._ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
                       "Select-Object -First 1).CurrentBrightness")
        if not out.strip().isdigit():
            raise SystemError_("I can't change the brightness on this screen. External monitors usually don't allow it.")
        return int(out)

    def set_brightness(self, pct: float) -> int:
        level = _clamp(pct)
        self._ps("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | "
                 f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}} | Out-Null")
        return self.brightness()
