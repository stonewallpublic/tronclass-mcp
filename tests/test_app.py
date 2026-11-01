from streamlit.testing.v1 import AppTest

from tronclass.config import ROOT
from tronclass.contracts import AnswerResult


def test_chat_and_data_ui_share_backend_and_show_verification_state(monkeypatch):
    import tronclass.factory
    requests = []

    class Backend:
        def answer(self, request):
            requests.append(request)
            return AnswerResult("介面測試回答", "fixture", 0.01, "data-hash", "prompt-hash")

    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setattr(tronclass.factory, "create_backend", lambda settings, **kwargs: Backend())
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["4", "10", "14"]
    app.chat_input[0].set_value("資料結構作業三何時截止？").run()
    assert not app.exception
    assert len(requests) == 1 and requests[0].history == []
    assert app.session_state["messages"][-1]["content"] == "介面測試回答"
    assert any("未驗證" in item.value for item in app.caption)
    next(widget for widget in app.selectbox if widget.label == "資料類別").select("公告").run()
    assert not app.exception
    assert any("更正" in expander.label for expander in app.expander)
    next(widget for widget in app.selectbox if widget.label == "課程篩選").select("C02").run()
    assert not app.exception
    assert all("資料結構" not in expander.label for expander in app.expander)


def test_missing_model_still_allows_data_browsing(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    assert not app.exception
    assert app.chat_input[0].disabled
    assert len(app.dataframe) == 1


def test_switching_to_cloudflare_keeps_data_available_and_clears_old_chat(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    app.session_state["messages"] = [{"role": "assistant", "content": "先前本機模型回答"}]
    app.selectbox(key="model_provider").select("cloudflare").run()
    assert not app.exception
    assert app.session_state["messages"] == []
    assert app.chat_input[0].disabled  # Account ID is not configured in this test.
    assert len(app.dataframe) == 1
