"""Две одновременные записи под аккаунтом Яндекса — два разных профиля.

Боевой случай 02.09.2026: встреча 15:30 ещё писалась, встреча 16:00 не
смогла войти («Не удалось войти в встречу») и зашла только после того, как
первая закончилась. Оба бота открывали Chromium на одном каталоге профиля.
"""
from __future__ import annotations

from pathlib import Path

from app.automation.recorder import browser


def _master(tmp_path: Path) -> Path:
    m = tmp_path / "browser-profile"
    (m / "Default" / "Network").mkdir(parents=True)
    (m / "Default" / "Cookies").write_bytes(b"cookies-v1")
    (m / "Default" / "Network" / "Cookies").write_bytes(b"net-cookies")
    (m / "Local State").write_text("{}")
    (m / "Default" / "Cache").mkdir()
    (m / "Default" / "Cache" / "data_0").write_bytes(b"x" * 1000)
    (m / "SingletonLock").write_text("host-123")
    return m


class TestКопияПрофиляПодСлот:
    def test_копия_несёт_вход_но_не_кэш_и_не_замки(self, tmp_path):
        clone = browser._slot_profile(_master(tmp_path), ":100")
        assert clone == tmp_path / "browser-profile-slot100"
        assert (clone / "Default" / "Cookies").read_bytes() == b"cookies-v1"
        assert (clone / "Default" / "Network" / "Cookies").exists()
        assert (clone / "Local State").exists()
        assert not (clone / "Default" / "Cache").exists()
        assert not (clone / "SingletonLock").exists()

    def test_два_слота_два_каталога(self, tmp_path):
        m = _master(tmp_path)
        a, b = browser._slot_profile(m, ":99"), browser._slot_profile(m, ":100")
        assert a != b and a != m and b != m
        assert a.is_dir() and b.is_dir()

    def test_копия_обновляется_после_нового_входа(self, tmp_path):
        m = _master(tmp_path)
        browser._slot_profile(m, ":99")
        (m / "Default" / "Cookies").write_bytes(b"cookies-v2")
        clone = browser._slot_profile(m, ":99")
        assert (clone / "Default" / "Cookies").read_bytes() == b"cookies-v2"

    def test_без_экрана_тег_solo_и_пустой_мастер_не_ломает(self, tmp_path):
        m = tmp_path / "browser-profile"
        m.mkdir()
        clone = browser._slot_profile(m, "")
        assert clone.name.endswith("-slotsolo") and clone.is_dir()

    def test_launch_берёт_копию_а_не_мастер(self, tmp_path, monkeypatch):
        """_launch в режиме profile должен передать Playwright копию под слот."""
        m = _master(tmp_path)
        seen = {}

        class _Ctx:
            pages = []
            def new_page(self): return object()

        class _Chromium:
            def launch_persistent_context(self, user_dir, **kw):
                seen["dir"] = user_dir; return _Ctx()

        class _PW:
            chromium = _Chromium()

        class _SyncPW:
            def start(self): return _PW()

        import sys, types
        fake = types.ModuleType("playwright.sync_api"); fake.sync_playwright = lambda: _SyncPW()
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
        monkeypatch.setattr(browser, "_profile_dir", lambda cfg: m)
        monkeypatch.setattr(browser, "_silent_wav", lambda: str(tmp_path / "s.wav"))
        bot = browser.TelemostBot({"auth_mode": "profile"}, display=":100", sink="meet1")
        bot._launch()
        assert seen["dir"] == str(tmp_path / "browser-profile-slot100")
        assert "--password-store=basic" in browser._LAUNCH_ARGS
