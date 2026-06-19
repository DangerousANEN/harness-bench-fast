#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

OPENAI_COMPAT_API_KEY_ENV = "MB12_OPENAI_COMPAT_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = str(REPO_ROOT / "results" / "runs")
DEFAULT_WORKSPACE_ROOT = str(REPO_ROOT / "materialized" / "runs")
DEFAULT_PROJECT_ROOT = str(REPO_ROOT)
DEFAULT_IMAGES = {
    "pi": "mb12-pi-isolated",
    "hermes": "mb12-hermes-isolated",
    "openclaw": "mb12-openclaw-isolated",
}
PI_THINKING_LEVELS = {"low", "medium", "high", "xhigh"}
OPENCLAW_THINKING_LEVELS = {"low", "medium", "high", "xhigh"}


def _python() -> str:
    return sys.executable


def _mb12_env(project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(project_root) if not existing else f"{project_root}:{existing}"
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
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
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
    return (provider.strip() or None), model_id.strip()


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _task_ids(project_root: Path, selection: list[str]) -> list[str]:
    if selection == ["all"]:
        result = _run([_python(), "-m", "microbench12", "list"], cwd=project_root, env=_mb12_env(project_root))
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return [line.split("\t", 1)[0] for line in result.stdout.splitlines() if line.strip()]
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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
    return matches[0] if len(matches) == 1 else None


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
    return matches[0] if len(matches) == 1 else None


def _resolve_openai_compat_base_url(explicit_base_url: str | None, provider_alias: str | None, model_id: str) -> str:
    if explicit_base_url:
        return explicit_base_url
    env_base_url = os.environ.get("MB12_OPENAI_COMPAT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if env_base_url:
        return env_base_url.strip()
    for resolver in (_pi_provider_base_url, _opencode_provider_base_url):
        resolved = resolver(provider_alias, model_id)
        if resolved:
            return resolved
    raise RuntimeError(
        "Unable to resolve an OpenAI-compatible base URL. Pass --openai-compatible-base-url explicitly or use a provider alias already configured in PI/OpenCode."
    )


def _models_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def _detect_backend_from_models(base_url: str, api_key: str, model_id: str) -> str | None:
    url = _models_endpoint(base_url)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return None

    candidates: list[dict[str, Any]] = []
    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(item for item in data if isinstance(item, dict))
    models = payload.get("models")
    if isinstance(models, list):
        candidates.extend(item for item in models if isinstance(item, dict))

    def _matches(item: dict[str, Any]) -> bool:
        for key in ("id", "model", "name"):
            value = item.get(key)
            if isinstance(value, str) and value == model_id:
                return True
        aliases = item.get("aliases")
        return isinstance(aliases, list) and model_id in aliases

    for item in candidates:
        if not _matches(item):
            continue
        owner = item.get("owned_by")
        if isinstance(owner, str):
            owner_l = owner.lower()
            if "llamacpp" in owner_l or "llama.cpp" in owner_l:
                return "llamacpp"
            if "vllm" in owner_l:
                return "vllm"

    for item in candidates:
        owner = item.get("owned_by")
        if isinstance(owner, str):
            owner_l = owner.lower()
            if "llamacpp" in owner_l or "llama.cpp" in owner_l:
                return "llamacpp"
            if "vllm" in owner_l:
                return "vllm"

    return None


def _resolve_backend(explicit_backend: str, base_url: str, api_key: str, model_id: str) -> str:
    if explicit_backend and explicit_backend != "auto":
        return explicit_backend
    detected = _detect_backend_from_models(base_url, api_key, model_id)
    return detected or "vllm"


def _resolve_openai_compat_api_key(explicit_api_key: str | None) -> str:
    if explicit_api_key:
        return explicit_api_key
    env_api_key = os.environ.get(OPENAI_COMPAT_API_KEY_ENV) or os.environ.get("OPENAI_API_KEY") or os.environ.get("MB12_OPENAI_COMPAT_API_KEY")
    return env_api_key if env_api_key else "dummy"


def _counts(task_records: list[dict[str, Any]]) -> dict[str, int]:
    counters = {"passed": 0, "failed": 0, "infra_error": 0, "runner_error": 0, "pending": 0}
    for record in task_records:
        counters[record.get("status", "pending")] += 1
    return counters


def _completed_ok(task_records: list[dict[str, Any]]) -> bool:
    return all(record.get("status") in {"passed", "failed", "infra_error", "runner_error"} for record in task_records)


def _run_state_payload(*, run_id: str, harness: str, model: str, variant: str, backend: str, task_ids: list[str], run_dir: Path, workspace_run_dir: Path, task_records: list[dict[str, Any]], started_at: str, finished_at: str | None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "harness": harness,
        "model": model,
        "variant": variant,
        "backend": backend,
        "task_ids": task_ids,
        "run_dir": str(run_dir),
        "workspace_run_dir": str(workspace_run_dir),
        "counts": _counts(task_records),
        "completed_ok": _completed_ok(task_records),
        "tasks": task_records,
    }


def _record_paths(harness: str, task_dir: Path) -> dict[str, str]:
    log_name = f"{harness}.log"
    paths = {
        "task_dir": str(task_dir),
        "harness_log": str(task_dir / log_name),
        "telemetry": str(task_dir / "telemetry.json"),
        "grade_result": str(task_dir / "grade_result.json"),
        "grade_error": str(task_dir / "grade_error.txt"),
        "task_record": str(task_dir / "task_record.json"),
    }
    if harness == "pi":
        paths["container_home"] = str(task_dir / "pi_home")
        paths["pi_models_config"] = str(task_dir / "pi_home" / ".pi" / "agent" / "models.json")
    elif harness == "hermes":
        paths["container_home"] = str(task_dir / "hermes_home")
        paths["hermes_config"] = str(task_dir / "hermes_home" / "hermes_home" / "config.yaml")
    elif harness == "openclaw":
        paths["container_home"] = str(task_dir / "openclaw_home")
        paths["openclaw_state_dir"] = str(task_dir / "openclaw_home" / "openclaw_state")
        paths["openclaw_config"] = str(task_dir / "openclaw_home" / "openclaw_state" / "openclaw.json")
    return paths


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


def _openclaw_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"openclaw exited with code {result.returncode}"
    fatal_needles = ["Node.js v22.12+ is required", "Permission prompt unavailable in non-interactive mode", "No credentials found", "Model not allowed"]
    for needle in fatal_needles:
        if needle in combined:
            return True, combined
    return False, combined


def _write_pi_home(*, home_dir: Path, provider_alias: str, model_id: str, base_url: str, api_key: str, reasoning_mode: str | None) -> None:
    payload = {
        "providers": {
            provider_alias: {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": api_key,
                "authHeader": True,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "supportsUsageInStreaming": False,
                    "maxTokensField": "max_tokens",
                },
                "models": [
                    {
                        "id": model_id,
                        "name": model_id,
                        "reasoning": reasoning_mode not in {None, "", "off", "none"},
                        "input": ["text"],
                        "contextWindow": 262144,
                        "maxTokens": 16384,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ],
            }
        }
    }
    _write_json(home_dir / ".pi" / "agent" / "models.json", payload)


def _yaml_string(value: str) -> str:
    return json.dumps(value)


def _write_hermes_home(*, home_dir: Path, provider_alias: str, model_id: str, base_url: str, reasoning_mode: str | None) -> None:
    reasoning_block = ""
    if reasoning_mode:
        reasoning_block = "\nagent:\n" + f"  reasoning_effort: {_yaml_string(reasoning_mode)}\n"
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
    _write_text(home_dir / "hermes_home" / "config.yaml", payload)


def _write_openclaw_state(*, home_dir: Path, workspace: Path, provider_alias: str, model_id: str, base_url: str, reasoning_mode: str | None, task_timeout_sec: float | None) -> Path:
    state_dir = home_dir / "openclaw_state"
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
                "timeoutSeconds": int(task_timeout_sec or 1200),
            }
        },
        "models": {
            "providers": {
                provider_alias: {
                    "baseUrl": base_url,
                    "apiKey": {"source": "env", "provider": "default", "id": OPENAI_COMPAT_API_KEY_ENV},
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


def _docker_base_command(*, docker_bin: str, container_network: str | None, workspace: Path, home_dir: Path, container_workdir: str = "/workspace") -> list[str]:
    cmd = [
        docker_bin,
        "run",
        "--rm",
        "--init",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,size=256m",
        "-e",
        "HOME=/home/agent",
        "-e",
        "XDG_CACHE_HOME=/home/agent/.cache",
        "-e",
        "XDG_CONFIG_HOME=/home/agent/.config",
        "-e",
        "XDG_DATA_HOME=/home/agent/.local/share",
        "-w",
        container_workdir,
        "-v",
        f"{workspace}:/workspace:rw",
        "-v",
        f"{home_dir}:/home/agent:rw",
    ]
    if container_network:
        cmd.extend(["--network", container_network])
    return cmd


def _build_pi_invocation(*, home_dir: Path, model: str, pi_tools: str, reasoning_mode: str | None) -> tuple[list[str], dict[str, str]]:
    cmd = [
        "--model",
        model,
        "--session-dir",
        "/home/agent/pi_session",
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
    env = {"PI_OFFLINE": "1"}
    return cmd, env


def _build_hermes_invocation(*, provider_alias: str, model_id: str, api_key: str) -> tuple[list[str], dict[str, str]]:
    cmd = ["chat", "-q", _build_message(), "-Q", "--yolo", "--ignore-rules", "--provider", provider_alias, "-m", model_id]
    env = {
        "HERMES_HOME": "/home/agent/hermes_home",
        "OPENAI_API_KEY": api_key,
        "HERMES_INFERENCE_PROVIDER": provider_alias,
        "HERMES_INFERENCE_MODEL": model_id,
    }
    return cmd, env


def _build_openclaw_invocation(*, provider_alias: str, model_id: str, task_id: str, model: str, reasoning_mode: str | None, task_timeout_sec: float | None, api_key: str) -> tuple[list[str], dict[str, str]]:
    cmd = [
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
    env = {
        OPENAI_COMPAT_API_KEY_ENV: api_key,
        "OPENCLAW_STATE_DIR": "/home/agent/openclaw_state",
        "OPENCLAW_CONFIG_PATH": "/home/agent/openclaw_state/openclaw.json",
        "OPENCLAW_LOG_LEVEL": "error",
    }
    return cmd, env


def run_benchmark(harness: str) -> int:
    parser = argparse.ArgumentParser(description=f"Run MicroBench-12 through {harness} in a one-shot Docker container.")
    parser.add_argument("--project-root", default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--task", action="append", required=True, help="Task id to run. Repeat the flag for multiple tasks, or pass --task all.")
    parser.add_argument("--workspace-root", default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--variant")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--task-timeout-sec", type=float, default=1200.0)
    parser.add_argument("--grade-timeout-sec", type=float, default=300.0)
    parser.add_argument("--openai-compatible-base-url")
    parser.add_argument("--openai-compatible-api-key")
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--container-image", default=DEFAULT_IMAGES[harness])
    parser.add_argument("--container-network")
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--pi-tools", default="read,bash,edit,write,grep,find,ls")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    results_root = Path(args.results_dir).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    task_ids = _task_ids(project_root, args.task)
    variant = args.variant or f"{harness}_container__{args.model.replace('/', '__')}"
    provider_alias, model_id = _split_model_selector(args.model)
    openai_compat_base_url = _resolve_openai_compat_base_url(args.openai_compatible_base_url, provider_alias, model_id)
    openai_compat_api_key = _resolve_openai_compat_api_key(args.openai_compatible_api_key)
    effective_backend = _resolve_backend(args.backend, openai_compat_base_url, openai_compat_api_key, model_id)
    provider_alias = provider_alias or "local-openai"

    run_id = "__".join([_now_stamp(), f"{harness}-container", _sanitize_component(args.model)])
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
            "harness": harness,
            "containerized": True,
            "container_image": args.container_image,
            "container_network": args.container_network,
            "docker_bin": args.docker_bin,
            "model": args.model,
            "variant": variant,
            "backend": effective_backend,
            "reasoning_mode": args.reasoning_mode,
            "task_timeout_sec": args.task_timeout_sec,
            "grade_timeout_sec": args.grade_timeout_sec,
            "pi_tools": args.pi_tools if harness == "pi" else None,
            "openai_compatible_base_url": openai_compat_base_url,
            "task_ids": task_ids,
            "project_root": str(project_root),
            "results_root": str(results_root),
            "workspace_root": str(workspace_root),
        },
    )

    def flush_progress(finished_at: str | None = None) -> None:
        payload = _run_state_payload(
            run_id=run_id,
            harness=harness,
            model=args.model,
            variant=variant,
            backend=effective_backend,
            task_ids=task_ids,
            run_dir=run_dir,
            workspace_run_dir=workspace_run_dir,
            task_records=task_records,
            started_at=started_at,
            finished_at=finished_at,
        )
        _write_json(run_dir / "run_progress.json", payload)
        _write_json(run_dir / "run_summary.json", payload)

    flush_progress()

    active_record: dict[str, Any] | None = None
    active_task_dir: Path | None = None
    active_task_started: float | None = None

    try:
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
                "paths": _record_paths(harness, task_dir),
            }
            task_records.append(record)
            active_record = record
            active_task_dir = task_dir
            active_task_started = task_started
            flush_progress()

            try:
                materialize = _run([
                    _python(), "-m", "microbench12", "materialize", "--task", task_id, "--out", str(workspace)
                ], cwd=project_root, env=_mb12_env(project_root))
                if materialize.returncode != 0:
                    record.update({
                        "status": "infra_error",
                        "error_type": "materialize_failed",
                        "error_message": (materialize.stderr or materialize.stdout).strip(),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                    })
                    _write_text(task_dir / "grade_error.txt", "\n".join(["materialize failed", "", "STDOUT:", materialize.stdout, "", "STDERR:", materialize.stderr]))
                    _write_json(task_dir / "task_record.json", record)
                    flush_progress()
                    continue

                home_dir = task_dir / f"{harness}_home"
                home_dir.mkdir(parents=True, exist_ok=True)
                if harness == "pi":
                    _write_pi_home(home_dir=home_dir, provider_alias=provider_alias, model_id=model_id, base_url=openai_compat_base_url, api_key=openai_compat_api_key, reasoning_mode=args.reasoning_mode)
                    docker_cmd = _docker_base_command(docker_bin=args.docker_bin, container_network=args.container_network, workspace=workspace, home_dir=home_dir)
                    inner_cmd, extra_env = _build_pi_invocation(home_dir=home_dir, model=args.model, pi_tools=args.pi_tools, reasoning_mode=args.reasoning_mode)
                elif harness == "hermes":
                    _write_hermes_home(home_dir=home_dir, provider_alias=provider_alias, model_id=model_id, base_url=openai_compat_base_url, reasoning_mode=args.reasoning_mode)
                    docker_cmd = _docker_base_command(docker_bin=args.docker_bin, container_network=args.container_network, workspace=workspace, home_dir=home_dir)
                    inner_cmd, extra_env = _build_hermes_invocation(provider_alias=provider_alias, model_id=model_id, api_key=openai_compat_api_key)
                else:
                    _write_openclaw_state(home_dir=home_dir, workspace=Path('/workspace'), provider_alias=provider_alias, model_id=model_id, base_url=openai_compat_base_url, reasoning_mode=args.reasoning_mode, task_timeout_sec=args.task_timeout_sec)
                    docker_cmd = _docker_base_command(docker_bin=args.docker_bin, container_network=args.container_network, workspace=workspace, home_dir=home_dir)
                    inner_cmd, extra_env = _build_openclaw_invocation(provider_alias=provider_alias, model_id=model_id, task_id=task_id, model=args.model, reasoning_mode=args.reasoning_mode, task_timeout_sec=args.task_timeout_sec, api_key=openai_compat_api_key)
                for key, value in extra_env.items():
                    docker_cmd.extend(["-e", f"{key}={value}"])
                run_cmd = docker_cmd + [args.container_image] + inner_cmd

                harness_started = time.time()
                task_timeout = args.task_timeout_sec if args.task_timeout_sec > 0 else None
                try:
                    run_result = _run(run_cmd, cwd=project_root, env=None, timeout=task_timeout)
                except subprocess.TimeoutExpired as exc:
                    wall_time_sec = time.time() - harness_started
                    log_path = Path(record["paths"]["harness_log"])
                    _write_text(log_path, "\n".join([
                        f"$ {shlex.join(run_cmd)}",
                        f"timeout_sec={task_timeout}",
                        "",
                        "=== STDOUT ===",
                        exc.output or "",
                        "",
                        "=== STDERR ===",
                        exc.stderr or "",
                        "",
                        "exit_code=timeout",
                    ]))
                    telemetry_path = task_dir / "telemetry.json"
                    _write_json(telemetry_path, {
                        "wall_time_sec": wall_time_sec,
                        "agent_time_sec": wall_time_sec,
                        "notes": f"containerized {harness} timed out after {task_timeout} seconds",
                        "stdout_log": str(log_path),
                        "harness": harness,
                    })
                    record.update({
                        "status": "runner_error",
                        "error_type": f"{harness}_timeout",
                        "error_message": f"{harness} did not finish within {task_timeout} seconds",
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                        "harness_exit_code": -signal.SIGKILL,
                    })
                    _write_text(task_dir / "grade_error.txt", record["error_message"])
                    _write_json(task_dir / "task_record.json", record)
                    flush_progress()
                    continue

                wall_time_sec = time.time() - harness_started
                log_path = Path(record["paths"]["harness_log"])
                _write_text(log_path, "\n".join([
                    f"$ {shlex.join(run_cmd)}",
                    "",
                    "=== STDOUT ===",
                    run_result.stdout,
                    "",
                    "=== STDERR ===",
                    run_result.stderr,
                    "",
                    f"exit_code={run_result.returncode}",
                ]))
                telemetry_path = task_dir / "telemetry.json"
                _write_json(telemetry_path, {
                    "wall_time_sec": wall_time_sec,
                    "agent_time_sec": wall_time_sec,
                    "notes": f"containerized {harness} run exit_code={run_result.returncode}",
                    "stdout_log": str(log_path),
                    "harness": harness,
                })

                checker = _pi_failed if harness == "pi" else _hermes_failed if harness == "hermes" else _openclaw_failed
                failed, failure_reason = checker(run_result)
                if failed:
                    record.update({
                        "status": "infra_error",
                        "error_type": f"{harness}_failed",
                        "error_message": failure_reason,
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                        "harness_exit_code": run_result.returncode,
                    })
                    _write_json(task_dir / "task_record.json", record)
                    flush_progress()
                    continue

                grade_result_path = task_dir / "grade_result.json"
                grade_cmd = [
                    _python(), "-m", "microbench12", "grade", "--task", task_id, "--workspace", str(workspace),
                    "--model", args.model, "--harness", harness, "--repeat", "1", "--variant", variant,
                    "--backend", effective_backend, "--out", str(grade_result_path), "--telemetry", str(telemetry_path),
                ]
                grade_timeout = args.grade_timeout_sec if args.grade_timeout_sec > 0 else None
                try:
                    grade_result = _run(grade_cmd, cwd=project_root, env=_mb12_env(project_root), timeout=grade_timeout)
                except subprocess.TimeoutExpired as exc:
                    record.update({
                        "status": "runner_error",
                        "error_type": "grade_timeout",
                        "error_message": f"hidden grader did not finish within {grade_timeout} seconds",
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                        "harness_exit_code": run_result.returncode,
                    })
                    _write_text(task_dir / "grade_error.txt", "\n".join([
                        f"$ {shlex.join(grade_cmd)}",
                        f"timeout_sec={grade_timeout}",
                        "",
                        "STDOUT:",
                        exc.output or "",
                        "",
                        "STDERR:",
                        exc.stderr or "",
                    ]))
                    _write_json(task_dir / "task_record.json", record)
                    flush_progress()
                    continue

                if grade_result.returncode != 0:
                    record.update({
                        "status": "infra_error",
                        "error_type": "grade_failed",
                        "error_message": (grade_result.stderr or grade_result.stdout).strip(),
                        "telemetry_path": str(telemetry_path),
                        "finished_at": _utc_iso(),
                        "duration_sec": round(time.time() - task_started, 6),
                        "harness_exit_code": run_result.returncode,
                    })
                    _write_text(task_dir / "grade_error.txt", "\n".join([
                        f"$ {shlex.join(grade_cmd)}",
                        "",
                        "STDOUT:",
                        grade_result.stdout,
                        "",
                        "STDERR:",
                        grade_result.stderr,
                    ]))
                    _write_json(task_dir / "task_record.json", record)
                    flush_progress()
                    continue

                result_payload = json.loads(grade_result_path.read_text(encoding="utf-8"))
                record.update({
                    "status": "passed" if result_payload["result"]["passed"] else "failed",
                    "score": result_payload["result"]["score"],
                    "passed": result_payload["result"]["passed"],
                    "result_path": str(grade_result_path),
                    "telemetry_path": str(telemetry_path),
                    "finished_at": _utc_iso(),
                    "duration_sec": round(time.time() - task_started, 6),
                    "harness_exit_code": run_result.returncode,
                })
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
            except Exception as exc:
                record.update({
                    "status": "runner_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "finished_at": _utc_iso(),
                    "duration_sec": round(time.time() - task_started, 6),
                })
                _write_text(task_dir / "grade_error.txt", traceback.format_exc())
                _write_json(task_dir / "task_record.json", record)
                flush_progress()
            finally:
                active_record = None
                active_task_dir = None
                active_task_started = None
    except KeyboardInterrupt:
        if active_record is not None and active_record.get("status") == "pending":
            duration = round(time.time() - active_task_started, 6) if active_task_started is not None else None
            active_record.update({
                "status": "runner_error",
                "error_type": "interrupted",
                "error_message": "run interrupted by user",
                "finished_at": _utc_iso(),
                "duration_sec": duration,
            })
            if active_task_dir is not None:
                _write_text(active_task_dir / "grade_error.txt", "run interrupted by user\n")
                _write_json(active_task_dir / "task_record.json", active_record)
        finished_at = _utc_iso()
        flush_progress(finished_at=finished_at)
        print(run_dir)
        return 130

    finished_at = _utc_iso()
    flush_progress(finished_at=finished_at)
    print(run_dir)
    return 0
