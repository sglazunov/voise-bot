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


def test_модель_выбирается_из_названия_движка(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nvapi-test")
    p = llm.get_provider("nvidia:deepseek-ai/deepseek-v3")
    assert p.name == "nvidia"
    assert p.model == "deepseek-ai/deepseek-v3"


def test_лучшие_модели_идут_первыми(monkeypatch):
    _fake_catalog(monkeypatch, [
        "meta/llama-3.1-8b-instruct",
        "deepseek-ai/deepseek-r1",
        "moonshotai/kimi-k2-instruct",
        "deepseek-ai/deepseek-v3",
        "meta/llama-3.3-70b-instruct",
    ])
    out = llm.nvidia_models("nvapi-test")
    assert out[0].startswith("moonshotai/kimi")
    # Reasoning-модель не должна опережать обычные — она мешает строгому JSON.
    assert out.index("deepseek-ai/deepseek-v3") < out.index("deepseek-ai/deepseek-r1")
    assert out.index("meta/llama-3.3-70b-instruct") < out.index("deepseek-ai/deepseek-r1")


def test_неизвестные_модели_не_теряются(monkeypatch):
    """Каталог пополняется — новое имя должно оставаться в списке, просто ниже."""
    _fake_catalog(monkeypatch, ["zzz/brand-new-model", "moonshotai/kimi-k2-instruct"])
    out = llm.nvidia_models("nvapi-test")
    assert "zzz/brand-new-model" in out
    assert out[0].startswith("moonshotai/kimi")


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
