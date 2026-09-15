"""Кривой JSON ≠ оборванный JSON (боевой случай GigaChat, CRM 15.09.2026).

Ответ начинался с настоящего протокола («{ "participants": [ {"name": "Зоя Р"…»),
но разбор его дважды отверг, и встреча ушла Groq с причиной «дважды ответил
не протоколом». По такой причине не понять, что делать: просить короче
(оборван) или исправленный (синтаксис). Теперь типовые огрехи чинятся без
модели, а причина отката называет диагноз.
"""
from __future__ import annotations

import json

import pytest

from app import analyze
from app.analyze import (MalformedAnswer, Protocol, TruncatedAnswer,
                         _extract_json, _parse_for, _repair_json)

FIELDS = Protocol.model_fields.keys()


class TestПочинка:
    def test_корректный_json_не_меняется(self):
        raw = json.dumps({"summary": "он сказал «да»\nи ушёл", "tasks": [{"task": "a\"b"}]},
                         ensure_ascii=False)
        assert _repair_json(raw) == raw
        assert json.loads(_repair_json(raw)) == json.loads(raw)

    def test_кавычка_внутри_строки(self):
        raw = '{"summary": "он сказал "да" и ушёл", "tasks": []}'
        assert json.loads(_repair_json(raw))["summary"] == 'он сказал "да" и ушёл'

    def test_перенос_строки_внутри_значения(self):
        raw = '{"summary": "первая\nвторая", "tasks": []}'
        assert json.loads(_repair_json(raw))["summary"] == "первая\nвторая"

    def test_запятая_перед_закрывающей_скобкой(self):
        raw = '{"summary": "s", "tasks": [{"task": "a"},],}'
        assert json.loads(_repair_json(raw))["tasks"] == [{"task": "a"}]

    def test_несуществующее_экранирование(self):
        raw = '{"summary": "it\\\'s", "tasks": []}'
        assert json.loads(_repair_json(raw))["summary"] == "it's"


class TestРазборОтличаетДиагнозы:
    def test_кривой_но_закрытый_ответ_чинится(self):
        raw = ('{"participants": [{"name": "Зоя Р", "role": "Директор"}], '
               '"summary": "решили "не делать" логирование", "decisions": ["a",]}')
        obj = _parse_for(Protocol, raw)
        assert obj["summary"] == 'решили "не делать" логирование'
        assert obj["decisions"] == ["a"]

    def test_нечинимый_закрытый_ответ_это_MalformedAnswer(self):
        raw = '{"summary": "s" "tasks": [1 2]}'
        with pytest.raises(MalformedAnswer):
            _extract_json(raw, expected_keys=FIELDS)

    def test_оборванный_остаётся_TruncatedAnswer(self):
        raw = '{"participants": [{"name": "Зоя Р"}], "summary": "s", "decisions": ["реш'
        with pytest.raises(TruncatedAnswer):
            _extract_json(raw, expected_keys=FIELDS)

    def test_MalformedAnswer_не_TruncatedAnswer(self):
        assert not issubclass(MalformedAnswer, TruncatedAnswer)
        assert issubclass(MalformedAnswer, ValueError)


class _Engine:
    accepts_should_stop = False

    def __init__(self, name, answer, demoted=None):
        self.name, self.model, self.answer = name, name + "-m", answer
        self.demoted = demoted if demoted is not None else []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        return self.answer

    def demote(self, reason):
        self.demoted.append(reason)
        return False


class TestПричинаОтката:
    def test_оборванный_ответ_назван_лимитом(self):
        eng = _Engine("gigachat", '{"participants": [{"name": "Зоя Р"}], "summary": "s')
        with pytest.raises(RuntimeError):
            analyze._protocol_from(eng, "промпт", None, "сведение")
        assert eng.demoted and "оборванный JSON" in eng.demoted[0]
        assert "лимит токенов" in eng.demoted[0]

    def test_кривой_ответ_назван_синтаксисом(self):
        eng = _Engine("gigachat", '{"summary": "s" "tasks": [1 2]}')
        with pytest.raises(RuntimeError):
            analyze._protocol_from(eng, "промпт", None, "сведение")
        assert eng.demoted and "синтаксической ошибкой" in eng.demoted[0]

    def test_проза_по_прежнему_не_протокол(self):
        eng = _Engine("custom", "Итак, встреча началась с обсуждения…")
        with pytest.raises(RuntimeError):
            analyze._protocol_from(eng, "промпт", None, "сведение")
        assert eng.demoted and "не протоколом" in eng.demoted[0]
