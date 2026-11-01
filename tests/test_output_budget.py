"""Regression coverage for the hidden 2048-token Ollama cutoff."""
import pytest
from streamlit.testing.v1 import AppTest

from tronclass.config import ROOT, Settings
from tronclass.llm import CloudflareClaudeClient, ModelError, OllamaClient
from tests.test_llm import model_server


def test_ollama_default_does_not_request_a_hidden_output_cap(model_server):
    url, state = model_server
    settings = Settings(base_url=url, model="fixture")
    OllamaClient(settings).complete([{"role": "user", "content": "finish the report"}])
    assert settings.max_tokens is None
    assert "max_tokens" not in state["requests"][0][1]


@pytest.mark.parametrize("value", ["", "0"])
def test_cli_can_explicitly_clear_the_old_limit(monkeypatch, value):
    monkeypatch.setenv("LLM_MAX_TOKENS", value)
    assert Settings.from_env(provider="ollama").max_tokens is None


def test_explicit_ollama_budget_still_reaches_the_api(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("LLM_MAX_TOKENS", "8192")
    settings = Settings.from_env(provider="ollama")
    from dataclasses import replace
    settings = replace(settings, base_url=url, model="fixture")
    OllamaClient(settings).complete([])
    assert state["requests"][0][1]["max_tokens"] == 8192


def test_cloudflare_keeps_a_required_positive_budget(monkeypatch):
    assert Settings(provider="cloudflare", base_url="", model="anthropic/fixture").max_tokens == 2048
    monkeypatch.setenv("LLM_MAX_TOKENS", "0")
    with pytest.raises(ValueError):
        Settings.from_env(provider="cloudflare")


def test_length_stop_without_visible_answer_explains_the_budget(model_server):
    url, state = model_server
    state["body"] = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    with pytest.raises(ModelError, match="輸出預算"):
        OllamaClient(Settings(base_url=url, model="fixture", max_tokens=16)).complete([])


def test_long_response_keeps_its_tail_and_stop_reason(model_server):
    url, state = model_server
    answer = "完整分析。" * 3000 + "總計：67.1 分。"
    state["body"] = {"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}
    result = OllamaClient(Settings(base_url=url, model="fixture")).complete([])
    assert result.text == answer
    assert result.metadata["finish_reason"] == "stop"


def test_ui_output_budget_is_optional_and_changes_the_real_backend_settings(monkeypatch):
    import tronclass.factory
    from tronclass.contracts import AnswerResult
    captured = []
    class Backend:
        def __init__(self, settings):
            self.settings = settings
        def answer(self, request):
            captured.append(self.settings.max_tokens)
            return AnswerResult("回答完成", "fixture", 0.1, "hash", "prompt")
    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setattr(tronclass.factory, "create_backend",
                        lambda settings, **kwargs: Backend(settings))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    assert not app.exception
    assert app.checkbox(key="limit_output_ollama").value is False
    app.chat_input[0].set_value("完整回答").run()
    assert captured == [None]
    app.checkbox(key="limit_output_ollama").check().run()
    app.number_input(key="max_tokens_ollama").set_value(8192).run()
    app.chat_input[0].set_value("使用自訂預算").run()
    assert captured == [None, 8192]



def test_ui_can_disable_a_previously_configured_2048_limit(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    assert app.checkbox(key="limit_output_ollama").value is True
    app.checkbox(key="limit_output_ollama").uncheck().run()
    assert not app.exception
    assert app.checkbox(key="limit_output_ollama").value is False
    assert not any(item.key == "max_tokens_ollama" for item in app.number_input)
