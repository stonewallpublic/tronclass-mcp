"""Only fake credentials and a loopback HTTP service are used here."""
import json

import pytest

from streamlit.testing.v1 import AppTest

from tests.test_llm import model_server
from tronclass.config import ROOT
from tronclass import config


@pytest.fixture(autouse=True)
def shared_remote_model(model_server):
    _, state = model_server
    state["models_body"] = {"data": [{"id": config.REMOTE_OLLAMA_MODEL}, {"id": "fixture-model"}]}
    state["body"]["model"] = config.REMOTE_OLLAMA_MODEL


def widget(app, kind, label):
    return next(item for item in getattr(app, kind) if item.label == label)


@pytest.mark.parametrize("configured", ["", "ollama", "cloudflare"])
def test_page_opens_on_remote_ollama_without_contacting_anything(model_server, monkeypatch, configured):
    # Testers should only paste the URL and key; config files keep driving the CLI, not the page.
    url, state = model_server
    monkeypatch.setenv("LLM_PROVIDER", configured)
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-env-must-not-be-used")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert not app.exception and not app.error
    assert app.selectbox(key="model_provider").value == "ollama"
    assert app.selectbox(key="ollama_mode").value == "遠端 Ollama API"
    assert widget(app, "text_input", "API base URL").value == ""
    assert widget(app, "text_input", "API key").value == ""
    assert app.chat_input[0].disabled
    assert state["requests"] == []


def test_remote_model_is_read_only_and_ignores_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "different-env-model")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    widget(app, "selectbox", "Ollama 連線方式").select("遠端 Ollama API").run()
    assert not any(item.label == "模型名稱" for item in app.text_input)
    assert not any(item.label == "從模型清單選擇" for item in app.selectbox)
    assert any("tronclass-system1:latest" in item.value for item in app.markdown)


@pytest.mark.parametrize("available", [[], [{"id": "unrelated-model"}]])
def test_missing_required_model_is_error_without_fallback(model_server, available):
    url, state = model_server
    state["models_body"] = {"data": available}
    app = remote_app(url)
    assert app.chat_input[0].disabled
    app.session_state["failed_question"] = "previous question"
    widget(app, "button", "檢查連線").click().run()
    assert any(config.REMOTE_OLLAMA_MODEL in e.value and "缺少" in e.value for e in app.error)
    assert not app.session_state["ollama_connected"]
    assert app.session_state["ollama_models"] == []
    assert app.chat_input[0].disabled
    assert widget(app, "button", "重新送出上一題").disabled
    assert [path for path, _ in state["requests"]] == ["/v1/models"]
    assert not any(item.label == "從模型清單選擇" for item in app.selectbox)


def test_developer_model_setting_is_single_source_and_invalidates_connection(model_server, monkeypatch):
    url, state = model_server
    app = remote_app(url)
    widget(app, "button", "檢查連線").click().run()
    monkeypatch.setattr(config, "REMOTE_OLLAMA_MODEL", "developer-fixture-model")
    app.run()
    assert not app.session_state["ollama_connected"]
    assert app.session_state["ollama_models"] == []
    assert app.chat_input[0].disabled
    assert any(config.REMOTE_OLLAMA_MODEL in item.value for item in app.markdown)
    state["models_body"] = {"data": [{"id": config.REMOTE_OLLAMA_MODEL}]}
    widget(app, "button", "檢查連線").click().run()
    app.chat_input[0].set_value("test").run()
    assert state["requests"][-1][1]["model"] == config.REMOTE_OLLAMA_MODEL


def remote_app(url, key="fake-session-a"):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    widget(app, "selectbox", "Ollama 連線方式").select("遠端 Ollama API").run()
    widget(app, "text_input", "API base URL").set_value(url).run()
    widget(app, "text_input", "API key").set_value(key).run()
    assert not app.exception
    return app


def test_ui_check_and_chat_use_same_session_connection(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-env-must-not-be-used")
    monkeypatch.setenv("OLLAMA_MODEL", "wrong-environment-model")
    app = remote_app(url)
    app.session_state["model_name_ollama_remote"] = "stale-session-model"
    app.run()
    field = widget(app, "text_input", "API key").proto
    assert field.type == field.PASSWORD
    assert state["requests"] == []  # Reruns must never query automatically.
    widget(app, "button", "檢查連線").click().run()
    assert app.session_state["ollama_connected"] is True
    assert config.REMOTE_OLLAMA_MODEL in app.session_state["ollama_models"]
    app.chat_input[0].set_value("課程有哪些？").run()
    assert not app.exception and not app.error
    assert [path for path, _ in state["requests"]] == ["/v1/models", "/v1/chat/completions"]
    assert state["requests"][1][1]["model"] == config.REMOTE_OLLAMA_MODEL
    assert all(headers["Authorization"] == "Bearer fake-session-a" for headers in state["headers"])
    records = json.dumps(app.session_state["records"], ensure_ascii=False)
    assert "fake-session-a" not in records and url not in records


def test_sessions_are_isolated_and_clear_does_not_fall_back_to_environment(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-env-must-not-be-used")
    first = remote_app(url, "fake-session-a")
    second = remote_app(url, "fake-session-b")
    for app in (first, second):
        widget(app, "button", "檢查連線").click().run()
    assert [h["Authorization"] for h in state["headers"]] == ["Bearer fake-session-a", "Bearer fake-session-b"]
    widget(first, "button", "清除連線／金鑰").click().run()
    assert widget(first, "text_input", "API key").value == ""
    assert widget(first, "text_input", "API base URL").value == ""
    assert first.session_state["ollama_models"] == []
    assert not first.session_state["ollama_connected"]
    assert widget(second, "text_input", "API key").value == "fake-session-b"
    widget(first, "button", "檢查連線").click().run()
    assert len(state["requests"]) == 2


def test_changes_clear_confirmation_models_and_conversation(model_server):
    url, state = model_server
    app = remote_app(url)
    for label, value in (("API key", "fake-new-key"),
                         ("API base URL", url + "/")):
        widget(app, "button", "檢查連線").click().run()
        app.session_state["messages"] = [{"role": "assistant", "content": "old"}]
        app.session_state["records"] = [{"old": True}]
        widget(app, "text_input", label).set_value(value).run()
        assert not app.session_state["ollama_connected"]
        assert app.session_state["ollama_models"] == []
        assert app.session_state["messages"] == []
        assert app.session_state["records"] == []
    count = len(state["requests"])
    widget(app, "selectbox", "Ollama 連線方式").select("本機 Ollama").run()
    assert not app.session_state["ollama_connected"]
    assert len(state["requests"]) == count


def test_model_picker_and_local_without_key(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("OLLAMA_BASE_URL", url)
    monkeypatch.setenv("OLLAMA_MODEL", "fixture-model")
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-env-ignored-by-ui")
    local = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    widget(local, "selectbox", "Ollama 連線方式").select("本機 Ollama").run()
    widget(local, "button", "檢查連線").click().run()
    widget(local, "selectbox", "從模型清單選擇").select("fixture-model").run()
    assert widget(local, "text_input", "模型名稱").value == "fixture-model"
    assert local.session_state["ollama_models"] == []
    assert not local.session_state["ollama_connected"]
    widget(local, "button", "檢查連線").click().run()
    local.chat_input[0].set_value("test").run()
    assert not local.exception and not local.error
    assert all("Authorization" not in h for h in state["headers"][-2:])


def test_remote_ui_requires_key_and_displays_safe_errors(model_server, caplog):
    url, state = model_server
    app = remote_app(url, "")
    widget(app, "button", "檢查連線").click().run()
    assert not state["requests"] and app.chat_input[0].disabled
    assert any("API key" in error.value for error in app.error)
    widget(app, "text_input", "API key").set_value("fake-session-a").run()
    state["status"] = 401
    widget(app, "button", "檢查連線").click().run()
    assert any("401" in error.value for error in app.error)
    assert "fake-session-a" not in str([e.value for e in app.error])
    assert url not in str([e.value for e in app.error])
    assert "fake-session-a" not in caplog.text and url not in caplog.text


def test_failed_recheck_does_not_show_old_success(model_server):
    url, state = model_server
    app = remote_app(url)
    widget(app, "button", "檢查連線").click().run()
    state["status"] = 429
    state["retry_after"] = "17"
    widget(app, "button", "檢查連線").click().run()
    assert not app.session_state["ollama_connected"]
    assert app.session_state["ollama_models"] == []
    assert not any("模型服務已連線" in item.value for item in app.success)
    assert any("17 秒" in item.value for item in app.error)


def test_cloudflare_ui_check_and_chat_keep_original_adapter(model_server, monkeypatch):
    import tronclass.llm
    url, state = model_server
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cloudflare-key")
    original_request = tronclass.llm.HttpModelClient._request

    def local_request(self, path, payload=None, timeout=None, headers=None, base_url=None):
        assert self.settings.provider == "cloudflare"
        return original_request(self, path, payload, timeout, headers, base_url=url)

    monkeypatch.setattr(tronclass.llm.HttpModelClient, "_request", local_request)
    state["body"] = {"model": "anthropic/fixture", "content": [{"type": "text", "text": "Claude 測試回答"}],
                     "stop_reason": "end_turn"}
    app = remote_app(url)
    app.selectbox(key="model_provider").select("cloudflare").run()
    widget(app, "button", "檢查憑證").click().run()
    assert any("token 有效" in item.value for item in app.success)
    app.chat_input[0].set_value("test").run()
    assert not app.exception and not app.error
    assert app.session_state["messages"][-1]["content"] == "Claude 測試回答"
    assert [path for path, _ in state["requests"]] == ["/v1/user/tokens/verify", "/v1/messages"]
    assert all(h["Authorization"] == "Bearer fake-cloudflare-key" for h in state["headers"])
    assert "seed" not in state["requests"][-1][1]


def test_upstream_echo_is_removed_from_ui_download_records(model_server, caplog):
    url, state = model_server
    state["body"]["choices"][0]["message"]["content"] = "answer fake-session-a " + url
    state["body"]["usage"] = {"echo": "fake-session-a " + url}
    app = remote_app(url)
    widget(app, "button", "檢查連線").click().run()
    app.chat_input[0].set_value("test").run()
    assert not app.exception
    exported = json.dumps(app.session_state["records"])
    assert "fake-session-a" not in exported and url not in exported
    assert "[redacted]" in exported
    assert "fake-session-a" not in caplog.text and url not in caplog.text


def test_batch_results_hide_explicit_credentials_and_endpoint(model_server, tmp_path, caplog):
    from evaluation.run import run_cases
    from tronclass.config import Settings
    from tronclass.factory import create_backend
    from tronclass.llm import OllamaClient
    url, state = model_server
    key = "fake-batch-key"
    state["body"]["choices"][0]["message"]["content"] = key + " " + url
    settings = Settings(base_url=url, model="fixture-model")
    backend = create_backend(settings, client=OllamaClient(settings, api_key=key))
    target = tmp_path / "fixture.jsonl"
    assert run_cases(backend, settings, [{"id": "fake", "question": "test"}], 1, target) == 0
    for path in (target, target.with_suffix(".csv")):
        exported = path.read_text(encoding="utf-8-sig")
        assert key not in exported and url not in exported
        assert str(ROOT) not in exported
    assert key not in caplog.text and url not in caplog.text


@pytest.mark.parametrize("url", ["http://private-fixture.invalid/v1", "https://private-fixture.invalid/fake-session-a/v1",
                                 "https://fake-session-a@private-fixture.invalid/v1", "https://private-fixture.invalid"])
def test_ui_rejects_unsafe_connection_before_settings_or_network(url, caplog):
    app = remote_app(url)
    widget(app, "button", "檢查連線").click().run()
    assert app.error and app.chat_input[0].disabled and not app.exception
    assert "fake-session-a" not in str(app.session_state["conversation_scope"])
    assert "private-fixture.invalid" not in str(app.session_state["conversation_scope"])
    assert "fake-session-a" not in str([e.value for e in app.error])
    assert "private-fixture.invalid" not in caplog.text
