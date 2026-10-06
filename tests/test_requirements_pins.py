"""Границы версий в requirements.txt, на которых уже ломался бой.

PyAV не указан у faster-whisper сверху (`av>=11`), и пересборка образа 05.10
подтянула PyAV 19 без параметра `metadata_errors` — после неё не распознавалась
ни одна встреча. Тест не даёт молча снять границу."""
from __future__ import annotations

import re
from pathlib import Path

REQ = Path(__file__).resolve().parent.parent / "requirements.txt"


def _spec(name: str) -> str:
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if re.match(rf"^{re.escape(name)}\b", line, re.I):
            return line
    return ""


def test_pyav_ниже_19_пока_faster_whisper_передаёт_metadata_errors():
    spec = _spec("av")
    assert spec, "PyAV должен быть закреплён явно: faster-whisper сверху его не ограничивает"
    m = re.search(r"<\s*(\d+)", spec)
    assert m and int(m.group(1)) <= 19, spec
    fw = _spec("faster-whisper")
    assert fw.startswith("faster-whisper==1.2"), (
        "faster-whisper сменился — проверьте, передаёт ли он ещё metadata_errors, "
        "и тогда пересмотрите границу PyAV")


def test_у_каждой_зависимости_есть_верхняя_граница():
    bad = []
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not re.search(r"(<|==)", line):
            bad.append(line)
    assert not bad, f"без верхней границы: {bad}"
