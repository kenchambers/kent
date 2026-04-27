"""
kent — interactive terminal AI agent CLI.

Layout follows the pattern of opencode / hermes-agent / claude-code:

  kent                    Launch interactive REPL (default)
  kent run "<prompt>"     One-shot: stream the answer to stdout, exit
  kent auth               Set or clear the saved API key
  kent models             List available models for the active service
  kent doctor             Show environment / shell backend / dependency health
  kent --help             Standard CLI help

Inside the REPL, slash commands:
  /help    /tools    /model    /clear    /exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builtin.shell import Shell, detect_shell_backend
from .builtin.spawn import Spawn
from .builtin.web_fetch import WebFetch
from .builtin.web_search import WebSearch
from .critic import critique
from .events import (
    AssistantMessageComplete,
    ModelError,
    Terminal,
    TextDelta,
    ToolCallComplete,
    ToolResult,
)
from .llm import LLM, OpenAICompatibleLLM
from .loop import run
from .tools import ToolRegistry

APP_NAME = "kent"
CONFIG_DIR = Path(os.environ.get("KENT_HOME", str(Path.home() / ".kent")))
CONFIG_PATH = CONFIG_DIR / "config.json"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"

SUPPORTED_SERVICES: dict[str, dict[str, Any]] = {
    "atlascloud": {
        "label": "Atlas Cloud",
        "base_url": "https://api.atlascloud.ai/v1",
        "api_key_env": "ATLASCLOUD_API_KEY",
        "default_model": "qwen/qwen3.6-35b-a3b",
        "default_context_window": 131_072,
        "models": ["qwen/qwen3.6-35b-a3b"],
    },
}

SYSTEM_PROMPT = (
    "You are kent, a terminal AI agent. You have these tools: "
    "web_search (DuckDuckGo HTML, no API key), web_fetch (URL → markdown), "
    "shell (host shell — bash on macOS/Linux/WSL, PowerShell on Windows), "
    "spawn_subagent (delegate a focused subtask). Prefer web_search before "
    "web_fetch. Prefer shell over re-implementing with another tool. Keep "
    "responses concise."
)


# ---------- config / credentials persistence ------------------------------- #

@dataclass
class StartupChoice:
    service_id: str
    service_label: str
    base_url: str
    model: str
    api_key: str
    context_window: int
    critic_model: str | None = None  # None = critic disabled


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict[str, Any]) -> None:
    _ensure_config_dir()
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def load_credentials() -> dict[str, str]:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_credentials(data: dict[str, str]) -> None:
    _ensure_config_dir()
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass  # Windows or other FS where chmod doesn't apply


def resolve_api_key(service_id: str, *, prompt_if_missing: bool = True) -> str | None:
    service = SUPPORTED_SERVICES[service_id]
    env_key = service["api_key_env"]
    if (val := os.environ.get(env_key)):
        return val
    if (val := load_credentials().get(service_id)):
        return val
    if prompt_if_missing:
        import getpass
        entered = getpass.getpass(
            f"API key for {service['label']} (or set {env_key}): "
        ).strip()
        return entered or None
    return None


# ---------- banner / printers --------------------------------------------- #

def _print_banner() -> None:
    print("=" * 60)
    print(f" {APP_NAME} — interactive terminal AI agent")
    print("=" * 60)


def _print_environment() -> None:
    backend = detect_shell_backend()
    print()
    print("[environment]")
    print(f"  OS         : {platform.system()} {platform.release()}")
    print(f"  Python     : {platform.python_version()}")
    print(f"  Shell tool : {backend.label}  ({backend.program})")


def _print_web_search_notice() -> None:
    print()
    print("[web search]")
    print("  Provider   : DuckDuckGo HTML  (https://html.duckduckgo.com/html/)")
    print("  API key    : none required")
    print("  Notes      : DDG may rate-limit; this is best-effort scraping.")
    print("               No queries are sent to any third-party search API.")


# ---------- prompts -------------------------------------------------------- #

def _prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        import getpass
        value = getpass.getpass(f"{label}{suffix}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def _prompt_choice(label: str, options: list[str], default: str) -> str:
    while True:
        rendered = ", ".join(options)
        value = _prompt(f"{label} ({rendered})", default=default)
        if value in options:
            return value
        print(f"  ! invalid choice: {value!r}. Pick one of: {rendered}")


def gather_startup_choice(*, save: bool = True) -> StartupChoice:
    """
    Ask the user (or load from config) which service / model / key to use.
    Saves non-secret choices to ~/.kent/config.json so subsequent runs
    skip the prompts (but still re-resolve the API key from env / credentials).
    """
    cfg = load_config()
    service_ids = list(SUPPORTED_SERVICES.keys())

    print()
    print("[llm setup]")
    service_id = _prompt_choice(
        "Service",
        service_ids,
        default=cfg.get("service_id", service_ids[0]),
    )
    service = SUPPORTED_SERVICES[service_id]

    model = _prompt_choice(
        "Model",
        service["models"],
        default=cfg.get("model", service["default_model"]),
    )

    api_key = resolve_api_key(service_id, prompt_if_missing=True)
    if not api_key:
        env_key = service["api_key_env"]
        print(f"  ! No API key entered and {env_key} is unset. Aborting.")
        sys.exit(2)

    print()
    print("[critic — second-pass review (optional)]")
    print("  A separate LLM re-checks each answer for accuracy/completeness.")
    print("  Helps non-reasoning models; redundant for o1/R1/extended-thinking.")
    print("  Roughly doubles per-turn cost.")
    prev_enabled = bool(cfg.get("critic_model"))
    enable_critic = _prompt_choice(
        "Enable critic",
        ["yes", "no"],
        default="yes" if prev_enabled else "no",
    ) == "yes"
    critic_model: str | None = None
    if enable_critic:
        critic_model = _prompt_choice(
            "Critic model",
            service["models"],
            default=cfg.get("critic_model") or model,
        )

    if save:
        save_config({
            "service_id": service_id,
            "model": model,
            "critic_model": critic_model,
        })

    return StartupChoice(
        service_id=service_id,
        service_label=service["label"],
        base_url=service["base_url"],
        model=model,
        api_key=api_key,
        context_window=service["default_context_window"],
        critic_model=critic_model,
    )


# ---------- registry / runtime -------------------------------------------- #

def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WebSearch())
    registry.register(WebFetch())
    registry.register(Shell())
    return registry


def _make_llm(choice: StartupChoice) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=choice.base_url,
        api_key=choice.api_key,
        model=choice.model,
        context_window=choice.context_window,
    )


def _make_critic_llm(choice: StartupChoice) -> OpenAICompatibleLLM | None:
    if not choice.critic_model:
        return None
    return OpenAICompatibleLLM(
        base_url=choice.base_url,
        api_key=choice.api_key,
        model=choice.critic_model,
        context_window=choice.context_window,
    )


async def _run_once(
    registry: ToolRegistry,
    llm: OpenAICompatibleLLM,
    history: list[dict],
    user_input: str,
    *,
    quiet_tools: bool = False,
) -> tuple[list[dict], str]:
    """Single agent turn. Returns (new_history, terminal_reason)."""
    history = [*history, {"role": "user", "content": user_input}]
    new_messages: list[dict] = []
    terminal_reason = "completed"
    async for ev in run(
        messages=history,
        tools=registry,
        llm=llm,
        system=SYSTEM_PROMPT,
        max_turns=15,
    ):
        if isinstance(ev, TextDelta):
            print(ev.text, end="", flush=True)
        elif isinstance(ev, ToolCallComplete) and not quiet_tools:
            args_preview = str(ev.call.arguments)
            if len(args_preview) > 120:
                args_preview = args_preview[:117] + "..."
            print(f"\n  → {ev.call.name}({args_preview})")
        elif isinstance(ev, ToolResult) and not quiet_tools:
            tag = "ERR" if ev.is_error else "OK"
            print(f"  ← [{tag}] {ev.call_id or '<no-id>'}")
        elif isinstance(ev, AssistantMessageComplete):
            new_messages.append(ev.message.to_openai_dict())
        elif isinstance(ev, ModelError):
            print(f"\n[model error] {type(ev.error).__name__}: {ev.error}")
        elif isinstance(ev, Terminal):
            terminal_reason = ev.reason
            if ev.reason != "completed":
                print(f"\n[terminal: {ev.reason}]")
    print()
    return [*history, *new_messages], terminal_reason


async def _stream_one_turn(
    registry: ToolRegistry,
    llm: OpenAICompatibleLLM,
    history: list[dict],
    user_input: str,
    *,
    critic_llm: LLM | None = None,
    quiet_tools: bool = False,
) -> tuple[list[dict], str]:
    """
    Run one turn; if a critic is provided and the turn completed cleanly,
    run a single critique pass and revise once if issues are flagged.
    """
    history, reason = await _run_once(
        registry, llm, history, user_input, quiet_tools=quiet_tools
    )
    if critic_llm is None or reason != "completed":
        return history, reason

    verdict = await critique(history, critic_llm)
    if not verdict:
        return history, reason

    preview = verdict if len(verdict) <= 200 else verdict[:197] + "..."
    print(f"\n[critic] flagged issues — revising")
    print(f"  {preview}")
    injected = (
        "A reviewer flagged the following issues with your previous answer. "
        f"Please address them concisely:\n\n{verdict}"
    )
    return await _run_once(
        registry, llm, history, injected, quiet_tools=quiet_tools
    )


# ---------- slash commands ------------------------------------------------- #

SLASH_HELP = """\
slash commands:
  /help            show this list
  /tools           list registered tools
  /model           show current service / model
  /clear           clear conversation history
  /exit, /quit     leave the session
"""


def _handle_slash(
    cmd: str,
    *,
    history: list[dict],
    registry: ToolRegistry,
    choice: StartupChoice,
) -> tuple[list[dict], bool]:
    """Returns (new_history, should_exit)."""
    head = cmd.strip().lower()
    if head in ("/exit", "/quit"):
        print("bye.")
        return history, True
    if head == "/help":
        print(SLASH_HELP)
    elif head == "/tools":
        print("tools: " + ", ".join(registry.names()))
    elif head == "/model":
        print(f"service: {choice.service_label} ({choice.base_url})")
        print(f"model  : {choice.model}")
        print(f"critic : {choice.critic_model or '<disabled>'}")
        print(f"window : {choice.context_window}")
    elif head == "/clear":
        print("[conversation cleared]")
        return [], False
    else:
        print(f"unknown slash command: {cmd!r}. /help for the list.")
    return history, False


# ---------- subcommands ---------------------------------------------------- #

async def _repl(choice: StartupChoice) -> None:
    llm = _make_llm(choice)
    critic_llm = _make_critic_llm(choice)
    registry = build_registry()
    registry.register(Spawn(parent_registry=registry, llm=llm))

    print()
    print("[ready]")
    print(f"  Service: {choice.service_label}  ({choice.base_url})")
    print(f"  Model  : {choice.model}")
    print(f"  Critic : {choice.critic_model or '<disabled>'}")
    print(f"  Tools  : {', '.join(registry.names())}")
    print("  Type your message. /help for slash commands. /exit to quit.")
    print("-" * 60)

    history: list[dict] = []
    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return
        if not user_input:
            continue
        if user_input.startswith("/"):
            history, should_exit = _handle_slash(
                user_input, history=history, registry=registry, choice=choice
            )
            if should_exit:
                return
            continue
        try:
            history, _ = await _stream_one_turn(
                registry, llm, history, user_input, critic_llm=critic_llm
            )
        except KeyboardInterrupt:
            print("\n[interrupted]")
            continue


def cmd_repl(_args: argparse.Namespace) -> int:
    _print_banner()
    _print_environment()
    _print_web_search_notice()
    choice = gather_startup_choice()
    try:
        asyncio.run(_repl(choice))
    except KeyboardInterrupt:
        print("\nbye.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """One-shot mode: print the assistant's reply to stdout and exit."""
    cfg = load_config()
    service_id = args.service or cfg.get("service_id") or next(iter(SUPPORTED_SERVICES))
    if service_id not in SUPPORTED_SERVICES:
        print(f"unknown service: {service_id}", file=sys.stderr)
        return 2
    service = SUPPORTED_SERVICES[service_id]
    model = args.model or cfg.get("model") or service["default_model"]
    api_key = resolve_api_key(service_id, prompt_if_missing=False)
    if not api_key:
        env_key = service["api_key_env"]
        print(
            f"no API key for {service_id}: set {env_key} or run `{APP_NAME} auth`",
            file=sys.stderr,
        )
        return 2

    choice = StartupChoice(
        service_id=service_id,
        service_label=service["label"],
        base_url=service["base_url"],
        model=model,
        api_key=api_key,
        context_window=service["default_context_window"],
        critic_model=cfg.get("critic_model"),
    )
    llm = _make_llm(choice)
    critic_llm = _make_critic_llm(choice)
    registry = build_registry()
    registry.register(Spawn(parent_registry=registry, llm=llm))

    async def _go() -> str:
        _, reason = await _stream_one_turn(
            registry, llm, [], args.prompt,
            critic_llm=critic_llm, quiet_tools=args.quiet,
        )
        return reason

    try:
        reason = asyncio.run(_go())
    except KeyboardInterrupt:
        return 130
    return 0 if reason == "completed" else 1


def cmd_auth(args: argparse.Namespace) -> int:
    service_id = args.service
    if service_id not in SUPPORTED_SERVICES:
        print(f"unknown service: {service_id}", file=sys.stderr)
        return 2
    service = SUPPORTED_SERVICES[service_id]
    creds = load_credentials()

    if args.clear:
        if service_id in creds:
            del creds[service_id]
            save_credentials(creds)
            print(f"cleared credential for {service_id}")
        else:
            print(f"no saved credential for {service_id}")
        return 0

    import getpass
    key = getpass.getpass(f"API key for {service['label']}: ").strip()
    if not key:
        print("no key entered, aborting.", file=sys.stderr)
        return 2
    creds[service_id] = key
    save_credentials(creds)
    print(f"saved credential to {CREDENTIALS_PATH} (chmod 0600 attempted)")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    service_id = args.service
    if service_id not in SUPPORTED_SERVICES:
        print(f"unknown service: {service_id}", file=sys.stderr)
        return 2
    service = SUPPORTED_SERVICES[service_id]
    print(f"{service['label']}  ({service['base_url']})")
    cfg = load_config()
    active = cfg.get("model") if cfg.get("service_id") == service_id else None
    for m in service["models"]:
        marker = "*" if m == active else " "
        default_tag = "  (default)" if m == service["default_model"] else ""
        print(f"  {marker} {m}{default_tag}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    _print_environment()
    _print_web_search_notice()
    print()
    print("[config]")
    print(f"  config dir   : {CONFIG_DIR}")
    print(f"  config file  : {CONFIG_PATH}  (exists: {CONFIG_PATH.exists()})")
    print(f"  credentials  : {CREDENTIALS_PATH}  (exists: {CREDENTIALS_PATH.exists()})")
    cfg = load_config()
    if cfg:
        print(f"  saved choice : service={cfg.get('service_id')}  model={cfg.get('model')}")
        print(f"  critic       : {cfg.get('critic_model') or '<disabled>'}")
    else:
        print("  saved choice : <none — run `kent` once to set up>")

    print()
    print("[providers]")
    for sid, svc in SUPPORTED_SERVICES.items():
        env_key = svc["api_key_env"]
        env_present = "yes" if os.environ.get(env_key) else "no"
        cred_present = "yes" if sid in load_credentials() else "no"
        print(f"  {sid:<12} env({env_key})={env_present}  saved-credential={cred_present}")

    print()
    print("[deps]")
    for mod in ("openai", "httpx", "selectolax", "markdownify", "tiktoken", "pydantic"):
        try:
            __import__(mod)
            print(f"  {mod:<14} OK")
        except ImportError as e:
            print(f"  {mod:<14} MISSING ({e})")
    return 0


# ---------- argparse ------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="kent — interactive terminal AI agent",
    )
    sub = parser.add_subparsers(dest="command")

    # `kent run "<prompt>"`
    p_run = sub.add_parser("run", help="One-shot: send a single prompt and exit")
    p_run.add_argument("prompt", help="The user prompt")
    p_run.add_argument("--service", default=None, help="Override service id")
    p_run.add_argument("--model", default=None, help="Override model id")
    p_run.add_argument("--quiet", action="store_true", help="Suppress tool-call chatter")
    p_run.set_defaults(func=cmd_run)

    # `kent auth`
    p_auth = sub.add_parser("auth", help="Save or clear an API key for a service")
    p_auth.add_argument(
        "--service",
        default=next(iter(SUPPORTED_SERVICES)),
        help="Service id (default: atlascloud)",
    )
    p_auth.add_argument("--clear", action="store_true", help="Remove saved credential")
    p_auth.set_defaults(func=cmd_auth)

    # `kent models`
    p_models = sub.add_parser("models", help="List models for a service")
    p_models.add_argument(
        "--service",
        default=next(iter(SUPPORTED_SERVICES)),
        help="Service id (default: atlascloud)",
    )
    p_models.set_defaults(func=cmd_models)

    # `kent doctor`
    p_doctor = sub.add_parser("doctor", help="Show environment / config / dependency health")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return cmd_repl(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
