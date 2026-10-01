from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .file_tools import execute, file_hash, list_files, safe_path, tree, validate_target
from .llm import DEFAULT_GOAL, AgentError, run_agent
from .models import Action

MAX_FILES = 100


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def print_plan(actions: list[Action]) -> None:
    print("Suggested organization")
    labels = ("CREATE", "MOVE", "MOVE + RENAME", "ARCHIVE", "ARCHIVE + RENAME", "KEEP", "POSSIBLE_DUPLICATE", "UNCERTAIN")
    for label in labels:
        selected = [action for action in actions if _display_kind(action) == label]
        if not selected:
            continue
        print(f"\n{label}")
        for action in selected:
            if action.kind == "CREATE":
                print(f"  {action.destination}/")
            else:
                arrow = f" -> {action.destination}" if action.destination else ""
                print(f"  {action.source}{arrow}\n    {action.reason} ({action.confidence:.0%})")


def _display_kind(action: Action) -> str:
    if action.kind not in {"MOVE", "ARCHIVE"} or not action.source or not action.destination:
        return action.kind
    renamed = Path(action.source).name != Path(action.destination).name
    return f"{action.kind} + RENAME" if renamed else action.kind


def make_audit(log_dir: Path):
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"organizer_{datetime.now():%Y%m%d_%H%M%S_%f}.log"

    def write(event: str, detail) -> None:
        value = json.dumps(detail, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {event} | {value}\n")

    return path, write


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Human-approved semantic file organizer")
    result.add_argument("target", type=Path)
    result.add_argument("--goal", default=DEFAULT_GOAL, help="Organization goal given to the agent")
    result.add_argument("--yes", action="store_true", help="Approve the generated plan")
    return result


def run(argv: list[str] | None = None, project_root: Path | None = None) -> int:
    args = parser().parse_args(argv)
    project_root = (project_root or Path.cwd()).resolve()
    configured_root = Path(os.getenv("FILE_ORGANIZER_ALLOWED_ROOT", "."))
    allowed_root = configured_root if configured_root.is_absolute() else project_root / configured_root
    try:
        target = validate_target(args.target, allowed_root)
    except ValueError as exc:
        print(f"Safety error: {exc}")
        return 2

    print("\n1. Current directory tree\n" + tree(target))
    files = list_files(target)
    if len(files) > MAX_FILES:
        print(f"Stopped: {len(files)} files exceeds the safe limit of {MAX_FILES}.")
        return 3

    print(f"\n2. Agent exploration\nGoal: {args.goal}")
    try:
        outcome = run_agent(target, args.goal)
    except AgentError as exc:
        print(f"Agent stopped safely: {exc}")
        return 3

    for event in outcome.trace:
        print(
            f"  step {event['step']}: {event['tool']} "
            f"[{event['status']}] - {event['summary']}"
        )
    print(f"Agent finished with {outcome.llm_calls} LLM call(s) and {len(outcome.trace)} tool call(s).")

    actions = outcome.actions
    print("\n3. Proposed organization plan")
    print(outcome.summary)
    print_plan(actions)

    audit_path, log = make_audit(project_root / "logs")
    log("GOAL", args.goal)
    log("AGENT_TRACE", outcome.trace)
    log("LLM_CALLS", outcome.llm_calls)
    log("PLAN", [asdict(action) for action in actions])

    print("\n4. Approval")
    if args.yes:
        approved = True
        print("Approved via --yes.")
    else:
        try:
            approved = input("Approve this plan? [y/N] ").strip().casefold() in {"y", "yes"}
        except EOFError:
            approved = False

    log("APPROVAL", "APPROVED" if approved else "REJECTED")
    if not approved:
        print(f"Rejected. No actions executed.\n\n7. Audit log\n{audit_path}")
        return 0

    print("\n5. Execution result")
    results = execute(actions, target, log)
    for result in results:
        subject = result.source or result.destination
        print(f"{result.status:12} {result.kind:20} {subject} | {result.message}")

    print("\n6. Final directory tree\n" + tree(target))
    errors: list[str] = []
    for result in results:
        if result.kind in {"CREATE", "MOVE", "ARCHIVE"} and result.status != "SUCCESS":
            errors.append(f"{result.kind} {result.source or result.destination}: {result.message}")
        if result.status == "SUCCESS" and result.destination and not safe_path(target, result.destination).exists():
            errors.append(f"Missing destination: {result.destination}")
    remaining = list_files(target)
    for action in actions:
        if action.kind in {"MOVE", "ARCHIVE"} and action.destination:
            destination = safe_path(target, action.destination)
            if destination.is_file() and action.sha256 and file_hash(destination) != action.sha256:
                errors.append(f"Hash mismatch: {action.destination}")
    log("VERIFY", {"errors": errors, "files_after_execution": remaining})
    print(f"Verification {'passed' if not errors else 'failed'}; {len(remaining)} file(s) remain in the tree.")
    print(f"\n7. Audit log\n{audit_path}")
    return 0
