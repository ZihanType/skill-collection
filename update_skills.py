#!/usr/bin/env python3
"""Synchronize configured GitHub skills into this repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib

MANIFEST_NAME = "skills.toml"
LOCK_NAME = "skills.lock"
SKILLS_DIRECTORY_NAME = "skills"
LOCK_FORMAT_VERSION = 1
PLAN_FORMAT_VERSION = 1

# Git object IDs are SHA-1 today for GitHub repository commits. The updater stores
# the 40-hex representation returned by the API rather than an abbreviated ID.
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

# Content identities encode path/content lengths as unsigned 64-bit integers so
# concatenated fields cannot become ambiguous. Git tracks only whether a file is
# executable, represented here by the union of the three executable permission bits.
TREE_LENGTH_FIELD_BYTES = 8
GIT_EXECUTABLE_PERMISSION_MASK = 0o111

# Repository checkouts live only for one plan. Sixteen SHA-256 hex characters give
# stable, filesystem-safe names without exposing owner/repository punctuation.
REPOSITORY_CACHE_KEY_HEX_LENGTH = 16
MANAGED_SKILL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)


class ConfigurationError(ValueError):
    """The Manifest cannot describe an unambiguous collection."""


class UpdateError(RuntimeError):
    """A Sync Plan cannot be built or applied safely."""


@dataclass(frozen=True)
class SkillMapping:
    source_repository: str
    source_path: str
    managed_skill: str
    source_skill: str


@dataclass(frozen=True)
class LockRecord:
    managed_skill: str
    source_repository: str
    source_path: str
    source_skill: str
    commit: str
    sha256: str


@dataclass(frozen=True)
class SyncOperation:
    operation: str
    mapping: SkillMapping
    commit: str
    sha256: str
    before_sha256: str | None
    payload: Path | None


@dataclass(frozen=True)
class StoredPlan:
    manifest_sha256: str
    initial_records: dict[str, LockRecord]
    operations: list[SyncOperation]


def validate_lock_record(record: LockRecord, *, context: str) -> None:
    try:
        validate_managed_skill(record.managed_skill)
    except ConfigurationError as error:
        raise UpdateError(
            f"{context} contains an invalid Managed Skill: {record.managed_skill!r}"
        ) from error
    try:
        validate_source_repository(record.source_repository)
        validate_source_path(record.source_path)
        validate_source_skill(record.source_skill)
    except ConfigurationError as error:
        raise UpdateError(
            f"{context} contains invalid source coordinates: {error}"
        ) from error
    if not GIT_COMMIT_PATTERN.fullmatch(record.commit):
        raise UpdateError(f"{context} contains an invalid source commit")
    if not SHA256_PATTERN.fullmatch(record.sha256):
        raise UpdateError(f"{context} contains an invalid content digest")


def contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def validate_source_repository(value: str) -> None:
    if not REPOSITORY_PATTERN.fullmatch(value):
        raise ConfigurationError(
            f"Source Repository must use the public GitHub owner/repo form: {value!r}"
        )


def validate_source_path(value: str) -> None:
    parsed_path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or contains_control_character(value)
        or str(parsed_path) != value
        or ".." in parsed_path.parts
    ):
        raise ConfigurationError(
            f"Source Path must be a normalized relative POSIX path (or '.'): {value!r}"
        )


def validate_managed_skill(value: str) -> None:
    if not MANAGED_SKILL_PATTERN.fullmatch(value):
        raise ConfigurationError(
            f"Managed Skill names must match [A-Za-z0-9][A-Za-z0-9._-]*: {value!r}"
        )


def validate_source_skill(value: str) -> None:
    if (
        value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or contains_control_character(value)
    ):
        raise ConfigurationError(
            f"Source Skill must be one safe directory name: {value!r}"
        )


def parse_manifest(document: dict[str, Any]) -> list[SkillMapping]:
    mappings: list[SkillMapping] = []
    managed_names: dict[str, str] = {}
    source_skills: dict[tuple[str, str, str], str] = {}

    for source_repository, repository_value in document.items():
        validate_source_repository(source_repository)
        if not isinstance(repository_value, dict):
            raise ConfigurationError(
                f"{source_repository!r} must contain Source Path tables"
            )
        for source_path, path_value in repository_value.items():
            validate_source_path(source_path)
            if not isinstance(path_value, dict):
                raise ConfigurationError(
                    f"{source_repository!r}.{source_path!r} must be a table"
                )
            for managed_skill, source_skill in path_value.items():
                if not isinstance(source_skill, str):
                    raise ConfigurationError(
                        f"{managed_skill!r} must map to a Source Skill name"
                    )
                validate_managed_skill(managed_skill)
                validate_source_skill(source_skill)
                normalized_name = managed_skill.casefold()
                if normalized_name in managed_names:
                    previous_name = managed_names[normalized_name]
                    raise ConfigurationError(
                        "Managed Skill names must be globally unique ignoring case: "
                        f"{previous_name!r} and {managed_skill!r}"
                    )
                source_identity = (
                    source_repository.casefold(),
                    source_path,
                    source_skill,
                )
                if source_identity in source_skills:
                    previous_name = source_skills[source_identity]
                    raise ConfigurationError(
                        "A Source Skill may have only one Managed Skill: "
                        f"{previous_name!r} and {managed_skill!r}"
                    )
                managed_names[normalized_name] = managed_skill
                source_skills[source_identity] = managed_skill
                mappings.append(
                    SkillMapping(
                        source_repository=source_repository,
                        source_path=source_path,
                        managed_skill=managed_skill,
                        source_skill=source_skill,
                    )
                )
    return mappings


def run_command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            input=input_text,
        )
    except FileNotFoundError as error:
        raise UpdateError(f"required command is unavailable: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (
            error.stderr.strip()
            or error.stdout.strip()
            or f"exit status {error.returncode}"
        )
        raise UpdateError(
            f"command failed ({' '.join(arguments)}): {detail}"
        ) from error
    return result.stdout


def read_github_json(endpoint: str) -> dict[str, Any]:
    output = run_command(["gh", "api", endpoint])
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise UpdateError(
            f"GitHub returned invalid JSON for {endpoint}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise UpdateError(f"GitHub returned an unexpected response for {endpoint}")
    return value


def source_skill_relative_path(mapping: SkillMapping) -> str:
    if mapping.source_path == ".":
        return mapping.source_skill
    return f"{mapping.source_path}/{mapping.source_skill}"


def validate_tree_safety(skill_directory: Path, description: str) -> None:
    if not skill_directory.is_dir() or skill_directory.is_symlink():
        raise UpdateError(f"{description} must be a regular directory")
    for directory, directory_names, file_names in os.walk(
        skill_directory, followlinks=False
    ):
        directory_path = Path(directory)
        for name in [*directory_names, *file_names]:
            entry = directory_path / name
            entry_status = entry.lstat()
            if stat.S_ISLNK(entry_status.st_mode):
                raise UpdateError(
                    f"{description} "
                    f"contains a symbolic link: {entry.relative_to(skill_directory).as_posix()}"
                )
            if not (
                stat.S_ISDIR(entry_status.st_mode) or stat.S_ISREG(entry_status.st_mode)
            ):
                raise UpdateError(
                    f"{description} "
                    f"contains an unsupported file type: {entry.relative_to(skill_directory).as_posix()}"
                )


def validate_source_tree(skill_directory: Path, mapping: SkillMapping) -> None:
    description = f"Source Skill {mapping.source_repository}:{source_skill_relative_path(mapping)}"
    validate_tree_safety(skill_directory, description)
    skill_file = skill_directory / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise UpdateError(f"{description} must contain a regular SKILL.md file")


def content_sha256(skill_directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in skill_directory.rglob("*") if path.is_file())
    for path in files:
        relative_path = path.relative_to(skill_directory).as_posix().encode("utf-8")
        executable = (
            b"1" if path.stat().st_mode & GIT_EXECUTABLE_PERMISSION_MASK else b"0"
        )
        content = path.read_bytes()
        digest.update(len(relative_path).to_bytes(TREE_LENGTH_FIELD_BYTES, "big"))
        digest.update(relative_path)
        digest.update(executable)
        digest.update(len(content).to_bytes(TREE_LENGTH_FIELD_BYTES, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_lock(workspace: Path) -> dict[str, LockRecord]:
    lock_path = workspace / LOCK_NAME
    if not lock_path.exists():
        return {}
    try:
        with lock_path.open("rb") as lock_file:
            document = tomllib.load(lock_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise UpdateError(f"could not read {LOCK_NAME}: {error}") from error
    if document.get("version") != LOCK_FORMAT_VERSION:
        raise UpdateError(f"{LOCK_NAME} must have version = {LOCK_FORMAT_VERSION}")
    raw_records = document.get("skills", [])
    if not isinstance(raw_records, list):
        raise UpdateError(f"{LOCK_NAME} skills must be an array of tables")

    records: dict[str, LockRecord] = {}
    required_fields = {
        "managed_skill",
        "source_repository",
        "source_path",
        "source_skill",
        "commit",
        "sha256",
    }
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != required_fields:
            raise UpdateError(f"{LOCK_NAME} contains an invalid skill record")
        if not all(isinstance(raw_record[field], str) for field in required_fields):
            raise UpdateError(f"{LOCK_NAME} skill record fields must be strings")
        record = LockRecord(**raw_record)
        validate_lock_record(record, context=LOCK_NAME)
        normalized_name = record.managed_skill.casefold()
        if normalized_name in records:
            raise UpdateError(f"{LOCK_NAME} contains duplicate Managed Skill records")
        records[normalized_name] = record
    return records


def fetch_repository(
    source_repository: str,
    mappings: list[SkillMapping],
    plan_directory: Path,
) -> tuple[str, Path]:
    repository_information = read_github_json(f"repos/{source_repository}")
    if repository_information.get("visibility") != "public":
        raise UpdateError(f"Source Repository must be public: {source_repository}")
    default_branch = repository_information.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise UpdateError(
            f"Source Repository has no default branch: {source_repository}"
        )
    encoded_branch = urllib.parse.quote(default_branch, safe="")
    commit_information = read_github_json(
        f"repos/{source_repository}/commits/{encoded_branch}"
    )
    commit = commit_information.get("sha")
    if not isinstance(commit, str) or not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise UpdateError(f"GitHub returned an invalid commit for {source_repository}")

    repository_key = hashlib.sha256(
        source_repository.casefold().encode("utf-8")
    ).hexdigest()[:REPOSITORY_CACHE_KEY_HEX_LENGTH]
    checkout = plan_directory / "repositories" / repository_key
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "gh",
            "repo",
            "clone",
            source_repository,
            str(checkout),
            "--",
            "--filter=blob:none",
            "--no-checkout",
        ]
    )
    sparse_paths = sorted({source_skill_relative_path(mapping) for mapping in mappings})
    run_command(
        [
            "git",
            "-C",
            str(checkout),
            "sparse-checkout",
            "set",
            "--cone",
            "--skip-checks",
            "--stdin",
        ],
        input_text="".join(f"{path}\n" for path in sparse_paths),
    )
    run_command(["git", "-C", str(checkout), "checkout", "--detach", commit])
    index_entries = run_command(["git", "-C", str(checkout), "ls-files", "--stage"])
    for line in index_entries.splitlines():
        if not line.startswith("160000 ") or "\t" not in line:
            continue
        submodule_path = line.split("\t", 1)[1]
        if any(
            submodule_path == selected_path
            or submodule_path.startswith(f"{selected_path}/")
            for selected_path in sparse_paths
        ):
            raise UpdateError(
                f"Source Repository contains a selected Git submodule: "
                f"{source_repository}:{submodule_path}"
            )
    return commit.lower(), checkout


def build_sync_plan(
    workspace: Path,
    mappings: list[SkillMapping],
    plan_directory: Path,
) -> list[SyncOperation]:
    lock_records = load_lock(workspace)
    skills_directory = workspace / SKILLS_DIRECTORY_NAME
    if skills_directory.exists():
        if not skills_directory.is_dir() or skills_directory.is_symlink():
            raise UpdateError("skills/ must be a regular directory")
        for entry in skills_directory.iterdir():
            if entry.name.casefold() not in lock_records:
                raise UpdateError(
                    f"skills/ contains content without a Lock Record: {entry.name}"
                )
            validate_tree_safety(entry, f"Managed Skill {entry.name}")

    mappings_by_repository: dict[str, list[SkillMapping]] = {}
    repository_names: dict[str, str] = {}
    for mapping in mappings:
        repository_key = mapping.source_repository.casefold()
        mappings_by_repository.setdefault(repository_key, []).append(mapping)
        repository_names.setdefault(repository_key, mapping.source_repository)

    operations: list[SyncOperation] = []
    payloads_directory = plan_directory / "payloads"
    for repository_key in sorted(mappings_by_repository):
        repository_mappings = mappings_by_repository[repository_key]
        commit, checkout = fetch_repository(
            repository_names[repository_key], repository_mappings, plan_directory
        )
        for mapping in repository_mappings:
            source_directory = checkout / source_skill_relative_path(mapping)
            validate_source_tree(source_directory, mapping)
            payload = payloads_directory / mapping.managed_skill
            payload.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_directory, payload, copy_function=shutil.copy2)
            source_digest = content_sha256(payload)
            normalized_name = mapping.managed_skill.casefold()
            record = lock_records.get(normalized_name)
            target = skills_directory / mapping.managed_skill
            if record is None:
                if target.exists() or target.is_symlink():
                    raise UpdateError(
                        f"Managed Skill has no Lock Record: {mapping.managed_skill}"
                    )
                operation_name = "add"
                before_digest = None
            else:
                source_changed = (
                    record.source_repository.casefold()
                    != mapping.source_repository.casefold()
                    or record.source_path != mapping.source_path
                    or record.source_skill != mapping.source_skill
                    or record.sha256 != source_digest
                )
                if target.exists() and not target.is_symlink():
                    validate_tree_safety(
                        target, f"Managed Skill {mapping.managed_skill}"
                    )
                    before_digest = content_sha256(target)
                    local_changed = before_digest != source_digest
                else:
                    before_digest = None
                    local_changed = True
                if not source_changed and not local_changed:
                    continue
                operation_name = "update"
            operations.append(
                SyncOperation(
                    operation=operation_name,
                    mapping=mapping,
                    commit=commit,
                    sha256=source_digest,
                    before_sha256=before_digest,
                    payload=payload,
                )
            )
    configured_names = {mapping.managed_skill.casefold() for mapping in mappings}
    for normalized_name, record in lock_records.items():
        if normalized_name in configured_names:
            continue
        deletion_target = skills_directory / record.managed_skill
        before_digest = (
            content_sha256(deletion_target)
            if deletion_target.exists() and not deletion_target.is_symlink()
            else None
        )
        operations.append(
            SyncOperation(
                operation="delete",
                mapping=SkillMapping(
                    source_repository=record.source_repository,
                    source_path=record.source_path,
                    managed_skill=record.managed_skill,
                    source_skill=record.source_skill,
                ),
                commit=record.commit,
                sha256=record.sha256,
                before_sha256=before_digest,
                payload=None,
            )
        )
    return sorted(
        operations, key=lambda operation: operation.mapping.managed_skill.casefold()
    )


def print_operations(operations: list[SyncOperation]) -> None:
    for operation in operations:
        mapping = operation.mapping
        print(
            f"{operation.operation.upper()} {mapping.managed_skill} "
            f"from {mapping.source_repository} (source: {mapping.source_skill})"
        )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_plan(
    workspace: Path,
    plan_directory: Path,
    operations: list[SyncOperation],
    initial_records: dict[str, LockRecord],
) -> None:
    repositories_directory = plan_directory / "repositories"
    if repositories_directory.exists():
        shutil.rmtree(repositories_directory)

    serialized_operations: list[dict[str, Any]] = []
    operation_lines: list[str] = []
    for operation in operations:
        mapping = operation.mapping
        serialized_operations.append(
            {
                "operation": operation.operation,
                "source_repository": mapping.source_repository,
                "source_path": mapping.source_path,
                "managed_skill": mapping.managed_skill,
                "source_skill": mapping.source_skill,
                "commit": operation.commit,
                "sha256": operation.sha256,
                "before_sha256": operation.before_sha256,
                "payload": (
                    operation.payload.relative_to(plan_directory).as_posix()
                    if operation.payload is not None
                    else None
                ),
            }
        )
        operation_lines.append(
            f"{operation.operation}\t{mapping.managed_skill}\t"
            f"{mapping.source_repository}\t{mapping.source_skill}"
        )

    plan_document = {
        "version": PLAN_FORMAT_VERSION,
        "manifest_sha256": file_sha256(workspace / MANIFEST_NAME),
        "initial_lock": [
            asdict(initial_records[name]) for name in sorted(initial_records)
        ],
        "operations": serialized_operations,
    }
    (plan_directory / "plan.json").write_text(
        json.dumps(plan_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (plan_directory / "operations.tsv").write_text(
        "".join(f"{line}\n" for line in operation_lines),
        encoding="utf-8",
    )


def lock_record_from_dict(value: Any, *, context: str) -> LockRecord:
    required_fields = {
        "managed_skill",
        "source_repository",
        "source_path",
        "source_skill",
        "commit",
        "sha256",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise UpdateError(f"{context} contains an invalid Lock Record")
    if not all(isinstance(value[field], str) for field in required_fields):
        raise UpdateError(f"{context} Lock Record fields must be strings")
    record = LockRecord(**value)
    validate_lock_record(record, context=context)
    return record


def read_plan(plan_directory: Path) -> StoredPlan:
    if not plan_directory.is_dir() or plan_directory.is_symlink():
        raise UpdateError(f"plan must be a regular directory: {plan_directory}")
    plan_file = plan_directory / "plan.json"
    try:
        document = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"could not read plan.json: {error}") from error
    if not isinstance(document, dict) or document.get("version") != PLAN_FORMAT_VERSION:
        raise UpdateError(f"plan.json must have version {PLAN_FORMAT_VERSION}")
    manifest_digest = document.get("manifest_sha256")
    if not isinstance(manifest_digest, str) or not SHA256_PATTERN.fullmatch(
        manifest_digest
    ):
        raise UpdateError("plan.json contains an invalid Manifest digest")

    raw_initial_records = document.get("initial_lock")
    if not isinstance(raw_initial_records, list):
        raise UpdateError("plan.json initial_lock must be an array")
    initial_records: dict[str, LockRecord] = {}
    for raw_record in raw_initial_records:
        record = lock_record_from_dict(raw_record, context="plan.json")
        normalized_name = record.managed_skill.casefold()
        if normalized_name in initial_records:
            raise UpdateError("plan.json contains duplicate initial Lock Records")
        initial_records[normalized_name] = record

    raw_operations = document.get("operations")
    if not isinstance(raw_operations, list):
        raise UpdateError("plan.json operations must be an array")
    operations: list[SyncOperation] = []
    seen_operations: set[str] = set()
    operation_fields = {
        "operation",
        "source_repository",
        "source_path",
        "managed_skill",
        "source_skill",
        "commit",
        "sha256",
        "before_sha256",
        "payload",
    }
    for raw_operation in raw_operations:
        if (
            not isinstance(raw_operation, dict)
            or set(raw_operation) != operation_fields
        ):
            raise UpdateError("plan.json contains an invalid Sync Operation")
        operation_name = raw_operation["operation"]
        if operation_name not in {"add", "update", "delete"}:
            raise UpdateError("plan.json contains an unknown Sync Operation")
        string_fields = operation_fields - {"payload", "before_sha256"}
        if not all(isinstance(raw_operation[field], str) for field in string_fields):
            raise UpdateError("plan.json Sync Operation fields must be strings")
        mapping = SkillMapping(
            source_repository=raw_operation["source_repository"],
            source_path=raw_operation["source_path"],
            managed_skill=raw_operation["managed_skill"],
            source_skill=raw_operation["source_skill"],
        )
        validate_source_repository(mapping.source_repository)
        validate_source_path(mapping.source_path)
        validate_managed_skill(mapping.managed_skill)
        validate_source_skill(mapping.source_skill)
        normalized_name = mapping.managed_skill.casefold()
        if normalized_name in seen_operations:
            raise UpdateError("plan.json contains duplicate Managed Skill operations")
        seen_operations.add(normalized_name)
        commit = raw_operation["commit"]
        digest = raw_operation["sha256"]
        if not GIT_COMMIT_PATTERN.fullmatch(commit) or not SHA256_PATTERN.fullmatch(
            digest
        ):
            raise UpdateError("plan.json contains an invalid commit or content digest")
        before_digest = raw_operation["before_sha256"]
        if before_digest is not None and (
            not isinstance(before_digest, str)
            or not SHA256_PATTERN.fullmatch(before_digest)
        ):
            raise UpdateError("plan.json contains an invalid previous content digest")
        raw_payload = raw_operation["payload"]
        if operation_name == "delete":
            if raw_payload is not None:
                raise UpdateError("delete operations must not contain a payload")
            payload = None
        else:
            if not isinstance(raw_payload, str):
                raise UpdateError("add and update operations require a payload")
            parsed_payload = PurePosixPath(raw_payload)
            if (
                not raw_payload
                or parsed_payload.is_absolute()
                or str(parsed_payload) != raw_payload
                or ".." in parsed_payload.parts
            ):
                raise UpdateError("plan.json contains an unsafe payload path")
            payload = plan_directory.joinpath(*parsed_payload.parts)
            current_path = plan_directory
            for path_component in parsed_payload.parts:
                current_path = current_path / path_component
                if current_path.is_symlink():
                    raise UpdateError("plan.json contains an unsafe payload path")
            try:
                payload.resolve().relative_to(plan_directory.resolve())
            except ValueError as error:
                raise UpdateError(
                    "plan.json contains an unsafe payload path"
                ) from error
            validate_source_tree(payload, mapping)
            if content_sha256(payload) != digest:
                raise UpdateError(
                    f"planned payload digest does not match: {mapping.managed_skill}"
                )
        operations.append(
            SyncOperation(
                operation=operation_name,
                mapping=mapping,
                commit=commit,
                sha256=digest,
                before_sha256=before_digest,
                payload=payload,
            )
        )
    return StoredPlan(
        manifest_sha256=manifest_digest,
        initial_records=initial_records,
        operations=operations,
    )


def lock_record_for_operation(operation: SyncOperation) -> LockRecord:
    mapping = operation.mapping
    return LockRecord(
        managed_skill=mapping.managed_skill,
        source_repository=mapping.source_repository,
        source_path=mapping.source_path,
        source_skill=mapping.source_skill,
        commit=operation.commit,
        sha256=operation.sha256,
    )


def records_after_operation(
    records: dict[str, LockRecord], operation: SyncOperation
) -> dict[str, LockRecord]:
    updated_records = dict(records)
    normalized_name = operation.mapping.managed_skill.casefold()
    if operation.operation == "delete":
        updated_records.pop(normalized_name, None)
    else:
        updated_records[normalized_name] = lock_record_for_operation(operation)
    return updated_records


def apply_one_from_plan(
    workspace: Path,
    plan_directory: Path,
    managed_skill: str,
) -> SyncOperation:
    plan = read_plan(plan_directory)
    if file_sha256(workspace / MANIFEST_NAME) != plan.manifest_sha256:
        raise UpdateError("skills.toml changed after the Sync Plan was created")
    selected_index = next(
        (
            index
            for index, operation in enumerate(plan.operations)
            if operation.mapping.managed_skill.casefold() == managed_skill.casefold()
        ),
        None,
    )
    if selected_index is None:
        raise UpdateError(f"Sync Plan has no operation for {managed_skill}")

    expected_records = dict(plan.initial_records)
    for previous_operation in plan.operations[:selected_index]:
        expected_records = records_after_operation(expected_records, previous_operation)
    current_records = load_lock(workspace)
    if current_records != expected_records:
        raise UpdateError(
            f"Sync Operations must be applied in plan order before {managed_skill}"
        )
    operation = plan.operations[selected_index]
    target = workspace / SKILLS_DIRECTORY_NAME / operation.mapping.managed_skill
    if target.exists() and target.is_dir() and not target.is_symlink():
        validate_tree_safety(target, f"Managed Skill {managed_skill}")
        current_digest = content_sha256(target)
    else:
        current_digest = None
    if current_digest != operation.before_sha256:
        raise UpdateError(
            f"Managed Skill {managed_skill} changed after the Sync Plan was created"
        )
    apply_operation(workspace, operation, current_records)
    return operation


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_lock(records: dict[str, LockRecord]) -> str:
    lines = [f"version = {LOCK_FORMAT_VERSION}"]
    for normalized_name in sorted(records):
        record = records[normalized_name]
        lines.extend(
            [
                "",
                "[[skills]]",
                f"managed_skill = {toml_string(record.managed_skill)}",
                f"source_repository = {toml_string(record.source_repository)}",
                f"source_path = {toml_string(record.source_path)}",
                f"source_skill = {toml_string(record.source_skill)}",
                f"commit = {toml_string(record.commit)}",
                f"sha256 = {toml_string(record.sha256)}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_lock(workspace: Path, records: dict[str, LockRecord]) -> None:
    lock_path = workspace / LOCK_NAME
    temporary_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=workspace,
            prefix=f".{LOCK_NAME}.",
            delete=False,
        ) as lock_file:
            temporary_file = Path(lock_file.name)
            lock_file.write(render_lock(records))
            lock_file.flush()
            os.fsync(lock_file.fileno())
        os.replace(temporary_file, lock_path)
    except OSError as error:
        if temporary_file is not None:
            temporary_file.unlink(missing_ok=True)
        raise UpdateError(f"could not write {LOCK_NAME}: {error}") from error


def restore_lock(workspace: Path, previous_lock: bytes | None) -> None:
    lock_path = workspace / LOCK_NAME
    if previous_lock is None:
        lock_path.unlink(missing_ok=True)
    else:
        lock_path.write_bytes(previous_lock)


def apply_replacement(
    workspace: Path,
    operation: SyncOperation,
    records: dict[str, LockRecord],
) -> dict[str, LockRecord]:
    skills_directory = workspace / SKILLS_DIRECTORY_NAME
    skills_directory.mkdir(exist_ok=True)
    target = skills_directory / operation.mapping.managed_skill
    if operation.operation == "add" and (target.exists() or target.is_symlink()):
        raise UpdateError(
            f"cannot add {operation.mapping.managed_skill}: its target already exists"
        )

    previous_lock = (
        (workspace / LOCK_NAME).read_bytes()
        if (workspace / LOCK_NAME).exists()
        else None
    )
    if operation.payload is None:
        raise UpdateError(f"{operation.operation} operation has no payload")
    with tempfile.TemporaryDirectory(
        prefix=".apply-", dir=skills_directory
    ) as staging_root_name:
        staging_root = Path(staging_root_name)
        staging_target = staging_root / "next"
        previous_target = staging_root / "previous"
        shutil.copytree(operation.payload, staging_target, copy_function=shutil.copy2)
        had_target = target.exists() or target.is_symlink()
        if had_target:
            os.replace(target, previous_target)
        os.replace(staging_target, target)
        updated_records = dict(records)
        mapping = operation.mapping
        updated_records[mapping.managed_skill.casefold()] = LockRecord(
            managed_skill=mapping.managed_skill,
            source_repository=mapping.source_repository,
            source_path=mapping.source_path,
            source_skill=mapping.source_skill,
            commit=operation.commit,
            sha256=operation.sha256,
        )
        try:
            write_lock(workspace, updated_records)
        except UpdateError:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if had_target:
                os.replace(previous_target, target)
            restore_lock(workspace, previous_lock)
            raise
        return updated_records


def apply_deletion(
    workspace: Path,
    operation: SyncOperation,
    records: dict[str, LockRecord],
) -> dict[str, LockRecord]:
    skills_directory = workspace / SKILLS_DIRECTORY_NAME
    target = skills_directory / operation.mapping.managed_skill
    previous_lock = (
        (workspace / LOCK_NAME).read_bytes()
        if (workspace / LOCK_NAME).exists()
        else None
    )
    updated_records = dict(records)
    updated_records.pop(operation.mapping.managed_skill.casefold(), None)

    if target.exists() or target.is_symlink():
        with tempfile.TemporaryDirectory(
            prefix=".delete-", dir=skills_directory
        ) as staging_root_name:
            previous_target = Path(staging_root_name) / "previous"
            os.replace(target, previous_target)
            try:
                write_lock(workspace, updated_records)
            except UpdateError:
                os.replace(previous_target, target)
                restore_lock(workspace, previous_lock)
                raise
    else:
        write_lock(workspace, updated_records)

    if skills_directory.exists() and not any(skills_directory.iterdir()):
        skills_directory.rmdir()
    return updated_records


def apply_operation(
    workspace: Path,
    operation: SyncOperation,
    records: dict[str, LockRecord],
) -> dict[str, LockRecord]:
    if operation.operation == "delete":
        return apply_deletion(workspace, operation, records)
    return apply_replacement(workspace, operation, records)


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser(
        "check", help="validate sources and report pending operations"
    )
    plan_parser = subcommands.add_parser("plan", help="build an immutable Sync Plan")
    plan_parser.add_argument(
        "--output", type=Path, required=True, help="new plan directory"
    )
    apply_parser = subcommands.add_parser(
        "apply-one", help="apply one planned operation"
    )
    apply_parser.add_argument(
        "--plan", type=Path, required=True, help="Sync Plan directory"
    )
    apply_parser.add_argument("--skill", required=True, help="Managed Skill to apply")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    manifest_path = Path.cwd() / MANIFEST_NAME
    try:
        with manifest_path.open("rb") as manifest_file:
            manifest = tomllib.load(manifest_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"error: could not read {MANIFEST_NAME}: {error}", file=sys.stderr)
        return 2

    try:
        mappings = parse_manifest(manifest)
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if options.command == "apply-one":
        try:
            operation = apply_one_from_plan(
                workspace=Path.cwd(),
                plan_directory=options.plan.resolve(),
                managed_skill=options.skill,
            )
        except (OSError, UpdateError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print_operations([operation])
        return 0

    if options.command == "plan":
        plan_directory = options.output.resolve()
        if plan_directory.exists() or plan_directory.is_symlink():
            print(
                f"error: plan output already exists: {plan_directory}", file=sys.stderr
            )
            return 2
        try:
            plan_directory.mkdir(parents=True)
            operations = build_sync_plan(
                workspace=Path.cwd(),
                mappings=mappings,
                plan_directory=plan_directory,
            )
            write_plan(
                workspace=Path.cwd(),
                plan_directory=plan_directory,
                operations=operations,
                initial_records=load_lock(Path.cwd()),
            )
        except (OSError, UpdateError) as error:
            if plan_directory.exists():
                shutil.rmtree(plan_directory)
            print(f"error: {error}", file=sys.stderr)
            return 2
        print_operations(operations)
        return 0

    plan_ready = False
    try:
        with tempfile.TemporaryDirectory(prefix="skill-update-plan-") as plan_directory:
            operations = build_sync_plan(
                workspace=Path.cwd(),
                mappings=mappings,
                plan_directory=Path(plan_directory),
            )
            plan_ready = True
            if options.command == "check":
                if operations:
                    print_operations(operations)
                    return 1
                print("No skill changes.")
                return 0

            records = load_lock(Path.cwd())
            for operation in operations:
                records = apply_operation(Path.cwd(), operation, records)
            if operations:
                print_operations(operations)
            else:
                print("No skill changes.")
            return 0
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        if not plan_ready:
            print("No workspace changes were applied.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
