"""NVIDIA NIM: список моделей приходит ПО КЛЮЧУ, лучшие — первыми.

Каталог NVIDIA — сотня открытых моделей, и он меняется. Захардкоженные
идентификаторы устарели бы молча: «модель не найдена» вылезло бы в момент
сборки протокола, когда встреча уже записана. Поэтому список запрашивается
через /v1/models, а код лишь ранжирует его под нашу задачу.

Задача — час русской речи → строгий JSON-протокол: нужны сильный русский,
длинный контекст и послушность формату. Reasoning-модели (deepseek-r1) стоят
НИЖЕ обычных: они склонны «размышлять» в ответе, а нам нужен чистый JSON.
"""
import json

from app import config, llm


class _Resp:
    def __init__(self, ids):
        self._body = json.dumps({"data": [{"id": i} for i in ids]}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_catalog(monkeypatch, ids):
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(ids))


def test_провайдер_зарегистрирован():
    assert "nvidia" in config.KEY_PROVIDERS
    assert "nvidia" in config.PROVIDER_LABELS
    assert "nvidia" in llm._PROVIDERS


def test_счётчик_ключей_знает_всех_провайдеров():
    """У user_creds был СВОЙ список провайдеров, и он разошёлся с config: без
    «nvidia» counts() возвращал 0, и карточка показывала «0 ключа» при рабочем
    подключённом ключе. Списку положено быть одним."""
    from app import user_creds
    assert set(user_creds.KEY_PROVIDERS) == set(config.KEY_PROVIDERS)


def test_модель_выбирается_из_названия_движка(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nvapi-test")
    p = llm.get_provider("nvidia:deepseek-ai/deepseek-v3")
    assert p.name == "nvidia"
    assert p.model == "deepseek-ai/deepseek-v3"


def test_лучшие_модели_идут_первыми(monkeypatch):
    _fake_catalog(monkeypatch, [
        "meta/llama-3.1-8b-instruct",
        "deepseek-ai/deepseek-r1",
        "moonshotai/kimi-k2.6",
        "deepseek-ai/deepseek-v3",
        "meta/llama-3.3-70b-instruct",
    ])
    out = llm.nvidia_models("nvapi-test")
    # Впереди — длинноконтекстные DeepSeek и Kimi, а не Llama.
    assert out[0].startswith(("deepseek-ai/deepseek-v", "moonshotai/kimi"))
    assert out.index("moonshotai/kimi-k2.6") < out.index("meta/llama-3.3-70b-instruct")
    # Reasoning-модель не должна опережать обычные — она мешает строгому JSON.
    assert out.index("deepseek-ai/deepseek-v3") < out.index("deepseek-ai/deepseek-r1")
    assert out.index("meta/llama-3.3-70b-instruct") < out.index("deepseek-ai/deepseek-r1")


def test_версии_разных_семейств_не_сравниваются(monkeypatch):
    """«deepseek-v3» и «kimi-k2.6» — числа из разных вселенных. Если сравнивать
    их напрямую, порядок внутри одинакового приоритета становится случайным."""
    _fake_catalog(monkeypatch, ["moonshotai/kimi-k2.6", "deepseek-ai/deepseek-v3",
                                "deepseek-ai/deepseek-v4-pro"])
    out = llm.nvidia_models("nvapi-test")
    # Внутри DeepSeek версия решает…
    assert out.index("deepseek-ai/deepseek-v4-pro") < out.index("deepseek-ai/deepseek-v3")
    # …а Kimi стоит цельным блоком, а не втискивается между версиями DeepSeek.
    assert out.index("moonshotai/kimi-k2.6") > out.index("deepseek-ai/deepseek-v3")


def test_свежая_версия_обгоняет_старую(monkeypatch):
    """Каталог обновляется чаще этого файла: «deepseek-v4» должен обойти «v3»
    сам, без правок кода. Раньше сравнение шло по полному имени, и новая версия
    уезжала в конец списка как незнакомая — то есть лучшая модель оказывалась
    худшей по порядку."""
    _fake_catalog(monkeypatch, [
        "deepseek-ai/deepseek-v3",
        "deepseek-ai/deepseek-v4-flash",
        "deepseek-ai/deepseek-v4-pro",
    ])
    out = llm.nvidia_models("nvapi-test")
    assert out.index("deepseek-ai/deepseek-v4-pro") < out.index("deepseek-ai/deepseek-v3")
    # Внутри одной версии полная модель важнее быстрой: для протокола решает
    # качество. Без этого правила «flash» опережал «pro» просто по алфавиту.
    assert out.index("deepseek-ai/deepseek-v4-pro") < out.index("deepseek-ai/deepseek-v4-flash")


def test_облегчённые_модели_ниже_полноразмерных(monkeypatch):
    _fake_catalog(monkeypatch, [
        "meta/llama-3.1-8b-instruct", "meta/llama-3.3-70b-instruct"])
    out = llm.nvidia_models("nvapi-test")
    assert out[0] == "meta/llama-3.3-70b-instruct"


def test_неизвестные_модели_не_теряются(monkeypatch):
    """Каталог пополняется — новое имя должно оставаться в списке, просто ниже."""
    _fake_catalog(monkeypatch, ["zzz/brand-new-model", "moonshotai/kimi-k2-instruct"])
    out = llm.nvidia_models("nvapi-test")
    assert "zzz/brand-new-model" in out
    assert out[0].startswith("moonshotai/kimi")


class TestРеальныйКаталог:
    """Дословные имена из каталога NVIDIA (скриншоты пользователя)."""

    CHAT = ["moonshotai/kimi-k2.6", "deepseek-ai/deepseek-v4-pro",
            "qwen/qwen3-next-80b-a3b-instruct", "zai/glm-5.2",
            "nvidia/nemotron-3-ultra-550b-a55b", "meta/llama-3.3-70b-instruct"]
    NOT_CHAT = ["nvidia/nv-embed-v1", "nvidia/magpie-tts-zeroshot",
                "nvidia/cosmos3-nano", "meta/esm2-650m",
                "meta/llama-guard-4-12b", "nvidia/bevformer",
                "nvidia/nemotron-nano-12b-v2-vl", "google/paligemma",
                "nvidia/riva-translate-4b-instruct-v2"]

    def test_не_чат_модели_не_предлагаются(self, monkeypatch):
        """Каталог — это не только LLM: эмбеддинги, синтез речи, зрение,
        автопилот, даже белки. Выбрав такую, человек получил бы непонятную
        ошибку уже ПОСЛЕ встречи."""
        _fake_catalog(monkeypatch, self.CHAT + self.NOT_CHAT)
        out = llm.nvidia_models("nvapi-test")
        assert set(out) == set(self.CHAT), f"лишние/потерянные: {set(out) ^ set(self.CHAT)}"

    def test_moe_имена_не_считаются_мелкими(self, monkeypatch):
        """«80b-a3b» — 80 млрд всего, 3 млрд активных. Сила по первому числу;
        раньше «a3b» принималось за 3B-модель, и qwen3-next уезжал в конец."""
        _fake_catalog(monkeypatch, ["qwen/qwen3-next-80b-a3b-instruct",
                                    "meta/llama-3.1-8b-instruct"])
        out = llm.nvidia_models("nvapi-test")
        assert out[0] == "qwen/qwen3-next-80b-a3b-instruct"

    def test_длинный_контекст_впереди(self, monkeypatch):
        """DeepSeek V4 (1M токенов) и Kimi K2.6 (262K) — на них час встречи
        влезает целиком, без разбиения на части, где мы теряем детали."""
        _fake_catalog(monkeypatch, self.CHAT)
        out = llm.nvidia_models("nvapi-test")
        assert out[0] == "deepseek-ai/deepseek-v4-pro"
        assert out.index("moonshotai/kimi-k2.6") < out.index("meta/llama-3.3-70b-instruct")


class TestКаталогНеРавенПравам:
    """Каталог NVIDIA перечисляет ВСЁ опубликованное, а не то, что вправе
    вызывать аккаунт. Боевой ключ отвечал на kimi-k2.6 ошибкой 404 «Function
    '…': Not found for account '…'» — при том, что модель в списке была.
    Совет «выберите другую из списка» тут бесполезен: выбирать наугад."""

    def _probe(self, monkeypatch, ok_model):
        from app import main

        class _P:
            def __init__(self, mid):
                self.model = mid

            def complete(self, *a, **k):
                if self.model != ok_model:
                    raise RuntimeError("HTTP 404 … Not found for account")
                return "ok"

        monkeypatch.setattr(main.llm, "nvidia_models",
                            lambda key: ["a/one", "b/two", "c/three"])
        monkeypatch.setattr(main.llm, "get_provider",
                            lambda spec, trial=None: _P(spec.split(":", 1)[1]))
        return main._first_working_model("nvidia", {}, "nvapi-test")

    def test_называем_модель_которая_ответила(self, monkeypatch):
        assert self._probe(monkeypatch, "b/two") == "b/two"

    def test_если_не_ответил_никто_молчим(self, monkeypatch):
        """Подсказка необязательна — тогда показываем ответ сервиса как есть."""
        assert self._probe(monkeypatch, "нет такой") is None

    def test_чужих_провайдеров_не_трогаем(self, monkeypatch):
        """У Gemini/Groq каталог совпадает с правами — лишние запросы не нужны."""
        from app import main
        monkeypatch.setattr(main.llm, "nvidia_models",
                            lambda key: (_ for _ in ()).throw(
                                AssertionError("каталог не должен запрашиваться")))
        assert main._first_working_model("gemini", {}, "key") is None


class TestПроверкаКлюча:
    """Список движков должен показывать не каталог, а то, что ключ реально
    может вызвать. Узнать это можно только вызовом, поэтому перебираем модели
    и кэшируем результат."""

    def _probe(self, monkeypatch, tmp_path, ok):
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(llm.time, "sleep", lambda *_: None)  # без пауз
        _fake_catalog(monkeypatch, ["a/one", "b/two", "c/three"])

        class _P:
            def __init__(self, model=None, api_key=None, extra=None):
                self.model = model

            def complete(self, *a, **k):
                if self.model not in ok:
                    raise RuntimeError("HTTP 404 Not found for account")
                return "ok"

        monkeypatch.setattr(llm, "NvidiaProvider", _P)
        return llm.nvidia_verify_models("nvapi-test")

    def test_остаются_только_ответившие(self, monkeypatch, tmp_path):
        assert self._probe(monkeypatch, tmp_path, {"a/one", "c/three"}) == \
            ["a/one", "c/three"]

    def test_результат_кэшируется(self, monkeypatch, tmp_path):
        self._probe(monkeypatch, tmp_path, {"b/two"})
        assert llm.nvidia_usable_models("nvapi-test") == ["b/two"]

    def test_до_проверки_отвечаем_none(self, monkeypatch, tmp_path):
        """None — сигнал «ещё не знаем»: интерфейс тогда покажет весь каталог,
        а не пустой список."""
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        assert llm.nvidia_usable_models("nvapi-непроверенный") is None

    def test_умолчание_берётся_из_каталога(self, monkeypatch, tmp_path):
        """Прибитое имя модели молча превращается в 404: за неделю
        «deepseek-v4-pro» из каталога NVIDIA исчез, появился
        «deepseek-v4-flash-0731»."""
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(llm.config, "NVIDIA_MODEL", "устаревшая/модель")
        _fake_catalog(monkeypatch, ["deepseek-ai/deepseek-v4-flash-0731",
                                    "meta/llama-3.1-8b-instruct"])
        assert llm.nvidia_default_model("nvapi-test") == \
            "deepseek-ai/deepseek-v4-flash-0731"

    def test_без_сети_умолчание_из_настроек(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(llm.config, "NVIDIA_MODEL", "запасная/модель")
        monkeypatch.setattr(llm, "nvidia_models", lambda *a, **k: [])
        assert llm.nvidia_default_model("nvapi-test") == "запасная/модель"

    def test_чужая_ошибка_не_выбрасывает_модель(self, monkeypatch, tmp_path):
        """Таймаут или 500 — это про наш запрос, а не про права аккаунта.
        Раньше модель выбрасывалась по ЛЮБОЙ ошибке, и список врал."""
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
        _fake_catalog(monkeypatch, ["a/one", "b/two"])

        class _P:
            def __init__(self, model=None, api_key=None, extra=None):
                self.model = model

            def complete(self, *a, **k):
                if self.model == "a/one":
                    raise RuntimeError("HTTP 500: internal server error")
                raise RuntimeError("404 Not found for account 'X'")

        monkeypatch.setattr(llm, "NvidiaProvider", _P)
        # a/one осталась (мы не доказали недоступность), b/two выброшена.
        assert llm.nvidia_verify_models("nvapi-test") == ["a/one"]

    def test_лимит_не_считается_недоступностью(self, monkeypatch, tmp_path):
        """429 значит «ключ устал», а не «модель не выдана». Иначе перебор
        выбросил бы половину каталога из-за скорости собственных запросов."""
        monkeypatch.setattr(llm.config, "DATA_DIR", tmp_path)
        _fake_catalog(monkeypatch, ["a/one"])
        monkeypatch.setattr(llm.time, "sleep", lambda *_: None)

        class _P:
            def __init__(self, model=None, api_key=None, extra=None):
                pass

            def complete(self, *a, **k):
                raise RuntimeError("HTTP 429: rate limit exceeded")

        monkeypatch.setattr(llm, "NvidiaProvider", _P)
        assert llm.nvidia_verify_models("nvapi-test") == ["a/one"]

    def test_ключ_в_кэш_не_попадает(self, monkeypatch, tmp_path):
        self._probe(monkeypatch, tmp_path, {"a/one"})
        assert "nvapi-test" not in (tmp_path / "nvidia_usable.json").read_text(
            encoding="utf-8")


def test_каталог_чистится_от_нечатовых(monkeypatch):
    """Из боевого каталога 06.08: эмбеддинги, оценщики ответов, разбор
    документов и генерация картинок в списке движков не нужны."""
    _fake_catalog(monkeypatch, [
        "deepseek-ai/deepseek-v4-pro", "baai/bge-m3",
        "nvidia/nemotron-4-340b-reward", "nvidia/nemotron-parse",
        "google/diffusiongemma-26b-a4b-it", "nvidia/llama3-chatqa-1.5-70b",
        "meta/codellama-70b",
    ])
    assert llm.nvidia_models("nvapi-test") == ["deepseek-ai/deepseek-v4-pro"]


def test_без_ключа_не_ходим_в_сеть(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("сеть не должна дёргаться")))
    assert llm.nvidia_models() == []


def test_сбой_сети_не_ломает_интерфейс(monkeypatch):
    """Список движков строится при каждом открытии страницы — падать нельзя."""
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("нет сети")))
    assert llm.nvidia_models("nvapi-test") == []
