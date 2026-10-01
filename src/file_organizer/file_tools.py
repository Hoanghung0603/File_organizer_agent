from __future__ import annotations

import csv
import hashlib
import mimetypes
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .models import Action, FileInfo, Result

SUPPORTED = {".txt", ".md", ".csv", ".docx", ".pdf"}
MAX_PREVIEW_CHARS = 2_000
MAX_READABLE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def list_files(root: Path) -> list[str]:
    """Return safe relative paths for all regular files below root."""
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file() and not path.is_symlink()
    ]


def scan(root: Path) -> list[FileInfo]:
    files: list[FileInfo] = []
    for relative in list_files(root):
        path = safe_path(root, relative)
        stat = path.stat()
        files.append(FileInfo(
            path=relative,
            extension=path.suffix.lower(),
            size=stat.st_size,
            modified_time=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            sha256=file_hash(path),
        ))
    return files


def inspect_metadata(root: Path, paths: list[str]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for relative in paths:
        path = safe_file(root, relative)
        stat = path.stat()
        result.append({
            "path": relative,
            "extension": path.suffix.lower(),
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "size": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    return result


def exact_duplicates(files: list[FileInfo]) -> dict[str, str]:
    groups: dict[tuple[int, str], list[FileInfo]] = {}
    for item in files:
        groups.setdefault((item.size, item.sha256), []).append(item)
    duplicates: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        original = min(group, key=lambda f: (bool(re.search(r"\(\d+\)", f.path)), f.path.casefold()))
        duplicates.update({item.path: original.path for item in group if item != original})
    return duplicates


def read_preview(path: Path) -> str | None:
    """Read a bounded preview; unsupported, large, or corrupt files return None."""
    if path.suffix.lower() not in SUPPORTED or path.stat().st_size > MAX_READABLE_BYTES:
        return None
    try:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md"}:
            with path.open(encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_PREVIEW_CHARS * 2)
        elif suffix == ".csv":
            rows: list[str] = []
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for index, row in enumerate(csv.reader(handle)):
                    rows.append(" | ".join(row))
                    if index >= 15 or sum(map(len, rows)) >= MAX_PREVIEW_CHARS:
                        break
            text = "\n".join(rows)
        elif suffix == ".docx":
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_READABLE_BYTES:
                    return None
                xml = archive.read(info)
            root = ElementTree.fromstring(xml)
            text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
        else:
            from pypdf import PdfReader
            chunks: list[str] = []
            for page in PdfReader(str(path)).pages[:3]:
                chunks.append(page.extract_text() or "")
                if sum(map(len, chunks)) >= MAX_PREVIEW_CHARS:
                    break
            text = " ".join(chunks)
        return re.sub(r"\s+", " ", text).strip()[:MAX_PREVIEW_CHARS] or None
    except Exception:
        return None


def safe_file(root: Path, relative: str) -> Path:
    path = safe_path(root, relative)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Not a readable regular file: {relative!r}")
    return path


def find_duplicate_groups(root: Path, paths: list[str]) -> list[list[str]]:
    """Return groups of selected files with identical size and SHA-256."""
    groups: dict[tuple[int, str], list[str]] = {}
    for relative in paths:
        path = safe_file(root, relative)
        key = (path.stat().st_size, file_hash(path))
        groups.setdefault(key, []).append(relative)
    return [sorted(group, key=str.casefold) for group in groups.values() if len(group) > 1]


def validate_target(target: Path, allowed_root: Path) -> Path:
    target, allowed_root = target.expanduser().resolve(), allowed_root.expanduser().resolve()
    if not target.is_dir():
        raise ValueError(f"Target is not an existing directory: {target}")
    if target == allowed_root or not target.is_relative_to(allowed_root):
        raise ValueError(f"Target must be a subdirectory of: {allowed_root}")
    return target


def safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    relative_path = Path(relative)
    if (
        not relative
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or not path.is_relative_to(root.resolve())
    ):
        raise ValueError(f"Unsafe path: {relative!r}")
    return path


def execute(actions: list[Action], root: Path, log) -> list[Result]:
    results: list[Result] = []
    for action in actions:
        if action.kind in {"UNCERTAIN", "POSSIBLE_DUPLICATE", "KEEP"}:
            message = "Already organized" if action.kind == "KEEP" else "Review only"
            result = Result(action.kind, action.source, action.destination, "NOT_EXECUTED", message)
        else:
            try:
                destination = safe_path(root, action.destination or "")
                if action.kind == "CREATE":
                    if destination.exists():
                        if destination.is_dir():
                            result = Result(action.kind, None, action.destination, "SUCCESS", "Directory already exists")
                        else:
                            result = Result(action.kind, None, action.destination, "SKIP", "Destination exists and is not a directory")
                    else:
                        destination.mkdir()
                        result = Result(action.kind, None, action.destination, "SUCCESS", "Directory created")
                else:
                    source = safe_path(root, action.source or "")
                    if not source.is_file() or source.is_symlink():
                        result = Result(action.kind, action.source, action.destination, "SKIP", "Source unavailable")
                    elif action.sha256 != file_hash(source):
                        result = Result(action.kind, action.source, action.destination, "SKIP", "Source changed after planning")
                    elif destination.exists():
                        result = Result(action.kind, action.source, action.destination, "SKIP", "Destination exists; overwrite forbidden")
                    elif not destination.parent.is_dir():
                        result = Result(action.kind, action.source, action.destination, "SKIP", "Destination directory unavailable")
                    else:
                        shutil.move(str(source), str(destination))
                        result = Result(action.kind, action.source, action.destination, "SUCCESS", "File moved")
            except Exception as exc:
                result = Result(action.kind, action.source, action.destination, "FAILED", str(exc))
        results.append(result)
        log(result.status, result.__dict__)
    return results


def tree(root: Path) -> str:
    lines = [f"{root.name}/"]

    def walk(folder: Path, prefix: str = "") -> None:
        children = sorted((p for p in folder.iterdir() if not p.is_symlink()), key=lambda p: (not p.is_dir(), p.name.casefold()))
        for index, child in enumerate(children):
            last = index == len(children) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{child.name}{'/' if child.is_dir() else ''}")
            if child.is_dir():
                walk(child, prefix + ("    " if last else "│   "))

    walk(root)
    return "\n".join(lines)
