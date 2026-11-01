"""Greeting fast path, timing evidence and Ollama thinking control."""
import csv
import json

import pytest
from streamlit.testing.v1 import AppTest

from evaluation.run import run_cases
from tronclass.config import ROOT, Settings
from tronclass.contracts import AnswerRequest
from tronclass.database import CourseRepository
from tronclass.llm import Completion, ModelError, OllamaClient, CloudflareClaudeClient
from tronclass.system1 import System1Assistant, is_pure_greeting
from tests.test_llm import model_server  # noqa: F401  (pytest fixture)


class RecordingClient:
    def __init__(self, usage=None):
        self.calls = []
        self.usage = usage

    def complete(self, messages):
        self.calls.append(messages)
        metadata = {"finish_reason": "stop", "usage": self.usage}
        return Completion("模型回答", "test-model", metadata)


class CountingRepository(CourseRepository):
    def __init__(self):
        super().__init__(ROOT / "sample_data" / "course.db")
        self.snapshots = 0

    def snapshot(self, student_id):
        self.snapshots += 1
        return super().snapshot(student_id)


def assistant(**overrides):
    settings = Settings(model="test-model", **overrides)
    client, repository = RecordingClient(), CountingRepository()
    return System1Assistant(settings, repository=repository, client=client), client, repository


def ask(backend, question, history=()):
    settings = backend.settings
    return backend.answer(AnswerRequest(question, settings.student_id, settings.reference_time, list(history)))


@pytest.mark.parametrize("question", [
    "你好", "您好", "嗨", "hello", "Hello!", "  hi  ", "HEY", "哈囉～", "你好！", "嗨，你好", "早安。",
])
def test_pure_greeting_skips_database_and_model(question):
    backend, client, repository = assistant()
    result = ask(backend, question)
    assert client.calls == [] and repository.snapshots == 0
    assert result.answer.strip() and "課業助理" in result.answer
    assert result.model == ""
    assert result.metadata["route"] == "greeting"
    assert result.metadata["model_called"] is False
    # No invented token usage, finish reason or model round trip.
    assert result.metadata["usage"] is None
    assert result.metadata["finish_reason"] is None
    assert result.metadata["timing"]["model_request_seconds"] is None
    assert result.metadata["timing"]["total_seconds"] >= 0
    assert result.context_sha256 == "" and result.metadata["context_characters"] == 0


@pytest.mark.parametrize("question", [
    "你好，幫我算各科成績",
    "你好 幫我查資料結構作業三",
    "hello, 作業三何時截止？",
    "嗨 上面那題再算一次",
    "你好像算錯了",
    "你好嗎？",
    "那你好好解釋一下",
    "hi there",
    "你好你好你好你好你好你好你好你好",
    "你好？作業",
    "",
])
def test_mixed_or_uncertain_questions_keep_the_full_flow(question):
    if not question:
        backend, client, _ = assistant()
        with pytest.raises(ValueError):
            ask(backend, question)
        assert client.calls == []
        return
    assert not is_pure_greeting(question)
    backend, client, repository = assistant()
    result = ask(backend, question)
    assert len(client.calls) == 1 and repository.snapshots == 1
    assert result.metadata["route"] == "full"
    assert result.metadata["model_called"] is True


def test_greeting_after_history_keeps_the_full_flow():
    history = [{"role": "user", "content": "資料結構作業三何時截止？"},
               {"role": "assistant", "content": "（先前回答）"}]
    assert not is_pure_greeting("你好", history)
    backend, client, repository = assistant()
    result = ask(backend, "你好", history)
    assert len(client.calls) == 1 and repository.snapshots == 1
    assert client.calls[0][1:3] == history
    assert result.metadata["route"] == "full"


def test_multi_turn_follow_up_still_sends_all_visible_courses_and_history():
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"},
               {"role": "user", "content": "統計學作業三怎麼交？"}, {"role": "assistant", "content": "紙本。"}]
    backend, client, _ = assistant()
    result = ask(backend, "那資料結構呢？兩科一起比較", history)
    messages = client.calls[0]
    assert messages[1:5] == history and messages[-1]["content"] == "那資料結構呢？兩科一起比較"
    assert all(course in messages[0]["content"] for course in ("C01", "C02", "C03", "C04"))
    assert result.metadata["context_characters"] == len(messages[0]["content"])


def test_full_flow_records_separate_preparation_and_model_round_trip_without_inventing_usage():
    backend, client, _ = assistant()
    result = ask(backend, "幫我算各科目前成績")
    timing = result.metadata["timing"]
    assert set(timing) == {"preparation_seconds", "model_request_seconds", "total_seconds"}
    assert all(isinstance(value, float) and value >= 0 for value in timing.values())
    assert timing["total_seconds"] >= timing["preparation_seconds"]
    assert result.metadata["usage"] is None  # The fake service reported none.
    backend.client = RecordingClient(usage={"prompt_tokens": 20000, "completion_tokens": 50})
    assert ask(backend, "再算一次").metadata["usage"] == {"prompt_tokens": 20000, "completion_tokens": 50}


def test_ollama_omits_usage_when_service_does_not_report_it(model_server):
    url, state = model_server
    del state["body"]["usage"]
    result = OllamaClient(Settings(base_url=url, model="fixture")).complete([])
    assert result.metadata["usage"] is None


@pytest.mark.parametrize("mode, effort", [("default", None), ("off", "none"), ("on", "medium")])
def test_ollama_thinking_mode_uses_openai_compatible_reasoning_effort(model_server, mode, effort):
    url, state = model_server
    OllamaClient(Settings(base_url=url, model="fixture", thinking=mode)).complete([])
    payload = state["requests"][0][1]
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) is (effort is not None)
    # Native /api/chat and other services' switches are not assumed on /v1.
    assert "think" not in payload and "enable_thinking" not in payload and "reasoning" not in payload
    assert payload["stream"] is False
    assert state["requests"][0][0] == "/v1/chat/completions"


def test_reasoning_is_measured_but_never_shown_or_exported(model_server):
    url, state = model_server
    state["body"]["choices"][0]["message"]["reasoning"] = "內部思考過程" * 10
    result = OllamaClient(Settings(base_url=url, model="fixture")).complete([])
    assert result.text == "測試回答"
    assert result.metadata["reasoning_characters"] == 60
    assert "內部思考過程" not in json.dumps(result.metadata, ensure_ascii=False)
    del state["body"]["choices"][0]["message"]["reasoning"]
    result = OllamaClient(Settings(base_url=url, model="fixture")).complete([])
    assert result.metadata["reasoning_characters"] is None  # Not returned: unknown, not zero.


def test_thinking_setting_is_validated_and_ollama_only(monkeypatch):
    with pytest.raises(ValueError):
        Settings(model="fixture", thinking="minimal")
    with pytest.raises(ValueError):
        Settings(provider="cloudflare", base_url="", model="anthropic/fixture", thinking="off")
    monkeypatch.setenv("OLLAMA_THINKING", "off")
    assert Settings.from_env(provider="ollama").thinking == "off"
    assert Settings.from_env(provider="cloudflare").thinking == "default"
    monkeypatch.setenv("OLLAMA_THINKING", "")
    assert Settings.from_env(provider="ollama").thinking == "default"
    monkeypatch.setenv("OLLAMA_THINKING", "enable_thinking")
    with pytest.raises(ValueError):
        Settings.from_env(provider="ollama")


def test_cloudflare_payload_has_no_thinking_parameters(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cloudflare-token")
    state["body"] = {"content": [{"type": "text", "text": "回答"}], "stop_reason": "end_turn"}
    CloudflareClaudeClient(Settings(provider="cloudflare", base_url=url, model="anthropic/fixture")).complete([])
    payload = state["requests"][0][1]
    assert not {"reasoning_effort", "think", "enable_thinking", "thinking"} & set(payload)


def test_rejected_parameter_explains_thinking_setting_without_retry(model_server):
    url, state = model_server
    state["status"] = 400
    with pytest.raises(ModelError, match="思考模式"):
        OllamaClient(Settings(base_url=url, model="fixture", thinking="off")).complete([])
    assert len(state["requests"]) == 1


def test_evaluation_records_route_timing_and_unknown_usage(tmp_path):
    backend, client, _ = assistant()
    cases = [{"id": "greet", "question": "你好"}, {"id": "full", "question": "你好，幫我算各科成績"}]
    output = tmp_path / "latency.jsonl"
    assert run_cases(backend, backend.settings, cases, 1, output) == 0
    assert len(client.calls) == 1
    with output.with_suffix(".csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = {row["case_id"]: row for row in csv.DictReader(stream)}
    assert rows["greet"]["route"] == "greeting" and rows["greet"]["model_called"] == "False"
    assert rows["greet"]["model_request_seconds"] == "" and rows["greet"]["completion_tokens"] == ""
    assert rows["full"]["route"] == "full" and rows["full"]["model_called"] == "True"
    assert float(rows["full"]["model_request_seconds"]) >= 0
    assert rows["full"]["prompt_tokens"] == ""  # Not reported by the fake service: unknown.
    assert rows["full"]["thinking"] == "default"


def test_ui_greeting_does_not_call_model_and_thinking_choice_reaches_backend(monkeypatch):
    import tronclass.llm
    calls, seen = [], []

    class Client:
        def __init__(self, settings):
            seen.append(settings.thinking)

        def complete(self, messages):
            calls.append(messages)
            return Completion("完整流程回答", "fixture", {"finish_reason": "stop", "usage": None})

    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setattr(tronclass.llm, "create_model_client", lambda settings, **kwargs: Client(settings))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.selectbox(key="ollama_mode").select("本機 Ollama").run()
    assert not app.exception
    assert app.selectbox(key="thinking_ollama").value == "default"
    app.chat_input[0].set_value("你好").run()
    assert not app.exception and calls == []
    assert "課業助理" in app.session_state["messages"][-1]["content"]
    assert any("未呼叫模型" in item.value for item in app.caption)
    app.chat_input[0].set_value("你好，幫我算各科成績").run()
    assert len(calls) == 1 and seen[-1] == "default"
    assert any("模型往返" in item.value for item in app.caption)
    app.selectbox(key="thinking_ollama").select("off").run()
    app.chat_input[0].set_value("資料結構作業三何時截止？").run()
    assert not app.exception and len(calls) == 2 and seen[-1] == "off"


def test_manual_latency_script_reports_only_measurements(monkeypatch, capsys):
    import evaluation.latency as latency
    backend, client, _ = assistant()
    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setattr(latency, "create_backend", lambda settings: backend)
    assert latency.main(["--thinking", "default", "--scenarios", "greeting", "follow_up"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["route"] for row in rows] == ["greeting", "full"]
    assert rows[0]["model_called"] is False and rows[0]["model_request_seconds"] is None
    assert rows[1]["prompt_tokens"] is None and rows[1]["context_characters"] > 0
    assert len(client.calls) == 1 and client.calls[0][1]["role"] == "user"
    assert "模型回答" not in json.dumps(rows, ensure_ascii=False)


def test_native_diagnostics_refuse_remote_services(monkeypatch):
    import evaluation.latency as latency
    monkeypatch.setenv("OLLAMA_MODEL", "fixture")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://remote.example.test/v1")
    with pytest.raises(SystemExit) as exit_info:
        latency.main(["--native"])
    assert exit_info.value.code == 2
    with pytest.raises(ValueError):
        latency.native_durations(Settings(base_url="https://remote.example.test/v1", model="x"), [], 1)


def test_native_diagnostic_replaces_v1_request_and_keeps_missing_durations_unknown(model_server):
    import evaluation.latency as latency
    url, state = model_server
    state["body"] = {"message": {"content": "回答", "thinking": "想"}, "load_duration": 2_000_000_000,
                     "prompt_eval_count": 17000, "eval_count": 300}
    settings = Settings(base_url=url, model="fixture", thinking="off")
    backend = System1Assistant(settings, client=OllamaClient(settings))
    row = latency.measure(backend, settings, "single_course", native=True)
    assert [path for path, _ in state["requests"]] == ["/api/chat"]
    payload = state["requests"][0][1]
    assert payload["think"] is False and payload["stream"] is False
    assert row["api"] == "native" and row["load_duration"] == 2.0 and row["prompt_eval_count"] == 17000
    assert row["prompt_eval_duration"] is None and row["eval_duration"] is None
    assert row["thinking_characters"] == 1


def test_manual_latency_errors_do_not_print_local_paths(tmp_path):
    import evaluation.latency as latency
    settings = Settings(model="fixture", db_path=tmp_path / "private-dir" / "missing.db")
    backend = System1Assistant(settings, client=RecordingClient())
    row = latency.measure(backend, settings, "single_course")
    assert row["status"] == "error" and row["error"] == "DataError"
    assert "private-dir" not in json.dumps(row, ensure_ascii=False)
