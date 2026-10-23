import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE_ROOT / ".agents/skills/context-rail/scripts/context_rail.py"
SPEC = importlib.util.spec_from_file_location("context_rail_under_test", SCRIPT)
CONTEXT_RAIL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTEXT_RAIL
SPEC.loader.exec_module(CONTEXT_RAIL)


def chain_block(upstream=None, downstream=None, sync=None):
    sections = []
    for label, pointers in (("上游", upstream), ("按需下游", downstream), ("同步对象", sync)):
        if pointers is None:
            continue
        lines = [f"**{label}**"]
        for target, heading, reason in pointers:
            lines.append(f"- `{target} §{heading}` — {reason}")
        sections.append("\n".join(lines))
    return "\n".join(["## 加载链", "", "<!-- context-rail:start -->", "\n\n".join(sections), "<!-- context-rail:end -->", ""])


class ContextRailCliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_repo(self, directory):
        root = Path(directory)
        projects = root / "workspace/projects"
        projects.mkdir(parents=True)
        config = {
            "version": 1,
            "roots": ["workspace/projects"],
            "entrypoints": ["workspace/projects/_index.md"],
            "exclude": [],
            "max_depth": 12,
        }
        (root / ".context-rail.json").write_text(json.dumps(config), encoding="utf-8")
        return root, projects

    def write_valid_chain(self, root, projects):
        project = projects / "alpha"
        project.mkdir()
        index = "# 项目索引\n\n## 项目目录\n\n" + chain_block(
            downstream=[("workspace/projects/alpha/_overview.md", "项目概览", "进入 Alpha 项目时读取")]
        )
        overview = "# Alpha\n\n## 项目概览\n\n项目说明。\n\n" + chain_block(
            upstream=[("workspace/projects/_index.md", "项目目录", "从项目索引进入 Alpha")]
        )
        (projects / "_index.md").write_text(index, encoding="utf-8")
        (project / "_overview.md").write_text(overview, encoding="utf-8")

    def test_init_is_non_destructive_and_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "# Existing\n")
            self.assertTrue((root / ".agents/skills/context-rail/SKILL.md").exists())
            checked = subprocess.run(
                [str(root / "context-rail"), "check", "--root", str(root)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_init_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "workspace").symlink_to(outside, target_is_directory=True)
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("symlink", result.stderr.lower())
            self.assertFalse((outside / "projects/_index.md").exists())
            self.assertFalse((root / ".context-rail.json").exists())

    def test_init_rejects_incomplete_existing_skill_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".agents/skills/context-rail").mkdir(parents=True)
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("incomplete", result.stderr)
            self.assertFalse((root / "context-rail").exists())
            self.assertFalse((root / ".context-rail.json").exists())

    def test_init_rejects_conflicting_or_non_executable_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "context-rail"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("conflicts", result.stderr)
            launcher.write_text(CONTEXT_RAIL.LAUNCHER_TEMPLATE, encoding="utf-8")
            launcher.chmod(0o401)
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("not executable", result.stderr)
            self.assertFalse((root / ".context-rail.json").exists())

    def test_init_rejects_unmanaged_existing_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "workspace/projects/_index.md"
            index.parent.mkdir(parents=True)
            index.write_text("# Existing project list\n", encoding="utf-8")
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("not managed", result.stderr)
            self.assertFalse((root / ".context-rail.json").exists())

    def test_init_rejects_existing_config_with_missing_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".context-rail.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roots": ["archive"],
                        "entrypoints": ["archive/_index.md"],
                        "exclude": [],
                        "max_depth": 12,
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_cli("init", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing entrypoint", result.stderr)
            self.assertFalse((root / "context-rail").exists())

    def test_valid_bidirectional_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            self.write_valid_chain(root, projects)
            result = self.run_cli("check", "--root", str(root), "--strict")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_requires_roots_and_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".context-rail.json").write_text(
                json.dumps({"version": 1, "roots": [], "entrypoints": [], "exclude": [], "max_depth": 12}),
                encoding="utf-8",
            )
            result = self.run_cli("check", "--root", str(root))
            self.assertEqual(result.returncode, 2)
            self.assertIn("may not be empty", result.stderr)

    def test_config_rejects_boolean_version_and_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for config in (
                {"version": True, "roots": ["docs"], "entrypoints": ["docs/index.md"], "exclude": [], "max_depth": 12},
                {"version": 1, "roots": ["docs"], "entrypoints": ["docs/index.md"], "exclude": [], "max_depth": True},
            ):
                (root / ".context-rail.json").write_text(json.dumps(config), encoding="utf-8")
                result = self.run_cli("check", "--root", str(root))
                self.assertEqual(result.returncode, 2)

    def test_config_rejects_non_normalized_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            (root / ".context-rail.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "roots": ["workspace/projects"],
                        "entrypoints": ["workspace//projects/_index.md"],
                        "exclude": [],
                        "max_depth": 12,
                    }
                ),
                encoding="utf-8",
            )
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            result = self.run_cli("graph", "--root", str(root), "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("non-normalized", result.stderr)

    def test_missing_target_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            text = "# 项目索引\n\n## 项目目录\n\n" + chain_block(
                downstream=[("workspace/projects/missing.md", "概览", "进入缺失项目")]
            )
            (projects / "_index.md").write_text(text, encoding="utf-8")
            result = self.run_cli("check", "--root", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertIn("TARGET_MISSING", result.stdout)

    def test_ambiguous_heading_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            self.write_valid_chain(root, projects)
            overview = projects / "alpha/_overview.md"
            overview.write_text(overview.read_text(encoding="utf-8") + "\n## 项目概览\n", encoding="utf-8")
            result = self.run_cli("check", "--root", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertIn("HEADING_AMBIGUOUS", result.stdout)

    def test_commonmark_headings_are_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            project = projects / "alpha"
            project.mkdir()
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n"
                + chain_block(
                    downstream=[
                        ("workspace/projects/alpha/_overview.md", "C#", "进入 Alpha"),
                        ("workspace/projects/alpha/setext.md", "Setext 标题", "读取 Setext 文档"),
                    ]
                ),
                encoding="utf-8",
            )
            (project / "_overview.md").write_text(
                "# Alpha\n\n   ## C#\n\n"
                + chain_block(upstream=[("workspace/projects/_index.md", "项目目录", "从索引进入")]),
                encoding="utf-8",
            )
            (project / "setext.md").write_text(
                "Setext 标题\n-----------\n\n"
                + chain_block(upstream=[("workspace/projects/_index.md", "项目目录", "从索引进入")]),
                encoding="utf-8",
            )
            result = self.run_cli("check", "--root", str(root), "--strict")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_markers_inside_fence_do_not_manage_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            (projects / "example.md").write_text(
                "# Example\n\n```markdown\n<!-- context-rail:start -->\n**上游**\n<!-- context-rail:end -->\n```\n",
                encoding="utf-8",
            )
            result = self.run_cli("check", "--root", str(root), "--format", "json")
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            codes = [issue["code"] for issue in payload["issues"]]
            self.assertIn("UNMANAGED", codes)
            self.assertNotIn("NO_UPSTREAM", codes)
            self.assertNotIn("UNREACHABLE", codes)

    def test_markers_inside_indented_code_do_not_manage_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            (projects / "example.md").write_text(
                "# Example\n\n    <!-- context-rail:start -->\n    **上游**\n    <!-- context-rail:end -->\n",
                encoding="utf-8",
            )
            result = self.run_cli("check", "--root", str(root), "--format", "json")
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            codes = [issue["code"] for issue in payload["issues"]]
            self.assertIn("UNMANAGED", codes)
            self.assertNotIn("NO_UPSTREAM", codes)

    def test_hash_only_atx_heading_is_not_a_target(self):
        lines = ["## ###"]
        self.assertEqual(CONTEXT_RAIL.parse_headings(lines), {})

    def test_excluded_malformed_subtree_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            config = json.loads((root / ".context-rail.json").read_text(encoding="utf-8"))
            config["exclude"] = ["**/excluded/**"]
            (root / ".context-rail.json").write_text(json.dumps(config), encoding="utf-8")
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            excluded = projects / "excluded"
            excluded.mkdir()
            (excluded / "broken.md").write_text(
                "<!-- context-rail:start -->\n- malformed\n<!-- context-rail:end -->\n", encoding="utf-8"
            )
            result = self.run_cli("check", "--root", str(root), "--strict")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unreadable_unexcluded_subtree_fails_instead_of_passing_cleanly(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission semantics required")
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            restricted = projects / "restricted"
            restricted.mkdir()
            (restricted / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
            restricted.chmod(0)
            try:
                result = self.run_cli("check", "--root", str(root), "--strict")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("I/O error", result.stderr)
            finally:
                restricted.chmod(0o700)

    def test_unreadable_excluded_subtree_is_pruned_without_error(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX permission semantics required")
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            config = json.loads((root / ".context-rail.json").read_text(encoding="utf-8"))
            config["exclude"] = ["**/restricted/**"]
            (root / ".context-rail.json").write_text(json.dumps(config), encoding="utf-8")
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n" + chain_block(downstream=[]), encoding="utf-8"
            )
            restricted = projects / "restricted"
            restricted.mkdir()
            (restricted / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
            restricted.chmod(0)
            try:
                result = self.run_cli("check", "--root", str(root), "--strict")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                restricted.chmod(0o700)

    def test_missing_backlink_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            project = projects / "alpha"
            project.mkdir()
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n"
                + chain_block(downstream=[("workspace/projects/alpha/_overview.md", "项目概览", "进入 Alpha")]),
                encoding="utf-8",
            )
            (project / "_overview.md").write_text(
                "# Alpha\n\n## 项目概览\n\n" + chain_block(), encoding="utf-8"
            )
            result = self.run_cli("check", "--root", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertIn("BACKLINK_MISSING", result.stdout)

    def test_cycle_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            project = projects / "alpha"
            project.mkdir()
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n"
                + chain_block(
                    upstream=[("workspace/projects/alpha/_overview.md", "项目概览", "循环入口")],
                    downstream=[("workspace/projects/alpha/_overview.md", "项目概览", "进入 Alpha")],
                ),
                encoding="utf-8",
            )
            (project / "_overview.md").write_text(
                "# Alpha\n\n## 项目概览\n\n"
                + chain_block(
                    upstream=[("workspace/projects/_index.md", "项目目录", "从索引进入")],
                    downstream=[("workspace/projects/_index.md", "项目目录", "返回索引")],
                ),
                encoding="utf-8",
            )
            result = self.run_cli("check", "--root", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertIn("CYCLE", result.stdout)

    def test_graph_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            self.write_valid_chain(root, projects)
            result = self.run_cli("graph", "--root", str(root), "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["nodes"]), 2)
            self.assertEqual(len([edge for edge in payload["edges"] if edge["relation"] == "downstream"]), 1)

    def test_cycle_detection_handles_deep_graph(self):
        size = 2000
        graph = {f"node-{index}": [f"node-{index + 1}"] for index in range(size - 1)}
        graph[f"node-{size - 1}"] = []
        self.assertEqual(CONTEXT_RAIL.find_cycles(graph), [])
        graph[f"node-{size - 1}"] = ["node-0"]
        cycles = CONTEXT_RAIL.find_cycles(graph)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0][0], "node-0")
        self.assertEqual(cycles[0][-1], "node-0")

    def test_text_graph_handles_deep_graph(self):
        size = 2000
        graph = {f"node-{index}": [f"node-{index + 1}"] for index in range(size - 1)}
        graph[f"node-{size - 1}"] = []
        analysis = CONTEXT_RAIL.Analysis(
            root=Path("."),
            config={"entrypoints": ["node-0"]},
            documents={},
            issues=[],
            reachable=set(graph),
            downstream=graph,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            CONTEXT_RAIL.print_text_graph(analysis)
        self.assertIn(f"node-{size - 1}", output.getvalue())

    def test_embedded_agents_template_matches_shipped_file(self):
        shipped = (PACKAGE_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(CONTEXT_RAIL.AGENTS_TEMPLATE, shipped)

    def test_rename_detects_existing_old_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            self.write_valid_chain(root, projects)
            result = self.run_cli(
                "check",
                "--root",
                str(root),
                "--rename",
                "workspace/projects/_index.md=workspace/projects/alpha/_overview.md",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("OLD_PATH_EXISTS", result.stdout)

    def test_rename_passes_after_complete_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root, projects = self.make_repo(directory)
            project = projects / "alpha"
            project.mkdir()
            new_path = "workspace/projects/alpha/overview.md"
            (projects / "_index.md").write_text(
                "# 项目索引\n\n## 项目目录\n\n"
                + chain_block(downstream=[(new_path, "项目概览", "进入 Alpha")]),
                encoding="utf-8",
            )
            (project / "overview.md").write_text(
                "# Alpha\n\n## 项目概览\n\n"
                + chain_block(upstream=[("workspace/projects/_index.md", "项目目录", "从索引进入")]),
                encoding="utf-8",
            )
            result = self.run_cli(
                "check",
                "--root",
                str(root),
                "--rename",
                f"workspace/projects/alpha/_overview.md={new_path}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
