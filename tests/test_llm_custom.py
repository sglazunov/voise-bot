"""Любой провайдер по одному ключу: адрес, авторизация и модели — сами.

Зачем это вообще. Под каждого поставщика писался свой класс, а поставщики
меняются: NVIDIA за неделю дважды поменяла состав бесплатных моделей и отобрала
DeepSeek, из-за чего протоколы молча уходили запасному движку. Универсальный
провайдер снимает саму причину: подключается что угодно OpenAI-совместимое.
"""
from __future__ import annotations

import json

import pytest

from app import llm_custom
from app.llm_custom import CustomProvider, detect, hint_for


class TestПодсказкаПоВидуКлюча:
    """Вид ключа сужает поиск: не перебирать же все известные адреса подряд."""

    def test_известные_ключи_узнаются(self):
        assert hint_for("nvapi-abc")[0] == "NVIDIA NIM"
        assert hint_for("gsk_abc")[0] == "Groq"
        assert hint_for("sk-or-abc")[0] == "OpenRouter"

    def test_незнакомый_ключ_не_выдумывает_поставщика(self):
        assert hint_for("abcdef")[0] == ""
        assert hint_for("")[0] == ""


class TestОпределение:
    def _fake_models(self, monkeypatch, ok_url, ok_style, models):
        def fake(base_url, key, style, timeout=15):
            return list(models) if (base_url == ok_url and style == ok_style) else []
        monkeypatch.setattr(llm_custom, "list_models", fake)

    def test_адрес_и_авторизация_подбираются(self, monkeypatch):
        self._fake_models(monkeypatch, "https://api.example.com/v1", "bearer",
                          ["m-1", "m-2"])
        got = detect("sk-whatever", "https://api.example.com/v1")
        assert got["ok"] and got["auth"] == "bearer"
        assert got["models"] == ["m-1", "m-2"]

    def test_нестандартный_заголовок_тоже_находится(self, monkeypatch):
        """MiMo от Xiaomi ждёт заголовок api-key, а не Authorization: Bearer."""
        self._fake_models(monkeypatch, "https://api.xiaomimimo.com/v1", "api-key",
                          ["mimo-v2.5-pro"])
        got = detect("любой-ключ", "https://api.xiaomimimo.com/v1")
        assert got["ok"] and got["auth"] == "api-key"

    def test_известный_ключ_не_требует_адреса(self, monkeypatch):
        self._fake_models(monkeypatch, "https://api.groq.com/openai/v1", "bearer",
                          ["llama"])
        got = detect("gsk_abc")
        assert got["ok"] and got["base_url"] == "https://api.groq.com/openai/v1"

    def test_негодный_ключ_даёт_понятный_отказ(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models",
                            lambda *a, **k: [])
        got = detect("мусор", "https://api.example.com/v1")
        assert not got["ok"] and "Проверьте адрес и ключ" in got["error"]

    def test_пустой_ключ_не_ходит_в_сеть(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: pytest.fail(
            "при пустом ключе запросов быть не должно"))
        assert detect("")["ok"] is False


class TestПровайдер:
    def _prov(self, **cfg):
        base = {"base_url": "https://api.example.com/v1", "auth": "bearer",
                "model": "m-1"}
        base.update(cfg)
        return CustomProvider(api_key="k", extra=json.dumps(base))

    def test_настройки_читаются_из_extra(self):
        p = self._prov()
        assert p.base_url == "https://api.example.com/v1"
        assert p.model == "m-1"
        assert p._headers() == {"Authorization": "Bearer k"}

    def test_нестандартный_заголовок(self):
        assert self._prov(auth="api-key")._headers() == {"api-key": "k"}

    def test_без_настроек_понятная_ошибка(self):
        """Молчаливый сбой здесь означал бы протокол, ушедший запасному движку
        без объяснения причины."""
        p = CustomProvider(api_key="k", extra="")
        with pytest.raises(RuntimeError, match="не настроен"):
            p.complete("текст")

    def test_битый_extra_не_роняет_создание(self):
        p = CustomProvider(api_key="k", extra="{это не json")
        assert p.base_url == "" and p.model == ""

    def test_запрос_уходит_по_нужному_адресу(self, monkeypatch):
        seen = {}

        def fake_post(url, payload, headers, timeout=None, max_retries=None):
            seen.update(url=url, payload=payload, headers=headers)
            return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

        monkeypatch.setattr(llm_custom, "_http_post_json", fake_post)
        assert self._prov().complete("текст") == '{"ok": 1}'
        assert seen["url"] == "https://api.example.com/v1/chat/completions"
        assert seen["payload"]["model"] == "m-1"
        assert seen["headers"]["Authorization"] == "Bearer k"

    def test_response_format_снимается_при_отказе(self, monkeypatch):
        """Его понимают не все модели, а JSON мы всё равно проверяем сами."""
        calls: list[dict] = []

        def fake_post(url, payload, headers, timeout=None, max_retries=None):
            calls.append(dict(payload))
            if "response_format" in payload:
                raise RuntimeError("HTTP 400: response_format is not supported")
            return {"choices": [{"message": {"content": "{}"}}]}

        monkeypatch.setattr(llm_custom, "_http_post_json", fake_post)
        self._prov().complete("текст")
        assert len(calls) == 2
        assert "response_format" in calls[0] and "response_format" not in calls[1]


class TestСвойСервер:
    """Локальный vLLM, llama.cpp, LM Studio или Ollama обычно вообще не
    спрашивает ключа — там достаточно адреса."""

    def test_без_ключа_но_с_адресом_подключается(self, monkeypatch):
        def fake(base_url, key, style, timeout=15):
            # Свой сервер отвечает и без заголовка авторизации.
            return ["deepseek-r1:8b"] if style == "none" else []
        monkeypatch.setattr(llm_custom, "list_models", fake)
        got = detect("", "http://vllm:8000/v1")
        assert got["ok"] and got["auth"] == "none"
        assert got["models"] == ["deepseek-r1:8b"]

    def test_без_ключа_и_без_адреса_понятный_отказ(self):
        got = detect("", "")
        assert not got["ok"] and "адрес" in got["error"]

    def test_заголовок_не_шлётся_когда_ключа_нет(self):
        p = CustomProvider(api_key="", extra=json.dumps(
            {"base_url": "http://vllm:8000/v1", "auth": "none", "model": "m"}))
        assert p._headers() == {}

    def test_подсказка_про_localhost_в_контейнере(self, monkeypatch):
        """Частая ошибка: localhost внутри контейнера — это сам контейнер."""
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: [])
        got = detect("", "http://localhost:8000/v1")
        assert "контейнер" in got["error"]


class TestВидимостьПровайдера:
    """Карточки на странице «Нейросети» строятся по PROVIDER_ORDER, а он берётся
    из .env — написан однажды и живёт годами. Новый провайдер иначе не появился
    бы в интерфейсе вовсе: способ подключиться был бы, а показать его негде."""

    def test_custom_есть_в_порядке(self):
        from app import config
        assert "custom" in config.PROVIDER_ORDER

    def test_ни_один_провайдер_не_потерян(self):
        """Каждому известному провайдеру нужно место в списке — иначе его
        карточка просто не отрисуется."""
        from app import config
        missing = [p for p in config.PROVIDER_LABELS if p not in config.PROVIDER_ORDER]
        assert not missing, f"нет в PROVIDER_ORDER: {missing}"

    def test_custom_принимает_ключ(self):
        from app import config
        assert "custom" in config.KEY_PROVIDERS


class TestНормализацияАдреса:
    """Люди вставляют полный URL из документации. Без обрезки вышло бы
    «…/chat/completions/models», и адрес молча не работал бы."""

    def test_хвосты_обрезаются(self):
        n = llm_custom.normalize_url
        assert n("https://api.x.com/v1/chat/completions") == "https://api.x.com/v1"
        assert n("https://api.x.com/v1/models") == "https://api.x.com/v1"
        assert n("https://api.x.com/v1/") == "https://api.x.com/v1"

    def test_обычный_адрес_не_трогается(self):
        assert llm_custom.normalize_url("https://api.deepseek.com") == "https://api.deepseek.com"


class TestЗащитаАдреса:
    """Ключ отправляется на указанный адрес, поэтому адрес проверяется ДО."""

    def test_служебный_адрес_облака_запрещён(self):
        """По 169.254.169.254 облачные машины отдают собственные учётные данные."""
        assert "служебный" in llm_custom.check_url("http://169.254.169.254/v1")

    def test_публичный_http_запрещён(self):
        """Иначе ключ уйдёт по сети незашифрованным."""
        assert "https" in llm_custom.check_url("http://api.example.com/v1")

    def test_свой_сервер_по_http_разрешён(self):
        assert llm_custom.check_url("http://vllm:8000/v1") == ""
        assert llm_custom.check_url("http://192.168.1.10:8000/v1") == ""
        assert llm_custom.check_url("http://localhost:11434/v1") == ""

    def test_чужая_схема_запрещена(self):
        assert "http://" in llm_custom.check_url("ftp://api.example.com/v1")


class TestНеподдерживаемыйКлюч:
    def test_anthropic_отвергается_без_запросов(self, monkeypatch):
        """У Anthropic другой формат API. Перебор чужих адресов означал бы
        отправку ключа третьим лицам — этого делать нельзя."""
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: pytest.fail(
            "ключ Anthropic не должен уходить ни на один адрес"))
        got = detect("sk-ant-api03-секрет")
        assert not got["ok"] and "Anthropic" in got["error"]


class TestФильтрМоделей:
    """В /models лежат вперемешку эмбеддинги и реранкеры: протокол они не
    соберут, а счётчик «доступно моделей — N» сбивают."""

    def test_не_чат_модели_отсеиваются(self):
        assert llm_custom.is_chat_model("deepseek-chat")
        assert llm_custom.is_chat_model("nvidia/nemotron-3-super-120b")
        assert not llm_custom.is_chat_model("text-embedding-3-large")
        assert not llm_custom.is_chat_model("bge-reranker-v2")
        assert not llm_custom.is_chat_model("whisper-large-v3")
        assert not llm_custom.is_chat_model("stable-diffusion-xl")

    def test_detect_возвращает_только_чатовые(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: [
            "чат/модель", "text-embedding-ada", "rerank-v1"])
        got = detect("ключ", "https://api.example.com/v1")
        assert got["models"] == ["чат/модель"]
        assert got["skipped"] == 2

    def test_только_эмбеддинги_считаются_неподходящим_адресом(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models",
                            lambda *a, **k: ["text-embedding-3"])
        assert detect("ключ", "https://api.example.com/v1")["ok"] is False


class TestПробныйВызов:
    def test_живая_модель(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "_http_post_json",
                            lambda *a, **k: {"choices": [{"message": {"content": "ok"}}]})
        ok, why = llm_custom.ping_model("https://x/v1", "k", "bearer", "m")
        assert ok and why == ""

    def test_модель_не_выдана_ключу(self, monkeypatch):
        """Тот самый разрыв: модель есть в каталоге, а вызов даёт 404."""
        def boom(*a, **k):
            raise RuntimeError("HTTP 404: Function not found for account")
        monkeypatch.setattr(llm_custom, "_http_post_json", boom)
        ok, why = llm_custom.ping_model("https://x/v1", "k", "bearer", "m")
        assert not ok and "404" in why


class TestПрефиксыИзКонфига:
    """Таблица в конфиге, а не в логике: поставщики меняются, и дополнять
    список правкой конфига проще, чем правкой кода."""

    def test_все_приставки_openai(self):
        for k in ("sk-proj-a", "sk-svcacct-a", "sk-None-a"):
            assert hint_for(k)[0] == "OpenAI", k

    def test_новые_поставщики(self):
        assert hint_for("pplx-a")[0] == "Perplexity"
        assert hint_for("tp-a")[0].startswith("Xiaomi MiMo")
        assert hint_for("AIzaSyA")[0] == "Google Gemini"


class TestЗапретМоделиПоКлючу:
    """404 «not found for account» у одного ключа не значит того же у другого:
    права выдаются на ключ. Поэтому запрет частный, а срок суточный — права
    меняются на стороне поставщика, и вечный запрет похоронил бы вернувшуюся
    модель навсегда."""

    def setup_method(self):
        llm_custom._denied.clear()

    def test_запрет_частный_для_ключа(self):
        llm_custom.mark_denied("ключ-один", "модель")
        assert llm_custom.is_denied("ключ-один", "модель")
        assert not llm_custom.is_denied("ключ-два", "модель")

    def test_срок_истекает(self, monkeypatch):
        """Сутки прошли — модель снова пробуется: у поставщика права меняются."""
        llm_custom.mark_denied("ключ", "модель")
        assert llm_custom.is_denied("ключ", "модель")
        # Оригинал фиксируем ДО подмены: лямбда, зовущая подменённый time.time,
        # вызвала бы саму себя.
        real_now = llm_custom.time.time()
        monkeypatch.setattr(llm_custom.time, "time",
                            lambda: real_now + llm_custom.DENY_TTL + 1)
        assert not llm_custom.is_denied("ключ", "модель")

    def test_сам_ключ_не_хранится(self):
        """В памяти только отпечаток: ключ не должен лежать в структурах."""
        llm_custom.mark_denied("nvapi-очень-секретный", "модель")
        assert not any("секретный" in str(k) for k in llm_custom._denied)

    def test_пометка_ставится_на_404(self, monkeypatch):
        p = CustomProvider(api_key="k", extra=json.dumps(
            {"base_url": "https://x/v1", "auth": "bearer", "model": "м"}))

        def boom(*a, **kw):
            raise RuntimeError("HTTP 404: Function 'x' not found for account")

        monkeypatch.setattr(p, "complete", boom)
        with pytest.raises(RuntimeError):
            p.complete_guarded("текст")
        assert llm_custom.is_denied("k", "м")

    def test_обычная_ошибка_не_помечает(self, monkeypatch):
        """Сетевой сбой или 500 — это не «модель не выдана»."""
        p = CustomProvider(api_key="k", extra=json.dumps(
            {"base_url": "https://x/v1", "auth": "bearer", "model": "м"}))
        monkeypatch.setattr(p, "complete", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("HTTP 500: internal error")))
        with pytest.raises(RuntimeError):
            p.complete_guarded("текст")
        assert not llm_custom.is_denied("k", "м")


class TestYandexCloud:
    """Yandex Cloud AI Studio даёт DeepSeek через OpenAI-совместимый шлюз, но
    список моделей не отдаёт, а имя модели включает идентификатор каталога:
    «gpt://<folder>/deepseek-v4-flash/latest»."""

    def test_подключение_из_переменных_окружения(self, monkeypatch):
        from app import config
        monkeypatch.setattr(config, "YANDEX_CLOUD_API_KEY", "AQVN-тест")
        monkeypatch.setattr(config, "YANDEX_CLOUD_FOLDER", "b1g9i77")
        monkeypatch.setattr(config, "YANDEX_CLOUD_MODEL", "deepseek-v4-flash/latest")
        cfg = json.loads(config.custom_env_extra())
        assert cfg["model"] == "gpt://b1g9i77/deepseek-v4-flash/latest"
        assert cfg["base_url"].endswith("/v1")

    def test_полное_имя_модели_не_ломается(self, monkeypatch):
        """Если в .env уже записан полный gpt://…, второй раз каталог не клеим."""
        from app import config
        monkeypatch.setattr(config, "YANDEX_CLOUD_API_KEY", "AQVN")
        monkeypatch.setattr(config, "YANDEX_CLOUD_FOLDER", "b1g")
        monkeypatch.setattr(config, "YANDEX_CLOUD_MODEL", "gpt://другой/модель")
        assert json.loads(config.custom_env_extra())["model"] == "gpt://другой/модель"

    def test_без_каталога_подключения_нет(self, monkeypatch):
        from app import config
        monkeypatch.setattr(config, "YANDEX_CLOUD_API_KEY", "AQVN")
        monkeypatch.setattr(config, "YANDEX_CLOUD_FOLDER", "")
        assert config.custom_env_extra() == ""

    def test_шлюз_без_списка_требует_модель(self, monkeypatch):
        """Список не спрашиваем — но и подключиться вслепую нельзя."""
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: pytest.fail(
            "у этого шлюза список моделей не запрашивается"))
        got = detect("AQVN-ключ", "https://ai.api.cloud.yandex.net/v1")
        assert not got["ok"] and "укажите их вручную" in got["error"].lower()

    def test_модель_подтверждается_вызовом(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: pytest.fail(
            "список запрашиваться не должен"))
        monkeypatch.setattr(llm_custom, "ping_model",
                            lambda url, key, style, model: (style == "bearer", "нет"))
        got = detect("AQVN-ключ", "https://ai.api.cloud.yandex.net/v1",
                     "gpt://b1g/deepseek-v4-flash/latest")
        assert got["ok"] and got["manual"] is True
        assert got["models"] == ["gpt://b1g/deepseek-v4-flash/latest"]

    def test_модель_с_двоеточиями_переживает_разбор(self):
        """Движок выбирается строкой «custom:<модель>», а в имени модели свои
        двоеточия и слэши — делить надо только по первому."""
        name = "custom:gpt://b1g/deepseek-v4-flash/latest"
        base, _, model = name.partition(":")
        assert base == "custom"
        assert model == "gpt://b1g/deepseek-v4-flash/latest"


class TestНесколькоМоделейНаКлюч:
    """На один ключ Yandex Cloud вешается несколько моделей. Ограничиться одной
    значило бы, что остальные, за которые уже заплачено, недоступны в выборе."""

    def test_разбор_списка(self):
        f = llm_custom.split_models
        assert f("a, b ,c") == ["a", "b", "c"]
        assert f("gpt://b1g/deepseek-v4-flash/latest, gpt://b1g/yandexgpt/latest") == [
            "gpt://b1g/deepseek-v4-flash/latest", "gpt://b1g/yandexgpt/latest"]
        assert f("a,,a, b") == ["a", "b"], "дубли и пустые отбрасываются"
        assert f("") == []

    def test_подключаются_только_ответившие(self, monkeypatch):
        """Не ответившая модель не должна попасть в список: выбрать её значит
        получить ошибку в момент сборки протокола."""
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: [])
        monkeypatch.setattr(llm_custom, "ping_model",
                            lambda url, key, style, m: ("живая" in m, "404"))
        got = detect("AQVN", "https://ai.api.cloud.yandex.net/v1",
                     "gpt://b/живая, gpt://b/мёртвая")
        assert got["ok"]
        assert got["models"] == ["gpt://b/живая"]
        assert got["failed"] == ["gpt://b/мёртвая"]

    def test_если_не_ответила_ни_одна(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: [])
        monkeypatch.setattr(llm_custom, "ping_model",
                            lambda *a, **k: (False, "HTTP 404"))
        got = detect("AQVN", "https://ai.api.cloud.yandex.net/v1", "a, b")
        assert not got["ok"] and "Ни одна из моделей" in got["error"]

    def test_каталог_подставляется_каждой(self, monkeypatch):
        from app import config
        monkeypatch.setattr(config, "YANDEX_CLOUD_API_KEY", "AQVN")
        monkeypatch.setattr(config, "YANDEX_CLOUD_FOLDER", "b1g")
        monkeypatch.setattr(config, "YANDEX_CLOUD_MODEL",
                            "deepseek-v4-flash/latest, gpt://чужой/модель, yandexgpt")
        models = json.loads(config.custom_env_extra())["models"]
        assert models == ["gpt://b1g/deepseek-v4-flash/latest",
                          "gpt://чужой/модель",      # уже полное — не трогаем
                          "gpt://b1g/yandexgpt"]
