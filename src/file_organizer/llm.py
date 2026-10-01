from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .file_tools import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    file_hash,
    find_duplicate_groups,
    inspect_metadata,
    list_files,
    read_preview,
    safe_file,
    safe_path,
)
from .models import Action, AgentOutcome


class AgentError(RuntimeError):
    pass


DEFAULT_GOAL = (
    "Phân tích và đề xuất cách tổ chức các file trong thư mục này dựa trên nội dung. "
    "Nếu chưa đủ thông tin, hãy tự sử dụng các công cụ được cung cấp để kiểm tra. "
    "Không thay đổi file trước khi tôi duyệt."
)

MAX_AGENT_STEPS = 12
MAX_TOOL_FILES = 100

SYSTEM_PROMPT = """You are a file organization agent. Work toward the user's goal by choosing
from the available read-only tools. Decide for yourself which tools are useful and whether the
current evidence is sufficient. Do not follow a fixed inspection sequence and do not inspect file
contents when names and metadata are already sufficient.

Safety rules:
- Never request deletion and never overwrite a file.
- Use only relative paths returned by list_files.
- A destination may contain nested folders and may use a clearer filename.
- Preserve the original extension when renaming.
- Move confirmed redundant copies to Duplicates/ instead of deleting them.
- Use UNCERTAIN when the evidence is insufficient.
- Use POSSIBLE_DUPLICATE only when similarity is suspected but not confirmed.
- Use KEEP for a file already in an appropriate location.
- Account for every file exactly once.
- Call submit_plan only after you have enough evidence. submit_plan does not modify the filesystem.
"""


PLAN_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "source", "destination", "reason", "confidence"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["MOVE", "ARCHIVE", "POSSIBLE_DUPLICATE", "UNCERTAIN", "KEEP"],
        },
        "source": {"type": "string"},
        "destination": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all regular files in the target directory recursively. Returns relative paths only.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_metadata",
            "description": "Inspect extension, media type, byte size and modified time for selected files.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["files"],
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a bounded text preview from one TXT, Markdown, CSV, DOCX or text-based PDF. "
                "Use only when more semantic evidence is needed."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file"],
                "properties": {"file": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_image",
            "description": (
                "Understand a PNG, JPEG, WEBP, GIF or the first page of a scanned PDF. "
                "Returns a bounded visual description."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file"],
                "properties": {"file": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_duplicates",
            "description": "Find exact duplicates among selected files using byte size and SHA-256.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["files"],
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "Finish exploration and submit the complete organization plan for human review.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "actions"],
                "properties": {
                    "summary": {"type": "string"},
                    "actions": {"type": "array", "items": PLAN_ACTION_SCHEMA},
                },
            },
        },
    },
]


def run_agent(root: Path, goal: str = DEFAULT_GOAL) -> AgentOutcome:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise AgentError("OPENROUTER_API_KEY is missing; configure it in .env")
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=key,
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
                "X-Title": os.getenv("OPENROUTER_APP_NAME", "File Organizer Agent"),
            },
        )
        return _AgentRuntime(root, client).run(goal)
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(f"OpenRouter agent failed: {exc}") from exc


class _AgentRuntime:
    def __init__(self, root: Path, client: Any) -> None:
        self.root = root
        self.client = client
        self.model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        self.vision_model = os.getenv("OPENROUTER_VISION_MODEL", self.model)
        self.trace: list[dict[str, object]] = []
        self.llm_calls = 0

    def run(self, goal: str) -> AgentOutcome:
        messages: list[Any] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        for step in range(1, MAX_AGENT_STEPS + 1):
            try:
                self.llm_calls += 1
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                )
            except Exception as exc:
                raise AgentError(f"OpenRouter tool-calling request failed: {exc}") from exc

            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            tool_calls = message.tool_calls or []
            if not tool_calls:
                messages.append({
                    "role": "user",
                    "content": "Continue by calling a read-only tool or submit_plan. Do not answer with plain text.",
                })
                continue

            for call in tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    if name == "submit_plan":
                        outcome = self._submit_plan(arguments)
                        self._record(step, name, arguments, "success", f"{len(outcome.actions)} executable/review actions")
                        return AgentOutcome(
                            actions=outcome.actions,
                            summary=outcome.summary,
                            trace=self.trace,
                            llm_calls=self.llm_calls,
                        )
                    result = self._execute_tool(name, arguments)
                    status = "success"
                except Exception as exc:
                    result = {"error": str(exc)}
                    status = "error"

                self._record(step, name, arguments, status, _result_summary(name, result))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        raise AgentError(f"Agent did not submit a valid plan within {MAX_AGENT_STEPS} steps")

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_files":
            files = list_files(self.root)
            if len(files) > MAX_TOOL_FILES:
                raise ValueError(f"Target contains {len(files)} files; safe limit is {MAX_TOOL_FILES}")
            return {"files": files, "count": len(files)}

        if name == "inspect_metadata":
            files = _file_arguments(arguments, minimum=1)
            return {"files": inspect_metadata(self.root, files)}

        if name == "read_file":
            relative = _one_file(arguments)
            path = safe_file(self.root, relative)
            preview = read_preview(path)
            if preview is None:
                return {"path": relative, "content": None, "note": "Unsupported, too large, corrupt or without extractable text"}
            return {"path": relative, "content": preview, "truncated": len(preview) >= 2_000}

        if name == "inspect_image":
            relative = _one_file(arguments)
            description = self._inspect_image(relative)
            return {"path": relative, "description": description}

        if name == "find_duplicates":
            files = _file_arguments(arguments, minimum=2)
            return {"exact_duplicate_groups": find_duplicate_groups(self.root, files)}

        raise ValueError(f"Unknown tool: {name}")

    def _inspect_image(self, relative: str) -> str:
        path = safe_file(self.root, relative)
        data_url = _image_data_url(path)
        try:
            self.llm_calls += 1
            response = self.client.chat.completions.create(
                model=self.vision_model,
                temperature=0,
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Describe this file for organization purposes. Identify its likely topic, document type, "
                                "project or event, visible dates and useful filename clues. Do not invent unreadable details."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            )
        except Exception as exc:
            raise ValueError(f"visual inspection failed: {exc}") from exc
        content = response.choices[0].message.content
        if not content:
            raise ValueError("visual inspection returned no description")
        return content[:2_000]

    def _submit_plan(self, raw: dict[str, Any]) -> AgentOutcome:
        summary = str(raw.get("summary", "")).strip()
        proposed = raw.get("actions")
        if not summary or not isinstance(proposed, list):
            raise ValueError("submit_plan requires summary and actions")

        known = set(list_files(self.root))
        sources: set[str] = set()
        destinations: set[str] = set()
        planned: list[Action] = []
        directories: set[str] = set()

        for item in proposed:
            if not isinstance(item, dict):
                raise ValueError("each plan action must be an object")
            kind = str(item.get("action", ""))
            source = str(item.get("source", ""))
            destination_value = item.get("destination")
            destination = str(destination_value) if destination_value is not None else None
            reason = str(item.get("reason", "")).strip()
            try:
                confidence = float(item.get("confidence"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid confidence for {source!r}") from exc

            if kind not in {"MOVE", "ARCHIVE", "POSSIBLE_DUPLICATE", "UNCERTAIN", "KEEP"}:
                raise ValueError(f"unsupported action {kind!r}")
            if source not in known:
                raise ValueError(f"unknown source path: {source!r}")
            if source in sources:
                raise ValueError(f"source appears more than once: {source!r}")
            if not reason or not 0 <= confidence <= 1:
                raise ValueError(f"invalid reason or confidence for {source!r}")
            sources.add(source)

            if kind in {"MOVE", "ARCHIVE"}:
                if not destination:
                    raise ValueError(f"{kind} requires a destination for {source!r}")
                safe_path(self.root, destination)
                if destination == source:
                    raise ValueError(f"use KEEP when source and destination are identical: {source!r}")
                if Path(destination).suffix.lower() != Path(source).suffix.lower():
                    raise ValueError(f"renaming must preserve the file extension: {source!r}")
                if kind == "ARCHIVE" and Path(destination).parts[0].casefold() != "archive":
                    raise ValueError(f"ARCHIVE destination must be inside Archive/: {destination!r}")
                if destination in destinations:
                    raise ValueError(f"two actions use the same destination: {destination!r}")
                if destination in known and destination != source:
                    raise ValueError(f"destination already exists: {destination!r}")
                destinations.add(destination)
                parent = Path(destination).parent
                while parent != Path("."):
                    parent_relative = parent.as_posix()
                    if parent_relative in known:
                        raise ValueError(f"a file blocks destination directory: {parent_relative!r}")
                    directories.add(parent_relative)
                    parent = parent.parent
            elif destination is not None:
                raise ValueError(f"{kind} must use destination=null for {source!r}")

            planned.append(Action(
                kind=kind,
                source=source,
                destination=destination,
                reason=reason,
                confidence=confidence,
                sha256=file_hash(safe_file(self.root, source)),
            ))

        missing = sorted(known - sources, key=str.casefold)
        if missing:
            raise ValueError(f"plan does not account for: {missing}")

        creates = [
            Action("CREATE", None, directory, "Required by approved plan", 1.0)
            for directory in sorted(directories, key=lambda value: (len(Path(value).parts), value.casefold()))
        ]
        return AgentOutcome(creates + planned, summary)

    def _record(
        self,
        step: int,
        tool: str,
        arguments: dict[str, Any],
        status: str,
        summary: str,
    ) -> None:
        self.trace.append({
            "step": step,
            "tool": tool,
            "arguments": arguments,
            "status": status,
            "summary": summary,
        })


def _file_arguments(arguments: dict[str, Any], minimum: int) -> list[str]:
    files = arguments.get("files")
    if not isinstance(files, list) or len(files) < minimum or not all(isinstance(item, str) for item in files):
        raise ValueError(f"files must contain at least {minimum} relative path(s)")
    if len(files) > MAX_TOOL_FILES:
        raise ValueError(f"a tool call may inspect at most {MAX_TOOL_FILES} files")
    if len(files) != len(set(files)):
        raise ValueError("files contains duplicate paths")
    return files


def _one_file(arguments: dict[str, Any]) -> str:
    relative = arguments.get("file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("file must be a relative path")
    return relative


def _image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        data = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    elif suffix == ".pdf":
        try:
            import pymupdf
        except ImportError as exc:
            raise ValueError("PyMuPDF is required to inspect scanned PDFs") from exc
        try:
            with pymupdf.open(path) as document:
                if document.page_count == 0:
                    raise ValueError("PDF has no pages")
                data = document[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")
            media_type = "image/png"
        except Exception as exc:
            raise ValueError(f"could not render PDF: {exc}") from exc
    else:
        raise ValueError(f"unsupported visual format: {suffix or '<none>'}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"visual input exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _result_summary(tool: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if tool == "list_files":
        return f"listed {result.get('count', 0)} files"
    if tool == "inspect_metadata":
        return f"inspected metadata for {len(result.get('files', []))} files"
    if tool == "read_file":
        content = result.get("content")
        return f"returned {len(content) if isinstance(content, str) else 0} preview characters"
    if tool == "inspect_image":
        return "returned a visual description"
    if tool == "find_duplicates":
        return f"found {len(result.get('exact_duplicate_groups', []))} duplicate groups"
    return "tool completed"

