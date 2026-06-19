#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PI_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
CODEX_REASONING_LEVELS = {"none", "low", "medium", "high", "xhigh"}
OPENCLAW_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
OPENAI_COMPAT_API_KEY_ENV = "MB12_OPENAI_COMPAT_API_KEY"


def _python() -> str:
    return sys.executable


def _mb12_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT) if not existing else f"{PROJECT_ROOT}:{existing}"
    )
    return env


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sanitize_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def _split_model_selector(value: str) -> tuple[str | None, str]:
    if "/" not in value:
        return None, value
    provider, model_id = value.split("/", 1)
    provider = provider.strip() or None
    model_id = model_id.strip()
    return provider, model_id


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _task_ids(selection: list[str]) -> list[str]:
    if selection == ["all"]:
        result = _run(
            [_python(), "-m", "microbench12", "list"],
            cwd=PROJECT_ROOT,
            env=_mb12_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return [
            line.split("\t", 1)[0]
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    return selection


def _session_title(task_id: str, model: str) -> str:
    return f"mb12::{task_id}::{model}"


def _openclaw_session_id(task_id: str, model: str) -> str:
    return _sanitize_component(f"mb12__{task_id}__{model}")


def _build_message() -> str:
    return (
        "Solve the benchmark task in the current directory. "
        "Read TASK_PROMPT.md and README.md first. "
        "Edit only the files needed to satisfy the task. "
        "Do not use the network. "
        "When you are done, stop without asking follow-up questions."
    )


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _counts(task_records: list[dict[str, Any]]) -> dict[str, int]:
    counters = {
        "passed": 0,
        "failed": 0,
        "infra_error": 0,
        "runner_error": 0,
        "pending": 0,
    }
    for record in task_records:
        status = record.get("status", "pending")
        if status not in counters:
            counters[status] = 0
        counters[status] += 1
    return counters


def _completed_ok(task_records: list[dict[str, Any]]) -> bool:
    return all(
        record.get("status") in {"passed", "failed", "infra_error", "runner_error"}
        for record in task_records
    )


def _run_state_payload(
    *,
    run_id: str,
    harness: str,
    model: str,
    variant: str,
    backend: str,
    reasoning_mode: str | None,
    task_ids: list[str],
    run_dir: Path,
    workspace_run_dir: Path,
    task_records: list[dict[str, Any]],
    started_at: str,
    finished_at: str | None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "harness": harness,
        "model": model,
        "variant": variant,
        "backend": backend,
        "reasoning_mode": reasoning_mode,
        "task_ids": task_ids,
        "run_dir": str(run_dir),
        "workspace_run_dir": str(workspace_run_dir),
        "counts": _counts(task_records),
        "completed_ok": _completed_ok(task_records),
        "tasks": task_records,
    }


def _record_paths(task_dir: Path, harness: str) -> dict[str, str]:
    paths = {
        "task_dir": str(task_dir),
        "harness_log": str(task_dir / f"{harness}.log"),
        "telemetry": str(task_dir / "telemetry.json"),
        "grade_result": str(task_dir / "grade_result.json"),
        "grade_error": str(task_dir / "grade_error.txt"),
        "task_record": str(task_dir / "task_record.json"),
    }
    if harness == "opencode":
        paths["opencode_log"] = paths["harness_log"]
    if harness == "pi":
        paths["pi_log"] = paths["harness_log"]
        paths["pi_session_dir"] = str(task_dir / "pi_session")
    if harness == "hermes":
        paths["hermes_log"] = paths["harness_log"]
        paths["hermes_home"] = str(task_dir / "hermes_home")
    if harness == "codex":
        paths["codex_log"] = paths["harness_log"]
        paths["codex_home"] = str(task_dir / "codex_home")
        paths["codex_last_message"] = str(task_dir / "codex_last_message.txt")
    if harness == "openclaw":
        paths["openclaw_log"] = paths["harness_log"]
        paths["openclaw_state_dir"] = str(task_dir / "openclaw_state")
        paths["openclaw_config"] = str(task_dir / "openclaw_state" / "openclaw.json")
    return paths


def _opencode_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())

    if result.returncode != 0:
        return True, combined or f"OpenCode exited with code {result.returncode}"

    fatal_needles = [
        'Error: "auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set',
        "Error: Model not found:",
    ]
    for needle in fatal_needles:
        if needle in combined:
            return True, combined

    return False, combined


def _pi_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"pi exited with code {result.returncode}"
    return False, combined


def _hermes_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"hermes exited with code {result.returncode}"
    return False, combined


def _codex_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"codex exited with code {result.returncode}"

    fatal_needles = [
        "Unexpected message role.",
        "failed to connect to websocket",
        "There was an error parsing the body",
    ]
    for needle in fatal_needles:
        if needle in combined:
            return True, combined
    return False, combined


def _openclaw_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"openclaw exited with code {result.returncode}"

    fatal_needles = [
        "Node.js v22.12+ is required",
        "Permission prompt unavailable in non-interactive mode",
        "No credentials found",
        "Model not allowed",
    ]
    for needle in fatal_needles:
        if needle in combined:
            return True, combined
    return False, combined


def _failure_checker(
    harness: str,
) -> Callable[[subprocess.CompletedProcess], tuple[bool, str]]:
    if harness == "pi":
        return _pi_failed
    if harness == "hermes":
        return _hermes_failed
    if harness == "codex":
        return _codex_failed
    if harness == "openclaw":
        return _openclaw_failed
    return _opencode_failed


def _default_harness_bin(harness: str) -> str:
    if harness == "pi":
        return "pi"
    if harness == "hermes":
        return "hermes"
    if harness == "codex":
        return "codex"
    if harness == "openclaw":
        return "openclaw"
    return "opencode"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _pi_provider_base_url(provider_alias: str | None, model_id: str) -> str | None:
    payload = _load_json(Path.home() / ".pi" / "agent" / "models.json")
    if not payload:
        return None
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return None
    if provider_alias:
        entry = providers.get(provider_alias)
        if isinstance(entry, dict):
            base_url = entry.get("baseUrl")
            if isinstance(base_url, str) and base_url.strip():
                return base_url.strip()
    matches: list[str] = []
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        base_url = entry.get("baseUrl")
        if not isinstance(models, list) or not isinstance(base_url, str):
            continue
        for model in models:
            if isinstance(model, dict) and model.get("id") == model_id:
                matches.append(base_url.strip())
                break
    if len(matches) == 1:
        return matches[0]
    return None


def _opencode_provider_base_url(provider_alias: str | None, model_id: str) -> str | None:
    payload = _load_json(Path.home() / ".config" / "opencode" / "opencode.json")
    if not payload:
        return None
    providers = payload.get("provider")
    if not isinstance(providers, dict):
        return None
    if provider_alias:
        entry = providers.get(provider_alias)
        if isinstance(entry, dict):
            options = entry.get("options")
            if isinstance(options, dict):
                base_url = options.get("baseURL")
                if isinstance(base_url, str) and base_url.strip():
                    return base_url.strip()
    matches: list[str] = []
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        options = entry.get("options")
        models = entry.get("models")
        if not isinstance(options, dict) or not isinstance(models, dict):
            continue
        base_url = options.get("baseURL")
        if not isinstance(base_url, str) or not base_url.strip():
            continue
        if model_id in models:
            matches.append(base_url.strip())
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_openai_compat_base_url(
    explicit_base_url: str | None,
    provider_alias: str | None,
    model_id: str,
) -> str:
    if explicit_base_url:
        return explicit_base_url
    env_base_url = (
        os.environ.get("MB12_OPENAI_COMPAT_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    if env_base_url:
        return env_base_url.strip()
    for resolver in (_pi_provider_base_url, _opencode_provider_base_url):
        resolved = resolver(provider_alias, model_id)
        if resolved:
            return resolved
    raise RuntimeError(
        "Unable to resolve an OpenAI-compatible base URL for this harness. "
        "Pass --openai-compatible-base-url explicitly or use a provider alias "
        "already configured in PI/OpenCode."
    )


def _resolve_openai_compat_api_key(explicit_api_key: str | None) -> str:
    if explicit_api_key:
        return explicit_api_key
    env_api_key = (
        os.environ.get(OPENAI_COMPAT_API_KEY_ENV)
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("MB12_OPENAI_COMPAT_API_KEY")
    )
    if env_api_key:
        return env_api_key
    return "vllm"


def _yaml_string(value: str) -> str:
    return json.dumps(value)


def _write_hermes_home(
    *,
    home_dir: Path,
    provider_alias: str,
    model_id: str,
    base_url: str,
    reasoning_mode: str | None,
) -> None:
    reasoning_block = ""
    if reasoning_mode:
        reasoning_block = (
            "\nagent:\n"
            f"  reasoning_effort: {_yaml_string(reasoning_mode)}\n"
        )
    payload = (
        "model:\n"
        f"  default: {_yaml_string(model_id)}\n"
        f"  provider: {_yaml_string(provider_alias)}\n"
        f"  base_url: {_yaml_string(base_url)}\n"
        "providers:\n"
        f"  {_yaml_string(provider_alias)}:\n"
        f"    name: {_yaml_string('Local vLLM')}\n"
        f"    api: {_yaml_string(base_url)}\n"
        f"    default_model: {_yaml_string(model_id)}\n"
        f"    transport: {_yaml_string('chat_completions')}\n"
        f"{reasoning_block}"
    )
    _write_text(home_dir / "config.yaml", payload)


def _write_codex_home(
    *,
    home_dir: Path,
    provider_alias: str,
    model_id: str,
    base_url: str,
    reasoning_mode: str | None,
) -> None:
    lines = [
        f'model = "{model_id}"',
        f'model_provider = "{provider_alias}"',
    ]
    if reasoning_mode in CODEX_REASONING_LEVELS:
        lines.append(f'model_reasoning_effort = "{reasoning_mode}"')
    lines.extend(
        [
            "",
            f"[model_providers.{provider_alias}]",
            'name = "Local vLLM"',
            f'base_url = "{base_url}"',
            'wire_api = "responses"',
            f'env_key = "{OPENAI_COMPAT_API_KEY_ENV}"',
            "supports_websockets = false",
            "",
        ]
    )
    _write_text(home_dir / "config.toml", "\n".join(lines))


def _write_openclaw_state(
    *,
    state_dir: Path,
    workspace: Path,
    provider_alias: str,
    model_id: str,
    base_url: str,
    reasoning_mode: str | None,
) -> Path:
    config_path = state_dir / "openclaw.json"
    model_ref = f"{provider_alias}/{model_id}"
    model_entry: dict[str, Any] = {
        "id": model_id,
        "name": model_id,
        "reasoning": reasoning_mode not in {None, "", "off", "none"},
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 262144,
        "maxTokens": 16384,
    }
    if reasoning_mode in OPENCLAW_THINKING_LEVELS:
        model_entry["compat"] = {
            "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
            "reasoningEffortMap": {"xhigh": "xhigh"},
        }
    payload: dict[str, Any] = {
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "skipBootstrap": True,
                "model": {"primary": model_ref},
                "models": {model_ref: {"alias": model_id}},
                "toolProgressDetail": "raw",
                "verboseDefault": "off",
                "elevatedDefault": "on",
                "timeoutSeconds": 1800,
            }
        },
        "models": {
            "providers": {
                provider_alias: {
                    "baseUrl": base_url,
                    "apiKey": {
                        "source": "env",
                        "provider": "default",
                        "id": OPENAI_COMPAT_API_KEY_ENV,
                    },
                    # `openai-completions` is more tolerant across local vLLM
                    # model families than the responses path. Gemma4 currently
                    # fails on `/v1/responses` under OpenClaw with a chat
                    # template rendering error, while the other harnesses in
                    # this benchmark already use completions successfully.
                    "api": "openai-completions",
                    "models": [model_entry],
                }
            }
        },
    }
    if reasoning_mode in OPENCLAW_THINKING_LEVELS:
        payload["agents"]["defaults"]["thinkingDefault"] = reasoning_mode
    _write_json(config_path, payload)
    return config_path


def _opencode_command(
    *,
    model: str,
    workspace: Path,
    task_id: str,
    bin_name: str,
    agent: str | None,
    skip_permissions: bool,
) -> tuple[list[str], Path, dict[str, str] | None]:
    cmd = [
        bin_name,
        "run",
        "--pure",
        "--model",
        model,
        "--dir",
        str(workspace),
        "--title",
        _session_title(task_id, model),
    ]
    if agent:
        cmd.extend(["--agent", agent])
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(_build_message())
    return cmd, PROJECT_ROOT, None


def _pi_command(
    *,
    model: str,
    workspace: Path,
    task_dir: Path,
    bin_name: str,
    pi_tools: str,
    reasoning_mode: str | None,
) -> tuple[list[str], Path, dict[str, str] | None]:
    session_dir = task_dir / "pi_session"
    cmd = [
        bin_name,
        "--model",
        model,
        "--session-dir",
        str(session_dir),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--offline",
        "--tools",
        pi_tools,
        "--print",
        _build_message(),
    ]
    if reasoning_mode in PI_THINKING_LEVELS:
        cmd.extend(["--thinking", reasoning_mode])
    env = dict(os.environ)
    env["PI_OFFLINE"] = "1"
    return cmd, workspace, env


def _hermes_command(
    *,
    model: str,
    workspace: Path,
    task_dir: Path,
    bin_name: str,
    reasoning_mode: str | None,
    openai_compat_base_url: str,
    openai_compat_api_key: str,
) -> tuple[list[str], Path, dict[str, str] | None]:
    provider_alias, model_id = _split_model_selector(model)
    provider_alias = provider_alias or "local-vllm"
    hermes_home = task_dir / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    _write_hermes_home(
        home_dir=hermes_home,
        provider_alias=provider_alias,
        model_id=model_id,
        base_url=openai_compat_base_url,
        reasoning_mode=reasoning_mode,
    )
    cmd = [
        bin_name,
        "chat",
        "-q",
        _build_message(),
        "-Q",
        "--yolo",
        "--ignore-rules",
        "--provider",
        provider_alias,
        "-m",
        model_id,
    ]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env["OPENAI_API_KEY"] = openai_compat_api_key
    env["HERMES_INFERENCE_PROVIDER"] = provider_alias
    env["HERMES_INFERENCE_MODEL"] = model_id
    return cmd, workspace, env


def _codex_command(
    *,
    model: str,
    workspace: Path,
    task_dir: Path,
    bin_name: str,
    reasoning_mode: str | None,
    openai_compat_base_url: str,
    openai_compat_api_key: str,
) -> tuple[list[str], Path, dict[str, str] | None]:
    provider_alias, model_id = _split_model_selector(model)
    provider_alias = provider_alias or "local-vllm"
    codex_home = task_dir / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    _write_codex_home(
        home_dir=codex_home,
        provider_alias=provider_alias,
        model_id=model_id,
        base_url=openai_compat_base_url,
        reasoning_mode=reasoning_mode,
    )
    cmd = [
        bin_name,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--sandbox",
        "workspace-write",
        "-C",
        str(workspace),
        "-o",
        str(task_dir / "codex_last_message.txt"),
        _build_message(),
    ]
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env[OPENAI_COMPAT_API_KEY_ENV] = openai_compat_api_key
    return cmd, PROJECT_ROOT, env


def _openclaw_command(
    *,
    model: str,
    workspace: Path,
    task_dir: Path,
    task_id: str,
    bin_name: str,
    reasoning_mode: str | None,
    openai_compat_base_url: str,
    openai_compat_api_key: str,
    task_timeout_sec: float | None,
) -> tuple[list[str], Path, dict[str, str] | None]:
    provider_alias, model_id = _split_model_selector(model)
    provider_alias = provider_alias or "local-vllm"
    state_dir = task_dir / "openclaw_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = _write_openclaw_state(
        state_dir=state_dir,
        workspace=workspace,
        provider_alias=provider_alias,
        model_id=model_id,
        base_url=openai_compat_base_url,
        reasoning_mode=reasoning_mode,
    )
    cmd = [
        bin_name,
        "agent",
        "--local",
        "--json",
        "--session-id",
        _openclaw_session_id(task_id, model),
        "--model",
        f"{provider_alias}/{model_id}",
        "--message",
        _build_message(),
    ]
    if reasoning_mode in OPENCLAW_THINKING_LEVELS:
        cmd.extend(["--thinking", reasoning_mode])
    if task_timeout_sec and task_timeout_sec > 0:
        cmd.extend(["--timeout", str(int(task_timeout_sec))])
    env = dict(os.environ)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env[OPENAI_COMPAT_API_KEY_ENV] = openai_compat_api_key
    env["OPENCLAW_LOG_LEVEL"] = "error"
    return cmd, PROJECT_ROOT, env


def _build_harness_command(
    *,
    harness: str,
    model: str,
    workspace: Path,
    task_dir: Path,
    task_id: str,
    harness_bin: str,
    agent: str | None,
    skip_permissions: bool,
    pi_tools: str,
    reasoning_mode: str | None,
    openai_compat_base_url: str | None,
    openai_compat_api_key: str | None,
    task_timeout_sec: float | None,
) -> tuple[list[str], Path, dict[str, str] | None]:
    if harness == "pi":
        return _pi_command(
            model=model,
            workspace=workspace,
            task_dir=task_dir,
            bin_name=harness_bin,
            pi_tools=pi_tools,
            reasoning_mode=reasoning_mode,
        )
    if harness == "hermes":
        assert openai_compat_base_url is not None
        assert openai_compat_api_key is not None
        return _hermes_command(
            model=model,
            workspace=workspace,
            task_dir=task_dir,
            bin_name=harness_bin,
            reasoning_mode=reasoning_mode,
            openai_compat_base_url=openai_compat_base_url,
            openai_compat_api_key=openai_compat_api_key,
        )
    if harness == "codex":
        assert openai_compat_base_url is not None
        assert openai_compat_api_key is not None
        return _codex_command(
            model=model,
            workspace=workspace,
            task_dir=task_dir,
            bin_name=harness_bin,
            reasoning_mode=reasoning_mode,
            openai_compat_base_url=openai_compat_base_url,
            openai_compat_api_key=openai_compat_api_key,
        )
    if harness == "openclaw":
        assert openai_compat_base_url is not None
        assert openai_compat_api_key is not None
        return _openclaw_command(
            model=model,
            workspace=workspace,
            task_dir=task_dir,
            task_id=task_id,
            bin_name=harness_bin,
            reasoning_mode=reasoning_mode,
            openai_compat_base_url=openai_compat_base_url,
            openai_compat_api_key=openai_compat_api_key,
            task_timeout_sec=task_timeout_sec,
        )
    return _opencode_command(
        model=model,
        workspace=workspace,
        task_id=task_id,
        bin_name=harness_bin,
        agent=agent,
        skip_permissions=skip_permissions,
    )


def _update_exit_metadata(record: dict[str, Any], harness: str, exit_code: int) -> None:
    record["harness_exit_code"] = exit_code
    if harness == "opencode":
        record["opencode_exit_code"] = exit_code
    elif harness == "pi":
        record["pi_exit_code"] = exit_code
    elif harness == "hermes":
        record["hermes_exit_code"] = exit_code
    elif harness == "codex":
        record["codex_exit_code"] = exit_code
    elif harness == "openclaw":
        record["openclaw_exit_code"] = exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MicroBench-12 tasks through a selectable harness."
    )
    parser.add_argument(
        "--harness",
        choices=["opencode", "pi", "hermes", "codex", "openclaw"],
        default="opencode",
        help="Harness to use for solving benchmark tasks.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="provider/model, e.g. myprovider/Qwen3.6-35B or local-vllm/Qwen3.6-35B",
    )
    parser.add_argument(
        "--task",
        action="append",
        required=True,
        help="Task id to run. Repeat the flag for multiple tasks, or pass --task all.",
    )
    parser.add_argument(
        "--workspace-root",
        default=str(PROJECT_ROOT / "materialized" / "runs"),
    )
    parser.add_argument(
        "--results-dir",
        default=str(PROJECT_ROOT / "results" / "runs"),
    )
    parser.add_argument(
        "--variant",
        help="Benchmark variant key. Defaults to <harness>__<provider-model>.",
    )
    parser.add_argument("--backend", default="vllm")
    parser.add_argument(
        "--reasoning-mode",
        help="Optional reasoning mode label to store in result JSON.",
    )
    parser.add_argument("--agent", help="Optional OpenCode agent name.")
    parser.add_argument(
        "--harness-bin",
        help="Override harness executable path. Defaults to the selected harness name.",
    )
    parser.add_argument(
        "--opencode-bin",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--skip-permissions", action="store_true")
    parser.add_argument("--cleanup-passed-workspaces", action="store_true")
    parser.add_argument("--run-name", help="Optional human-readable suffix.")
    parser.add_argument(
        "--task-timeout-sec",
        type=float,
        default=1800.0,
        help="Maximum seconds to let one harness task run. Use 0 to disable.",
    )
    parser.add_argument(
        "--grade-timeout-sec",
        type=float,
        default=300.0,
        help="Maximum seconds to let one hidden grader run. Use 0 to disable.",
    )
    parser.add_argument(
        "--pi-tools",
        default="read,bash,edit,write,grep,find,ls",
        help="Comma-separated tool allowlist for PI runs.",
    )
    parser.add_argument(
        "--openai-compatible-base-url",
        help=(
            "Base URL for local OpenAI-compatible backends used by Hermes/Codex/OpenClaw, "
            "for example http://192.168.90.17:8080/v1. If omitted, the runner "
            "tries MB12_OPENAI_COMPAT_BASE_URL, OPENAI_BASE_URL, then matching "
            "provider aliases from PI/OpenCode config."
        ),
    )
    parser.add_argument(
        "--openai-compatible-api-key",
        help=(
            "API key or bearer token for Hermes/Codex/OpenClaw OpenAI-compatible backends. "
            f"If omitted, the runner uses {OPENAI_COMPAT_API_KEY_ENV}, OPENAI_API_KEY, "
            "or the non-secret dummy value 'vllm'."
        ),
    )
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    results_root = Path(args.results_dir).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    task_ids = _task_ids(args.task)
    harness_bin = args.harness_bin or args.opencode_bin or _default_harness_bin(args.harness)
    variant = args.variant or f"{args.harness}__{args.model.replace('/', '__')}"
    provider_alias, model_id = _split_model_selector(args.model)
    openai_compat_base_url: str | None = None
    openai_compat_api_key: str | None = None
    if args.harness in {"hermes", "codex", "openclaw"}:
        openai_compat_base_url = _resolve_openai_compat_base_url(
            args.openai_compatible_base_url,
            provider_alias,
            model_id,
        )
        openai_compat_api_key = _resolve_openai_compat_api_key(
            args.openai_compatible_api_key
        )
    run_id = "__".join(
        part
        for part in [
            _now_stamp(),
            args.harness,
            _sanitize_component(args.model),
            _sanitize_component(args.run_name) if args.run_name else "",
        ]
        if part
    )
    run_dir = results_root / run_id
    task_logs_dir = run_dir / "tasks"
    workspace_run_dir = workspace_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    task_logs_dir.mkdir(parents=True, exist_ok=True)
    workspace_run_dir.mkdir(parents=True, exist_ok=True)

    started_at = _utc_iso()
    task_records: list[dict[str, Any]] = []

    _write_json(
        run_dir / "run_metadata.json",
        {
            "run_id": run_id,
            "started_at": started_at,
            "harness": args.harness,
            "harness_bin": harness_bin,
            "model": args.model,
            "variant": variant,
            "backend": args.backend,
            "reasoning_mode": args.reasoning_mode,
            "agent": args.agent,
            "skip_permissions": args.skip_permissions,
            "cleanup_passed_workspaces": args.cleanup_passed_workspaces,
            "task_timeout_sec": args.task_timeout_sec,
            "grade_timeout_sec": args.grade_timeout_sec,
            "pi_tools": args.pi_tools,
            "openai_compatible_base_url": openai_compat_base_url,
            "task_ids": task_ids,
            "project_root": str(PROJECT_ROOT),
            "results_root": str(results_root),
            "workspace_root": str(workspace_root),
        },
    )

    def flush_progress(finished_at: str | None = None) -> None:
        payload = _run_state_payload(
            run_id=run_id,
            harness=args.harness,
            model=args.model,
            variant=variant,
            backend=args.backend,
            reasoning_mode=args.reasoning_mode,
            task_ids=task_ids,
            run_dir=run_dir,
            workspace_run_dir=workspace_run_dir,
            task_records=task_records,
            started_at=started_at,
            finished_at=finished_at,
        )
        _write_json(run_dir / "run_progress.json", payload)
        _write_json(run_dir / "run_summary.json", payload)
        latest_dir = PROJECT_ROOT / "results"
        latest_dir.mkdir(parents=True, exist_ok=True)
        _write_text(latest_dir / "latest_run.txt", str(run_dir) + "\n")
        _write_text(latest_dir / f"latest_run_{args.harness}.txt", str(run_dir) + "\n")

    flush_progress()
    failure_checker = _failure_checker(args.harness)

    for task_index, task_id in enumerate(task_ids, start=1):
        task_name = f"{task_index:02d}__{task_id}"
        task_dir = task_logs_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        workspace = workspace_run_dir / task_name
        task_started = time.time()
        record: dict[str, Any] = {
            "task_id": task_id,
            "task_index": task_index,
            "started_at": _utc_iso(),
            "status": "pending",
            "workspace": str(workspace),
            "paths": _record_paths(task_dir, args.harness),
        }
        task_records.append(record)
        flush_progress()

        try:
            materialize = _run(
                [
                    _python(),
                    "-m",
                    "microbench12",
                    "materialize",
                    "--task",
                    task_id,
                    "--out",
                    str(workspace),
                ],
                cwd=PROJECT_ROOT,
                env=_mb12_env(),
            )
            if materialize.returncode != 0:
                record.update(
                    {
                        "status": "infra_error",
                        "error_type": "materialize_failed",
                        "error_message": (materialize.stderr or materialize.stdout).strip(),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    }
                )
                _write_text(
                    task_dir / "grade_error.txt",
                    "\n".join(
                        [
                            "materialize failed",
                            "",
                            "STDOUT:",
                            materialize.stdout,
                            "",
                            "STDERR:",
                            materialize.stderr,
                        ]
                    ),
                )
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
                continue

            run_cmd, run_cwd, run_env = _build_harness_command(
                harness=args.harness,
                model=args.model,
                workspace=workspace,
                task_dir=task_dir,
                task_id=task_id,
                harness_bin=harness_bin,
                agent=args.agent,
                skip_permissions=args.skip_permissions,
                pi_tools=args.pi_tools,
                reasoning_mode=args.reasoning_mode,
                openai_compat_base_url=openai_compat_base_url,
                openai_compat_api_key=openai_compat_api_key,
                task_timeout_sec=args.task_timeout_sec if args.task_timeout_sec > 0 else None,
            )

            harness_started = time.time()
            task_timeout = args.task_timeout_sec if args.task_timeout_sec > 0 else None
            try:
                run_result = _run(
                    run_cmd,
                    cwd=run_cwd,
                    env=run_env,
                    timeout=task_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                wall_time_sec = time.time() - harness_started
                log_path = Path(record["paths"]["harness_log"])
                stdout = exc.output or ""
                stderr = exc.stderr or ""
                _write_text(
                    log_path,
                    "\n".join(
                        [
                            f"$ {shlex.join(run_cmd)}",
                            f"cwd={run_cwd}",
                            f"timeout_sec={task_timeout}",
                            "",
                            "=== STDOUT ===",
                            stdout,
                            "",
                            "=== STDERR ===",
                            stderr,
                            "",
                            "exit_code=timeout",
                        ]
                    ),
                )
                telemetry_path = task_dir / "telemetry.json"
                _write_json(
                    telemetry_path,
                    {
                        "wall_time_sec": wall_time_sec,
                        "agent_time_sec": wall_time_sec,
                        "notes": (
                            f"{args.harness} timed out after "
                            f"{task_timeout} seconds"
                        ),
                        "stdout_log": str(log_path),
                        "harness": args.harness,
                    },
                )
                record.update(
                    {
                        "status": "runner_error",
                        "error_type": f"{args.harness}_timeout",
                        "error_message": (
                            f"{args.harness} did not finish within "
                            f"{task_timeout} seconds"
                        ),
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    }
                )
                _update_exit_metadata(record, args.harness, -signal.SIGKILL)
                _write_text(task_dir / "grade_error.txt", record["error_message"])
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
                continue
            wall_time_sec = time.time() - harness_started

            log_path = Path(record["paths"]["harness_log"])
            _write_text(
                log_path,
                "\n".join(
                    [
                        f"$ {shlex.join(run_cmd)}",
                        f"cwd={run_cwd}",
                        "",
                        "=== STDOUT ===",
                        run_result.stdout,
                        "",
                        "=== STDERR ===",
                        run_result.stderr,
                        "",
                        f"exit_code={run_result.returncode}",
                    ]
                ),
            )

            telemetry_path = task_dir / "telemetry.json"
            _write_json(
                telemetry_path,
                {
                    "wall_time_sec": wall_time_sec,
                    "agent_time_sec": wall_time_sec,
                    "notes": f"{args.harness} run exit_code={run_result.returncode}",
                    "stdout_log": str(log_path),
                    "harness": args.harness,
                },
            )

            failed, failure_reason = failure_checker(run_result)
            if failed:
                record.update(
                    {
                        "status": "infra_error",
                        "error_type": f"{args.harness}_failed",
                        "error_message": failure_reason,
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    }
                )
                _update_exit_metadata(record, args.harness, run_result.returncode)
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
                continue

            grade_result_path = task_dir / "grade_result.json"
            grade_cmd = [
                _python(),
                "-m",
                "microbench12",
                "grade",
                "--task",
                task_id,
                "--workspace",
                str(workspace),
                "--model",
                args.model,
                "--harness",
                args.harness,
                "--repeat",
                "1",
                "--variant",
                variant,
                "--backend",
                args.backend,
                "--out",
                str(grade_result_path),
                "--telemetry",
                str(telemetry_path),
            ]
            if args.reasoning_mode:
                grade_cmd.extend(["--reasoning-mode", args.reasoning_mode])

            grade_timeout = args.grade_timeout_sec if args.grade_timeout_sec > 0 else None
            try:
                grade_result = _run(
                    grade_cmd,
                    cwd=PROJECT_ROOT,
                    env=_mb12_env(),
                    timeout=grade_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                record.update(
                    {
                        "status": "runner_error",
                        "error_type": "grade_timeout",
                        "error_message": (
                            f"hidden grader did not finish within "
                            f"{grade_timeout} seconds"
                        ),
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    }
                )
                _update_exit_metadata(record, args.harness, run_result.returncode)
                _write_text(
                    task_dir / "grade_error.txt",
                    "\n".join(
                        [
                            f"$ {shlex.join(grade_cmd)}",
                            f"timeout_sec={grade_timeout}",
                            "",
                            "STDOUT:",
                            exc.output or "",
                            "",
                            "STDERR:",
                            exc.stderr or "",
                        ]
                    ),
                )
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
                continue
            if grade_result.returncode != 0:
                record.update(
                    {
                        "status": "infra_error",
                        "error_type": "grade_failed",
                        "error_message": (grade_result.stderr or grade_result.stdout).strip(),
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    }
                )
                _update_exit_metadata(record, args.harness, run_result.returncode)
                _write_text(
                    task_dir / "grade_error.txt",
                    "\n".join(
                        [
                            f"$ {shlex.join(grade_cmd)}",
                            "",
                            "STDOUT:",
                            grade_result.stdout,
                            "",
                            "STDERR:",
                            grade_result.stderr,
                        ]
                    ),
                )
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
                continue

            result_payload = json.loads(grade_result_path.read_text(encoding="utf-8"))
            record.update(
                {
                    "status": "passed" if result_payload["result"]["passed"] else "failed",
                    "score": result_payload["result"]["score"],
                    "passed": result_payload["result"]["passed"],
                    "result_path": str(grade_result_path),
                    "telemetry_path": str(telemetry_path),
                    "finished_at": _utc_iso(),
                    "duration_sec": round(time.time() - task_started, 6),
                }
            )
            _update_exit_metadata(record, args.harness, run_result.returncode)
            _write_json(task_dir / "task_record.json", record)

            if args.cleanup_passed_workspaces and result_payload["result"]["passed"]:
                shutil.rmtree(workspace, ignore_errors=True)
                record["workspace_cleaned_up"] = True
                _write_json(task_dir / "task_record.json", record)

            flush_progress()

        except Exception as exc:
            record.update(
                {
                    "status": "runner_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "finished_at": _utc_iso(),
                    "duration_sec": round(time.time() - task_started, 6),
                }
            )
            _write_text(task_dir / "grade_error.txt", traceback.format_exc())
            _write_json(task_dir / "task_record.json", record)
            flush_progress()

    finished_at = _utc_iso()
    flush_progress(finished_at=finished_at)
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
