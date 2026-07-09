"""Screen + audio capture via ffmpeg (Linux).

Grab the virtual display with `x11grab` (Xvfb on $DISPLAY) and the meeting audio
from a PulseAudio monitor source: the browser plays the call into a null-sink
(`meet0`, `meet1`, … one per recording slot) and we record its monitor
(`meet0.monitor`) — the virtual "cable" that carries the meeting's sound.

Everything sits behind `build_ffmpeg_cmd` / `FFmpegRecorder`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def _display() -> str:
    return os.environ.get("DISPLAY", ":99")


def _screen_size() -> str:
    """Xvfb resolution as ffmpeg -video_size (WxH), from VTX_SCREEN_RES."""
    res = os.environ.get("VTX_SCREEN_RES", "1920x1080x24")
    return "x".join(res.split("x")[:2]) or "1920x1080"


def _pulse_source(cfg: dict) -> str:
    """The PulseAudio source to record the meeting from (the null-sink monitor)."""
    return ((cfg.get("audio_device") or "").strip()
            or os.environ.get("VTX_PULSE_MONITOR", "meet0.monitor"))


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    if shutil.which(ffmpeg) or Path(ffmpeg).exists():
        return True
    return False


def list_audio_devices(ffmpeg: str = "ffmpeg") -> list[str]:
    """PulseAudio sources ffmpeg can record from (the `*.monitor` ones carry the
    meeting's sound)."""
    try:
        proc = subprocess.run(["pactl", "list", "short", "sources"],
                              capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        return []
    names = []
    for line in proc.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1]:
            names.append(cols[1])
    return names


def test_audio_level(ffmpeg: str, device: str, seconds: int = 3) -> dict:
    """Record `seconds` from `device` and measure its volume (silence detector).

    Lets the UI tell the user whether the meeting's sound actually reaches the
    chosen device, instead of finding out only after a recording came out mute.
    """
    device = device or os.environ.get("VTX_PULSE_MONITOR", "meet0.monitor")
    infmt = ["-f", "pulse", "-i", device]
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", *infmt,
             "-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=seconds + 25)
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg не найден."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Таймаут проверки."}
    err = proc.stderr or ""
    m_mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    m_max = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    max_db = float(m_max.group(1)) if m_max else None
    mean_db = float(m_mean.group(1)) if m_mean else None
    if max_db is None:
        return {"ok": False, "error": "Не удалось открыть устройство: "
                + " ".join(err.strip().splitlines()[-2:])[:200]}
    has_sound = max_db > -80.0
    return {"ok": True, "has_sound": has_sound, "max_db": max_db, "mean_db": mean_db}


def build_ffmpeg_cmd(out_path: str, cfg: dict, window_title: str | None = None,
                     display: str | None = None, source: str | None = None) -> list[str]:
    """Build the ffmpeg capture command from settings.

    `display`/`source` pin capture to a specific Xvfb display and PulseAudio
    monitor (the parallel-recording slot); they default to the single-slot values.
    Records into one mp4. `window_title` is irrelevant on a headless display.
    """
    ffmpeg = cfg.get("ffmpeg_path") or "ffmpeg"
    capture_video = bool(cfg.get("capture_video", True))
    disp = display or _display()
    src = source or _pulse_source(cfg)

    cmd = [ffmpeg, "-y", "-hide_banner"]
    if capture_video:
        # -draw_mouse 0 hides the mouse cursor (Xvfb draws a bare "X" without a
        # cursor theme) so it never appears in the recording.
        cmd += ["-f", "x11grab", "-draw_mouse", "0", "-framerate", "10",
                "-video_size", _screen_size(), "-i", disp]
    cmd += ["-f", "pulse", "-i", src]
    if capture_video:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", out_path]
    return cmd


def readiness(cfg: dict) -> dict:
    ffmpeg = cfg.get("ffmpeg_path") or "ffmpeg"
    if not ffmpeg_available(ffmpeg):
        return {"ready": False,
                "detail": "ffmpeg не найден. Установите: sudo apt install ffmpeg."}
    sources = list_audio_devices(ffmpeg)
    monitors = [s for s in sources if s.startswith("meet") and s.endswith(".monitor")]
    if not monitors:
        return {"ready": False, "devices": sources,
                "detail": "Не найдено ни одного PulseAudio-монитора «meet*.monitor». "
                          "Проверьте, что запущен с VTX_RECORDER_ENABLED=1 (null-sink'и "
                          "создаются при старте)."}
    slots = os.getenv("VTX_MAX_CONCURRENT_RECORDINGS", "4")
    return {"ready": True, "devices": monitors,
            "detail": f"До {slots} параллельных записей "
                      f"(экраны+звук {', '.join(monitors)})"}


class FFmpegRecorder:
    """Start/stop an ffmpeg capture, finalising the file cleanly on stop."""

    def __init__(self, out_path: str, cfg: dict, on_log=None, window_title=None,
                 display=None, source=None):
        self.out_path = out_path
        self.cfg = cfg
        self._on_log = on_log or (lambda *_: None)
        self._proc: subprocess.Popen | None = None
        self.window_title = window_title
        self._display = display
        self._source = source
        self._log_path = out_path + ".ffmpeg.log"
        self._log_file = None

    def start(self) -> None:
        cmd = build_ffmpeg_cmd(self.out_path, self.cfg, self.window_title,
                               display=self._display, source=self._source)
        self._on_log("ffmpeg: " + " ".join(cmd))
        # Keep ffmpeg's stderr in a log so an immediate failure (window not
        # found, bad audio device) is diagnosable instead of a silent empty file.
        self._log_file = open(self._log_path, "w", encoding="utf-8", errors="replace")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=self._log_file)

    def error_tail(self, lines: int = 6) -> str:
        try:
            if self._log_file:
                self._log_file.flush()
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                return " | ".join(t.strip() for t in f.read().splitlines()[-lines:] if t.strip())
        except Exception:
            return ""

    def stop(self, timeout: int = 15) -> None:
        if not self._proc:
            return
        try:
            # 'q' tells ffmpeg to stop and write the moov atom (valid mp4).
            if self._proc.stdin:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
            self._proc.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        finally:
            self._proc = None
            try:
                if self._log_file:
                    self._log_file.close()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
