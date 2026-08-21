from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPOSITORY_ROOT / "update_skills.py"


class UpdateSkillsCliTests(unittest.TestCase):
    def run_updater(
        self,
        workspace: Path,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        return subprocess.run(
            [sys.executable, str(UPDATER), *arguments],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            env=process_environment,
        )

    def create_source_repository(
        self, root: Path, source_skill: str = "source-name"
    ) -> Path:
        repository = root / "source-repository"
        skill_directory = repository / "skills" / source_skill
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_text(
            "---\nname: source-name\n---\n\n# Source skill\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(repository)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Test Author"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-m", "Add source skill"],
            check=True,
            capture_output=True,
        )
        return repository

    def create_fake_gh(self, root: Path, source_repository: Path) -> dict[str, str]:
        binary_directory = root / "bin"
        binary_directory.mkdir()
        fake_gh = binary_directory / "gh"
        fake_gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import subprocess
                import sys

                arguments = sys.argv[1:]
                repository_name = os.environ["FAKE_GH_REPOSITORY"]
                source_repository = os.environ["FAKE_GH_SOURCE"]

                if arguments == ["api", f"repos/{repository_name}"]:
                    print(json.dumps({"visibility": "public", "default_branch": "main"}))
                elif arguments == ["api", f"repos/{repository_name}/commits/main"]:
                    commit = subprocess.run(
                        ["git", "-C", source_repository, "rev-parse", "HEAD"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    print(json.dumps({"sha": commit}))
                elif arguments[:3] == ["repo", "clone", repository_name]:
                    destination = arguments[3]
                    subprocess.run(
                        ["git", "clone", "--no-checkout", source_repository, destination],
                        check=True,
                        capture_output=True,
                    )
                else:
                    print(f"unexpected gh arguments: {arguments!r}", file=sys.stderr)
                    raise SystemExit(64)
                """
            ),
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        return {
            "PATH": f"{binary_directory}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_REPOSITORY": "owner/repository",
            "FAKE_GH_SOURCE": str(source_repository),
        }

    def test_check_reports_no_changes_for_an_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "skills.toml").write_text(
                "# No managed skills yet.\n", encoding="utf-8"
            )

            result = self.run_updater(workspace, "check")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No skill changes.", result.stdout)
            self.assertFalse((workspace / "skills.lock").exists())

    def test_check_rejects_managed_skill_names_that_only_differ_by_case(self) -> None:
        manifest = """
["first/repository"."skills"]
shared-name = "source-one"

["second/repository"."other-skills"]
Shared-Name = "source-two"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")

            result = self.run_updater(workspace, "check")

            self.assertEqual(result.returncode, 2)
            self.assertIn("Managed Skill names must be globally unique", result.stderr)

    def test_check_rejects_a_source_skill_mapped_to_multiple_managed_skills(
        self,
    ) -> None:
        manifest = """
["owner/repository"."skills"]
first-name = "one-source"
second-name = "one-source"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")

            result = self.run_updater(workspace, "check")

            self.assertEqual(result.returncode, 2)
            self.assertIn("Source Skill may have only one Managed Skill", result.stderr)

    def test_check_rejects_source_path_traversal(self) -> None:
        manifest = """
["owner/repository"."../skills"]
safe-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")

            result = self.run_updater(workspace, "check")

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Source Path must be a normalized relative POSIX path", result.stderr
            )

    def test_check_reports_an_add_without_modifying_the_workspace(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)

            result = self.run_updater(workspace, "check", environment=environment)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(
                "ADD managed-name from owner/repository (source: source-name)",
                result.stdout,
            )
            self.assertFalse((workspace / "skills").exists())
            self.assertFalse((workspace / "skills.lock").exists())

    def test_default_command_adds_a_managed_skill_and_writes_its_lock_record(
        self,
    ) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)

            result = self.run_updater(workspace, environment=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ADD managed-name", result.stdout)
            self.assertTrue(
                (workspace / "skills" / "managed-name" / "SKILL.md").is_file()
            )
            with (workspace / "skills.lock").open("rb") as lock_file:
                lock = tomllib.load(lock_file)
            self.assertEqual(lock["version"], 1)
            self.assertEqual(
                lock["skills"][0]["source_repository"],
                "owner/repository",
            )
            self.assertEqual(lock["skills"][0]["managed_skill"], "managed-name")

    def test_default_command_updates_a_changed_source_skill(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            first_result = self.run_updater(workspace, environment=environment)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)

            source_skill_file = (
                source_repository / "skills" / "source-name" / "SKILL.md"
            )
            source_skill_file.write_text(
                "---\nname: source-name\n---\n\n# Updated source skill\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(source_repository), "add", "."], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "commit",
                    "-m",
                    "Update source skill",
                ],
                check=True,
                capture_output=True,
            )

            result = self.run_updater(workspace, environment=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("UPDATE managed-name", result.stdout)
            managed_content = (
                workspace / "skills" / "managed-name" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("# Updated source skill", managed_content)

    def test_default_command_deletes_a_skill_removed_from_the_manifest(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            manifest_path = workspace / "skills.toml"
            manifest_path.write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            first_result = self.run_updater(workspace, environment=environment)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            manifest_path.write_text("# The collection is empty.\n", encoding="utf-8")

            result = self.run_updater(workspace, environment=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DELETE managed-name from owner/repository", result.stdout)
            self.assertFalse((workspace / "skills" / "managed-name").exists())
            with (workspace / "skills.lock").open("rb") as lock_file:
                lock = tomllib.load(lock_file)
            self.assertEqual(lock, {"version": 1})

    def test_plan_writes_an_immutable_payload_without_modifying_the_workspace(
        self,
    ) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            plan_directory = root / "plan"

            result = self.run_updater(
                workspace,
                "plan",
                "--output",
                str(plan_directory),
                environment=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((plan_directory / "plan.json").is_file())
            self.assertEqual(
                (plan_directory / "operations.tsv").read_text(encoding="utf-8"),
                "add\tmanaged-name\towner/repository\tsource-name\n",
            )
            self.assertTrue(
                (plan_directory / "payloads" / "managed-name" / "SKILL.md").is_file()
            )
            self.assertFalse((workspace / "skills").exists())
            self.assertFalse((workspace / "skills.lock").exists())

    def test_apply_one_uses_the_payload_frozen_in_the_plan(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            plan_directory = root / "plan"
            plan_result = self.run_updater(
                workspace,
                "plan",
                "--output",
                str(plan_directory),
                environment=environment,
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
            (source_repository / "skills" / "source-name" / "SKILL.md").write_text(
                "# Changed after planning\n",
                encoding="utf-8",
            )

            result = self.run_updater(
                workspace,
                "apply-one",
                "--plan",
                str(plan_directory),
                "--skill",
                "managed-name",
                environment=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            managed_content = (
                workspace / "skills" / "managed-name" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("# Source skill", managed_content)
            self.assertNotIn("Changed after planning", managed_content)

    def test_apply_one_rejects_a_managed_skill_changed_after_planning(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            add_result = self.run_updater(workspace, environment=environment)
            self.assertEqual(add_result.returncode, 0, add_result.stderr)
            source_skill_file = (
                source_repository / "skills" / "source-name" / "SKILL.md"
            )
            source_skill_file.write_text(
                "# Planned upstream update\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "-C", str(source_repository), "add", "."], check=True
            )
            subprocess.run(
                ["git", "-C", str(source_repository), "commit", "-m", "Plan an update"],
                check=True,
                capture_output=True,
            )
            plan_directory = root / "plan"
            plan_result = self.run_updater(
                workspace,
                "plan",
                "--output",
                str(plan_directory),
                environment=environment,
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
            managed_file = workspace / "skills" / "managed-name" / "SKILL.md"
            managed_file.write_text("# Concurrent local change\n", encoding="utf-8")

            result = self.run_updater(
                workspace,
                "apply-one",
                "--plan",
                str(plan_directory),
                "--skill",
                "managed-name",
                environment=environment,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("changed after the Sync Plan was created", result.stderr)
            self.assertEqual(
                managed_file.read_text(encoding="utf-8"), "# Concurrent local change\n"
            )

    def test_check_ignores_a_submodule_outside_the_selected_source_skill(self) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            commit = subprocess.run(
                ["git", "-C", str(source_repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{commit},unselected-submodule",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "commit",
                    "-m",
                    "Add unrelated submodule",
                ],
                check=True,
                capture_output=True,
            )
            environment = self.create_fake_gh(root, source_repository)

            result = self.run_updater(workspace, "check", environment=environment)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("ADD managed-name", result.stdout)

    def test_check_rejects_an_unsafe_managed_skill_name_in_the_lock(self) -> None:
        unsafe_lock = """
version = 1

[[skills]]
managed_skill = "../outside"
source_repository = "owner/repository"
source_path = "skills"
source_skill = "source-name"
commit = "1111111111111111111111111111111111111111"
sha256 = "2222222222222222222222222222222222222222222222222222222222222222"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "skills.toml").write_text(
                "# Empty Manifest.\n", encoding="utf-8"
            )
            (workspace / "skills.lock").write_text(unsafe_lock, encoding="utf-8")
            (workspace / "skills").mkdir()
            outside = workspace / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("must survive\n", encoding="utf-8")

            result = self.run_updater(workspace, "check")

            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid Managed Skill", result.stderr)
            self.assertTrue((outside / "keep.txt").is_file())

    def test_check_accepts_a_source_skill_name_that_starts_with_a_dash(self) -> None:
        manifest = """
["owner/repository"."."]
managed-name = "-source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(
                root, source_skill="-source-name"
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "mv",
                    "--",
                    "skills/-source-name",
                    "-source-name",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "commit",
                    "-m",
                    "Move skill to root",
                ],
                check=True,
                capture_output=True,
            )
            environment = self.create_fake_gh(root, source_repository)

            result = self.run_updater(workspace, "check", environment=environment)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("ADD managed-name", result.stdout)

    def test_apply_one_rejects_a_payload_reached_through_a_symlinked_parent(
        self,
    ) -> None:
        manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "skills.toml").write_text(manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            plan_directory = root / "plan"
            plan_result = self.run_updater(
                workspace,
                "plan",
                "--output",
                str(plan_directory),
                environment=environment,
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
            external_payloads = root / "external-payloads"
            (plan_directory / "payloads").rename(external_payloads)
            (plan_directory / "payloads").symlink_to(
                external_payloads, target_is_directory=True
            )

            result = self.run_updater(
                workspace,
                "apply-one",
                "--plan",
                str(plan_directory),
                "--skill",
                "managed-name",
                environment=environment,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("unsafe payload path", result.stderr)
            self.assertFalse((workspace / "skills").exists())

    def test_missing_skill_file_aborts_the_entire_plan_without_workspace_changes(
        self,
    ) -> None:
        initial_manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
"""
        failing_manifest = """
["owner/repository"."skills"]
managed-name = "source-name"
invalid-name = "missing-skill-file"
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            manifest_path = workspace / "skills.toml"
            manifest_path.write_text(initial_manifest, encoding="utf-8")
            source_repository = self.create_source_repository(root)
            environment = self.create_fake_gh(root, source_repository)
            first_result = self.run_updater(workspace, environment=environment)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)

            managed_file = workspace / "skills" / "managed-name" / "SKILL.md"
            previous_managed_content = managed_file.read_bytes()
            previous_lock = (workspace / "skills.lock").read_bytes()
            (source_repository / "skills" / "source-name" / "SKILL.md").write_text(
                "# This valid update must not be applied\n",
                encoding="utf-8",
            )
            invalid_source = source_repository / "skills" / "missing-skill-file"
            invalid_source.mkdir()
            (invalid_source / "README.md").write_text(
                "This directory is not a skill.\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(source_repository), "add", "."], check=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repository),
                    "commit",
                    "-m",
                    "Break one source",
                ],
                check=True,
                capture_output=True,
            )
            manifest_path.write_text(failing_manifest, encoding="utf-8")

            result = self.run_updater(workspace, environment=environment)

            self.assertEqual(result.returncode, 2)
            self.assertIn("must contain a regular SKILL.md file", result.stderr)
            self.assertIn("No workspace changes were applied.", result.stderr)
            self.assertEqual(managed_file.read_bytes(), previous_managed_content)
            self.assertEqual((workspace / "skills.lock").read_bytes(), previous_lock)
            self.assertFalse((workspace / "skills" / "invalid-name").exists())


if __name__ == "__main__":
    unittest.main()
