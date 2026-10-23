#!/usr/bin/env python3
"""ContextRail: initialize, validate, and display Markdown loading chains."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


CONFIG_NAME = ".context-rail.json"
START_MARKER = "<!-- context-rail:start -->"
END_MARKER = "<!-- context-rail:end -->"
DEFAULT_CONFIG = {
    "version": 1,
    "roots": ["workspace/projects"],
    "entrypoints": ["workspace/projects/_index.md"],
    "exclude": [
        "**/.git/**",
        "**/.agents/**",
        "**/node_modules/**",
        "**/.venv/**",
    ],
    "max_depth": 12,
}

RELATION_LABELS = {
    "上游": "upstream",
    "按需下游": "downstream",
    "同步对象": "sync",
}

ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
SETEXT_HEADING_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
RELATION_RE = re.compile(r"^\s*\*\*(上游|按需下游|同步对象)[：:]?\*\*\s*$")
POINTER_RE = re.compile(r"^\s*-\s+`([^`]+)`\s+[—-]\s+(.+?)\s*$")
VAGUE_REASON_RE = re.compile(r"^(相关时|需要时|参见文件|按需|相关任务时)[。.]?$")


@dataclass(frozen=True)
class Pointer:
    relation: str
    target: str
    heading: str
    reason: str
    line: int


@dataclass
class Document:
    path: str
    headings: Dict[str, List[int]]
    pointers: List[Pointer] = field(default_factory=list)
    managed: bool = False

    def pointers_for(self, relation: str) -> List[Pointer]:
        return [pointer for pointer in self.pointers if pointer.relation == relation]


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    line: int
    message: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "message": self.message,
        }


@dataclass
class Analysis:
    root: Path
    config: Dict[str, object]
    documents: Dict[str, Document]
    issues: List[Issue]
    reachable: Set[str]
    downstream: Dict[str, List[str]]


class ContextRailError(Exception):
    pass


def posix_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def fenced_line_mask(lines: Sequence[str]) -> List[bool]:
    """Return a mask for lines inside fenced code blocks, including fences."""
    mask = [False] * len(lines)
    fence_character = ""
    fence_length = 0
    for index, line in enumerate(lines):
        if not fence_character:
            match = FENCE_OPEN_RE.match(line)
            if not match:
                continue
            marker = match.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            mask[index] = True
            continue
        mask[index] = True
        closing = re.compile(rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$")
        if closing.match(line):
            fence_character = ""
            fence_length = 0
    return mask


def parse_headings(lines: Sequence[str], fenced: Optional[Sequence[bool]] = None) -> Dict[str, List[int]]:
    headings: Dict[str, List[int]] = {}
    fenced = list(fenced) if fenced is not None else fenced_line_mask(lines)
    previous: Optional[Tuple[str, int]] = None
    for index, line in enumerate(lines):
        number = index + 1
        if fenced[index]:
            previous = None
            continue
        match = ATX_HEADING_RE.match(line)
        if match:
            raw_title = match.group(2)
            if re.fullmatch(r"#+[ \t]*", raw_title):
                title = ""
            else:
                title = re.sub(r"[ \t]+#+[ \t]*$", "", raw_title).strip()
            if title:
                headings.setdefault(title, []).append(number)
            previous = None
            continue
        if SETEXT_HEADING_RE.match(line) and previous is not None:
            title, title_line = previous
            headings.setdefault(title, []).append(title_line)
            previous = None
            continue
        stripped = line.strip()
        if stripped and not line.startswith("    ") and not line.startswith("\t"):
            previous = (stripped, number)
        else:
            previous = None
    return headings


def parse_pointer_token(token: str) -> Optional[Tuple[str, str]]:
    parts = re.split(r"\s+§\s*", token.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    target, heading = (part.strip() for part in parts)
    if not target or not heading:
        return None
    return target, heading


def parse_document(path: Path, root: Path) -> Tuple[Document, List[Issue]]:
    relative = posix_path(path, root)
    issues: List[Issue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        document = Document(path=relative, headings={})
        issues.append(Issue("error", "UTF8", relative, 1, "file is not valid UTF-8"))
        return document, issues

    lines = text.splitlines()
    fenced = fenced_line_mask(lines)
    document = Document(path=relative, headings=parse_headings(lines, fenced))
    starts = [
        index
        for index, line in enumerate(lines)
        if not fenced[index]
        and not line.startswith("    ")
        and not line.startswith("\t")
        and line.strip() == START_MARKER
    ]
    ends = [
        index
        for index, line in enumerate(lines)
        if not fenced[index]
        and not line.startswith("    ")
        and not line.startswith("\t")
        and line.strip() == END_MARKER
    ]

    if not starts and not ends:
        return document, issues
    document.managed = True
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        issues.append(
            Issue(
                "error",
                "MARKERS",
                relative,
                (starts[0] + 1) if starts else 1,
                "expected exactly one ordered context-rail:start/end marker pair",
            )
        )
        return document, issues

    current_relation: Optional[str] = None
    seen_relations: Set[str] = set()
    for index in range(starts[0] + 1, ends[0]):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        relation_match = RELATION_RE.match(stripped)
        if relation_match:
            label = relation_match.group(1)
            current_relation = RELATION_LABELS[label]
            if current_relation in seen_relations:
                issues.append(
                    Issue("error", "DUPLICATE_SECTION", relative, index + 1, f"duplicate relation section: {label}")
                )
            seen_relations.add(current_relation)
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if stripped.startswith("-"):
            if current_relation is None:
                issues.append(
                    Issue("error", "POINTER_SECTION", relative, index + 1, "pointer appears before a relation heading")
                )
                continue
            pointer_match = POINTER_RE.match(line)
            if not pointer_match:
                issues.append(
                    Issue(
                        "error",
                        "POINTER_FORMAT",
                        relative,
                        index + 1,
                        "expected: - `path §heading` — concrete reason",
                    )
                )
                continue
            parsed = parse_pointer_token(pointer_match.group(1))
            if parsed is None:
                issues.append(
                    Issue("error", "POINTER_TARGET", relative, index + 1, "pointer must contain path and § heading")
                )
                continue
            target, heading = parsed
            reason = pointer_match.group(2).strip()
            document.pointers.append(Pointer(current_relation, target, heading, reason, index + 1))
            continue
        issues.append(
            Issue(
                "error",
                "BLOCK_CONTENT",
                relative,
                index + 1,
                "loading-chain block may contain only relation headings, pointers, comments, and blank lines",
            )
        )
    return document, issues


def normalize_config(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        raise ContextRailError("configuration must be a JSON object")
    config = dict(DEFAULT_CONFIG)
    config.update(raw)
    if type(config.get("version")) is not int or config.get("version") != 1:
        raise ContextRailError("only configuration version 1 is supported")
    for key in ("roots", "entrypoints", "exclude"):
        value = config.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ContextRailError(f"configuration field {key!r} must be a list of non-empty strings")
        if key in ("roots", "entrypoints") and not value:
            raise ContextRailError(f"configuration field {key!r} may not be empty")
    for key in ("roots", "entrypoints"):
        normalized_values = []
        for value in config[key]:
            normalized = safe_repo_path(value)
            if normalized is None or normalized != value:
                raise ContextRailError(f"configuration field {key!r} contains a non-normalized path: {value}")
            normalized_values.append(normalized)
        config[key] = normalized_values
    max_depth = config.get("max_depth")
    if type(max_depth) is not int or max_depth < 1:
        raise ContextRailError("configuration field 'max_depth' must be a positive integer")
    return config


def load_config(root: Path) -> Dict[str, object]:
    config_path = root / CONFIG_NAME
    if not config_path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextRailError(f"cannot read {CONFIG_NAME}: {error}") from error
    return normalize_config(raw)


def safe_repo_path(value: str) -> Optional[str]:
    if "\\" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        return None
    return pure.as_posix()


def excluded(relative: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def excluded_directory(relative: str, patterns: Iterable[str]) -> bool:
    probe = relative.rstrip("/") + "/__context_rail_probe__"
    return excluded(relative, patterns) or excluded(probe, patterns)


def raise_walk_error(error: OSError) -> None:
    """Prevent os.walk from silently skipping unreadable, unexcluded directories."""
    raise error


def scan_documents(root: Path, config: Dict[str, object]) -> Tuple[Dict[str, Document], List[Issue]]:
    documents: Dict[str, Document] = {}
    issues: List[Issue] = []
    patterns = list(config["exclude"])
    for root_value in config["roots"]:
        normalized = safe_repo_path(root_value)
        if normalized is None:
            issues.append(Issue("error", "CONFIG_PATH", CONFIG_NAME, 1, f"invalid root path: {root_value}"))
            continue
        scan_root = (root / normalized).resolve()
        if not is_within(scan_root, root):
            issues.append(Issue("error", "CONFIG_PATH", CONFIG_NAME, 1, f"root escapes repository: {root_value}"))
            continue
        if not scan_root.exists():
            issues.append(Issue("error", "ROOT_MISSING", CONFIG_NAME, 1, f"configured root does not exist: {root_value}"))
            continue
        if not scan_root.is_dir():
            issues.append(Issue("error", "ROOT_TYPE", CONFIG_NAME, 1, f"configured root is not a directory: {root_value}"))
            continue
        for directory, directory_names, file_names in os.walk(
            scan_root, topdown=True, onerror=raise_walk_error, followlinks=False
        ):
            directory_path = Path(directory)
            kept_directories = []
            for name in sorted(directory_names):
                candidate = directory_path / name
                relative_directory = posix_path(candidate, root)
                if candidate.is_symlink() or excluded_directory(relative_directory, patterns):
                    continue
                kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in sorted(file_names):
                if not name.endswith(".md"):
                    continue
                path = directory_path / name
                relative = posix_path(path, root)
                if excluded(relative, patterns):
                    continue
                resolved = path.resolve()
                if not is_within(resolved, root) or not is_within(resolved, scan_root):
                    issues.append(
                        Issue("error", "SYMLINK_ESCAPE", relative, 1, "Markdown path resolves outside its configured root")
                    )
                    continue
                document, parse_issues = parse_document(path, root)
                documents[relative] = document
                issues.extend(parse_issues)
    return documents, issues


def find_cycles(graph: Dict[str, List[str]]) -> List[List[str]]:
    state: Dict[str, int] = {}
    cycles: List[List[str]] = []
    seen_signatures: Set[Tuple[str, ...]] = set()

    def canonical(cycle: List[str]) -> Tuple[str, ...]:
        body = cycle[:-1]
        smallest = min(body)
        index = body.index(smallest)
        return tuple(body[index:] + body[:index])

    for start_node in graph:
        if state.get(start_node, 0) != 0:
            continue
        state[start_node] = 1
        path = [start_node]
        positions = {start_node: 0}
        stack: List[Tuple[str, object]] = [(start_node, iter(graph.get(start_node, [])))]
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                stack.pop()
                state[node] = 2
                positions.pop(node, None)
                path.pop()
                continue
            if child not in graph:
                continue
            child_state = state.get(child, 0)
            if child_state == 0:
                state[child] = 1
                positions[child] = len(path)
                path.append(child)
                stack.append((child, iter(graph.get(child, []))))
            elif child_state == 1:
                cycle = path[positions[child] :] + [child]
                signature = canonical(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(cycle)
    return cycles


def calculate_reachable(entrypoints: Sequence[str], graph: Dict[str, List[str]]) -> Tuple[Set[str], Dict[str, int]]:
    reachable: Set[str] = set()
    depths: Dict[str, int] = {}
    queue = deque((entrypoint, 0) for entrypoint in entrypoints)
    while queue:
        node, depth = queue.popleft()
        if node in reachable and depth >= depths[node]:
            continue
        reachable.add(node)
        depths[node] = depth
        for child in graph.get(node, []):
            queue.append((child, depth + 1))
    return reachable, depths


def analyze(root: Path) -> Analysis:
    root = root.resolve()
    if not root.exists() or not root.is_dir():
        raise ContextRailError(f"repository root is not a directory: {root}")
    config = load_config(root)
    documents, issues = scan_documents(root, config)
    entrypoints: List[str] = []
    for value in config["entrypoints"]:
        normalized = safe_repo_path(value)
        if normalized is None:
            issues.append(Issue("error", "CONFIG_PATH", CONFIG_NAME, 1, f"invalid entrypoint path: {value}"))
            continue
        entrypoints.append(normalized)
        document = documents.get(normalized)
        if document is None:
            issues.append(Issue("error", "ENTRYPOINT_MISSING", CONFIG_NAME, 1, f"entrypoint is not scanned: {normalized}"))
        elif not document.managed:
            issues.append(Issue("error", "ENTRYPOINT_UNMANAGED", normalized, 1, "entrypoint lacks ContextRail markers"))

    managed = {path: doc for path, doc in documents.items() if doc.managed}
    for path, document in documents.items():
        if not document.managed:
            issues.append(Issue("warning", "UNMANAGED", path, 1, "Markdown file is not managed by ContextRail"))

    pointer_keys: Set[Tuple[str, str, str, str]] = set()
    downstream: Dict[str, List[str]] = {path: [] for path in managed}
    for source, document in managed.items():
        if source not in entrypoints and not document.pointers_for("upstream"):
            issues.append(Issue("error", "NO_UPSTREAM", source, 1, "non-entrypoint document has no upstream pointer"))
        for pointer in document.pointers:
            key = (source, pointer.relation, pointer.target, pointer.heading)
            if key in pointer_keys:
                issues.append(Issue("warning", "DUPLICATE_POINTER", source, pointer.line, "duplicate pointer"))
            pointer_keys.add(key)

            normalized = safe_repo_path(pointer.target)
            if normalized is None or normalized != pointer.target:
                issues.append(
                    Issue(
                        "error",
                        "PATH_FORMAT",
                        source,
                        pointer.line,
                        f"target must be a normalized repository-relative path: {pointer.target}",
                    )
                )
                continue
            target_doc = documents.get(normalized)
            if target_doc is None:
                issues.append(Issue("error", "TARGET_MISSING", source, pointer.line, f"target is not scanned: {normalized}"))
                continue
            if not target_doc.managed:
                issues.append(Issue("error", "TARGET_UNMANAGED", source, pointer.line, f"target is not managed: {normalized}"))
                continue
            heading_lines = target_doc.headings.get(pointer.heading, [])
            if not heading_lines:
                issues.append(
                    Issue("error", "HEADING_MISSING", source, pointer.line, f"heading not found in {normalized}: {pointer.heading}")
                )
            elif len(heading_lines) > 1:
                issues.append(
                    Issue(
                        "error",
                        "HEADING_AMBIGUOUS",
                        source,
                        pointer.line,
                        f"heading occurs {len(heading_lines)} times in {normalized}: {pointer.heading}",
                    )
                )
            if VAGUE_REASON_RE.match(pointer.reason):
                issues.append(Issue("warning", "VAGUE_REASON", source, pointer.line, f"pointer reason is too vague: {pointer.reason}"))
            if normalized == source and pointer.relation != "sync":
                issues.append(Issue("error", "SELF_LINK", source, pointer.line, f"{pointer.relation} pointer targets its own file"))
            if pointer.relation == "downstream":
                downstream[source].append(normalized)

    for source, document in managed.items():
        for pointer in document.pointers_for("downstream"):
            target_doc = managed.get(pointer.target)
            if target_doc and not any(item.target == source for item in target_doc.pointers_for("upstream")):
                issues.append(
                    Issue(
                        "error",
                        "BACKLINK_MISSING",
                        source,
                        pointer.line,
                        f"downstream target lacks upstream backlink: {pointer.target}",
                    )
                )
        for pointer in document.pointers_for("upstream"):
            parent_doc = managed.get(pointer.target)
            if parent_doc and not any(item.target == source for item in parent_doc.pointers_for("downstream")):
                issues.append(
                    Issue(
                        "error",
                        "FORWARD_LINK_MISSING",
                        source,
                        pointer.line,
                        f"upstream target lacks downstream pointer back to this file: {pointer.target}",
                    )
                )

    for cycle in find_cycles(downstream):
        issues.append(Issue("error", "CYCLE", cycle[0], 1, "downstream cycle: " + " -> ".join(cycle)))

    reachable, depths = calculate_reachable(entrypoints, downstream)
    for path in managed:
        if path not in reachable:
            issues.append(Issue("error", "UNREACHABLE", path, 1, "managed document is unreachable from configured entrypoints"))
    max_depth = int(config["max_depth"])
    for path, depth in depths.items():
        if depth > max_depth:
            issues.append(
                Issue("warning", "DEPTH", path, 1, f"document depth {depth} exceeds configured max_depth {max_depth}")
            )

    issues.sort(key=lambda issue: (issue.path, issue.line, issue.severity, issue.code))
    return Analysis(root, config, documents, issues, reachable, downstream)


def issue_counts(issues: Iterable[Issue]) -> Tuple[int, int]:
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    return errors, warnings


def rename_issues(analysis: Analysis, specifications: Sequence[str]) -> List[Issue]:
    issues: List[Issue] = []
    for specification in specifications:
        if "=" not in specification:
            raise ContextRailError(f"rename must use OLD=NEW: {specification}")
        old_value, new_value = specification.split("=", 1)
        old_path = safe_repo_path(old_value.strip())
        new_path = safe_repo_path(new_value.strip())
        if old_path is None or new_path is None or old_path == new_path:
            raise ContextRailError(f"rename must contain two different normalized repository-relative paths: {specification}")

        old_on_disk = analysis.root / old_path
        if old_on_disk.exists():
            issues.append(Issue("error", "OLD_PATH_EXISTS", old_path, 1, "declared old path still exists after rename"))
        new_document = analysis.documents.get(new_path)
        if new_document is None:
            issues.append(Issue("error", "RENAMED_TARGET_MISSING", CONFIG_NAME, 1, f"declared new path is not scanned: {new_path}"))
        elif not new_document.managed:
            issues.append(Issue("error", "RENAMED_TARGET_UNMANAGED", new_path, 1, "declared new path is not managed"))

        for source, document in analysis.documents.items():
            for pointer in document.pointers:
                if pointer.target == old_path:
                    issues.append(
                        Issue("error", "STALE_PATH", source, pointer.line, f"pointer still references renamed path: {old_path}")
                    )
    return issues


def command_check(args: argparse.Namespace) -> int:
    analysis = analyze(Path(args.root))
    analysis.issues.extend(rename_issues(analysis, args.rename))
    analysis.issues.sort(key=lambda issue: (issue.path, issue.line, issue.severity, issue.code))
    errors, warnings = issue_counts(analysis.issues)
    if args.format == "json":
        payload = {
            "root": str(analysis.root),
            "managed_documents": sum(document.managed for document in analysis.documents.values()),
            "errors": errors,
            "warnings": warnings,
            "issues": [issue.as_dict() for issue in analysis.issues],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for issue in analysis.issues:
            print(f"{issue.severity.upper()} {issue.code} {issue.path}:{issue.line} {issue.message}")
        managed_count = sum(document.managed for document in analysis.documents.values())
        print(f"ContextRail: {managed_count} managed document(s), {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (args.strict and warnings) else 0


def graph_payload(analysis: Analysis) -> Dict[str, object]:
    managed = sorted(path for path, document in analysis.documents.items() if document.managed)
    edges = []
    for source in managed:
        document = analysis.documents[source]
        for pointer in document.pointers:
            edges.append(
                {
                    "source": source,
                    "target": pointer.target,
                    "relation": pointer.relation,
                    "heading": pointer.heading,
                    "reason": pointer.reason,
                }
            )
    return {
        "entrypoints": list(analysis.config["entrypoints"]),
        "nodes": managed,
        "edges": edges,
        "unreachable": sorted(set(managed) - analysis.reachable),
    }


def print_text_graph(analysis: Analysis) -> None:
    visited: Set[str] = set()
    for entrypoint in analysis.config["entrypoints"]:
        active: Set[str] = set()
        stack: List[Tuple[str, str, str]] = [("enter", entrypoint, "")]
        while stack:
            action, node, prefix = stack.pop()
            if action == "exit":
                active.discard(node)
                continue
            marker = ""
            if node in active:
                marker = " [cycle]"
            elif node in visited:
                marker = " [seen]"
            print(prefix + node + marker)
            if marker:
                continue
            visited.add(node)
            active.add(node)
            stack.append(("exit", node, prefix))
            for child in reversed(analysis.downstream.get(node, [])):
                stack.append(("enter", child, prefix + "  -> "))
    for path in sorted(set(analysis.downstream) - visited):
        print(f"[unreachable] {path}")


def command_graph(args: argparse.Namespace) -> int:
    analysis = analyze(Path(args.root))
    payload = graph_payload(analysis)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "dot":
        print("digraph ContextRail {")
        print('  rankdir="LR";')
        for node in payload["nodes"]:
            style = ", shape=box" if node in payload["entrypoints"] else ""
            print(f"  {json.dumps(node)} [label={json.dumps(node)}{style}];")
        for edge in payload["edges"]:
            if edge["relation"] == "upstream":
                continue
            style = "dashed" if edge["relation"] == "sync" else "solid"
            print(
                f"  {json.dumps(edge['source'])} -> {json.dumps(edge['target'])} "
                f"[label={json.dumps(edge['relation'])}, style={style}];"
            )
        print("}")
    else:
        print_text_graph(analysis)
    errors, _warnings = issue_counts(analysis.issues)
    return 1 if errors else 0


AGENTS_TEMPLATE = """# ContextRail 路由

- 项目文档位于 `.context-rail.json` 的 `roots` 目录中。
- 请求涉及进入、继续、查阅或维护项目文档时，调用 `$context-rail`。
- 先读取配置中的入口文件；定位项目后先读该项目的 `_overview.md`。
- 只沿与当前任务匹配的“按需下游”继续读取；信息足够即停止，禁止默认读取整个项目树。
- “同步对象”只表示写入联动，不属于默认读取路径。
- 新建、移动、重命名、拆分或删除受管 Markdown 时，使用 `$context-rail` 同步受影响的双向指针并运行校验。
- 未明确项目且不能从当前路径确定时，询问项目名；不得扫描聊天历史、Codex 会话或个人记忆推断。
- 普通代码修改不触发 ContextRail，除非同时改变受管文档或用户明确要求审计加载链。
"""

INDEX_TEMPLATE = """# 项目索引

这是 ContextRail 的项目入口。项目较多时，可以按年份、客户或业务域增加中间索引。

## 项目目录

<!-- context-rail:start -->
**按需下游**

<!-- context-rail:end -->
"""

LAUNCHER_TEMPLATE = """#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)
exec python3 \"$SCRIPT_DIR/.agents/skills/context-rail/scripts/context_rail.py\" \"$@\"
"""


REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/schema.md",
    "scripts/context_rail.py",
)


def safe_install_target(root: Path, relative: str) -> Path:
    normalized = safe_repo_path(relative)
    if normalized is None or normalized != relative:
        raise ContextRailError(f"unsafe installation path: {relative}")
    current = root
    parts = PurePosixPath(normalized).parts
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise ContextRailError(f"refusing to install through symlink: {current}")
        if current.exists() and index < len(parts) - 1 and not current.is_dir():
            raise ContextRailError(f"installation parent is not a directory: {current}")
    resolved_parent = current.parent.resolve(strict=False)
    if not is_within(resolved_parent, root):
        raise ContextRailError(f"installation path escapes repository: {current}")
    return current


def validate_file_target(path: Path) -> None:
    if path.is_symlink():
        raise ContextRailError(f"refusing to replace symlink: {path}")
    if path.exists() and not path.is_file():
        raise ContextRailError(f"expected a file but found another type: {path}")


def validate_skill_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ContextRailError(f"skill destination exists but is not a real directory: {path}")
    resolved_root = path.resolve()
    missing = []
    unsafe = []
    for relative in REQUIRED_SKILL_FILES:
        candidate = path / relative
        if not candidate.exists() or not candidate.is_file():
            missing.append(relative)
        elif candidate.is_symlink() or not is_within(candidate.resolve(), resolved_root):
            unsafe.append(relative)
    if missing or unsafe:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unsafe:
            details.append("unsafe " + ", ".join(unsafe))
        raise ContextRailError(f"existing ContextRail skill is incomplete: {'; '.join(details)}")


def validate_existing_launcher(path: Path) -> None:
    validate_file_target(path)
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ContextRailError(f"cannot inspect existing launcher {path}: {error}") from error
    if content != LAUNCHER_TEMPLATE:
        raise ContextRailError(f"existing launcher conflicts with ContextRail template: {path}")
    if not os.access(path, os.X_OK):
        raise ContextRailError(f"existing ContextRail launcher is not executable: {path}")


def validate_existing_entrypoint(path: Path, root: Path) -> None:
    validate_file_target(path)
    if not path.exists():
        return
    document, issues = parse_document(path, root)
    if not document.managed:
        raise ContextRailError(f"existing entrypoint is not managed by ContextRail: {posix_path(path, root)}")
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ContextRailError(
            f"existing entrypoint has an invalid loading-chain block: {posix_path(path, root)} ({errors[0].code})"
        )


def write_if_missing(path: Path, content: str, executable: bool = False) -> bool:
    validate_file_target(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    return True


def command_init(args: argparse.Namespace) -> int:
    requested_root = Path(args.root).expanduser()
    requested_root.mkdir(parents=True, exist_ok=True)
    root = requested_root.resolve()
    if not root.is_dir():
        raise ContextRailError(f"target is not a directory: {root}")

    created: List[str] = []
    skipped: List[str] = []

    config_path = safe_install_target(root, CONFIG_NAME)
    agents_path = safe_install_target(root, "AGENTS.md")
    launcher_path = safe_install_target(root, "context-rail")
    for file_target in (config_path, agents_path):
        validate_file_target(file_target)
    validate_existing_launcher(launcher_path)

    config_exists = config_path.exists()
    if config_exists:
        existing_config = load_config(root)
        for entrypoint in existing_config["entrypoints"]:
            entrypoint_path = safe_install_target(root, entrypoint)
            validate_existing_entrypoint(entrypoint_path, root)
            if not entrypoint_path.exists():
                raise ContextRailError(
                    f"existing configuration references a missing entrypoint; create it explicitly: {entrypoint}"
                )
        index_path = None
    else:
        index_path = safe_install_target(root, "workspace/projects/_index.md")
        validate_existing_entrypoint(index_path, root)

    source_skill = Path(__file__).resolve().parent.parent
    validate_skill_tree(source_skill)
    target_skill = safe_install_target(root, ".agents/skills/context-rail")
    try:
        same_skill = source_skill.resolve() == target_skill.resolve()
    except OSError:
        same_skill = False
    if target_skill.exists() or target_skill.is_symlink():
        validate_skill_tree(target_skill)

    config_text = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n"
    (created if write_if_missing(config_path, config_text) else skipped).append(CONFIG_NAME)

    if index_path is not None:
        (created if write_if_missing(index_path, INDEX_TEMPLATE) else skipped).append("workspace/projects/_index.md")

    agents_created = write_if_missing(agents_path, AGENTS_TEMPLATE)
    (created if agents_created else skipped).append("AGENTS.md")

    (created if write_if_missing(launcher_path, LAUNCHER_TEMPLATE, executable=True) else skipped).append("context-rail")

    if same_skill or target_skill.exists():
        skipped.append(".agents/skills/context-rail")
    else:
        target_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_skill, target_skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        created.append(".agents/skills/context-rail")

    print(f"ContextRail initialized at {root}")
    if created:
        print("Created:")
        for value in created:
            print(f"  + {value}")
    if skipped:
        print("Kept existing:")
        for value in skipped:
            print(f"  = {value}")
    if not agents_created:
        print("\nAGENTS.md was not modified. Merge this routing block if it is not already present:\n")
        print(AGENTS_TEMPLATE.rstrip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context-rail", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize ContextRail without overwriting existing files")
    init_parser.add_argument("root", nargs="?", default=".", help="repository root (default: current directory)")
    init_parser.set_defaults(handler=command_init)

    check_parser = subparsers.add_parser("check", help="validate loading-chain structure")
    check_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    check_parser.add_argument("--strict", action="store_true", help="treat warnings as a failing result")
    check_parser.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="fail if a declared old path still exists or is referenced; may be repeated",
    )
    check_parser.add_argument("--format", choices=("text", "json"), default="text")
    check_parser.set_defaults(handler=command_check)

    graph_parser = subparsers.add_parser("graph", help="print the loading-chain graph")
    graph_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    graph_parser.add_argument("--format", choices=("text", "json", "dot"), default="text")
    graph_parser.set_defaults(handler=command_graph)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ContextRailError as error:
        print(f"ContextRail error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"ContextRail I/O error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"ContextRail internal error: {type(error).__name__}: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
