"""Screen + audio capture via ffmpeg (cross-platform).

Linux (this deploy): grab the virtual display with `x11grab` (Xvfb on $DISPLAY)
and the meeting audio from a PulseAudio monitor source. The browser plays the
call into a null-sink (default sink "meet"); its monitor `meet.monitor` is what
we record — the Linux equivalent of a Windows loopback/VB-CABLE.

Windows (original): `gdigrab` for the desktop/window + `dshow` for a loopback
device (Stereo Mix / VB-CABLE). Both backends sit behind the same
`build_ffmpeg_cmd` / `FFmpegRecorder` interface.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _display() -> str:
    return os.environ.get("DISPLAY", ":99")


def _screen_size() -> str:
    """Xvfb resolution as ffmpeg -video_size (WxH), from VTX_SCREEN_RES."""
    res = os.environ.get("VTX_SCREEN_RES", "1920x1080x24")
    return "x".join(res.split("x")[:2]) or "1920x1080"


def _pulse_source(cfg: dict) -> str:
    """The PulseAudio source to record the meeting from (the null-sink monitor)."""
    return ((cfg.get("audio_device") or "").strip()
            or os.environ.get("VTX_PULSE_MONITOR", "meet.monitor"))


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    if shutil.which(ffmpeg) or Path(ffmpeg).exists():
        return True
    return False


def list_audio_devices(ffmpeg: str = "ffmpeg") -> list[str]:
    """Audio capture sources: PulseAudio sources on Linux, dshow on Windows."""
    if _is_linux():
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
    if sys.platform != "win32" or not ffmpeg_available(ffmpeg):
        return []
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow",
         "-i", "dummy"],
        capture_output=True, text=True, errors="replace")
    return _parse_dshow_audio(proc.stderr)


def _parse_dshow_audio(stderr: str) -> list[str]:
    """Parse `ffmpeg -list_devices` output for audio device names.

    ffmpeg prints lines like:  [dshow @ ...]  "CABLE Output (VB-Audio...)" (audio)
    Older builds print a separate "DirectShow audio devices" section instead.
    """
    names, in_audio_section = [], False
    for line in stderr.splitlines():
        low = line.lower()
        if "audio devices" in low:
            in_audio_section = True
            continue
        if "video devices" in low:
            in_audio_section = False
            continue
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        if "(audio)" in low or (in_audio_section and "alternative name" not in low):
            name = m.group(1)
            if name not in names and not name.startswith("@device"):
                names.append(name)
    return names


def test_audio_level(ffmpeg: str, device: str, seconds: int = 3) -> dict:
    """Record `seconds` from `device` and measure its volume (silence detector).

    Lets the UI tell the user whether the meeting's sound actually reaches the
    chosen device, instead of finding out only after a recording came out mute.
    """
    if _is_linux():
        device = device or os.environ.get("VTX_PULSE_MONITOR", "meet.monitor")
        infmt = ["-f", "pulse", "-i", device]
    elif not device:
        return {"ok": False, "error": "Не выбрано аудио-устройство."}
    else:
        infmt = ["-f", "dshow", "-i", f"audio={device}"]
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


def build_ffmpeg_cmd(out_path: str, cfg: dict, window_title: str | None = None) -> list[str]:
    """Build the ffmpeg capture command from settings.

    Records video (just the browser window if `window_title` is given, else the
    whole desktop) and the configured audio device into one mp4.
    """
    ffmpeg = cfg.get("ffmpeg_path") or "ffmpeg"
    capture_video = bool(cfg.get("capture_video", True))
    cmd = [ffmpeg, "-y", "-hide_banner"]

    if _is_linux():
        # Capture the whole Xvfb display + the PulseAudio monitor of the sink the
        # browser plays into. window_title is irrelevant headless.
        if capture_video:
            # -draw_mouse 0 hides the mouse cursor (Xvfb draws a bare "X" without
            # a cursor theme) so it never appears in the recording.
            cmd += ["-f", "x11grab", "-draw_mouse", "0", "-framerate", "10",
                    "-video_size", _screen_size(), "-i", _display()]
        cmd += ["-f", "pulse", "-i", _pulse_source(cfg)]
        if capture_video:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "128k", out_path]
        return cmd

    audio = (cfg.get("audio_device") or "").strip()
    if capture_video:
        # Capture only the meeting's browser window (cleaner than the whole
        # desktop); fall back to the full desktop if no title is known.
        src = f"title={window_title}" if window_title else "desktop"
        cmd += ["-f", "gdigrab", "-framerate", "10", "-i", src]
    if audio:
        cmd += ["-f", "dshow", "-i", f"audio={audio}"]
    if capture_video:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [out_path]
    return cmd


def readiness(cfg: dict) -> dict:
    ffmpeg = cfg.get("ffmpeg_path") or "ffmpeg"
    if not ffmpeg_available(ffmpeg):
        return {"ready": False,
                "detail": "ffmpeg не найден. Установите ffmpeg и/или укажите путь."}
    if _is_linux():
        src = _pulse_source(cfg)
        sources = list_audio_devices(ffmpeg)
        if sources and src not in sources:
            return {"ready": False, "devices": sources,
                    "detail": f"PulseAudio-источник «{src}» не найден. Доступны: {sources}. "
                              "Проверьте, что null-sink 'meet' создан (entrypoint)."}
        return {"ready": True, "devices": sources,
                "detail": f"Linux: экран {_display()} ({_screen_size()}) + звук pulse «{src}»"}
    audio = (cfg.get("audio_device") or "").strip()
    if not audio:
        devices = list_audio_devices(ffmpeg)
        keys = ("cable", "voicemeeter", "stereo mix", "стерео микшер",
                "loopback", "what u hear", "what you hear")
        has_loop = any(any(k in d.lower() for k in keys) for d in devices)
        if has_loop:
            hint = f"Выберите аудио-устройство из списка (есть подходящее). Найдено: {devices}"
        elif devices:
            hint = ("Нет виртуального аудио-устройства для записи звука встречи. "
                    "Нажмите «Установить виртуальное аудио (VB-CABLE)» ниже, "
                    f"либо выберите подходящее вручную. Найдено: {devices}")
        else:
            hint = ("Нет ни одного аудио-устройства для захвата. Нажмите "
                    "«Установить виртуальное аудио (VB-CABLE)» ниже.")
        return {"ready": False, "detail": hint, "devices": devices}
    return {"ready": True, "detail": f"ffmpeg + аудио: {audio}"}


class FFmpegRecorder:
    """Start/stop an ffmpeg capture, finalising the file cleanly on stop."""

    def __init__(self, out_path: str, cfg: dict, on_log=None, window_title=None):
        self.out_path = out_path
        self.cfg = cfg
        self._on_log = on_log or (lambda *_: None)
        self._proc: subprocess.Popen | None = None
        self.window_title = window_title
        self._log_path = out_path + ".ffmpeg.log"
        self._log_file = None

    def start(self) -> None:
        cmd = build_ffmpeg_cmd(self.out_path, self.cfg, self.window_title)
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
