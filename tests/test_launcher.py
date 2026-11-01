"""Launcher guards and loopback port checks; never starts the app or browser."""
import re
import shutil
import subprocess

import pytest

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


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_launcher_skips_a_port_occupied_by_a_loopback_listener(address):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("The Windows launcher requires Windows PowerShell.")
    script_path = str(SCRIPT).replace("'", "''")
    command = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__SCRIPT__', [ref]$tokens, [ref]$errors)
foreach ($name in @('Test-AppPortAvailable', 'Get-AppPort')) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if (-not $definition) { throw "Missing launcher function: $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$address = [Net.IPAddress]::Parse('__ADDRESS__')
if (-not ([Net.Dns]::GetHostAddresses('localhost') | Where-Object { $_.Equals($address) })) { exit 77 }
$listener = New-Object Net.Sockets.TcpListener($address, 0)
$listener.Server.ExclusiveAddressUse = $true
try {
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    if (Test-AppPortAvailable -Port $port) { throw 'An occupied port was reported as available.' }
    $selected = Get-AppPort -StartPort $port
    if ($selected -le $port) { throw 'The launcher did not advance beyond the occupied port.' }
    if (-not (Test-AppPortAvailable -Port $selected)) { throw 'The selected replacement port is unavailable.' }
} finally { $listener.Stop() }
if (-not (Test-AppPortAvailable -Port $port)) { throw 'The released port was not reusable.' }
""".replace("__SCRIPT__", script_path).replace("__ADDRESS__", address)
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode == 77:
        pytest.skip("This loopback address is not used by localhost on this machine.")
    assert result.returncode == 0, result.stdout + result.stderr
