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
DEFAULT_PROJECT_ROOT = str(REPO_ROOT)
DEFAULT_RESULTS_ROOT = str(REPO_ROOT / "results" / "runs")
DEFAULT_WORKSPACE_ROOT = str(REPO_ROOT / "materialized" / "runs")


def _python() -> str:
    return sys.executable


def _mb12_env(project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(project_root) if not existing else f"{project_root}:{existing}"
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


def _task_ids(project_root: Path, selection: list[str]) -> list[str]:
    if selection == ["all"]:
        result = _run(
            [_python(), "-m", "microbench12", "list"],
            cwd=project_root,
            env=_mb12_env(project_root),
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
        "Unable to resolve an OpenAI-compatible base URL. "
        "Pass --openai-compatible-base-url explicitly or use a provider alias "
        "already configured in PI/OpenCode."
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
    env_api_key = (
        os.environ.get(OPENAI_COMPAT_API_KEY_ENV)
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("MB12_OPENAI_COMPAT_API_KEY")
    )
    if env_api_key:
        return env_api_key
    return "dummy"


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
        counters[status] = counters.get(status, 0) + 1
    return counters


def _completed_ok(task_records: list[dict[str, Any]]) -> bool:
    return all(
        record.get("status") in {"passed", "failed", "infra_error", "runner_error"}
        for record in task_records
    )


def _run_state_payload(
    *,
    run_id: str,
    model: str,
    variant: str,
    backend: str,
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
        "harness": "opencode",
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


def _record_paths(task_dir: Path) -> dict[str, str]:
    return {
        "task_dir": str(task_dir),
        "harness_log": str(task_dir / "opencode.log"),
        "telemetry": str(task_dir / "telemetry.json"),
        "grade_result": str(task_dir / "grade_result.json"),
        "grade_error": str(task_dir / "grade_error.txt"),
        "task_record": str(task_dir / "task_record.json"),
        "container_config": str(task_dir / "opencode_home" / ".config" / "opencode" / "opencode.json"),
    }


def _opencode_failed(result: subprocess.CompletedProcess) -> tuple[bool, str]:
    stdout = _strip_ansi(result.stdout or "")
    stderr = _strip_ansi(result.stderr or "")
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
    if result.returncode != 0:
        return True, combined or f"OpenCode exited with code {result.returncode}"
    fatal_needles = [
        'Error: "auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set',
        "Error: Model not found:",
        "No credentials found",
    ]
    for needle in fatal_needles:
        if needle in combined:
            return True, combined
    return False, combined


def _write_opencode_config(
    *,
    task_dir: Path,
    provider_alias: str,
    model_id: str,
    base_url: str,
    api_key: str,
) -> Path:
    config_dir = task_dir / "opencode_home" / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": [provider_alias],
        "provider": {
            provider_alias: {
                "name": provider_alias,
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key,
                },
                "models": {
                    model_id: {
                        "name": model_id,
                    }
                },
            }
        },
        "permission": "allow",
    }
    config_path = config_dir / "opencode.json"
    _write_json(config_path, payload)
    return config_path


def _docker_opencode_command(
    *,
    docker_bin: str,
    container_image: str,
    container_network: str | None,
    workspace: Path,
    opencode_home: Path,
    model: str,
    task_id: str,
    skip_permissions: bool,
) -> list[str]:
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
        "/workspace",
        "-v",
        f"{workspace}:/workspace:rw",
        "-v",
        f"{opencode_home}:/home/agent:rw",
    ]
    if container_network:
        cmd.extend(["--network", container_network])
    cmd.append(container_image)
    cmd.extend(
        [
            "run",
            "--pure",
            "--model",
            model,
            "--dir",
            "/workspace",
            "--title",
            _session_title(task_id, model),
        ]
    )
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(_build_message())
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MicroBench-12 through OpenCode in a one-shot Docker container."
    )
    parser.add_argument(
        "--project-root",
        default=DEFAULT_PROJECT_ROOT,
        help="Original MicroBench-12 project root.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="provider/model, e.g. myprovider/Qwen3.6-35B",
    )
    parser.add_argument(
        "--task",
        action="append",
        required=True,
        help="Task id to run. Repeat the flag for multiple tasks, or pass --task all.",
    )
    parser.add_argument(
        "--workspace-root",
        default=DEFAULT_WORKSPACE_ROOT,
    )
    parser.add_argument(
        "--results-dir",
        default=DEFAULT_RESULTS_ROOT,
    )
    parser.add_argument("--variant")
    parser.add_argument("--backend", default="auto")
    parser.add_argument(
        "--skip-permissions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow OpenCode to run without permission prompts. Enabled by default.",
    )
    parser.add_argument(
        "--task-timeout-sec",
        type=float,
        default=1200.0,
        help="Maximum seconds to let one harness task run. Use 0 to disable.",
    )
    parser.add_argument(
        "--grade-timeout-sec",
        type=float,
        default=300.0,
        help="Maximum seconds to let one hidden grader run. Use 0 to disable.",
    )
    parser.add_argument(
        "--openai-compatible-base-url",
        help="Explicit model server base URL, for example http://192.168.90.17:8080/v1.",
    )
    parser.add_argument(
        "--openai-compatible-api-key",
        help="Explicit model server API key/bearer token.",
    )
    parser.add_argument(
        "--docker-bin",
        default="docker",
        help="Docker executable to use.",
    )
    parser.add_argument(
        "--container-image",
        default="mb12-opencode-isolated",
        help="Prebuilt Docker image that contains the OpenCode binary.",
    )
    parser.add_argument(
        "--container-network",
        help="Optional Docker network name for the harness container.",
    )
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    results_root = Path(args.results_dir).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    task_ids = _task_ids(project_root, args.task)
    variant = args.variant or f"opencode_container__{args.model.replace('/', '__')}"
    provider_alias, model_id = _split_model_selector(args.model)
    openai_compat_base_url = _resolve_openai_compat_base_url(
        args.openai_compatible_base_url,
        provider_alias,
        model_id,
    )
    openai_compat_api_key = _resolve_openai_compat_api_key(
        args.openai_compatible_api_key
    )
    effective_backend = _resolve_backend(args.backend, openai_compat_base_url, openai_compat_api_key, model_id)

    run_id = "__".join(
        [
            _now_stamp(),
            "opencode-container",
            _sanitize_component(args.model),
        ]
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
            "harness": "opencode",
            "containerized": True,
            "container_image": args.container_image,
            "container_network": args.container_network,
            "docker_bin": args.docker_bin,
            "model": args.model,
            "variant": variant,
            "backend": effective_backend,
            "skip_permissions": args.skip_permissions,
            "task_timeout_sec": args.task_timeout_sec,
            "grade_timeout_sec": args.grade_timeout_sec,
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
                "paths": _record_paths(task_dir),
            }
            task_records.append(record)
            active_record = record
            active_task_dir = task_dir
            active_task_started = task_started
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
                    cwd=project_root,
                    env=_mb12_env(project_root),
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

                config_path = _write_opencode_config(
                    task_dir=task_dir,
                    provider_alias=provider_alias or "local-openai",
                    model_id=model_id,
                    base_url=openai_compat_base_url,
                    api_key=openai_compat_api_key,
                )
                run_cmd = _docker_opencode_command(
                    docker_bin=args.docker_bin,
                    container_image=args.container_image,
                    container_network=args.container_network,
                    workspace=workspace,
                    opencode_home=config_path.parents[2],
                    model=args.model,
                    task_id=task_id,
                    skip_permissions=args.skip_permissions,
                )

                harness_started = time.time()
                task_timeout = args.task_timeout_sec if args.task_timeout_sec > 0 else None
                try:
                    run_result = _run(
                        run_cmd,
                        cwd=project_root,
                        env=None,
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
                            "notes": f"containerized opencode timed out after {task_timeout} seconds",
                            "stdout_log": str(log_path),
                            "harness": "opencode",
                        },
                    )
                    record.update(
                        {
                            "status": "runner_error",
                            "error_type": "opencode_timeout",
                            "error_message": (
                                f"opencode did not finish within {task_timeout} seconds"
                            ),
                            "telemetry_path": str(telemetry_path),
                            "finished_at": _utc_iso(),
                            "duration_sec": round(time.time() - task_started, 6),
                            "harness_exit_code": -signal.SIGKILL,
                        }
                    )
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
                        "notes": f"containerized opencode run exit_code={run_result.returncode}",
                        "stdout_log": str(log_path),
                        "harness": "opencode",
                    },
                )

                failed, failure_reason = _opencode_failed(run_result)
                if failed:
                    record.update(
                        {
                            "status": "infra_error",
                            "error_type": "opencode_failed",
                            "error_message": failure_reason,
                            "telemetry_path": str(telemetry_path),
                            "finished_at": _utc_iso(),
                            "duration_sec": round(time.time() - task_started, 6),
                            "harness_exit_code": run_result.returncode,
                        }
                    )
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
                    "opencode",
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

                grade_timeout = args.grade_timeout_sec if args.grade_timeout_sec > 0 else None
                try:
                    grade_result = _run(
                        grade_cmd,
                        cwd=project_root,
                        env=_mb12_env(project_root),
                        timeout=grade_timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    record.update(
                        {
                            "status": "runner_error",
                            "error_type": "grade_timeout",
                            "error_message": (
                                f"hidden grader did not finish within {grade_timeout} seconds"
                            ),
                            "telemetry_path": str(telemetry_path),
                            "finished_at": _utc_iso(),
                            "duration_sec": round(time.time() - task_started, 6),
                            "harness_exit_code": run_result.returncode,
                        }
                    )
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
                            "harness_exit_code": run_result.returncode,
                        }
                    )
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
                        "harness_exit_code": run_result.returncode,
                    }
                )
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
            finally:
                active_record = None
                active_task_dir = None
                active_task_started = None
    except KeyboardInterrupt:
        if active_record is not None and active_record.get("status") == "pending":
            duration = round(time.time() - active_task_started, 6) if active_task_started is not None else None
            active_record.update(
                {
                    "status": "runner_error",
                    "error_type": "interrupted",
                    "error_message": "run interrupted by user",
                    "finished_at": _utc_iso(),
                    "duration_sec": duration,
                }
            )
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


if __name__ == "__main__":
    raise SystemExit(main())
