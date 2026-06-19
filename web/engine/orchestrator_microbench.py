"""MicroBench-16 task runner — bridges the web panel to microbench12 CLI/Python API.

Execution flow per task:
1. materialize: create workspace from starter files
2. run harness: execute the CLI agent in the workspace
3. grade: run the hidden grader to check results
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MicroBenchResult:
    """Result of running a single microbench task."""

    task_id: str
    passed: bool
    message: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0
    agent_steps: int | None = None
    agent_tool_calls: int | None = None
    agent_shell_commands: int | None = None
    agent_llm_calls: int | None = None
    agent_input_tokens: int | None = None
    agent_output_tokens: int | None = None
    agent_total_tokens: int | None = None
    agent_transcript: str | None = None
    grader_details: dict = field(default_factory=dict)


class MicroBenchRunner:
    """Runs MicroBench-16 tasks using microbench12 CLI or Python API."""

    def __init__(self, microbench_root: str | None = None) -> None:
        """
        Args:
            microbench_root: Path to the microbench_16 repository root.
                           If None, tries to find it via the microbench12 package.
        """
        self._root = microbench_root or self._detect_root()

    @staticmethod
    def _detect_root() -> str | None:
        """Try to detect microbench12 install location."""
        try:
            import microbench12
            pkg_dir = Path(microbench12.__file__).parent
            # The tasks/ dir should be at the repo root, one level above the package
            repo_root = pkg_dir.parent
            if (repo_root / "tasks").is_dir():
                return str(repo_root)
        except ImportError:
            pass
        return None

    def materialize(self, task_id: str, out_dir: Path) -> bool:
        """Materialize a task workspace (copy starter files)."""
        if self._root:
            # Use CLI
            cmd = [
                "python", "-m", "microbench12", "materialize",
                "--task", task_id,
                "--out", str(out_dir),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60,
                    cwd=self._root,
                )
                if result.returncode != 0:
                    logger.warning(
                        f"materialize failed for {task_id}: {result.stderr}"
                    )
                    return False
                return True
            except Exception as e:
                logger.warning(f"materialize error for {task_id}: {e}")
                return False

        # Fallback: try Python API
        try:
            from microbench12.tasks import materialize_task
            materialize_task(task_id, out_dir, force=True)
            return True
        except Exception as e:
            logger.warning(f"Python materialize failed for {task_id}: {e}")
        return False

    def grade(
        self,
        task_id: str,
        workspace: Path,
        model: str = "unknown",
        harness: str = "cli",
    ) -> dict:
        """Grade a completed workspace. Returns grader result dict."""
        result_file = workspace / "_grade_result.json"

        if self._root:
            cmd = [
                "python", "-m", "microbench12", "grade",
                "--task", task_id,
                "--workspace", str(workspace),
                "--model", model,
                "--harness", harness,
                "--repeat", "1",
                "--out", str(result_file),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                    cwd=self._root,
                )
                if result_file.exists():
                    return json.loads(result_file.read_text(encoding="utf-8"))
                # Parse stdout as fallback
                if result.returncode == 0:
                    return {"passed": True, "message": result.stdout.strip()}
                return {
                    "passed": False,
                    "message": result.stderr.strip() or result.stdout.strip(),
                }
            except Exception as e:
                return {"passed": False, "message": f"grade error: {e}"}

        # Fallback: try Python grader API
        try:
            from microbench12.grader import grade_workspace
            from microbench12.types import AttemptMetadata
            meta = AttemptMetadata(
                task_id=task_id,
                model=model,
                harness=harness,
                repeat=1,
            )
            grade_res = grade_workspace(task_id, workspace, meta)
            res_dict = grade_res.get("result", {})
            return {
                "passed": res_dict.get("passed", False),
                "message": res_dict.get("summary", ""),
            }
        except Exception as e:
            return {"passed": False, "message": f"Python grade error: {e}"}

    async def run_task(
        self,
        task_id: str,
        cli_command: str,
        model: str = "",
        base_url: str | None = None,
        timeout: int = 600,
        env_vars: dict[str, str] | None = None,
    ) -> MicroBenchResult:
        """
        Run a single microbench task end-to-end:
        1. Materialize workspace
        2. Write TASK_PROMPT.md
        3. Execute CLI harness
        4. Grade the result
        """
        start = time.time()

        with TemporaryDirectory(prefix=f"microbench_{task_id}_") as tmpdir:
            workspace = Path(tmpdir)

            # Step 1: Materialize
            ok = self.materialize(task_id, workspace)
            if not ok:
                return MicroBenchResult(
                    task_id=task_id,
                    passed=False,
                    message="Failed to materialize workspace",
                    elapsed_seconds=time.time() - start,
                )

            # Step 2: Read prompt (from materialized workspace or tasks dir)
            prompt = self._read_prompt(task_id, workspace)

            # Step 3: Execute CLI harness
            harness_env = os.environ.copy()
            if env_vars:
                harness_env.update(env_vars)
            if base_url:
                harness_env["OPENAI_BASE_URL"] = base_url
                harness_env["OPENAI_API_BASE"] = base_url

            # Replace {workspace} and {model} placeholders
            resolved_command = cli_command.replace("{workspace}", str(workspace))
            if model:
                resolved_command = resolved_command.replace("{model}", model)
            argv = shlex.split(resolved_command)
            # Some harnesses accept the prompt as argument, others read TASK_PROMPT.md
            task_prompt_file = workspace / "TASK_PROMPT.md"
            if not task_prompt_file.exists() and prompt:
                task_prompt_file.write_text(prompt, encoding="utf-8")

            # Append prompt to CLI args if it looks like a prompt-accepting CLI
            cmd_lower = cli_command.lower()
            if any(h in cmd_lower for h in ("hermes", "opencode", "openclaw", "free-code")):
                argv.append(prompt)

            transcript = ""
            try:
                result = subprocess.run(
                    argv,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=harness_env,
                    encoding="utf-8",
                    errors="replace",
                )
                transcript = f"--- STDOUT ---\n{result.stdout}\n--- STDERR ---\n{result.stderr}"
            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                return MicroBenchResult(
                    task_id=task_id,
                    passed=False,
                    message="timeout",
                    elapsed_seconds=elapsed,
                )
            except Exception as e:
                elapsed = time.time() - start
                return MicroBenchResult(
                    task_id=task_id,
                    passed=False,
                    message=str(e),
                    elapsed_seconds=elapsed,
                )

            elapsed = time.time() - start

            # Step 4: Grade
            harness_name = self._extract_harness_name(cli_command)
            grade_result = self.grade(
                task_id, workspace,
                model=model or "unknown",
                harness=harness_name,
            )

            # Parse token stats from harness output
            from web.engine.orchestrator import parse_tokens_from_text
            stats = parse_tokens_from_text(transcript)

            return MicroBenchResult(
                task_id=task_id,
                passed=grade_result.get("passed", False),
                message=grade_result.get("message", ""),
                elapsed_seconds=elapsed,
                agent_transcript=transcript,
                agent_steps=stats.get("agent_steps"),
                agent_tool_calls=stats.get("agent_tool_calls"),
                agent_input_tokens=stats.get("agent_input_tokens"),
                agent_output_tokens=stats.get("agent_output_tokens"),
                agent_total_tokens=stats.get("agent_total_tokens"),
                grader_details=grade_result,
            )

    def _read_prompt(self, task_id: str, workspace: Path) -> str:
        """Read the task prompt from the materialized workspace or tasks dir."""
        # Check workspace first
        for name in ("TASK_PROMPT.md", "prompt.md"):
            p = workspace / name
            if p.exists():
                return p.read_text(encoding="utf-8")

        # Check tasks directory in repo
        if self._root:
            prompt_file = Path(self._root) / "tasks" / task_id / "prompt.md"
            if prompt_file.exists():
                return prompt_file.read_text(encoding="utf-8")

        return ""

    @staticmethod
    def _extract_harness_name(cli_command: str) -> str:
        """Extract harness name from CLI command for grading metadata."""
        cmd = cli_command.lower()
        for name in ("hermes", "opencode", "openclaw", "pi", "free-code", "codex"):
            if name in cmd:
                return name
        return "cli"
