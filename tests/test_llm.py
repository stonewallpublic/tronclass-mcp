import errno
import http.client
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

from tronclass.config import Settings
from tronclass.llm import CloudflareClaudeClient, ModelError, OllamaClient


@pytest.fixture
def model_server():
    state = {"requests": [], "headers": [], "status": 200, "body": {
        "model": "fixture-model", "choices": [{"message": {"content": "測試回答"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 10},
    }}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["headers"].append(dict(self.headers))
            state["requests"].append((self.path, None))
            if self.path.endswith("/user/tokens/verify"):
                self.respond({"success": True, "result": {"status": "active"}})
            else:
                self.respond(state.get("models_body", {"data": [{"id": "fixture-model"}]}))

        def do_POST(self):
            state["headers"].append(dict(self.headers))
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body))
            self.respond(state["body"])

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            if "retry_after" in state:
                self.send_header("Retry-After", state["retry_after"])
            if state["status"] == 302:
                self.send_header("Location", "/credential-leak-target")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_openai_compatible_ollama_wire_format_and_generation_settings(model_server):
    url, state = model_server
    client = OllamaClient(Settings(base_url=url, model="fixture-model", max_tokens=2048))
    assert client.list_models() == ["fixture-model"]
    messages = [{"role": "user", "content": "中文問題"}]
    result = client.complete(messages)
    assert state["requests"][0][0] == "/v1/models"
    path, body = state["requests"][1]
    assert path == "/v1/chat/completions"
    assert body == {"model": "fixture-model", "messages": messages, "temperature": 0,
                    "seed": 42, "max_tokens": 2048, "stream": False}
    assert result.text == "測試回答"
    assert result.metadata["usage"]["prompt_tokens"] == 123


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}])
def test_malformed_model_response_is_an_error_not_an_answer(model_server, body):
    url, state = model_server
    state["body"] = body
    with pytest.raises(ModelError):
        OllamaClient(Settings(base_url=url, model="fixture-model")).complete([])


def test_http_failure_reports_actionable_error(model_server):
    url, state = model_server
    state["status"] = 404
    with pytest.raises(ModelError, match="HTTP 404"):
        OllamaClient(Settings(base_url=url, model="missing-model")).complete([])


def test_cloudflare_messages_format_authentication_and_seed_not_sent(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cloudflare-token")
    monkeypatch.setenv("OLLAMA_API_KEY", "different-ollama-key")
    state["body"] = {
        "id": "msg_fixture", "model": "claude-fixture-pinned", "stop_reason": "max_tokens",
        "content": [{"type": "thinking", "thinking": "hidden reasoning"},
                    {"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
        "usage": {"input_tokens": 120, "output_tokens": 10},
    }
    settings = Settings(provider="cloudflare", base_url=url, model="anthropic/fixture", cloudflare_gateway_id="team-gateway")
    result = CloudflareClaudeClient(settings).complete([
        {"role": "system", "content": "課程資料"}, {"role": "user", "content": "中文問題"},
    ])
    path, payload = state["requests"][0]
    assert path == "/v1/messages"
    assert payload["system"] == "課程資料"
    assert payload["messages"] == [{"role": "user", "content": "中文問題"}]
    assert "seed" not in payload
    headers = {key.lower(): value for key, value in state["headers"][0].items()}
    assert headers["authorization"] == "Bearer fake-cloudflare-token"
    assert headers["cf-aig-skip-cache"] == "true"
    assert headers["cf-aig-gateway-id"] == "team-gateway"
    assert result.text == "第一段\n第二段"
    assert result.model == "claude-fixture-pinned"
    assert result.metadata["finish_reason"] == "length"
    assert result.metadata["seed_applied"] is False
    assert "fake-cloudflare-token" not in json.dumps(result.metadata)


def test_cloudflare_billing_error_does_not_echo_response_secrets(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cloudflare-token")
    state["status"] = 402
    state["body"] = {"error": "do-not-log-this-body"}
    with pytest.raises(ModelError, match="402.*credits") as error:
        CloudflareClaudeClient(Settings(provider="cloudflare", base_url=url, model="anthropic/fixture")).complete([])
    assert "fake-cloudflare-token" not in str(error.value)
    assert "do-not-log-this-body" not in str(error.value)


def test_cloudflare_redirect_is_not_followed_with_credentials(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-cloudflare-token")
    state["status"] = 302
    with pytest.raises(ModelError, match="302"):
        CloudflareClaudeClient(Settings(provider="cloudflare", base_url=url, model="anthropic/fixture")).complete([])
    assert len(state["requests"]) == 1


def test_missing_cloudflare_token_fails_before_network_request(model_server):
    url, state = model_server
    with pytest.raises(ModelError, match="CLOUDFLARE_API_TOKEN"):
        CloudflareClaudeClient(Settings(provider="cloudflare", base_url=url, model="anthropic/fixture")).complete([])
    assert not state["requests"]


@pytest.mark.parametrize("api_key", [None, "", "  ", "fake-ollama-key", "  fake-ollama-key  "])
def test_ollama_optional_api_key_on_models_and_completions(model_server, monkeypatch, api_key):
    url, state = model_server
    if api_key is not None:
        monkeypatch.setenv("OLLAMA_API_KEY", api_key)
    settings = Settings(base_url=url, model="fixture-model")
    client = OllamaClient(settings)
    client.list_models()
    result = client.complete([{"role": "user", "content": "fixture question"}])
    for request_headers in state["headers"]:
        headers = {key.lower(): value for key, value in request_headers.items()}
        if api_key and api_key.strip():
            assert headers["authorization"] == "Bearer " + api_key.strip()
        else:
            assert "authorization" not in headers
    assert "fake-ollama-key" not in repr(settings)
    assert "fake-ollama-key" not in json.dumps(result.metadata)


def test_ollama_unauthorized_error_explains_api_key_without_echoing_secrets(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-ollama-key")
    state["status"] = 401
    state["body"] = {"error": "do-not-log-this-body"}
    with pytest.raises(ModelError, match="401.*OLLAMA_API_KEY") as error:
        OllamaClient(Settings(base_url=url, model="fixture-model")).complete([])
    assert "fake-ollama-key" not in str(error.value)
    assert "do-not-log-this-body" not in str(error.value)


def test_ollama_redirect_is_not_followed_with_api_key(model_server, monkeypatch):
    url, state = model_server
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-ollama-key")
    state["status"] = 302
    with pytest.raises(ModelError, match="302"):
        OllamaClient(Settings(base_url=url, model="fixture-model")).list_models()
    assert len(state["requests"]) == 1
    headers = {key.lower(): value for key, value in state["headers"][0].items()}
    assert headers["authorization"] == "Bearer fake-ollama-key"


def test_explicit_keys_override_environment_and_are_not_repr_or_exports(model_server, monkeypatch):
    from dataclasses import asdict
    url, state = model_server
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-env-key")
    first = OllamaClient(Settings(base_url=url, model="fixture-model"), api_key="fake-first-key")
    second = OllamaClient(Settings(base_url=url, model="fixture-model"), api_key="fake-second-key")
    first.list_models()
    second.list_models()
    assert [h["Authorization"] for h in state["headers"]] == ["Bearer fake-first-key", "Bearer fake-second-key"]
    assert "fake-first-key" not in repr(first)
    assert "api_key" not in asdict(first.settings)
    state["body"]["choices"][0]["message"]["content"] = "fake-first-key " + url
    state["body"]["usage"] = {"echo": "fake-first-key " + url}
    result = json.dumps(asdict(first.complete([])))
    assert "fake-first-key" not in result and url not in result
    assert "fake-first-key" not in json.dumps(first.list_models())


@pytest.mark.parametrize("url", ["http://remote.invalid/v1", "https://fake-key@remote.invalid/v1",
                                 "https://remote.invalid/v1?key=fake-key", "https://remote.invalid",
                                 "https://remote.invalid/fake-key/v1"])
def test_credential_transport_rejects_insecure_or_url_credentials(url):
    with pytest.raises((ValueError, ModelError)):
        OllamaClient(Settings(base_url=url, model="fixture"), api_key="fake-key").list_models()


@pytest.mark.parametrize("status,hint", [(401, "API"), (404, "模型"), (429, "17"), (530, "入口"), (302, "重新導向")])
def test_safe_actionable_errors_and_retry_after(model_server, status, hint):
    import traceback
    url, state = model_server
    state.update(status=status, retry_after="17", body={"secret": "fake-key " + url})
    client = OllamaClient(Settings(base_url=url, model="fixture"), api_key="fake-key")
    with pytest.raises(ModelError, match=hint) as error:
        client.complete([])
    rendered = "".join(traceback.format_exception(error.value))
    assert "fake-key" not in rendered and url not in rendered


def test_timeout_and_expired_endpoint_hide_network_details(monkeypatch):
    import socket
    import traceback
    from urllib.error import URLError
    import tronclass.llm
    class Opener:
        def open(self, *args, **kwargs):
            raise failure
    monkeypatch.setattr(tronclass.llm, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(tronclass.llm, "getproxies", dict)
    client = OllamaClient(Settings(base_url="https://private-fixture.invalid/v1"), api_key="fake-key")
    for failure, hint in [(socket.timeout("fake-key"), "逾時"),
                          (URLError("https://private-fixture.invalid/v1 fake-key"), "入口")]:
        with pytest.raises(ModelError, match=hint) as error:
            client.list_models()
        rendered = "".join(traceback.format_exception(error.value))
        assert "fake-key" not in rendered and "private-fixture" not in rendered


class UnlistedNetworkError(OSError):
    pass


@pytest.mark.parametrize("failure,proxies,hint", [
    (URLError(socket.gaierror(11001, "getaddrinfo failed fake-key")), {}, "DNS"),
    (URLError(ssl.SSLCertVerificationError(1, "certificate verify failed fake-key")), {}, "憑證"),
    (URLError(ssl.SSLError(1, "wrong version fake-key")), {}, "HTTPS"),
    (URLError(ConnectionRefusedError(10061, "refused fake-key")), {}, "拒絕"),
    (URLError(ConnectionRefusedError(10061, "refused fake-key")), {"https": "http://127.0.0.1:7890"}, "代理"),
    (URLError(ConnectionResetError(10054, "reset fake-key")), {}, "中斷"),
    (URLError(ConnectionAbortedError(10053, "aborted fake-key")), {}, "中斷"),
    # urllib lets errors from reading the response escape unwrapped.
    (http.client.RemoteDisconnected("closed fake-key"), {}, "中斷"),
    (URLError(OSError(errno.ENETUNREACH, "unreachable fake-key")), {}, "網路無法到達"),
    (URLError(OSError(errno.EHOSTUNREACH, "unreachable fake-key")), {}, "網路無法到達"),
    (URLError(OSError(getattr(errno, "WSAEACCES", 10013), "forbidden fake-key")), {}, "防火牆"),
    (URLError(UnlistedNetworkError(1, "odd fake-key")), {}, "UnlistedNetworkError"),
])
def test_connection_failures_name_the_cause_without_details(monkeypatch, failure, proxies, hint):
    # Each tester's machine fails differently; the message must say which way, not echo details.
    import traceback
    import tronclass.llm
    class Opener:
        def open(self, *args, **kwargs):
            raise failure
    monkeypatch.setattr(tronclass.llm, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(tronclass.llm, "getproxies", lambda: proxies)
    monkeypatch.setattr(tronclass.llm, "proxy_bypass", lambda host: False)
    client = OllamaClient(Settings(base_url="https://private-fixture.invalid/v1"), api_key="fake-key")
    with pytest.raises(ModelError, match=hint) as error:
        client.list_models()
    assert str(error.value).startswith("無法連線 Ollama")
    rendered = "".join(traceback.format_exception(error.value))
    assert "fake-key" not in rendered and "private-fixture" not in rendered and "7890" not in rendered


@pytest.mark.parametrize("value,expected", [(None, "稍候"), ("fake-key https://private.invalid", "稍候"),
                                           ("Wed, 01 Jan 2020 00:00:00 GMT", "0 秒")])
def test_retry_after_parsing_never_echoes_untrusted_header(value, expected):
    from tronclass.llm import retry_hint
    hint = retry_hint(value)
    assert expected in hint
    assert "fake-key" not in hint and "private.invalid" not in hint
