"""Streamlit frontend. Model orchestration belongs in the backend, not this file."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timezone

import streamlit as st

from tronclass import config
from tronclass.config import Settings
from tronclass.contracts import AnswerRequest
from tronclass.database import CourseRepository, DataError, serialize_snapshot
from tronclass.factory import create_backend
from tronclass.fingerprints import sha256
from tronclass.export_privacy import redact_connection
from tronclass.llm import CloudflareClaudeClient, ModelError, create_model_client, validate_ollama_connection

st.set_page_config(page_title="TronClass 課業助理", page_icon="📚", layout="wide")

SECTIONS = {
    "課程資訊": "courses",
    "授課教師": "course_instructors",
    "課程大綱": "course_outline",
    "公告": "announcements",
    "作業": "assignments",
    "我的繳交紀錄": "submissions",
    "教材活動": "learning_materials",
    "講義段落": "materials",
    "附件": "attachments",
    "附件文字與位置": "attachment_chunks",
    "線上測驗": "quizzes",
    "我的測驗紀錄": "quiz_attempts",
    "我的點名": "attendance_records",
    "成績配分": "grade_categories",
    "我的成績明細": "student_grade_items",
    "我的總成績": "student_course_grades",
    "討論區（非官方）": "discussions",
    "停課日": "no_class_days",
}
THINKING_LABELS = {"default": "模型預設", "off": "關閉", "on": "開啟"}
COLUMN_NAMES = {
    "course_id": "課程編號", "name": "名稱", "title": "標題", "content": "內容",
    "announcement_id": "公告編號", "posted_at": "發布時間", "author": "發布人",
    "assignment_id": "作業編號", "start_at": "開始時間", "due": "系統截止時間",
    "submission": "繳交方式", "assignment_type": "作業類型", "unit_name": "章節",
    "status": "狀態", "score": "已公布分數", "actual_score": "已公布實際得分",
    "score_published": "成績已公布", "score_note": "成績備註", "submitted_at": "繳交時間",
    "student_id": "學號", "checkin_at": "點名時間", "source": "來源",
    "note": "備註", "filename": "檔名", "file_type": "格式", "locator_type": "位置類型",
    "locator": "頁／投影片／段落", "section": "段落標題", "body_text": "教材內容",
    "intro_zh": "課程簡介", "intro_en": "英文簡介", "grading_json": "評分方式",
    "late_policy": "遲交規則", "instructor_name": "教師姓名", "role": "角色",
    "course_code": "課程代碼", "subject_code": "科目代碼", "credits": "學分",
    "weekday": "星期", "time": "上課時間", "room": "教室", "weight_percent": "配分比例",
    "published": "總成績已公布", "final_grade": "已公布總成績", "raw_grade": "已公布原始成績",
    "quiz_id": "測驗編號", "open_at": "開放時間", "close_at": "截止時間",
    "max_score": "滿分", "attempt_no": "作答次數", "submission_method": "交卷方式",
    "date": "日期", "reason": "原因", "end_at": "結束時間", "completion_rule": "完成指標",
}


def render_data(snapshot):
    st.subheader("課程資料")
    st.caption("資料來自 SQLite。這裡顯示原始紀錄；AI 回答不會改寫資料庫。")
    labels = {c["course_id"]: c["name"] for c in snapshot["courses"]}
    course = st.selectbox("課程篩選", ["全部課程", *labels], format_func=lambda x: labels.get(x, x))
    selected = st.selectbox("資料類別", list(SECTIONS))
    rows = snapshot[SECTIONS[selected]]
    if course != "全部課程":
        rows = [r for r in rows if r.get("course_id", course) == course]
    if selected == "作業":
        st.info("截止時間是作業欄位的原始值。另請查看同課程的公告，資料中保留了改期情境。")
    if not rows:
        st.info("此範圍沒有資料。")
        return
    if selected in {"公告", "講義段落", "附件文字與位置", "教材活動", "討論區（非官方）"}:
        for row in rows:
            title = row.get("title") or row.get("section") or row.get("filename") or f"討論 {row.get('post_id')}"
            with st.expander(f"{labels.get(row.get('course_id'), '')} · {title}"):
                location = " · ".join(str(row[k]) for k in (
                    "announcement_id", "source", "posted_at", "author", "locator_type", "locator"
                ) if row.get(k) is not None)
                if location:
                    st.caption(location)
                st.text(row.get("content") or row.get("body_text") or "（沒有文字內容）")
    else:
        display = [{COLUMN_NAMES.get(k, k): v for k, v in row.items() if k != "local_path"} for row in rows]
        st.dataframe(display, width="stretch", hide_index=True)


def run_summary(result):
    """Show only measured values; fields the service did not report stay unknown."""
    meta = result["metadata"]
    timing = meta.get("timing") or {}
    if meta.get("model_called") is False:
        return f"未呼叫模型 · 寒暄快速回覆，未讀取課程資料 · 總計 {result['elapsed_seconds']:.2f} 秒"
    parts = [result["model"], f"總計 {result['elapsed_seconds']:.2f} 秒"]
    if timing.get("model_request_seconds") is not None:
        parts += [f"資料準備 {timing['preparation_seconds']:.2f} 秒", f"模型往返 {timing['model_request_seconds']:.2f} 秒"]
    usage = meta.get("usage")
    if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
        parts.append(f"輸入 {usage['prompt_tokens']} / 輸出 {usage.get('completion_tokens', '未知')} token")
    elif "usage" in meta:
        parts.append("token 用量未提供")
    return " · ".join([*parts, result["verification"]])


def invalidate_connection():
    """Widget callbacks run before rerendering; no credentials enter a scope hash."""
    st.session_state.ollama_connected = False
    st.session_state.ollama_models = []
    st.session_state.connection_revision = st.session_state.get("connection_revision", 0) + 1
    st.session_state.messages = []
    st.session_state.records = []
    st.session_state.failed_question = None
    st.session_state.pop("ollama_model_choice", None)


def clear_connection():
    invalidate_connection()
    for key in ("remote_api_key", "base_url_ollama_remote"):
        st.session_state[key] = ""


def choose_model():
    selected = st.session_state.get("ollama_model_choice")
    if selected and selected != "手動輸入":
        st.session_state.model_name_ollama_local = selected
        invalidate_connection()


def main():
    st.title("📚 TronClass 課業助理")
    st.caption("System 1 · 單一模型讀取模擬課程資料，協助查詢跨課程資訊。")
    with st.sidebar:
        st.header("模型設定")
        providers = {"ollama": "Ollama", "cloudflare": "Cloudflare → Claude"}
        # The page always opens on remote Ollama; LLM_PROVIDER and [app] provider still drive the CLI.
        provider = st.selectbox("模型服務", list(providers), index=0,
                                format_func=providers.get, key="model_provider", on_change=invalidate_connection)
        try:
            defaults = Settings.from_env(provider=provider)
        except (ValueError, OSError) as exc:
            st.error(f"設定有誤：{exc}")
            st.stop()
        api_key = ""  # UI never falls back to a process-wide Ollama credential.
        remote = False
        if provider == "ollama":
            mode = st.selectbox("Ollama 連線方式", ["本機 Ollama", "遠端 Ollama API"], index=1,
                                key="ollama_mode", on_change=invalidate_connection)
            remote = mode == "遠端 Ollama API"
            mode_key = "remote" if remote else "local"
            st.button("清除連線／金鑰", on_click=clear_connection)
            base_url = st.text_input("API base URL", value="" if remote else defaults.base_url,
                                    key=f"base_url_ollama_{mode_key}", on_change=invalidate_connection,
                                    placeholder="https://<目前入口>/v1", help="需包含 /v1；金鑰不可放在網址中。")
            if remote:
                api_key = st.text_input("API key", type="password", key="remote_api_key",
                                        on_change=invalidate_connection,
                                        help="只保留於目前 session；清除連線可移除金鑰。遠端請使用 HTTPS。")
                model = config.REMOTE_OLLAMA_MODEL
                st.markdown(f"模型名稱：`{model}`")
                if st.session_state.get("remote_model_setting") != model:
                    invalidate_connection()
                    st.session_state.remote_model_setting = model
            else:
                model = st.text_input("模型名稱", value=defaults.model,
                                      key="model_name_ollama_local", on_change=invalidate_connection,
                                      placeholder="填入 ollama list 的完整模型名稱與 tag")
            if not remote and st.session_state.get("ollama_models"):
                st.selectbox("從模型清單選擇", ["手動輸入", *st.session_state.ollama_models],
                             key="ollama_model_choice", on_change=choose_model)
            check_connection = st.button("檢查連線")
            if st.session_state.get("ollama_connected") and not check_connection:
                st.success("模型服務已連線")
                if not st.session_state.ollama_models:
                    st.warning("服務尚未提供模型，請聯絡負責人。")
                elif model and model not in st.session_state.ollama_models:
                    st.warning("清單中沒有此模型名稱，請確認名稱與 tag。")
        else:
            base_url = st.text_input("模型服務網址", value=defaults.base_url, key="base_url_cloudflare", disabled=True)
            model = st.text_input("模型名稱", value=defaults.model, key="model_name_cloudflare",
                                  placeholder="anthropic/claude-…")
            check_connection = st.button("檢查憑證")
        temperature = st.number_input("Temperature", min_value=0.0, max_value=1.0 if provider == "cloudflare" else 2.0,
                                      value=defaults.temperature, step=0.1, key=f"temperature_{provider}")
        if provider == "cloudflare":
            st.caption("金鑰從本機設定讀取。問答會使用 Cloudflare 帳戶額度；Claude 模式不使用 seed。")
        max_tokens = defaults.max_tokens
        if provider == "ollama":
            limit_output = st.checkbox("自訂輸出長度上限", value=defaults.max_tokens is not None,
                                       key="limit_output_ollama")
            max_tokens = int(st.number_input("最大輸出 token", min_value=1,
                value=defaults.max_tokens or 8192, step=1024, key="max_tokens_ollama")) if limit_output else None
            st.caption("未勾選時由模型決定輸出長度；仍受模型上下文及連線服務限制。")
        thinking = "default"
        if provider == "ollama":
            thinking = st.selectbox("思考模式", list(THINKING_LABELS), format_func=THINKING_LABELS.get,
                                    index=list(THINKING_LABELS).index(defaults.thinking), key="thinking_ollama")
            st.caption("模型預設不送出設定；關閉／開啟透過 reasoning_effort（none／medium）要求，需 Ollama 版本與模型支援。")
        reference_time = st.text_input("固定情境時間", value=defaults.reference_time)
        st.caption("這是模擬資料的參考時間，非電腦目前時間。比較測試時請維持相同設定。")

    connection_error = ""
    if provider == "ollama":
        if not base_url.strip():
            connection_error = "請填入 API base URL，需包含 /v1。"
        elif remote and not api_key.strip():
            connection_error = "請填入遠端 API key。"
        else:
            try:
                validate_ollama_connection(base_url.strip(), api_key.strip())
            except ModelError as exc:
                connection_error = str(exc)
    if connection_error and check_connection:
        st.sidebar.error(connection_error)

    try:
        # Keep data browsing available while rejecting unsafe connection input.
        settings_url = base_url.strip().rstrip("/")
        if connection_error:
            settings_url = "http://localhost:11434/v1"
        settings = replace(defaults, base_url=settings_url, model=model.strip(),
                           temperature=temperature, max_tokens=max_tokens, thinking=thinking, reference_time=reference_time.strip())
        snapshot = CourseRepository(settings.db_path).snapshot(settings.student_id)
        prompt_hash = sha256(settings.prompt_path.read_text(encoding="utf-8"))
    except (DataError, ValueError, OSError) as exc:
        st.error(str(exc))
        st.stop()

    context_hash = sha256(serialize_snapshot(snapshot))
    scope = (str(settings), context_hash, prompt_hash, st.session_state.get("connection_revision", 0))
    if st.session_state.get("conversation_scope") != scope:
        st.session_state.conversation_scope = scope
        st.session_state.messages = []
        st.session_state.records = []
        st.session_state.failed_question = None

    with st.sidebar:
        if check_connection and not connection_error:
            try:
                client = create_model_client(settings, api_key=api_key)
                if isinstance(client, CloudflareClaudeClient):
                    client.check_credentials()
                    st.success("Cloudflare token 有效")
                    st.caption("此檢查只確認憑證有效；模型權限與額度需透過實際問答確認。")
                else:
                    models = client.list_models()
                    if remote and model not in models:
                        raise ModelError(f"遠端服務缺少指定模型 {model}，請聯絡負責人安裝或確認入口；不會改用其他模型。")
                    st.session_state.ollama_connected = True
                    st.session_state.ollama_models = models
                    st.rerun()
            except ModelError as exc:
                st.session_state.ollama_connected = False
                st.session_state.ollama_models = []
                st.error(str(exc))
        st.divider()
        st.success("SQLite 已連線")
        st.write(snapshot["student"]["name"])
        st.caption(f"{settings.student_id} · 所有資料皆為虛構")
        if st.button("清除對話"):
            st.session_state.messages = []
            st.session_state.records = []
            st.session_state.failed_question = None
            st.rerun()

    columns = st.columns(3)
    for column, title, section in zip(columns, ["課程", "公告", "作業"], ["courses", "announcements", "assignments"]):
        column.metric(title, len(snapshot[section]))

    chat_tab, data_tab = st.tabs(["課業問答", "資料查詢"])
    with data_tab:
        render_data(snapshot)
    with chat_tab:
        st.info("此為 System 1 原型，回答尚未經驗證 agent 查證。")
        if connection_error or not settings.model or not settings.base_url:
            st.warning(connection_error or "請完成模型設定；Cloudflare 需設定帳戶 ID，Ollama 需指定已安裝模型。資料查詢可直接使用。")
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
        if st.session_state.records:
            latest = st.session_state.records[-1]["result"]
            st.caption(run_summary(latest))
            meta = latest["metadata"]
            if meta.get("thinking") == "off" and meta.get("reasoning_characters"):
                st.warning("已要求關閉思考，但服務仍回傳思考內容；目前 Ollama 版本或模型可能不支援此設定。")
            if latest["metadata"].get("finish_reason") == "length":
                st.warning("模型回報回答未完成（length）。請取消或提高自訂輸出上限，並檢查剩餘上下文；調整後重新提問。此狀況已記錄於匯出結果。")
            if latest["sources"] or latest["tool_calls"]:
                with st.expander("來源與工具紀錄"):
                    st.json({"sources": latest["sources"], "tool_calls": latest["tool_calls"]})
            st.download_button(
                "下載對話與執行紀錄（JSON）",
                json.dumps(st.session_state.records, ensure_ascii=False, indent=2),
                file_name="tronclass_conversation.json", mime="application/json",
            )
        if remote and not connection_error and not st.session_state.get("ollama_connected"):
            st.info("請先按「檢查連線」，確認遠端服務提供指定模型。")
        can_chat = not connection_error and bool(settings.model and settings.base_url) and (
            not remote or st.session_state.get("ollama_connected", False))
        question = st.chat_input("例如：資料結構作業三什麼時候截止？", disabled=not can_chat)
        if st.session_state.failed_question and st.button("重新送出上一題", disabled=not can_chat):
            question = st.session_state.failed_question
        if can_chat and question and question.strip():
            request = AnswerRequest(question, settings.student_id, settings.reference_time,
                                    history=list(st.session_state.messages))
            with st.chat_message("user"):
                st.markdown(question)
            try:
                with st.spinner("正在讀取課程資料並等待模型回答…"):
                    client = create_model_client(settings, api_key=api_key)
                    result = create_backend(settings, client=client).answer(request)
            except (ModelError, DataError, ValueError, OSError) as exc:
                st.session_state.failed_question = question
                st.error(str(exc))
            else:
                st.session_state.messages.extend([
                    {"role": "user", "content": question}, {"role": "assistant", "content": result.answer},
                ])
                st.session_state.records.append(redact_connection({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request": asdict(request), "result": asdict(result),
                }, settings.base_url, api_key))
                st.session_state.failed_question = None
                st.rerun()


if __name__ == "__main__":
    main()
