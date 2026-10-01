from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from file_organizer.file_tools import (
    exact_duplicates,
    execute,
    file_hash,
    find_duplicate_groups,
    list_files,
    scan,
    validate_target,
)
from file_organizer.llm import _AgentRuntime
from file_organizer.models import Action


class OrganizerTests(unittest.TestCase):
    def test_exact_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "report.pdf").write_bytes(b"same")
            (root / "report (1).pdf").write_bytes(b"same")
            self.assertEqual(exact_duplicates(scan(root)), {"report (1).pdf": "report.pdf"})

    def test_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            source.write_text("source")
            (root / "Topic").mkdir()
            (root / "Topic" / "source.txt").write_text("existing")
            action = Action("MOVE", "source.txt", "Topic/source.txt", "test", 1, file_hash(source))
            result = execute([action], root, lambda *_: None)[0]
            self.assertEqual(result.status, "SKIP")
            self.assertEqual(source.read_text(), "source")

    def test_changed_source_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            source.write_text("before")
            old_hash = file_hash(source)
            (root / "Topic").mkdir()
            source.write_text("after")
            action = Action("MOVE", "source.txt", "Topic/source.txt", "test", 1, old_hash)
            self.assertEqual(execute([action], root, lambda *_: None)[0].status, "SKIP")

    def test_target_must_be_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = root / "sandbox"
            sandbox.mkdir()
            self.assertEqual(validate_target(sandbox, root), sandbox.resolve())
            with self.assertRaises(ValueError):
                validate_target(root, root)

    def test_lists_and_hashes_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nested").mkdir()
            (root / "report.txt").write_text("same")
            (root / "nested" / "copy.txt").write_text("same")

            self.assertEqual(list_files(root), ["nested/copy.txt", "report.txt"])
            self.assertEqual(
                find_duplicate_groups(root, list_files(root)),
                [["nested/copy.txt", "report.txt"]],
            )

    def test_plan_supports_nested_folders_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "IMG_8291.jpg"
            source.write_bytes(b"image")
            runtime = object.__new__(_AgentRuntime)
            runtime.root = root

            outcome = runtime._submit_plan({
                "summary": "Organize the project image",
                "actions": [{
                    "action": "MOVE",
                    "source": "IMG_8291.jpg",
                    "destination": "Projects/Alpha/architecture_diagram.jpg",
                    "reason": "The image is an architecture diagram for Alpha",
                    "confidence": 0.9,
                }],
            })

            self.assertEqual(
                [action.destination for action in outcome.actions[:2]],
                ["Projects", "Projects/Alpha"],
            )
            results = execute(outcome.actions, root, lambda *_: None)
            self.assertTrue(all(result.status == "SUCCESS" for result in results))
            self.assertTrue((root / "Projects" / "Alpha" / "architecture_diagram.jpg").is_file())
            self.assertFalse(source.exists())

    def test_plan_must_account_for_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.txt").write_text("one")
            (root / "two.txt").write_text("two")
            runtime = object.__new__(_AgentRuntime)
            runtime.root = root

            with self.assertRaisesRegex(ValueError, "plan does not account for"):
                runtime._submit_plan({
                    "summary": "Incomplete plan",
                    "actions": [{
                        "action": "KEEP",
                        "source": "one.txt",
                        "destination": None,
                        "reason": "Already organized",
                        "confidence": 1.0,
                    }],
                })

    def test_agent_chooses_tools_then_submits_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("ambiguous")
            calls = [
                _response(_tool_call("call-1", "list_files", {})),
                _response(_tool_call("call-2", "submit_plan", {
                    "summary": "The file needs manual review",
                    "actions": [{
                        "action": "UNCERTAIN",
                        "source": "notes.txt",
                        "destination": None,
                        "reason": "The filename alone is insufficient",
                        "confidence": 0.4,
                    }],
                })),
            ]
            client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **_: calls.pop(0)),
                ),
            )

            outcome = _AgentRuntime(root, client).run("Organize this directory")

            self.assertEqual(outcome.llm_calls, 2)
            self.assertEqual([event["tool"] for event in outcome.trace], ["list_files", "submit_plan"])
            self.assertEqual(outcome.actions[0].kind, "UNCERTAIN")


def _tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(tool_call: SimpleNamespace) -> SimpleNamespace:
    message = SimpleNamespace(
        tool_calls=[tool_call],
        model_dump=lambda **_: {
            "role": "assistant",
            "tool_calls": [{
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }],
        },
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


if __name__ == "__main__":
    unittest.main()
