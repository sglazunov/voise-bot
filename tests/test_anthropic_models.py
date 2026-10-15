"""Claude по API на моделях текущего поколения (15.10.2026).

Протокол «Виртуальный помощник — с заказчиком» ушёл запасному Gemini с
ответом Anthropic 400 «`temperature` is deprecated for this model»: провайдер
всегда слал temperature=0.1. У тех же моделей мышление включено, первым блоком
ответа идёт `thinking`, и прежнее `content[0].text` упало бы и без 400.
Подменяется только SDK (`anthropic.Anthropic`) — код провайдера настоящий."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

import pytest

from app import config, llm, usage


class _Stream:
    def __init__(self, owner, params):
        self.owner, self.params = owner, params

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        self.owner.calls.append(dict(self.params))
        reply = self.owner.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def sdk(monkeypatch):
    state = NS(calls=[], replies=[])

    class Anthropic:
        def __init__(self, api_key=None):
            self.messages = NS(stream=lambda **kw: _Stream(state, kw))

    fake = types.ModuleType("anthropic")
    fake.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delenv("VTX_ANTHROPIC_EFFORT", raising=False)
    llm._NO_TEMPERATURE.clear()
    return state


def _msg(*blocks, stop="end_turn", details=None):
    return NS(content=list(blocks), stop_reason=stop, stop_details=details,
              usage=NS(input_tokens=100, cache_read_input_tokens=0, output_tokens=50))


THINK = NS(type="thinking", thinking="")
TEXT = NS(type="text", text=' {"summary": "ok"} ')


def test_новая_модель_без_temperature_и_с_запасом_на_рассуждения(sdk):
    sdk.replies = [_msg(THINK, TEXT)]
    out = llm.AnthropicProvider(model="claude-opus-5-5", api_key="k").complete("p", max_tokens=10000)
    assert out == '{"summary": "ok"}', "текст берётся из блока text, а не из первого блока"
    call = sdk.calls[0]
    assert "temperature" not in call
    assert call["max_tokens"] == 10000 + llm._THINK_HEADROOM


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
                                   "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5"])
def test_temperature_не_шлётся_моделям_где_его_убрали(sdk, model):
    sdk.replies = [_msg(TEXT)]
    llm.AnthropicProvider(model=model, api_key="k").complete("p")
    assert "temperature" not in sdk.calls[0]


def test_прошлое_поколение_по_прежнему_с_temperature_без_запаса(sdk):
    sdk.replies = [_msg(TEXT)]
    llm.AnthropicProvider(model="claude-sonnet-4-6", api_key="k").complete("p", max_tokens=3000)
    assert sdk.calls[0]["temperature"] == 0.1
    assert sdk.calls[0]["max_tokens"] == 3000


def test_неизвестная_модель_отвергла_temperature_повтор_без_него(sdk):
    err = RuntimeError("Error code: 400 - `temperature` is deprecated for this model.")
    sdk.replies = [err, _msg(TEXT), _msg(TEXT)]
    p = llm.AnthropicProvider(model="claude-nova-1", api_key="k")
    assert p.complete("p") == '{"summary": "ok"}'
    assert "temperature" in sdk.calls[0] and "temperature" not in sdk.calls[1]
    p.complete("p")
    assert "temperature" not in sdk.calls[2], "отказ запоминается для модели"


def test_прочие_ошибки_не_глотаются(sdk):
    sdk.replies = [RuntimeError("Error code: 529 overloaded")]
    with pytest.raises(RuntimeError, match="529"):
        llm.AnthropicProvider(model="claude-sonnet-4-6", api_key="k").complete("p")


def test_отказ_модели_внятная_ошибка(sdk):
    sdk.replies = [_msg(stop="refusal", details=NS(category="cyber"))]
    with pytest.raises(RuntimeError, match="отказался.*cyber"):
        llm.AnthropicProvider(model="claude-opus-5-5", api_key="k").complete("p")


def test_лимит_ушёл_на_рассуждения_подсказка(sdk):
    sdk.replies = [_msg(THINK, stop="max_tokens")]
    with pytest.raises(RuntimeError, match="VTX_ANTHROPIC_THINK_TOKENS"):
        llm.AnthropicProvider(model="claude-opus-5-5", api_key="k").complete("p")


def test_усилие_из_настройки(sdk, monkeypatch):
    monkeypatch.setenv("VTX_ANTHROPIC_EFFORT", "low")
    sdk.replies = [_msg(TEXT)]
    llm.AnthropicProvider(model="claude-opus-5-5", api_key="k").complete("p")
    assert sdk.calls[0]["output_config"] == {"effort": "low"}


def test_потолок_max_tokens():
    assert llm.anthropic_max_tokens("claude-opus-5-5", 120000) == 128000


def test_список_моделей_и_цены_текущего_поколения():
    values = [m["value"] for m in config.PROVIDER_MODELS["anthropic"]]
    assert "claude-opus-5-5" in values and "claude-sonnet-5-5" in values
    # Убрать модель из списка = «сохранённый выбор недоступен» у тех, кто её
    # выбрал (15.10 так пропал Opus 4.8). Прошлое поколение остаётся.
    assert "claude-opus-4-8" in values and "claude-sonnet-4-6" in values
    assert usage.price_of("anthropic/claude-opus-5-5") == (4.0, 20.0)
    assert usage.price_of("anthropic/claude-sonnet-5-5") == (2.0, 10.0)
    assert usage.price_of("anthropic/claude-haiku-5-5") == (0.10, 0.50)
    assert usage.price_of("anthropic/claude-sonnet-4-6") == (3.0, 15.0)
