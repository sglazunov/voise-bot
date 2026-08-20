"""Атака: `POST /api/model/download?name=…` качает что угодно и кому угодно.

Маршрут (main.py:1176) не спрашивает ни роль, ни имя модели: у него нет
`Depends(current_user)` (пускает общий шлюз — то есть ЛЮБОЙ вошедший, а
регистрация в сервисе открыта всем, кто знает адрес — см. CLAUDE.md,
«Особенности»), и нет проверки `name`.

`whisper_setup._eff` (whisper_setup.py:29) возвращает строку как есть, а
`faster_whisper.utils.download_model` трактует имя со слэшем как
идентификатор репозитория HuggingFace:

    if re.match(r".*/.*", size_or_path):
        repo_id = size_or_path          # качаем ЧУЖОЙ репозиторий

Что можно сделать:
  * забить диск боевого сервера (7.7 ГБ ОЗУ / общий том `./data` и кэш HF) —
    достаточно назвать крупный репозиторий и повторить с разными именами;
  * затянуть в контейнер произвольные файлы из интернета;
  * распухнуть словарь `whisper_setup._dl`: он бессрочно хранит запись на
    КАЖДОЕ присланное имя.

В main.py:207 записано, что зависимость `require_admin` убрана, потому что
«общесерверных операций не осталось». Эта — осталась.
"""
from __future__ import annotations

import pytest

from app import whisper_setup
from conftest import register


class TestModelDownloadInput:
    def test_arbitrary_repo_id_is_rejected(self, client):
        register(client)
        r = client.post("/api/model/download?name=attacker/huge-model-repo")
        assert r.status_code == 400, (
            "сервер принял чужой репозиторий HuggingFace как «модель»: "
            f"{r.status_code} {r.json()}")

    def test_unknown_model_name_is_rejected(self, client):
        register(client)
        r = client.post("/api/model/download?name=../../../etc")
        assert r.status_code == 400, f"{r.status_code} {r.json()}"

    def test_progress_table_does_not_grow_on_garbage(self, client):
        register(client)
        before = set(whisper_setup._dl)
        for i in range(20):
            client.post(f"/api/model/download?name=junk-{i}")
        assert set(whisper_setup._dl) == before, (
            "каждое присланное имя оседает в памяти навсегда: "
            f"{sorted(set(whisper_setup._dl) - before)}")
