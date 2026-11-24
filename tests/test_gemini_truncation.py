import types
import pytest
import ai_router


class _Resp:
    def __init__(self, text, reason):
        self.text = text
        self.candidates = [types.SimpleNamespace(finish_reason=reason)]


def _run(monkeypatch, resp):
    import google.generativeai as genai
    class M:
        def __init__(self, *a, **k): pass
        def generate_content(self, *a, **k): return resp
    monkeypatch.setattr(genai, "GenerativeModel", M)
    monkeypatch.setattr(genai, "configure", lambda **k: None)
    return ai_router._gemini_chat("s", "u", "m", "k", 100)


def test_complete_answer_is_returned(monkeypatch):
    assert _run(monkeypatch, _Resp(" full text ", "STOP")) == "full text"


def test_answer_cut_off_by_token_limit_is_an_error(monkeypatch):
    with pytest.raises(ValueError, match="token limit"):
        _run(monkeypatch, _Resp("half a resu", "MAX_TOKENS"))
