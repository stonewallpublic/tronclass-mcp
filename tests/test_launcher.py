"""Static guards for the double-click launcher; nothing here starts a process."""
import re

from tronclass.config import ROOT

BAT = ROOT / "Start-App.bat"
SCRIPT = ROOT / "tools" / "Start-App.ps1"


def test_batch_file_only_hands_off_to_the_powershell_launcher():
    raw = BAT.read_bytes()
    assert raw.isascii()  # cmd.exe misreads non-ASCII batch files on many code pages.
    text = raw.decode("ascii").lower()
    assert r"tools\start-app.ps1" in text
    assert "-noprofile" in text and "-executionpolicy bypass" in text


def test_launcher_is_readable_by_windows_powershell_51():
    # Without a BOM, Windows PowerShell 5.1 decodes the Chinese messages with the ANSI code page.
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_launcher_serves_only_this_computer_and_uses_project_environment():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert re.search(r"--server\.address['\"]?\s*,\s*['\"]localhost", text)
    assert "--server.headless" in text  # No first-run email prompt blocking the window.
    assert "requirements.txt" in text and "requirements-dev" not in text
    assert ".venv" in text and "(3, 11)" in text
    # Deep folders break pip on Windows' 260-character limit; say so instead of blaming the network.
    assert "LongPathsEnabled" in text


def test_launcher_never_touches_credentials_or_machine_paths():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    for secret in ("OLLAMA_API_KEY", "CLOUDFLARE_API_TOKEN", "api-key.txt", "TronClassOllamaAPI"):
        assert secret not in text
    assert not re.search(r"\$env:\w+\s*=", text)
    assert not re.search(r"[A-Za-z]:\\Users", text)
