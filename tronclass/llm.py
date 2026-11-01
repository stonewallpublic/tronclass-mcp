"""Model adapters. Credentials never enter public settings or result metadata."""

import json
import os
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, getproxies, proxy_bypass

from tronclass.config import Settings, cloudflare_token
from tronclass.export_privacy import redact_connection


class ModelError(RuntimeError):
    pass


# Verified against Ollama's OpenAI-compatible /v1/chat/completions: "none" asks
# for no thinking; boolean-only thinking models map other recognized efforts to
# true. Older servers may not support this field, so "default" sends nothing.
REASONING_EFFORT = {"default": None, "off": "none", "on": "medium"}


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    metadata: dict


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def retry_hint(value):
    """Only render parsed durations, never an untrusted header verbatim."""
    try:
        seconds = int(value) if value and value.isascii() and value.isdecimal() else None
        if seconds is None:
            seconds = max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        return f"請等待 {seconds} 秒後再試。"
    except (TypeError, ValueError, OverflowError):
        return "請稍候再試。"


def connection_hint(exc, base_url):
    """Name the failure class so each tester can act on it; never echo the URL, key or proxy."""
    reason = getattr(exc, "reason", exc)
    url = urlparse(base_url)
    # urllib follows the system proxy (environment or Windows settings); curl.exe does not.
    if getproxies().get(url.scheme) and not proxy_bypass(url.hostname or ""):
        return "這台電腦的 Python 使用系統代理伺服器連線但失敗；請確認代理／VPN 軟體正在執行，或關閉系統代理後重開介面。"
    if isinstance(reason, socket.gaierror):
        return "找不到主機（DNS）；請確認網址拼字與目前入口，或改用其他網路／DNS。"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "HTTPS 憑證驗證失敗；防毒軟體或公司網路可能攔截 HTTPS，請關閉 HTTPS 掃描或改用其他網路。"
    if isinstance(reason, ssl.SSLError):
        return "HTTPS 連線失敗；請關閉 VPN、代理或防毒 HTTPS 掃描後再試。"
    if isinstance(reason, ConnectionRefusedError):
        return "連線被拒絕；本機請確認 ollama serve 正在執行，遠端請確認目前入口。"
    return "本機請確認 ollama serve；遠端請確認網路並向負責人取得目前入口。"


def validate_ollama_connection(base_url, api_key=""):
    """Validate before sending credentials or constructing public UI Settings."""
    try:
        url = urlparse(base_url)
        valid = (url.scheme in {"http", "https"} and url.hostname and not url.query
                 and not url.fragment and url.username is None and url.password is None)
        url.port  # Reject malformed ports without echoing the supplied URL.
    except ValueError:
        valid = False
    if not valid:
        raise ModelError("API base URL 需為有效 HTTP(S) 網址，不可包含帳密、金鑰或 query string。")
    if api_key and api_key in unquote(base_url):
        raise ModelError("API key 不可放在 URL 中，請只填入金鑰欄位。")
    if not url.path.rstrip("/").endswith("/v1"):
        raise ModelError("API base URL 需包含 /v1。")
    if api_key and url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelError("含 API key 的遠端連線必須使用 HTTPS；只有 localhost 可使用 HTTP。")
    if any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise ModelError("API key 格式無效，請重新貼上金鑰。")


class HttpModelClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request(self, path: str, payload=None, timeout=None, headers=None, base_url=None):
        base = self.settings.base_url if base_url is None else base_url
        if not base:
            raise ModelError("尚未設定模型服務。Cloudflare 模式請填入 CLOUDFLARE_ACCOUNT_ID。")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            base.rstrip("/") + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="GET" if payload is None else "POST",
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout or self.settings.timeout_seconds) as response:
                body = json.load(response)
        except HTTPError as exc:
            # Do not echo request contents or a potentially sensitive server body.
            if self.settings.provider == "cloudflare":
                hints = {
                    401: "請確認 token 具有 Workers AI Read 權限及正確的帳戶範圍。",
                    403: "請確認此 token 的帳戶存取權與 Workers AI Read 權限。",
                    402: "請確認 Cloudflare Unified Billing 已啟用且有足夠 credits。",
                    404: "請確認帳戶、模型 ID 與 Cloudflare API 端點。",
                    429: "請稍後再試，並確認 Cloudflare 與模型的額度或頻率限制。",
                }
                hint = hints.get(exc.code, "請確認模型、帳戶權限與 Cloudflare 服務狀態。")
                raise ModelError(f"Cloudflare 回傳 HTTP {exc.code}。{hint}") from None
            hints = {
                400: "服務不接受此請求參數；若已設定思考模式，請改回「模型預設」或確認 Ollama 版本支援 reasoning_effort。",
                401: "請確認 API key（CLI 為 OLLAMA_API_KEY）是否正確。",
                403: "請確認 API key 的存取權限。",
                404: "請確認模型名稱存在、網址包含 /v1，或向負責人取得目前入口。",
                429: retry_hint(exc.headers.get("Retry-After")) + "頻率限制由伺服器控制。",
                502: "遠端入口或模型服務無法使用，請聯絡負責人。",
                503: "服務忙碌，請等待其他請求結束後再試。",
                530: "遠端入口已失效或離線，請向負責人取得目前入口。",
            }
            hint = "服務要求重新導向；為保護金鑰不會轉送，請取得正確入口。" if 300 <= exc.code < 400 else hints.get(exc.code, "請確認服務狀態與目前入口。")
            raise ModelError(f"模型服務回傳 HTTP {exc.code}。{hint}") from None
        except (TimeoutError, socket.timeout) as exc:
            raise ModelError("模型回應逾時，請稍後重試或聯絡負責人確認模型執行狀況。") from None
        except (URLError, OSError) as exc:
            if self.settings.provider == "cloudflare":
                raise ModelError("無法連線 Cloudflare，請確認網路與服務網址。") from None
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                raise ModelError("模型回應逾時，請稍後重試。") from None
            raise ModelError(f"無法連線 Ollama。{connection_hint(exc, base)}") from None
        except (ValueError, UnicodeError) as exc:
            raise ModelError("模型服務未回傳有效 JSON，遠端入口可能失效，請確認目前入口。") from None
        if not isinstance(body, dict):
            raise ModelError("模型服務回傳格式錯誤。")
        return body


class OllamaClient(HttpModelClient):

    def __init__(self, settings: Settings, *, api_key=None):
        super().__init__(settings)
        # None retains CLI compatibility; an explicit empty string disables fallback.
        self._api_key = (os.getenv("OLLAMA_API_KEY", "") if api_key is None else api_key).strip()

    def redact(self, value):
        return redact_connection(value, self.settings.base_url, self._api_key)

    def _headers(self):
        validate_ollama_connection(self.settings.base_url, self._api_key)
        token = self._api_key
        return {"Authorization": f"Bearer {token}"} if token else {}

    def list_models(self) -> list[str]:
        body = self._request("/models", timeout=min(self.settings.timeout_seconds, 5), headers=self._headers())
        models = body.get("data")
        if not isinstance(models, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in models):
            raise ModelError("模型清單格式錯誤。")
        return sorted(self.redact(m["id"]) for m in models)

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        if not self.settings.model.strip():
            raise ModelError("請填入 ollama list 顯示的完整模型名稱。")
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "seed": self.settings.seed,
            "stream": False,
        }
        if self.settings.max_tokens is not None:
            payload["max_tokens"] = self.settings.max_tokens
        effort = REASONING_EFFORT[self.settings.thinking]
        if effort is not None:
            payload["reasoning_effort"] = effort
        body = self._request("/chat/completions", payload, headers=self._headers())
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
            reasoning = choice["message"].get("reasoning")
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("模型服務未回傳預期的 choices/message/content。") from None
        if not isinstance(text, str) or not text.strip():
            if finish_reason == "length":
                raise ModelError("模型已耗盡輸出預算，但尚未產生可顯示的回答；請取消或提高自訂上限，並檢查模型上下文容量。")
            raise ModelError("模型沒有回傳文字回答。")
        model = body.get("model", self.settings.model)
        if not isinstance(model, str):
            raise ModelError("模型名稱回傳格式錯誤。")
        return Completion(text=self.redact(text), model=self.redact(model), metadata=self.redact({
            "response_id": body.get("id"),
            "system_fingerprint": body.get("system_fingerprint"),
            "usage": body.get("usage"),
            "finish_reason": finish_reason,
            # Only the size is kept: thinking text is neither shown nor exported,
            # and a missing field means "not reported", not "did not think".
            "reasoning_characters": len(reasoning) if isinstance(reasoning, str) else None,
        }))


class CloudflareClaudeClient(HttpModelClient):
    """Account REST API with Cloudflare Unified Billing; Anthropic Messages wire format."""

    def _headers(self):
        token = cloudflare_token()
        if not token:
            raise ModelError("請於本機 secrets.toml 或環境變數設定 CLOUDFLARE_API_TOKEN。")
        headers = {"Authorization": f"Bearer {token}", "cf-aig-skip-cache": "true"}
        if self.settings.cloudflare_gateway_id:
            headers["cf-aig-gateway-id"] = self.settings.cloudflare_gateway_id
        return headers

    def check_credentials(self):
        body = self._request("/user/tokens/verify", headers=self._headers(), timeout=10,
                             base_url="https://api.cloudflare.com/client/v4")
        result = body.get("result")
        if body.get("success") is not True or not isinstance(result, dict) or result.get("status") != "active":
            raise ModelError("Cloudflare 未確認 token 為 active。")
        return True

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        if not self.settings.model:
            raise ModelError("請填入 Cloudflare Claude 模型 ID。")
        system = []
        conversation = []
        for message in messages:
            if message.get("role") == "system":
                system.append(message["content"])
            elif message.get("role") in {"user", "assistant"}:
                conversation.append({"role": message["role"], "content": message["content"]})
            else:
                raise ModelError("Claude 訊息只能包含 system、user 或 assistant。")
        payload = {
            "model": self.settings.model,
            "messages": conversation,
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "stream": False,
        }
        if system:
            payload["system"] = "\n\n".join(system)
        # Anthropic does not support seed; do not pretend it was applied.
        body = self._request("/messages", payload, headers=self._headers())
        content = body.get("content")
        if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
            raise ModelError("Claude 未回傳預期的 content 區塊。")
        texts = [block.get("text") for block in content if block.get("type") == "text"]
        if not texts or any(not isinstance(text, str) for text in texts) or not any(text.strip() for text in texts):
            raise ModelError("Claude 沒有回傳文字回答。")
        model = body.get("model", self.settings.model)
        if not isinstance(model, str):
            raise ModelError("Claude 回傳的模型名稱格式錯誤。")
        stop_reason = body.get("stop_reason")
        return Completion(text="\n".join(texts), model=model, metadata={
            "response_id": body.get("id"),
            "usage": body.get("usage"),
            "finish_reason": "length" if stop_reason == "max_tokens" else stop_reason,
            "stop_reason": stop_reason,
            "seed_applied": False,
            "skip_cache": True,
        })


def create_model_client(settings: Settings, *, api_key=None):
    if settings.provider == "cloudflare":
        return CloudflareClaudeClient(settings)
    return OllamaClient(settings, api_key=api_key)
