"""Unattended maintainer / knowledge worker for a desk instance.

Runs deterministic maintenance steps: inbox ingest, optional subscription poll,
wiki evolve, lint, index rebuild, and gap proposals. Content-changing automation
stays proposal-oriented where policy requires review (LLM extraction is optional
and disabled by default). Manual CLI + MCP remain first-class without this worker.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.explore import explore_gaps
from knowledge_desk.index import rebuild_index
from knowledge_desk.ingest import IngestMetadata, ingest_path, successful as ingest_successful
from knowledge_desk.lint import lint_vault
from knowledge_desk.util import utc_now, write_json_synced, write_text_synced
from knowledge_desk.wiki import evolve_wiki


DEFAULT_STEPS: tuple[str, ...] = (
    "inbox_ingest",
    "subscribe_poll",
    "wiki_evolve",
    "lint",
    "index_rebuild",
    "explore_gaps",
)

JOBS_DIR = "system/jobs"
LEDGER_NAME = "ledger.jsonl"
LAST_RUN_NAME = "last-run.json"
DEAD_LETTER_NAME = "dead-letter.jsonl"


@dataclass
class StepResult:
    step: str
    status: str  # ok | skipped | failed | noop
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class MaintainResult:
    operation: str = "maintain.once"
    status: str = "failed"
    job_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    steps: list[dict[str, object]] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_steps(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return list(DEFAULT_STEPS)
    steps = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [step for step in steps if step not in DEFAULT_STEPS]
    if unknown:
        raise KnowledgeDeskError(f"unknown maintain steps: {', '.join(unknown)}")
    return steps


def run_maintain_cycle(
    vault_root: Path,
    *,
    steps: list[str] | None = None,
    max_inbox_files: int | None = None,
    propose_gaps: bool = True,
    poll_subscriptions: bool = True,
    job_id: str | None = None,
) -> MaintainResult:
    """Run one idempotent maintenance cycle and record durable job state."""
    vault_root = vault_root.resolve()
    result = MaintainResult(
        job_id=job_id or f"job-{utc_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}",
        started_at=utc_now(),
    )
    selected = steps or list(DEFAULT_STEPS)
    jobs_root = _ensure_jobs_dir(vault_root)
    step_results: list[StepResult] = []

    try:
        for step in selected:
            step_results.append(
                _run_step(
                    vault_root,
                    step,
                    max_inbox_files=max_inbox_files,
                    propose_gaps=propose_gaps,
                    poll_subscriptions=poll_subscriptions,
                )
            )
        failed = [item for item in step_results if item.status == "failed"]
        result.steps = [item.to_dict() for item in step_results]
        result.finished_at = utc_now()
        if failed:
            result.status = "partial" if len(failed) < len(step_results) else "failed"
            result.message = f"{len(failed)} step(s) failed of {len(step_results)}"
            _append_dead_letter(jobs_root, result)
        else:
            result.status = "ok"
            result.message = f"completed {len(step_results)} step(s)"
        _append_ledger(jobs_root, result)
        _write_last_run(jobs_root, result)
        return result
    except Exception as exc:  # noqa: BLE001 - job boundary must not crash the loop
        result.finished_at = utc_now()
        result.status = "failed"
        result.message = str(exc)
        result.steps = [item.to_dict() for item in step_results]
        _append_dead_letter(jobs_root, result)
        _append_ledger(jobs_root, result)
        _write_last_run(jobs_root, result)
        return result


def run_maintain_loop(
    vault_root: Path,
    *,
    interval_seconds: float = 300.0,
    steps: list[str] | None = None,
    max_cycles: int | None = None,
    max_inbox_files: int | None = None,
    propose_gaps: bool = True,
    poll_subscriptions: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MaintainResult:
    """Poll loop for containerized maintainers. Stops after max_cycles when set."""
    vault_root = vault_root.resolve()
    if interval_seconds < 1:
        raise KnowledgeDeskError("interval_seconds must be >= 1")
    cycles = 0
    last = MaintainResult(operation="maintain.loop", status="noop", message="no cycles run")
    while True:
        last = run_maintain_cycle(
            vault_root,
            steps=steps,
            max_inbox_files=max_inbox_files,
            propose_gaps=propose_gaps,
            poll_subscriptions=poll_subscriptions,
        )
        last.operation = "maintain.loop"
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            last.message = f"stopped after {cycles} cycle(s); last={last.status}"
            return last
        sleep_fn(interval_seconds)


def last_run(vault_root: Path) -> dict[str, object]:
    path = vault_root.resolve() / JOBS_DIR / LAST_RUN_NAME
    if not path.is_file():
        return {"operation": "maintain.status", "status": "never", "message": "no jobs recorded yet"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"operation": "maintain.status", "status": "error", "message": str(exc)}
    if isinstance(payload, dict):
        payload.setdefault("operation", "maintain.status")
        return payload
    return {"operation": "maintain.status", "status": "error", "message": "last-run is not an object"}


def _run_step(
    vault_root: Path,
    step: str,
    *,
    max_inbox_files: int | None,
    propose_gaps: bool,
    poll_subscriptions: bool,
) -> StepResult:
    if step == "inbox_ingest":
        return _step_inbox_ingest(vault_root, max_files=max_inbox_files)
    if step == "subscribe_poll":
        if not poll_subscriptions:
            return StepResult(step=step, status="skipped", message="subscribe poll disabled")
        return _step_subscribe_poll(vault_root)
    if step == "wiki_evolve":
        return _step_wiki_evolve(vault_root)
    if step == "lint":
        return _step_lint(vault_root)
    if step == "index_rebuild":
        return _step_index_rebuild(vault_root)
    if step == "explore_gaps":
        return _step_explore_gaps(vault_root, propose=propose_gaps)
    return StepResult(step=step, status="failed", message=f"unknown step {step!r}")


def _step_inbox_ingest(vault_root: Path, *, max_files: int | None) -> StepResult:
    inbox = vault_root / "inbox"
    if not inbox.is_dir():
        return StepResult(step="inbox_ingest", status="noop", message="inbox/ missing")
    files = sorted(
        (
            path
            for path in inbox.iterdir()
            if path.is_file() and not path.name.startswith(".") and path.name != "README.md"
        ),
        key=lambda path: path.name.casefold(),
    )
    if max_files is not None:
        files = files[: max(0, max_files)]
    if not files:
        return StepResult(step="inbox_ingest", status="noop", message="inbox empty")
    # Ingest each file individually so one bad file does not block the rest.
    results = []
    for path in files:
        results.extend(ingest_path(vault_root, path, IngestMetadata()))
    ok = ingest_successful(results)
    created = sum(1 for item in results if item.status == "created")
    noop = sum(1 for item in results if item.status == "noop")
    failed = sum(1 for item in results if item.status == "failed")
    return StepResult(
        step="inbox_ingest",
        status="ok" if ok else "failed",
        message=f"ingested {len(results)} file(s): created={created} noop={noop} failed={failed}",
        details={
            "results": [item.to_dict() for item in results],
            "created": created,
            "noop": noop,
            "failed": failed,
        },
    )


def _step_subscribe_poll(vault_root: Path) -> StepResult:
    subs_dir = vault_root / "system" / "subscriptions"
    if not subs_dir.is_dir() or not any(subs_dir.glob("sub-*.json")):
        return StepResult(step="subscribe_poll", status="skipped", message="no subscriptions")
    try:
        from knowledge_desk.subscribe import poll_subscriptions

        payload = poll_subscriptions(vault_root)
    except Exception as exc:  # noqa: BLE001 - step isolation
        return StepResult(step="subscribe_poll", status="failed", message=str(exc))
    status = "ok" if payload.get("status") == "ok" else "failed"
    return StepResult(
        step="subscribe_poll",
        status=status,
        message=str(payload.get("message") or payload.get("status") or ""),
        details={"poll": payload},
    )


def _step_wiki_evolve(vault_root: Path) -> StepResult:
    try:
        result = evolve_wiki(vault_root)
    except Exception as exc:  # noqa: BLE001
        return StepResult(step="wiki_evolve", status="failed", message=str(exc))
    status = "ok" if result.status in {"evolved", "noop"} else "failed"
    return StepResult(
        step="wiki_evolve",
        status=status if result.status != "noop" else "noop",
        message=result.message,
        details=result.to_dict(),
    )


def _step_lint(vault_root: Path) -> StepResult:
    try:
        report = lint_vault(vault_root)
    except Exception as exc:  # noqa: BLE001
        return StepResult(step="lint", status="failed", message=str(exc))
    # Lint findings are review suggestions; only hard-fail on invalid vault.
    status = "ok" if report.vault_valid else "failed"
    return StepResult(
        step="lint",
        status=status,
        message=report.message,
        details={
            "valid": report.valid,
            "vault_valid": report.vault_valid,
            "finding_count": len(report.findings),
        },
    )


def _step_index_rebuild(vault_root: Path) -> StepResult:
    try:
        result = rebuild_index(vault_root)
    except Exception as exc:  # noqa: BLE001
        return StepResult(step="index_rebuild", status="failed", message=str(exc))
    status = "ok" if result.status == "rebuilt" else "failed"
    return StepResult(
        step="index_rebuild",
        status=status,
        message=result.message,
        details=result.to_dict(),
    )


def _step_explore_gaps(vault_root: Path, *, propose: bool) -> StepResult:
    try:
        result = explore_gaps(vault_root, propose=propose)
    except Exception as exc:  # noqa: BLE001
        return StepResult(step="explore_gaps", status="failed", message=str(exc))
    return StepResult(
        step="explore_gaps",
        status="ok",
        message=result.message,
        details=result.to_dict(),
    )


def _ensure_jobs_dir(vault_root: Path) -> Path:
    path = vault_root / JOBS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_ledger(jobs_root: Path, result: MaintainResult) -> None:
    path = jobs_root / LEDGER_NAME
    line = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append_dead_letter(jobs_root: Path, result: MaintainResult) -> None:
    path = jobs_root / DEAD_LETTER_NAME
    line = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_last_run(jobs_root: Path, result: MaintainResult) -> None:
    write_json_synced(jobs_root / LAST_RUN_NAME, result.to_dict())


def default_steps_from_env() -> list[str]:
    return parse_steps(os.environ.get("KNOWLEDGE_DESK_MAINTAIN_STEPS"))
