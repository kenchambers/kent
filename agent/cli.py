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
  /memory  /recall <query>    /recall-here <query>    /forget
  /wing    /wing <name>       /wings
  /diary <text>
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
from typing import Any, TYPE_CHECKING

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

if TYPE_CHECKING:
    from .memory.store import MemoryStore

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

_SYSTEM_PROMPT_BASE = (
    "You are kent, a terminal AI agent. You have these tools: "
    "web_search (DuckDuckGo HTML, no API key), web_fetch (URL → markdown), "
    "shell (host shell — bash on macOS/Linux/WSL, PowerShell on Windows), "
    "spawn_subagent (delegate a focused subtask), "
    "memory_recall (search long-term memory from previous sessions), "
    "memory_recall_here (search the active project wing's diary), "
    "diary_write (record observations, findings, decisions, patterns), "
    "set_wing (switch or register a named project context). "
    "Prefer web_search before web_fetch. Prefer shell over re-implementing with another tool. "
    "Use memory_recall when the user asks about things you might have discussed before. "
    "Use memory_recall_here for project-specific context. "
    "Use diary_write to capture important observations, decisions, or patterns. "
    "Keep responses concise."
)

# Kept for backward compatibility with external imports
SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE


def _build_system_prompt(memory_store: "MemoryStore | None" = None) -> str:
    """Build the system prompt, optionally appending current wings list."""
    base = _SYSTEM_PROMPT_BASE
    if memory_store is None:
        return base
    try:
        from .memory.mempalace_store import MemPalaceStore
        from .memory.wings import list_wings, read_intent

        if not isinstance(memory_store, MemPalaceStore):
            return base

        home = memory_store.kent_home
        all_wings = list_wings(home=home)
        if not all_wings:
            return base

        # Sort by mtime (most recently modified first), cap at 20
        def _wing_mtime(w: str) -> float:
            try:
                return (home / "diaries" / w).stat().st_mtime
            except OSError:
                return 0.0

        sorted_wings = sorted(all_wings, key=_wing_mtime, reverse=True)
        cap = 20
        overflow = len(sorted_wings) - cap
        displayed = sorted_wings[:cap]

        active = memory_store.active_wing
        wing_lines: list[str] = []
        for w in displayed:
            intent = read_intent(w, home=home)
            intent_part = f" — {intent}" if intent else ""
            marker = " (active)" if w == active else ""
            wing_lines.append(f"  {w}{marker}{intent_part}")

        overflow_note = f"\n  (and {overflow} more — use /wings)" if overflow > 0 else ""
        return (
            base
            + "\n\nActive project wings:\n"
            + "\n".join(wing_lines)
            + overflow_note
            + "\nWhen the user states a project intent that doesn't match an existing wing, "
            "propose set_wing(name='...') after confirming the name with the user, "
            "then call again with intent='...' to register."
        )
    except Exception:
        return base


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


def _resolve_wing(args: argparse.Namespace) -> str | None:
    """Return explicitly-requested wing name, or None (store constructor handles default)."""
    wing = getattr(args, "wing", None)
    if wing:
        return wing
    return os.environ.get("KENT_WING") or None


async def _run_once(
    registry: ToolRegistry,
    llm: OpenAICompatibleLLM,
    history: list[dict],
    user_input: str,
    *,
    quiet_tools: bool = False,
    memory_store: "MemoryStore | None" = None,
    system_prompt: str = _SYSTEM_PROMPT_BASE,
) -> tuple[list[dict], str]:
    """Single agent turn. Returns (new_history, terminal_reason)."""
    history = [*history, {"role": "user", "content": user_input}]
    new_messages: list[dict] = []
    terminal_reason = "completed"
    async for ev in run(
        messages=history,
        tools=registry,
        llm=llm,
        system=system_prompt,
        max_turns=15,
        memory_store=memory_store,
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
    memory_store: "MemoryStore | None" = None,
    system_prompt: str = _SYSTEM_PROMPT_BASE,
) -> tuple[list[dict], str]:
    """
    Run one turn; if a critic is provided and the turn completed cleanly,
    run a single critique pass and revise once if issues are flagged.
    """
    history, reason = await _run_once(
        registry, llm, history, user_input,
        quiet_tools=quiet_tools, memory_store=memory_store,
        system_prompt=system_prompt,
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
        registry, llm, history, injected,
        quiet_tools=quiet_tools, memory_store=memory_store,
        system_prompt=system_prompt,
    )


# ---------- slash commands ------------------------------------------------- #

SLASH_HELP = """\
slash commands:
  /help                  show this list
  /tools                 list registered tools
  /model                 show current service / model
  /clear                 clear conversation history
  /memory                show memory store info
  /recall <query>        search long-term memory (global)
  /recall-here <query>   search active wing's diary
  /forget                delete current session transcript (with confirmation)
  /wing                  show active wing
  /wing <name>           switch to a wing (must exist)
  /wings                 list all wings with intents
  /diary <text>          record an OBSERVATION in the active wing's diary
  /exit, /quit           leave the session
"""


def _handle_slash(
    cmd: str,
    *,
    history: list[dict],
    registry: ToolRegistry,
    choice: StartupChoice,
    memory_store: "MemoryStore | None" = None,
) -> tuple[list[dict], bool]:
    """Returns (new_history, should_exit)."""
    cmd_stripped = cmd.strip()
    head = cmd_stripped.lower()

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
    elif head == "/memory":
        if memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                if isinstance(memory_store, MemPalaceStore):
                    palace = memory_store.palace_path
                    transcript = memory_store.transcript_path
                    print(f"  palace     : {palace}  (exists: {palace.exists()})")
                    print(f"  transcript : {transcript}  (exists: {transcript.exists()})")
                    print(f"  session id : {memory_store.session_id}")
                    print(f"  wing       : {memory_store.active_wing}")
                else:
                    print(f"  session id : {memory_store.session_id}")
            except Exception as e:
                print(f"  error: {e}")
    elif head.startswith("/recall-here"):
        query = cmd_stripped[len("/recall-here"):].strip()
        if not query:
            print("usage: /recall-here <query>")
        elif memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                if isinstance(memory_store, MemPalaceStore):
                    result = memory_store.recall_in_wing(query)
                    print(result or f"(no memories found in wing '{memory_store.active_wing}')")
                else:
                    print("wing-scoped recall not supported for this store type")
            except Exception as e:
                print(f"  error: {e}")
    elif head.startswith("/recall"):
        query = cmd_stripped[len("/recall"):].strip()
        if not query:
            print("usage: /recall <query>")
        elif memory_store is None:
            print("no memory store configured")
        else:
            result = memory_store.recall(query)
            print(result or "(no memories found)")
    elif head == "/forget":
        if memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                if isinstance(memory_store, MemPalaceStore):
                    transcript = memory_store.transcript_path
                    try:
                        confirm = input(
                            "Delete current session transcript? (y/N): "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        confirm = ""
                    if confirm == "y":
                        if transcript.exists():
                            transcript.unlink()
                            print("[session transcript deleted]")
                            print(
                                "Note: long-term palace entries persist. "
                                "Use mempalace tools to clear them."
                            )
                        else:
                            print("[no transcript file to delete]")
                    else:
                        print("[cancelled]")
                else:
                    print("forget not supported for this store type")
            except Exception as e:
                print(f"  error: {e}")
    elif head == "/wing":
        if memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                from .memory.wings import read_intent
                if isinstance(memory_store, MemPalaceStore):
                    w = memory_store.active_wing
                    intent = read_intent(w, home=memory_store.kent_home)
                    intent_part = f" — {intent}" if intent else ""
                    print(f"  active wing: {w}{intent_part}")
                else:
                    print("wings not supported for this store type")
            except Exception as e:
                print(f"  error: {e}")
    elif head.startswith("/wing "):
        name = cmd_stripped[len("/wing "):].strip()
        if not name:
            print("usage: /wing <name>")
        elif memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                from .memory.wings import list_wings, sanitize_wing, read_intent
                if isinstance(memory_store, MemPalaceStore):
                    clean = sanitize_wing(name)
                    existing = list_wings(home=memory_store.kent_home)
                    if clean not in existing:
                        print(
                            f"  wing {clean!r} doesn't exist. "
                            "Use set_wing tool or /wings to see available wings."
                        )
                    else:
                        memory_store.set_active_wing(clean)
                        intent = read_intent(clean, home=memory_store.kent_home)
                        intent_part = f" — {intent}" if intent else ""
                        print(f"  switched to wing {clean!r}{intent_part}")
                else:
                    print("wings not supported for this store type")
            except ValueError as e:
                print(f"  error: {e}")
            except Exception as e:
                print(f"  error: {e}")
    elif head == "/wings":
        try:
            from .memory.mempalace_store import MemPalaceStore
            from .memory.wings import list_wings, read_intent
            if memory_store is not None and isinstance(memory_store, MemPalaceStore):
                wings = list_wings(home=memory_store.kent_home)
                active = memory_store.active_wing
                if not wings:
                    print("  no wings yet. Use set_wing tool to create one.")
                else:
                    for w in wings:
                        intent = read_intent(w, home=memory_store.kent_home)
                        marker = " *" if w == active else "  "
                        intent_part = f" — {intent}" if intent else ""
                        print(f"{marker} {w}{intent_part}")
            else:
                print("wings not supported for this store type")
        except Exception as e:
            print(f"  error: {e}")
    elif head.startswith("/diary"):
        text = cmd_stripped[len("/diary"):].strip()
        if not text:
            print("usage: /diary <text>")
        elif memory_store is None:
            print("no memory store configured")
        else:
            try:
                from .memory.mempalace_store import MemPalaceStore
                if isinstance(memory_store, MemPalaceStore):
                    memory_store.write_diary("OBSERVATION", text)
                    print(f"  [diary] OBSERVATION recorded in wing '{memory_store.active_wing}'")
                else:
                    print("diary not supported for this store type")
            except Exception as e:
                print(f"  error: {e}")
    else:
        print(f"unknown slash command: {cmd!r}. /help for the list.")
    return history, False


# ---------- subcommands ---------------------------------------------------- #

async def _repl(choice: StartupChoice, *, wing_override: str | None = None) -> None:
    from .memory import MemPalaceStore
    from .builtin.memory_recall import MemoryRecall
    from .builtin.memory_recall_here import MemoryRecallHere
    from .builtin.diary_write import DiaryWrite
    from .builtin.set_wing import SetWing

    llm = _make_llm(choice)
    critic_llm = _make_critic_llm(choice)
    memory_store = MemPalaceStore()

    if wing_override:
        try:
            memory_store.set_active_wing(wing_override)
        except ValueError as e:
            print(f"[warning] invalid --wing value: {e}")

    registry = build_registry()
    registry.register(Spawn(parent_registry=registry, llm=llm, memory_store=memory_store))
    registry.register(MemoryRecall(memory_store))
    registry.register(MemoryRecallHere(memory_store))
    registry.register(DiaryWrite(memory_store))
    registry.register(SetWing(memory_store))

    system_prompt = _build_system_prompt(memory_store)

    # Use wake_up_full at session start: global + wing-scoped diary
    history: list[dict] = []
    recalled = memory_store.wake_up_full()
    if recalled:
        history = [{"role": "system", "content": f"<recalled-memory>{recalled}</recalled-memory>"}]

    print()
    print("[ready]")
    print(f"  Service: {choice.service_label}  ({choice.base_url})")
    print(f"  Model  : {choice.model}")
    print(f"  Critic : {choice.critic_model or '<disabled>'}")
    print(f"  Tools  : {', '.join(registry.names())}")
    print(f"  Memory : {memory_store.palace_path}")
    print(f"  Wing   : {memory_store.active_wing}")
    print("  Type your message. /help for slash commands. /exit to quit.")
    print("-" * 60)

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
                user_input,
                history=history,
                registry=registry,
                choice=choice,
                memory_store=memory_store,
            )
            if should_exit:
                return
            continue
        try:
            history, _ = await _stream_one_turn(
                registry, llm, history, user_input,
                critic_llm=critic_llm, memory_store=memory_store,
                system_prompt=system_prompt,
            )
        except KeyboardInterrupt:
            print("\n[interrupted]")
            continue


def cmd_repl(args: argparse.Namespace) -> int:
    _print_banner()
    _print_environment()
    _print_web_search_notice()
    choice = gather_startup_choice()
    wing_override = _resolve_wing(args)
    try:
        asyncio.run(_repl(choice, wing_override=wing_override))
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

    from .memory import MemPalaceStore
    from .builtin.memory_recall import MemoryRecall
    from .builtin.memory_recall_here import MemoryRecallHere
    from .builtin.diary_write import DiaryWrite
    from .builtin.set_wing import SetWing

    memory_store = MemPalaceStore()

    wing_override = _resolve_wing(args)
    if wing_override:
        try:
            memory_store.set_active_wing(wing_override)
        except ValueError as e:
            print(f"[warning] invalid --wing value: {e}", file=sys.stderr)

    registry = build_registry()
    registry.register(Spawn(parent_registry=registry, llm=llm, memory_store=memory_store))
    registry.register(MemoryRecall(memory_store))
    registry.register(MemoryRecallHere(memory_store))
    registry.register(DiaryWrite(memory_store))
    registry.register(SetWing(memory_store))

    system_prompt = _build_system_prompt(memory_store)

    history: list[dict] = []
    recalled = memory_store.wake_up_full()
    if recalled:
        history = [{"role": "system", "content": f"<recalled-memory>{recalled}</recalled-memory>"}]

    async def _go() -> str:
        _, reason = await _stream_one_turn(
            registry, llm, history, args.prompt,
            critic_llm=critic_llm, quiet_tools=args.quiet,
            memory_store=memory_store, system_prompt=system_prompt,
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


def cmd_train(args: argparse.Namespace) -> int:
    """Run APO training for one resource across configured swap pairs."""
    import asyncio
    from .training import TrainingConfig
    from .training.apo_runner import train_resource, RESOURCE_BASELINES
    from .training.swap_pair import sweep_pairs
    from .training.datasets import load_examples_dir, TrainingExample
    from .training.rollout import set_active_config
    from .training.eval_harness import run_collusion_probes

    apo_base_url = getattr(args, "apo_base_url", None)
    gradient_model = getattr(args, "gradient_model", None) or "gpt-5-mini"
    apply_edit_model = getattr(args, "apply_edit_model", None) or "gpt-4.1-mini"

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if apo_base_url:
        # Routing APO at a non-OpenAI endpoint — reuse the actor service's key.
        cfg_for_apo = load_config()
        svc_id_for_apo = cfg_for_apo.get("service_id") or next(iter(SUPPORTED_SERVICES))
        openai_key = resolve_api_key(svc_id_for_apo, prompt_if_missing=False) or openai_key
        if not openai_key:
            print(
                f"--apo-base-url set but no api key found in env or saved credentials for {svc_id_for_apo!r}",
                file=sys.stderr,
            )
            return 2
    elif not openai_key:
        print("OPENAI_API_KEY not set — APO requires OpenAI for prompt rewriting (or pass --apo-base-url to use an OpenAI-compatible endpoint)", file=sys.stderr)
        return 2

    resource = args.resource
    if resource not in RESOURCE_BASELINES:
        print(f"unknown resource: {resource!r}", file=sys.stderr)
        print(f"valid: {', '.join(RESOURCE_BASELINES)}", file=sys.stderr)
        return 2

    if getattr(args, "sweep", False):
        pairs = list(sweep_pairs())
        if not pairs:
            print("no swap pairs defined in ~/.kent/swap_pairs.toml", file=sys.stderr)
            return 2
    else:
        pair_str = getattr(args, "pair", None)
        if not pair_str or "+" not in pair_str:
            print("--pair required: format actor_model+critic_model", file=sys.stderr)
            return 2
        actor_model, critic_model = pair_str.split("+", 1)
        cfg_base = load_config()
        svc_id = cfg_base.get("service_id") or next(iter(SUPPORTED_SERVICES))
        svc = SUPPORTED_SERVICES.get(svc_id, next(iter(SUPPORTED_SERVICES.values())))
        api_key = resolve_api_key(svc_id, prompt_if_missing=False) or ""
        from .training.swap_pair import SwapPair
        pairs = [SwapPair(
            actor_base_url=svc["base_url"], actor_api_key=api_key,
            actor_model=actor_model, actor_family=svc_id,
            critic_base_url=svc["base_url"], critic_api_key=api_key,
            critic_model=critic_model, critic_family=f"{svc_id}-critic",
        )]

    train_size = getattr(args, "train_size", 10)
    n_rounds = getattr(args, "rounds", 1)
    n_runners = getattr(args, "runners", 2)
    examples_dir = getattr(args, "examples_dir", None)
    skip_collusion = getattr(args, "skip_collusion_check", False)

    if examples_dir:
        dir_path = Path(examples_dir).expanduser()
        if not dir_path.exists():
            print(f"examples dir not found: {dir_path}", file=sys.stderr)
            return 2
        examples = load_examples_dir(dir_path)
        if not examples:
            print(f"no .jsonl examples found in {dir_path}", file=sys.stderr)
            return 2
        if len(examples) > train_size:
            examples = examples[:train_size]
    else:
        # Plan v1 ships without bundled real-task data; fall back to synthetic prompts
        # but make the limitation visible so it's not silently used in production.
        print(
            "[warning] --examples-dir not provided; using synthetic prompts. "
            "Train on real examples for meaningful APO signal.",
            file=sys.stderr,
        )
        examples = [
            TrainingExample(task_id=f"ex{i}", prompt=f"Task {i}: what do you remember?")
            for i in range(train_size)
        ]

    val_split = max(1, len(examples) // 5)
    train_examples = [{"task_id": ex.task_id, "prompt": ex.prompt} for ex in examples]
    val_examples = train_examples[:val_split]

    for pair in pairs:
        config = TrainingConfig(
            actor_base_url=pair.actor_base_url,
            actor_api_key=pair.actor_api_key,
            actor_model=pair.actor_model,
            actor_family=pair.actor_family,
            critic_base_url=pair.critic_base_url,
            critic_api_key=pair.critic_api_key,
            critic_model=pair.critic_model,
            critic_family=pair.critic_family,
            n_runners=n_runners,
            max_rounds=n_rounds,
            train_size=train_size,
            current_resource=resource,
        )

        if not skip_collusion:
            critic_llm_for_probe = OpenAICompatibleLLM(
                base_url=config.critic_base_url,
                api_key=config.critic_api_key,
                model=config.critic_model,
            )
            probe = asyncio.run(run_collusion_probes(critic_llm_for_probe))
            if probe.get("colluding"):
                print(
                    f"[abort] critic_pass_rate={probe['critic_pass_rate']:.2f} > 1/5 "
                    f"— pair {pair.actor_model}×{pair.critic_model} appears to collude. "
                    "Use --skip-collusion-check to override.",
                    file=sys.stderr,
                )
                continue

        set_active_config(config)
        try:
            metrics = train_resource(
                resource_name=resource,
                train_dataset=train_examples,
                val_dataset=val_examples,
                store_path=config.lightning_store,
                n_rounds=n_rounds,
                n_runners=n_runners,
                openai_api_key=openai_key,
                apo_base_url=apo_base_url,
                gradient_model=gradient_model,
                apply_edit_model=apply_edit_model,
            )
        finally:
            set_active_config(None)

        print(
            f"[{pair.actor_model}×{pair.critic_model}] "
            f"val_reward={metrics['val_reward']:.3f}  "
            f"saved to {metrics['output_path']}"
        )

    return 0


def cmd_wake_up(args: argparse.Namespace) -> int:
    """Run recall self-improvement games against the live palace."""
    import asyncio
    import time
    import json
    from .memory.mempalace_store import MemPalaceStore, _DEFAULT_KENT_HOME, _DEFAULT_PALACE

    cfg = load_config()
    svc_id = cfg.get("service_id") or next(iter(SUPPORTED_SERVICES))
    svc = SUPPORTED_SERVICES.get(svc_id, next(iter(SUPPORTED_SERVICES.values())))
    model = cfg.get("model") or svc["default_model"]
    api_key = resolve_api_key(svc_id, prompt_if_missing=False) or ""

    from .llm import OpenAICompatibleLLM
    actor_llm = OpenAICompatibleLLM(base_url=svc["base_url"], api_key=api_key, model=model)
    critic_llm = actor_llm

    palace_path = _DEFAULT_PALACE
    kent_home = _DEFAULT_KENT_HOME
    metrics_dir = kent_home / "lightning_store" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    try:
        duration_s = _parse_duration(getattr(args, "duration", "5m"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    from .training.recall_games import run_recall_game_a
    from .training.scope_eval import run_scope_eval
    from .training.closet_fidelity import run_closet_fidelity

    async def _run_games() -> None:
        deadline = time.monotonic() + duration_s
        round_n = 0
        while time.monotonic() < deadline:
            round_n += 1
            print(f"[wake-up] round {round_n}")
            results: dict = {}

            a = await run_recall_game_a(palace_path, actor_llm)
            results.update(a)

            b = await run_scope_eval(palace_path, kent_home, critic_llm)
            results.update(b)

            c = await run_closet_fidelity(palace_path, actor_llm)
            results.update(c)

            entry = {"ts": time.time(), "round": round_n, **results}
            with open(metrics_dir / "wake_up.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")

            print(f"  recall_a1={results.get('recall_a1', 0):.3f}  "
                  f"recall_a2={results.get('recall_a2', 0):.3f}  "
                  f"scope={results.get('scope_accuracy', 0):.3f}  "
                  f"closet={results.get('closet_fidelity', 0):.3f}")

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(1)

    asyncio.run(_run_games())
    return 0


def _parse_duration(s: str) -> float:
    """Parse duration strings like '5m', '2h', '30s' into seconds."""
    s = s.strip()
    try:
        if s.endswith("h"):
            return float(s[:-1]) * 3600
        if s.endswith("m"):
            return float(s[:-1]) * 60
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except ValueError as e:
        raise ValueError(
            f"invalid duration {s!r} — expected forms like '30s', '5m', '2h'"
        ) from e


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
    print("[memory]")
    try:
        from datetime import datetime
        from .memory.mempalace_store import _TRANSCRIPT_BASE, _DEFAULT_PALACE
        from mempalace.layers import MemoryStack
        palace = _DEFAULT_PALACE
        print(f"  palace     : {palace}  (exists: {palace.exists()})")
        print(f"  transcripts: {_TRANSCRIPT_BASE}  (exists: {_TRANSCRIPT_BASE.exists()})")

        if palace.exists():
            try:
                status = MemoryStack(str(palace)).status()
                print(f"  drawers    : {status.get('total_drawers', '?')}")
            except Exception as e:
                print(f"  drawers    : <error: {e}>")
            try:
                mtimes = [p.stat().st_mtime for p in palace.rglob("*") if p.is_file()]
                if mtimes:
                    last = datetime.fromtimestamp(max(mtimes)).isoformat(timespec="seconds")
                    print(f"  last-write : {last}")
                else:
                    print("  last-write : <empty palace>")
            except Exception as e:
                print(f"  last-write : <error: {e}>")
        else:
            print("  drawers    : 0  (palace not yet created)")
            print("  last-write : <never>")
    except ImportError:
        print("  mempalace  : NOT INSTALLED  (pip install 'mempalace>=3.3')")
    except Exception as e:
        print(f"  error      : {e}")

    print()
    print("[wings]")
    try:
        from .memory.wings import list_wings, read_active_wing, read_intent
        from .memory.mempalace_store import _DEFAULT_KENT_HOME

        home = _DEFAULT_KENT_HOME
        active_wing = read_active_wing(home=home)
        all_wings = list_wings(home=home)
        cap = 50
        displayed = all_wings[:cap]
        overflow = len(all_wings) - cap

        if not all_wings:
            print("  no wings yet")
        else:
            for w in displayed:
                intent = read_intent(w, home=home)
                marker = " *" if w == active_wing else "  "
                intent_part = f" — {intent}" if intent else ""
                diary_dir = home / "diaries" / w
                try:
                    file_count = len(list(diary_dir.glob("*.md")))
                except OSError:
                    file_count = 0
                print(f"{marker} {w}{intent_part}  [{file_count} diary file(s)]")
            if overflow > 0:
                print(f"  ({overflow} more...)")
    except Exception as e:
        print(f"  error: {e}")

    print()
    print("[diary]")
    try:
        from datetime import datetime
        from .memory.wings import list_wings, read_active_wing
        from .memory.mempalace_store import _DEFAULT_KENT_HOME

        home = _DEFAULT_KENT_HOME
        all_wings = list_wings(home=home)

        if not all_wings:
            print("  no diary entries yet")
        else:
            for w in all_wings:
                diary_dir = home / "diaries" / w
                try:
                    md_files = sorted(diary_dir.glob("*.md"))
                    total_entries = 0
                    last_mtime = 0.0
                    for f in md_files:
                        total_entries += f.read_text(encoding="utf-8", errors="ignore").count("\n## ")
                        mtime = f.stat().st_mtime
                        if mtime > last_mtime:
                            last_mtime = mtime
                    last_str = (
                        datetime.fromtimestamp(last_mtime).isoformat(timespec="seconds")
                        if last_mtime > 0
                        else "<never>"
                    )
                    print(f"  {w:<20} {total_entries:>4} entries  last-write: {last_str}")
                except Exception as e:
                    print(f"  {w:<20} <error: {e}>")
    except Exception as e:
        print(f"  error: {e}")

    print()
    print("[deps]")
    for mod in (
        "openai", "httpx", "selectolax", "markdownify",
        "tiktoken", "pydantic", "mempalace",
    ):
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
    parser.add_argument(
        "--wing",
        default=None,
        metavar="NAME",
        help="Set the active project wing for this session",
    )
    sub = parser.add_subparsers(dest="command")

    # `kent run "<prompt>"`
    p_run = sub.add_parser("run", help="One-shot: send a single prompt and exit")
    p_run.add_argument("prompt", help="The user prompt")
    p_run.add_argument("--service", default=None, help="Override service id")
    p_run.add_argument("--model", default=None, help="Override model id")
    p_run.add_argument("--quiet", action="store_true", help="Suppress tool-call chatter")
    p_run.add_argument(
        "--wing",
        default=None,
        metavar="NAME",
        help="Set the active project wing for this run",
    )
    p_run.set_defaults(func=cmd_run)

    # `kent train`
    p_train = sub.add_parser("train", help="APO training: optimize a prompt resource")
    p_train.add_argument(
        "--resource",
        required=True,
        help="Resource to optimize (actor_system_prompt, retrieval_policy, ...)",
    )
    p_train.add_argument(
        "--pair",
        default=None,
        metavar="ACTOR+CRITIC",
        help="Model pair: actor_model+critic_model (e.g. gpt-4o-mini+gpt-4o)",
    )
    p_train.add_argument("--sweep", action="store_true", help="Run all pairs from swap_pairs.toml")
    p_train.add_argument("--rounds", type=int, default=1, help="APO rounds (default: 1)")
    p_train.add_argument("--runners", type=int, default=2, help="Parallel runners (default: 2)")
    p_train.add_argument("--train-size", type=int, default=10, dest="train_size", help="Training examples (default: 10)")
    p_train.add_argument(
        "--examples-dir",
        default=None,
        dest="examples_dir",
        metavar="DIR",
        help="Directory of *.jsonl files with real training examples (each line: {task_id, prompt, ...})",
    )
    p_train.add_argument(
        "--skip-collusion-check",
        action="store_true",
        dest="skip_collusion_check",
        help="Skip the mandatory collusion probe (NOT recommended; documented as mandatory in plan)",
    )
    p_train.add_argument(
        "--apo-base-url",
        default=None,
        dest="apo_base_url",
        metavar="URL",
        help="Override OpenAI endpoint for APO (e.g. https://api.atlascloud.ai/v1 to run APO against an OpenAI-compatible alternative)",
    )
    p_train.add_argument(
        "--gradient-model",
        default=None,
        dest="gradient_model",
        metavar="MODEL",
        help="Model APO uses for textual gradients (default: gpt-5-mini)",
    )
    p_train.add_argument(
        "--apply-edit-model",
        default=None,
        dest="apply_edit_model",
        metavar="MODEL",
        help="Model APO uses for applying edits (default: gpt-4.1-mini)",
    )
    p_train.set_defaults(func=cmd_train)

    # `kent wake-up`
    p_wake = sub.add_parser("wake-up", help="Run memory recall self-improvement games")
    p_wake.add_argument(
        "--duration",
        default="5m",
        metavar="DURATION",
        help="How long to run games (e.g. 5m, 2h, 30s; default: 5m)",
    )
    p_wake.set_defaults(func=cmd_wake_up)

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
