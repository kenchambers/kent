import asyncio
import platform

import pytest

from agent.builtin.shell import (
    Shell,
    ShellArgs,
    detect_shell_backend,
    _truncate,
)
from agent.builtin._executors import (
    BashExecutor,
    ElevatedExecutor,
    PowerShellExecutor,
    WSLExecutor,
)
from agent.builtin.web_fetch import _validate_url, _to_markdown
from agent.builtin.web_search import _parse_results, _unwrap_ddg_redirect
from agent.cli import (
    APP_NAME,
    SUPPORTED_SERVICES,
    _build_parser,
    build_registry,
    cmd_doctor,
    cmd_models,
)
from agent.tools import ToolContext


def test_app_name_is_kent():
    assert APP_NAME == "kent"


def test_parser_default_command_is_repl():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.command is None  # main() routes None → cmd_repl


def test_parser_run_subcommand():
    parser = _build_parser()
    args = parser.parse_args(["run", "what is 2+2"])
    assert args.command == "run"
    assert args.prompt == "what is 2+2"
    assert args.quiet is False
    assert args.func.__name__ == "cmd_run"


def test_parser_auth_subcommand_defaults_to_atlascloud():
    parser = _build_parser()
    args = parser.parse_args(["auth"])
    assert args.command == "auth"
    assert args.service == "atlascloud"
    assert args.clear is False


def test_parser_models_default_service():
    parser = _build_parser()
    args = parser.parse_args(["models"])
    assert args.command == "models"
    assert args.service == "atlascloud"


def test_parser_doctor_subcommand():
    parser = _build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.func.__name__ == "cmd_doctor"


def test_cmd_models_lists_qwen(capsys):
    parser = _build_parser()
    rc = cmd_models(parser.parse_args(["models"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Atlas Cloud" in out
    assert "qwen/qwen3.6-35b-a3b" in out
    assert "(default)" in out


def test_cmd_doctor_runs_clean(capsys):
    parser = _build_parser()
    rc = cmd_doctor(parser.parse_args(["doctor"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[environment]" in out
    assert "[web search]" in out
    assert "[providers]" in out
    assert "atlascloud" in out


def test_atlascloud_default_model_is_qwen():
    svc = SUPPORTED_SERVICES["atlascloud"]
    assert svc["base_url"] == "https://api.atlascloud.ai/v1"
    assert svc["default_model"] == "qwen/qwen3.6-35b-a3b"
    assert svc["api_key_env"] == "ATLASCLOUD_API_KEY"
    assert "qwen/qwen3.6-35b-a3b" in svc["models"]


def test_default_registry_has_web_and_shell_tools():
    names = build_registry().names()
    assert "web_search" in names
    assert "web_fetch" in names
    assert "shell" in names


def test_unwrap_ddg_redirect_decodes_uddg_param():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpath%3Fa%3D1"
    assert _unwrap_ddg_redirect(wrapped) == "https://example.com/path?a=1"


def test_unwrap_ddg_redirect_passthrough_when_not_wrapped():
    assert _unwrap_ddg_redirect("https://example.com/foo") == "https://example.com/foo"


def test_parse_results_extracts_title_url_snippet():
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2F1">Hit One</a>
      <a class="result__snippet" href="#">snippet one</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://b.example/2">Hit Two</a>
      <div class="result__snippet">snippet two</div>
    </div>
    <div class="result">
      <span>no anchor here</span>
    </div>
    """
    hits = _parse_results(html, limit=10)
    assert len(hits) == 2
    assert hits[0] == {
        "title": "Hit One",
        "url": "https://a.example/1",
        "snippet": "snippet one",
    }
    assert hits[1]["url"] == "https://b.example/2"
    assert hits[1]["title"] == "Hit Two"
    assert hits[1]["snippet"] == "snippet two"


def test_parse_results_respects_limit():
    html = '<div class="result"><a class="result__a" href="https://a.example/x">x</a></div>' * 5
    assert len(_parse_results(html, limit=2)) == 2


@pytest.mark.parametrize(
    "url,bad",
    [
        ("https://example.com/", False),
        ("http://example.com/x", False),
        ("ftp://example.com/", True),
        ("https://user:pass@example.com/", True),
        ("https://localhost/", True),  # no dot
        ("not a url", True),
        ("https://" + "x" * 3000, True),
    ],
)
def test_validate_url(url, bad):
    err = _validate_url(url)
    assert (err is not None) is bad


def test_to_markdown_html_vs_text():
    md = _to_markdown("text/html; charset=utf-8", "<h1>Hi</h1><p>body</p>")
    assert "Hi" in md
    assert "<h1>" not in md
    assert _to_markdown("text/plain", "raw text") == "raw text"


def test_truncate_caps_at_max_output_bytes():
    big = "a" * 100_000
    out = _truncate(big)
    assert "<truncated total_bytes=100000>" in out
    assert len(out) < len(big)
    assert _truncate("short") == "short"


def test_detect_shell_backend_matches_host():
    backend = detect_shell_backend()
    system = platform.system()
    if system == "Darwin":
        assert backend.program == "/bin/bash"
    elif system == "Linux":
        assert backend.program == "/bin/bash"
    elif system == "Windows":
        assert backend.program in ("wsl.exe", "powershell.exe")


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shell required")
@pytest.mark.asyncio
async def test_shell_runs_simple_command():
    sh = Shell()
    result = await sh.call(ShellArgs(command='echo agent-ok'), ToolContext())
    assert not result.is_error
    assert isinstance(result.output, dict)
    assert result.output["exit_code"] == 0
    assert "agent-ok" in result.output["stdout"]


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shell required")
@pytest.mark.asyncio
async def test_shell_aborts_on_signal():
    sh = Shell()
    sig = asyncio.Event()
    asyncio.get_event_loop().call_later(0.1, sig.set)
    ctx = ToolContext(signal=sig)
    result = await sh.call(ShellArgs(command="sleep 10", timeout_seconds=30), ctx)
    assert result.is_error
    assert "aborted" in result.output


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX shell required")
@pytest.mark.asyncio
async def test_shell_times_out():
    sh = Shell()
    result = await sh.call(
        ShellArgs(command="sleep 5", timeout_seconds=0.2), ToolContext()
    )
    assert result.is_error
    assert "timed out" in result.output


# ---------- executor argv shape ------------------------------------------- #
# These are pure-function tests — no host or subprocess needed, so they run
# uniformly on every platform.

def test_bash_executor_argv():
    ex = BashExecutor()
    assert ex.build_argv("ls -la") == ["/bin/bash", "-lc", "ls -la"]


def test_powershell_executor_argv():
    ex = PowerShellExecutor()
    assert ex.build_argv("Get-Process") == [
        "powershell.exe", "-NoProfile", "-Command", "Get-Process"
    ]


def test_wsl_executor_argv_minimal():
    ex = WSLExecutor()
    assert ex.build_argv("uname -a") == ["wsl.exe", "--", "bash", "-lc", "uname -a"]
    assert ex.label == "wsl.exe → bash"


def test_wsl_executor_argv_with_distro_and_user():
    ex = WSLExecutor(distro="Ubuntu-22.04", user="root")
    # -d before -u before -- before bash -lc
    assert ex.build_argv("whoami") == [
        "wsl.exe", "-d", "Ubuntu-22.04", "-u", "root", "--", "bash", "-lc", "whoami"
    ]
    assert "Ubuntu-22.04" in ex.label
    assert "root" in ex.label


def test_elevated_executor_posix_prepends_sudo():
    # Force POSIX path regardless of host: WSLExecutor on POSIX is unusual but
    # it's a pure-function test — only argv shape matters here.
    inner = BashExecutor()
    wrapped = ElevatedExecutor(inner)
    if platform.system() == "Windows":
        pytest.skip("POSIX sudo path only")
    argv = wrapped.build_argv("id -u")
    assert argv[:2] == ["sudo", "-n"]
    assert argv[2:] == ["/bin/bash", "-lc", "id -u"]
    assert wrapped.label.startswith("elevated[")


def test_elevated_executor_rejects_double_wrap():
    inner = ElevatedExecutor(BashExecutor())
    with pytest.raises(ValueError):
        ElevatedExecutor(inner)


@pytest.mark.skipif(platform.system() == "Windows", reason="non-Windows behavior only")
@pytest.mark.asyncio
async def test_shell_warns_when_distro_ignored_on_non_wsl():
    sh = Shell()
    result = await sh.call(
        ShellArgs(command="echo ok", distro="Ubuntu", user="root"), ToolContext()
    )
    assert isinstance(result.output, dict)
    assert "warnings" in result.output
    assert any("distro/user ignored" in w for w in result.output["warnings"])
