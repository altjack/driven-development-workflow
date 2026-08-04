"""Deterministic feature workflow runner."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


CONFIG_PATH = Path(__file__).with_name("workflow.json")
CONFIG_SCHEMA_PATH = Path(__file__).with_name("workflow.schema.json")
PROMPTS_DIR = Path(__file__).with_name("prompts")
TEMPLATES_DIR = Path(__file__).with_name("templates")
MAIN_BRANCHES = {"main", "master"}
CONFIG_VERSION = 1
WORKFLOW_NAMES = ("docs", "code", "e2e")
ROLES = ("author", "reviewer")
PROVIDERS = {"claude", "codex"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
CODEX_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


class ProviderFailure(ValueError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def config_error(path: str, message: str) -> ValueError:
    return ValueError(f"invalid workflow config at {path}: {message}")


def validate_keys(
    value: dict,
    required: set[str],
    optional: set[str],
    path: str,
) -> None:
    missing = required - set(value)
    if missing:
        raise config_error(path, f"missing keys: {', '.join(sorted(missing))}")
    unknown = set(value) - required - optional
    if unknown:
        raise config_error(path, f"unknown keys: {', '.join(sorted(unknown))}")


def validate_route(route: object, path: str) -> None:
    if not isinstance(route, dict):
        raise config_error(path, "must be an object")
    validate_keys(route, {"provider", "model", "effort"}, {"sandbox"}, path)
    provider = route["provider"]
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise config_error(f"{path}.provider", f"must be one of {sorted(PROVIDERS)}")
    if not isinstance(route["model"], str) or not route["model"].strip():
        raise config_error(f"{path}.model", "must be a non-empty string")
    if not isinstance(route["effort"], str) or route["effort"] not in EFFORTS:
        raise config_error(f"{path}.effort", f"must be one of {sorted(EFFORTS)}")
    if provider == "codex":
        sandbox = route.get("sandbox")
        if not isinstance(sandbox, str) or sandbox not in CODEX_SANDBOXES:
            raise config_error(
                f"{path}.sandbox",
                f"is required for Codex and must be one of {sorted(CODEX_SANDBOXES)}",
            )
    elif "sandbox" in route:
        raise config_error(f"{path}.sandbox", "is only supported by Codex")


def route_identity(route: dict) -> tuple[str, str]:
    return route["provider"], route["model"]


def validate_pair(author: dict, reviewer: dict, path: str) -> None:
    if author["model"] == reviewer["model"]:
        raise config_error(path, "author and reviewer must use different models")


def validate_workflow(workflow: object, path: str) -> None:
    if not isinstance(workflow, dict):
        raise config_error(path, "must be an object")
    validate_keys(workflow, {"author", "reviewer", "fallbacks"}, set(), path)
    for role in ROLES:
        validate_route(workflow[role], f"{path}.{role}")
    validate_pair(workflow["author"], workflow["reviewer"], path)
    if not isinstance(workflow["fallbacks"], list):
        raise config_error(f"{path}.fallbacks", "must be an array")

    transitions = {}
    for index, transition in enumerate(workflow["fallbacks"]):
        transition_path = f"{path}.fallbacks[{index}]"
        if not isinstance(transition, dict):
            raise config_error(transition_path, "must be an object")
        validate_keys(transition, {"on"}, {"to", "stop"}, transition_path)
        if ("to" in transition) == ("stop" in transition):
            raise config_error(transition_path, "must define exactly one of to or stop")
        trigger = transition["on"]
        if not isinstance(trigger, dict):
            raise config_error(f"{transition_path}.on", "must be an object")
        validate_keys(
            trigger,
            {"role", "provider", "model"},
            set(),
            f"{transition_path}.on",
        )
        if not isinstance(trigger["role"], str) or trigger["role"] not in ROLES:
            raise config_error(
                f"{transition_path}.on.role", f"must be one of {list(ROLES)}"
            )
        if (
            not isinstance(trigger["provider"], str)
            or trigger["provider"] not in PROVIDERS
        ):
            raise config_error(
                f"{transition_path}.on.provider",
                f"must be one of {sorted(PROVIDERS)}",
            )
        if not isinstance(trigger["model"], str) or not trigger["model"].strip():
            raise config_error(
                f"{transition_path}.on.model", "must be a non-empty string"
            )
        trigger_key = (
            trigger["role"],
            trigger["provider"],
            trigger["model"],
        )
        if trigger_key in transitions:
            raise config_error(transition_path, f"duplicate fallback trigger: {trigger_key}")
        transitions[trigger_key] = transition

        if "to" in transition:
            target = transition["to"]
            if not isinstance(target, dict):
                raise config_error(f"{transition_path}.to", "must be an object")
            validate_keys(target, set(ROLES), set(), f"{transition_path}.to")
            for role in ROLES:
                validate_route(target[role], f"{transition_path}.to.{role}")
            validate_pair(
                target["author"],
                target["reviewer"],
                f"{transition_path}.to",
            )
            failed_target = target[trigger["role"]]
            if route_identity(failed_target) == (
                trigger["provider"],
                trigger["model"],
            ):
                raise config_error(
                    f"{transition_path}.to.{trigger['role']}",
                    "must change the failed provider or model",
                )
        elif transition["stop"] != "needs_user":
            raise config_error(
                f"{transition_path}.stop", "must be exactly needs_user"
            )

    initial = (workflow["author"], workflow["reviewer"])
    pending = [initial]
    reachable_states = set()
    reachable_triggers = set()
    while pending:
        author, reviewer = pending.pop()
        state_key = (route_identity(author), route_identity(reviewer))
        if state_key in reachable_states:
            continue
        reachable_states.add(state_key)
        for role, route in zip(ROLES, (author, reviewer)):
            trigger_key = (role, *route_identity(route))
            transition = transitions.get(trigger_key)
            if transition is None:
                raise config_error(
                    f"{path}.fallbacks",
                    f"missing terminal or fallback for reachable {role} route "
                    f"{route['provider']}/{route['model']}",
                )
            reachable_triggers.add(trigger_key)
            if "to" in transition:
                pending.append(
                    (transition["to"]["author"], transition["to"]["reviewer"])
                )

    unreachable = set(transitions) - reachable_triggers
    if unreachable:
        trigger = sorted(unreachable)[0]
        raise config_error(
            f"{path}.fallbacks",
            f"unreachable fallback trigger: {trigger}",
        )


def validate_config(config: object) -> dict:
    if not isinstance(config, dict):
        raise config_error("$", "must be an object")
    validate_keys(
        config,
        {"version", *WORKFLOW_NAMES, "limits"},
        set(),
        "$",
    )
    if isinstance(config["version"], bool) or config["version"] != CONFIG_VERSION:
        raise config_error(
            "$.version", f"must be the supported version {CONFIG_VERSION}"
        )
    for name in WORKFLOW_NAMES:
        validate_workflow(config[name], f"$.{name}")
    limits = config["limits"]
    if not isinstance(limits, dict):
        raise config_error("$.limits", "must be an object")
    validate_keys(limits, {"max_reviews"}, set(), "$.limits")
    max_reviews = limits["max_reviews"]
    if isinstance(max_reviews, bool) or not isinstance(max_reviews, int):
        raise config_error("$.limits.max_reviews", "must be an integer")
    if not 1 <= max_reviews <= 3:
        raise config_error("$.limits.max_reviews", "must be between 1 and 3")
    return config


def load_config(path: Path | None = None) -> dict:
    config_path = path or CONFIG_PATH
    return validate_config(json.loads(config_path.read_text()))


def workflow_config(config: dict, unit: str) -> dict:
    if unit == "validation.md":
        return config["e2e"]
    return config["docs" if unit.endswith(".md") else unit]


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def safe_code_path(relative: str) -> bool:
    path = Path(relative)
    if relative.startswith("specs/"):
        return False
    names = {part.lower() for part in path.parts}
    if any(name.startswith(".env") for name in names):
        return False
    return not (
        names
        & {
            "credentials.json",
            "secrets.json",
            ".git",
            ".netrc",
            ".npmrc",
            ".pypirc",
            "id_rsa",
            "id_ed25519",
            "node_modules",
        }
        or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
    )


def git_paths(root: Path, *args: str) -> list[str]:
    process = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if process.returncode:
        raise ValueError(process.stderr.decode(errors="replace").strip() or "git failed")
    return [item.decode(errors="replace") for item in process.stdout.split(b"\0") if item]


def code_paths(root: Path, base_commit: str) -> tuple[list[str], set[str]]:
    changed = git_paths(root, "diff", "--name-only", "-z", base_commit, "--")
    untracked = set(git_paths(root, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted(path for path in set(changed) | untracked if safe_code_path(path)), untracked


def code_snapshot(root: Path, base_commit: str) -> str:
    paths, untracked = code_paths(root, base_commit)
    chunks = []
    for relative in paths:
        path = root / relative
        if relative in untracked:
            if not path.is_file() or path.is_symlink():
                continue
            content = path.read_text(errors="replace")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            chunks.append(f"--- /dev/null\n+++ b/{relative}\nsha256:{digest}\n{content}")
        else:
            chunks.append(git(root, "diff", "--binary", "--no-ext-diff", base_commit, "--", relative))
    return "\n".join(chunk for chunk in chunks if chunk)


def code_hash(root: Path, base_commit: str) -> str | None:
    snapshot = code_snapshot(root, base_commit)
    return "sha256:" + hashlib.sha256(snapshot.encode()).hexdigest() if snapshot else None


def commit_code(root: Path, branch: str, base_commit: str) -> str:
    paths, _ = code_paths(root, base_commit)
    if not paths:
        raise ValueError("code diff is empty")
    status = git(root, "status", "--porcelain", "--", *paths)
    if status:
        git(root, "add", "--", *paths)
        git(root, "commit", "-m", f"feature-flow: {branch}", "--", *paths)
    result = git(root, "rev-parse", "HEAD")
    if result == base_commit:
        raise ValueError("accepted code was not committed")
    return result


def commit_artifacts(root: Path, branch: str) -> str:
    feature_dir = root / "specs" / "features" / branch
    paths = [
        str((feature_dir / name).relative_to(root))
        for name in ("requirements.md", "plan.md", "validation.md", "review-ledger.md")
        if (feature_dir / name).is_file()
    ]
    if len(paths) != 4:
        raise ValueError("cannot commit incomplete workflow artifacts")
    git(root, "add", "--", *paths)
    if not git(root, "status", "--porcelain", "--", *paths):
        raise ValueError("workflow artifacts have no changes to commit")
    git(root, "commit", "-m", f"feature-flow: {branch} artifacts", "--", *paths)
    return git(root, "rev-parse", "HEAD")


def has_real_requirements(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    if len([line for line in text.splitlines() if line.strip()]) <= 20:
        return False
    for heading in ("Problema", "Scope", "Decisioni di design"):
        section = re.search(
            rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)", text
        )
        if section and "TODO" in section.group(1).upper():
            return False
    return True


def has_real_plan(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    return not re.search(r"(?i)\bTODO\b", text) and bool(
        re.search(r"(?m)^\s*-\s+\[[ xX]\]\s+", text)
    )


def has_real_validation(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    return bool(
        "TODO" not in text.upper()
        and re.search(r"(?m)^##\s+Test eseguiti\s*$", text)
        and re.search(r"(?m)^\s*-\s+\[[ xX]\]\s+", text)
        and re.search(r"(?i)\b(passed|failed|blocked)\b", text)
    )


def artifact_ready(path: Path) -> bool:
    if path.name == "requirements.md":
        return has_real_requirements(path)
    if path.name == "plan.md":
        return has_real_plan(path)
    if path.name == "validation.md":
        return has_real_validation(path)
        raise ValueError(f"unsupported docs artifact: {path.name}")


def latest_ledger_block(ledger: Path, artifact: Path) -> str | None:
    if not ledger.is_file():
        return None
    blocks = re.split(r"(?m)^##\s+", ledger.read_text(errors="replace"))[1:]
    for block in reversed(blocks):
        artifact_fields = re.findall(r"(?m)^artifact:\s*(.+?)\s*$", block)
        if artifact_fields and Path(artifact_fields[-1]).name == artifact.name:
            return block
    return None


def ledger_field(ledger: Path, artifact: Path, field: str) -> str | None:
    block = latest_ledger_block(ledger, artifact)
    values = re.findall(rf"(?m)^{re.escape(field)}:\s*(.+?)\s*$", block or "")
    return values[-1] if values else None


def gate_accepted(ledger: Path, artifact: Path, current_hash: str) -> bool:
    return (
        ledger_field(ledger, artifact, "status") == "accepted"
        and ledger_field(ledger, artifact, "accepted_hash") == current_hash
        and ledger_field(ledger, artifact, "open_items") == "none"
    )


def roadmap_summary(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "open": 0, "active": 0}
    markers = re.findall(r"(?m)^\s*-\s+\[([ xX~])\]", path.read_text(errors="replace"))
    return {
        "exists": True,
        "open": markers.count(" "),
        "active": markers.count("~"),
    }


def state_dir_for(root: Path, branch: str) -> Path:
    return (
        Path.home()
        / ".local"
        / "state"
        / "feature-flow"
        / hashlib.sha256(str(root).encode()).hexdigest()[:16]
        / quote(branch, safe="")
    )


def inspect_repo(repo: Path, unit: str | None = None) -> dict:
    root = Path(git(repo.resolve(), "rev-parse", "--show-toplevel")).resolve()
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    if not branch:
        raise ValueError("detached HEAD is not supported")

    config = load_config()
    feature_dir = root / "specs" / "features" / branch
    ledger = feature_dir / "review-ledger.md"
    roadmap = root / "specs" / "roadmap.md"
    state_dir = state_dir_for(root, branch)

    result = {
        "repo": str(root),
        "branch": branch,
        "state_dir": str(state_dir),
        "roadmap": roadmap_summary(roadmap),
    }
    if branch in MAIN_BRANCHES:
        result.update(state="idle", next={"action": "select_roadmap_task"})
        return result
    if not feature_dir.is_dir():
        result.update(
            state="corrupt",
            next={"action": "stop", "reason": f"missing {feature_dir.relative_to(root)}"},
        )
        return result

    if unit not in {None, "requirements.md", "plan.md", "code", "validation.md"}:
        raise ValueError(f"unsupported workflow unit: {unit}")
    state_file = state_dir / "state.json"
    operational = json.loads(state_file.read_text()) if state_file.is_file() else {}
    requirements = feature_dir / "requirements.md"
    requirements_hash = sha256(requirements) if requirements.is_file() else None
    requirements_accepted = bool(
        requirements_hash
        and has_real_requirements(requirements)
        and gate_accepted(ledger, requirements, requirements_hash)
    )
    plan = feature_dir / "plan.md"
    plan_hash = sha256(plan) if plan.is_file() else None
    plan_accepted = bool(
        requirements_accepted
        and plan_hash
        and has_real_plan(plan)
        and gate_accepted(ledger, plan, plan_hash)
    )
    code_base = (
        operational.get("base_commit")
        if operational.get("unit") == "code"
        else ledger_field(ledger, Path("code"), "base_commit")
    )
    code_target_hash = code_hash(root, code_base) if code_base else None
    code_accepted = bool(
        plan_accepted
        and code_target_hash
        and gate_accepted(ledger, Path("code"), code_target_hash)
    )
    if unit == "plan.md" and not requirements_accepted:
        raise ValueError("plan.md requires accepted requirements.md")
    if unit == "code" and not plan_accepted:
        raise ValueError("code requires accepted plan.md")
    if unit == "validation.md" and not code_accepted:
        raise ValueError("validation.md requires accepted code")
    unit = unit or (
        "validation.md"
        if code_accepted
        else "code"
        if plan_accepted
        else "plan.md"
        if requirements_accepted
        else "requirements.md"
    )
    workflow = workflow_config(config, unit)
    result["routing"] = {"author": workflow["author"], "reviewer": workflow["reviewer"]}
    if unit == "code":
        base_commit = (
            operational.get("base_commit")
            if operational.get("unit") == "code"
            else code_base or git(root, "rev-parse", "HEAD")
        )
        target_hash = code_hash(root, base_commit)
        real = target_hash is not None
        artifact = Path("code")
        result["base_commit"] = base_commit
    else:
        artifact = feature_dir / unit
        target_hash = sha256(artifact) if artifact.is_file() else None
        real = artifact_ready(artifact)
        if unit == "validation.md":
            result["base_commit"] = code_base
            result["code_hash"] = code_target_hash
    accepted = bool(target_hash and real and gate_accepted(ledger, artifact, target_hash))
    result["artifact"] = {
        "path": unit if unit == "code" else str(artifact.relative_to(root)),
        "exists": real if unit == "code" else artifact.is_file(),
        "hash": target_hash,
        "accepted": accepted,
    }
    operational_finalization = bool(
        (
            operational.get("repo"),
            operational.get("branch"),
            operational.get("unit"),
        )
        == (str(root), branch, unit)
        and operational.get("status") in {"running", "needs_user"}
        and (
            operational.get("phase") == "artifact_decision"
            or operational.get("resume_phase") == "artifact_decision"
        )
    )
    if accepted and not operational_finalization:
        result.update(
            state=f"{artifact.stem}_accepted",
            next={"action": "stop", "reason": "launcher slice not implemented"},
        )
        return result

    if (operational.get("repo"), operational.get("branch"), operational.get("unit")) == (
        str(root),
        branch,
        unit,
    ) and operational.get("status") in {"running", "needs_user"}:
        result["routing"] = {
            "author": operational.get("author_route", workflow["author"]),
            "reviewer": operational.get("reviewer_route", workflow["reviewer"]),
        }
        result["operational"] = {
            key: operational.get(key)
            for key in ("status", "phase", "review_round", "target_hash")
            if operational.get(key) is not None
        }
        if operational.get("finding"):
            result["operational"]["finding"] = operational["finding"].get("id", "unknown")
        if operational["status"] == "needs_user":
            result.update(state="needs_user", next={"action": "stop", "reason": "needs_user"})
            return result
        if operational.get("phase") == "artifact_decision":
            result.update(
                state="validation_finalizing",
                next={"unit": unit, "action": "commit_artifacts"},
            )
            return result
        role = "author" if operational.get("phase") == "author_needed" else "reviewer"
    else:
        role = "reviewer" if real else "author"
    result.update(
        state=f"{artifact.stem}_review" if role == "reviewer" else f"{artifact.stem}_authoring",
        next={"unit": unit, "action": role, **result["routing"][role]},
    )
    return result


def run_fake(model: str, effort: str, prompt: str, cwd: Path, session_id: str | None = None) -> dict:
    """Mimic an author or reviewer without executing a model."""
    request = json.loads(prompt)
    role = request["CONTROL"]["role"]
    target = request["TARGET"]
    unit = request.get("CONTRACT", {}).get("unit", Path(target["path"]).name)
    artifact = cwd / target["path"]
    new_id = "fake-" + hashlib.sha256(f"{model}\0{prompt}\0{cwd}".encode()).hexdigest()[:12]
    if role == "author":
        if unit == "code":
            fake_change = cwd / "feature-flow-fake-change.txt"
            fake_change.write_text(fake_change.read_text() + "updated\n" if fake_change.is_file() else "updated\n")
            response = {
                "status": "updated",
                "artifact": "code",
                "tests": ["python3 -m unittest: passed"],
            }
        elif unit == "validation.md":
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                artifact.read_text() + "\nRevision: validation updated.\n"
                if artifact.is_file()
                else "# Validation\n\nStatus: passed\n\n## Test eseguiti\n\n"
                "- [x] `python3 -m unittest` - passed\n\n"
                "## Criteri di accettazione\n\n- [x] Workflow verificato end-to-end.\n\n"
                "## Evidenze\n\n- Output completi nei log locali.\n\n"
                "## Verifiche residue\n\n- none\n"
            )
            response = {
                "status": "updated",
                "artifact": target["path"],
                "tests": ["python3 -m unittest: passed"],
            }
        elif artifact.is_file():
            artifact.write_text(
                artifact.read_text() + f"\nRevision {request['CONTROL']['round']}: findings addressed.\n"
            )
        else:
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if artifact.name == "requirements.md":
                artifact.write_text(
                    "# Requirements\n\n## Problema\n\nProblema verificabile.\n\n"
                    "## Scope\n\n- Comportamento richiesto.\n- Stato persistente.\n- Review con hash.\n\n"
                    "## Fuori scope\n\n- Provider reali.\n- Plan e codice.\n\n"
                    "## Decisioni di design\n\n- Standard library.\n- Un solo writer.\n"
                    "- Massimo tre review.\n\n## Criteri di accettazione\n\n"
                    "- Artifact creato.\n- Hash verificato.\n- Ledger aggiornato.\n"
                    "- Sessioni separate.\n- Output compatto.\n- Log locali.\n"
                    "- Dry-run read-only.\n- Stato riprendibile.\n"
                )
            else:
                artifact.write_text(
                    "# Piano di implementazione\n\n## Obiettivo\n\n"
                    "Estendere il workflow rispettando i requirements accettati.\n\n"
                    "## Vincoli\n\n- Standard library.\n- Un solo writer.\n- Review read-only.\n\n"
                    "## Sequenza\n\n### Task 1: implementazione\n\n"
                    "- [ ] Complete implementation slice\n"
                    "- File: `feature_flow.py`\n- Simboli: state machine documentale\n"
                    "- Dipendenze: requirements accettati\n- Verifica: test unitari mirati\n\n"
                    "### Task 2: test\n\n- [ ] Complete regression coverage\n"
                    "- File: `tests/test_feature_flow.py`\n- Verifica: suite completa\n\n"
                    "## Rischi\n\n- Separazione sessioni tra artifact.\n"
                    "- Hash del target controllato prima dell'accettazione.\n"
                )
        if unit not in {"code", "validation.md"}:
            response = {"status": "updated", "artifact": target["path"]}
    else:
        response = {
            "status": "accepted",
            "reviewed_artifact": target["path"],
            "reviewed_hash": target["hash"],
            "findings": [],
        }
    return {
        "provider": "fake",
        "model": model,
        "effort": effort,
        "session_id": session_id or new_id,
        "response": response,
        "returncode": 0,
    }


def context_pack(
    root: Path,
    artifact: Path | None,
    role: str,
    round_number: int,
    target_hash: str | None,
    findings: list,
    base_commit: str | None = None,
) -> str:
    unit = artifact.name if artifact else "code"
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    evidence = {}
    for relative in (
        "CLAUDE.md",
        "AGENTS.md",
        "specs/mission.md",
        "specs/tech-stack.md",
        "specs/roadmap.md",
    ):
        path = root / relative
        if path.is_file():
            evidence[relative] = path.read_text(errors="replace")
    if unit == "plan.md":
        requirements = artifact.with_name("requirements.md")
        evidence[str(requirements.relative_to(root))] = requirements.read_text(errors="replace")
    if unit in {"code", "validation.md"}:
        feature_dir = root / "specs" / "features" / branch
        for name in ("requirements.md", "plan.md"):
            path = feature_dir / name
            evidence[str(path.relative_to(root))] = path.read_text(errors="replace")
    if unit == "code":
        target_path = "code"
        target_content = code_snapshot(root, base_commit)
    else:
        target_path = str(artifact.relative_to(root))
        target_content = artifact.read_text(errors="replace") if artifact.is_file() else None
        if unit == "validation.md":
            evidence["accepted-code.diff"] = code_snapshot(root, base_commit)
    if role == "author":
        output_schema = {"status": "updated", "artifact": target_path}
        if unit in {"code", "validation.md"}:
            output_schema["tests"] = ["command: result"]
    else:
        output_schema = {
            "status": "accepted | needs_changes | needs_user",
            "reviewed_artifact": target_path,
            "reviewed_hash": "sha256:<hash>",
            "findings": [],
        }
    payload = {
            "INSTRUCTIONS": (PROMPTS_DIR / f"{Path(unit).stem}-{role}.txt").read_text(),
            "CONTROL": {"role": role, "round": round_number},
            "CONTRACT": {
                "unit": unit,
                "max_reviews": load_config()["limits"]["max_reviews"],
                "author_may_write": role == "author",
                "base_commit": base_commit,
            },
            "TARGET": {
                "path": target_path,
                "hash": target_hash,
                "content": target_content,
            },
            "EVIDENCE": evidence,
            "OPEN FINDINGS": findings,
            "OUTPUT SCHEMA": output_schema,
    }
    if artifact:
        payload["TEMPLATE"] = (TEMPLATES_DIR / artifact.name).read_text()
    return json.dumps(payload, separators=(",", ":"))


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def active_run_dir(cwd: Path) -> Path:
    root = Path(git(cwd.resolve(), "rev-parse", "--show-toplevel")).resolve()
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    state = json.loads((state_dir_for(root, branch) / "state.json").read_text())
    if (
        state.get("repo") != str(root)
        or state.get("branch") != branch
        or state.get("status") != "running"
        or not state.get("run_id")
    ):
        raise ValueError("no active feature-flow run")
    run_dir = state_dir_for(root, branch) / "runs" / state["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def process_paths(cwd: Path, provider: str) -> tuple[Path, Path, str]:
    stem = f"raw-{uuid4().hex[:8]}-{provider}"
    run_dir = active_run_dir(cwd)
    return run_dir / f"{stem}.stdout", run_dir / f"{stem}.stderr", stem


def structured(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def prompt_role(prompt: str) -> str:
    try:
        return json.loads(prompt)["CONTROL"]["role"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "reviewer"


def prompt_unit(prompt: str) -> str | None:
    try:
        return json.loads(prompt)["CONTRACT"]["unit"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def response_schema(prompt: str) -> dict:
    if prompt_role(prompt) == "author":
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "const": "updated"},
                "artifact": {"type": "string"},
            },
            "required": ["status", "artifact"],
        }
        if prompt_unit(prompt) in {"code", "validation.md"}:
            schema["properties"]["tests"] = {
                "type": "array",
                "items": {"type": "string"},
            }
            schema["required"].append("tests")
        return schema
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "severity": {
                "type": "string",
                "enum": ["blocker", "critical", "major", "minor"],
            },
            "owner": {"type": "string", "enum": ["author", "reviewer", "user"]},
            "evidence": {"type": "string"},
            "issue": {"type": "string"},
            "required_change": {"type": "string"},
            "why": {"type": "string"},
        },
        "required": [
            "id",
            "severity",
            "owner",
            "evidence",
            "issue",
            "required_change",
            "why",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "needs_changes", "needs_user"],
            },
            "reviewed_artifact": {"type": "string"},
            "reviewed_hash": {"type": "string"},
            "findings": {"type": "array", "items": finding},
        },
        "required": ["status", "reviewed_artifact", "reviewed_hash", "findings"],
    }


def normalized_process(
    provider: str,
    model: str,
    effort: str,
    process: subprocess.CompletedProcess,
    session_id: str | None,
    response: dict | None,
    stdout_path: Path,
    stderr_path: Path,
    extra_logs: dict | None = None,
) -> dict:
    logs = {"stdout": str(stdout_path), "stderr": str(stderr_path)}
    logs.update(extra_logs or {})
    ok = process.returncode == 0 and bool(session_id) and response is not None
    return {
        "ok": ok,
        "provider": provider,
        "model": model,
        "effort": effort,
        "session_id": session_id,
        "response": response,
        "returncode": process.returncode,
        "error": None if ok else ("process_failed" if process.returncode else "invalid_output"),
        "logs": logs,
    }


def run_claude(
    model: str,
    effort: str,
    prompt: str,
    cwd: Path,
    session_id: str | None = None,
) -> dict:
    stdout_path, stderr_path, _ = process_paths(cwd, "claude")
    command = [
        "claude",
        "-p",
        "--safe-mode",
        "--output-format",
        "json",
        "--model",
        model,
        "--effort",
        effort,
        "--json-schema",
        json.dumps(response_schema(prompt), separators=(",", ":")),
    ]
    role = prompt_role(prompt)
    command += ["--permission-mode", "acceptEdits" if role == "author" else "plan"]
    if session_id:
        command += ["--resume", session_id]
    command.append(prompt)
    process = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout_path.write_text(process.stdout or "")
    stderr_path.write_text(process.stderr or "")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    response = structured(result.get("structured_output"))
    if response is None:
        response = structured(result.get("result"))
    return normalized_process(
        "claude",
        model,
        effort,
        process,
        result.get("session_id") or session_id,
        response,
        stdout_path,
        stderr_path,
    )


def run_codex(
    model: str,
    effort: str,
    prompt: str,
    cwd: Path,
    sandbox: str,
    session_id: str | None = None,
) -> dict:
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(f"invalid Codex sandbox: {sandbox}")
    stdout_path, stderr_path, stem = process_paths(cwd, "codex")
    run_dir = stdout_path.parent
    schema_path = run_dir / f"{prompt_role(prompt)}-schema.json"
    write_json(schema_path, response_schema(prompt))
    response_path = run_dir / f"{stem}.response"
    common = [
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--output-schema",
        str(schema_path),
        "-o",
        str(response_path),
    ]
    if session_id:
        command = [
            "codex",
            "exec",
            "resume",
            *common,
            "-c",
            f'sandbox_mode="{sandbox}"',
            session_id,
            prompt,
        ]
    else:
        command = [
            "codex",
            "exec",
            *common,
            "--color",
            "never",
            "-s",
            sandbox,
            "-C",
            str(cwd),
            prompt,
        ]
    process = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout_path.write_text(process.stdout or "")
    stderr_path.write_text(process.stderr or "")
    thread_id = session_id
    for line in (process.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = event["thread_id"]
    response = structured(response_path.read_text()) if response_path.is_file() else None
    return normalized_process(
        "codex",
        model,
        effort,
        process,
        thread_id,
        response,
        stdout_path,
        stderr_path,
        {"response": str(response_path)},
    )


@contextmanager
def workflow_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("workflow already running for this repository and branch") from error
        yield


def call_provider(provider, route: dict, prompt: str, root: Path, session_id: str | None, log: Path):
    try:
        if provider is not None:
            result = provider(route["model"], route["effort"], prompt, root, session_id)
        elif route["provider"] == "claude":
            result = run_claude(route["model"], route["effort"], prompt, root, session_id)
        elif route["provider"] == "codex":
            result = run_codex(
                route["model"],
                route["effort"],
                prompt,
                root,
                route["sandbox"],
                session_id,
            )
        else:
            raise ValueError(f"unsupported provider: {route['provider']}")
    except (OSError, ValueError) as error:
        write_json(
            log,
            {
                "ok": False,
                "provider": route["provider"],
                "model": route["model"],
                "session_id": session_id,
                "response": None,
                "error": "provider_exception",
            },
        )
        raise ProviderFailure(f"{route['provider']} failed; see local run logs") from error
    write_json(log, result)
    if not isinstance(result, dict):
        raise ProviderFailure("provider returned an invalid result")
    if result.get("ok") is False:
        raise ProviderFailure(f"{result.get('provider', 'provider')} failed; see local run logs")
    if not result.get("session_id") or not isinstance(result.get("response"), dict):
        raise ProviderFailure("provider returned an invalid result")
    return result


def append_fallback(ledger: Path, role: str, failed: dict, state: dict, stopped: bool) -> None:
    number = len(state.get("fallbacks", []))
    unit = state["unit"]
    lines = [
        f"## {unit} - fallback {number}",
        f"artifact: {unit}",
        f"role: {role}",
        f"failed_model: {failed['model']}",
        f"status: {'needs_user' if stopped else 'fallback'}",
    ]
    if not stopped:
        lines += [
            f"author_model: {state['author_route']['model']}",
            f"reviewer_model: {state['reviewer_route']['model']}",
        ]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as output:
        if ledger.stat().st_size:
            output.write("\n")
        output.write("\n".join(lines) + "\n")


def apply_fallback(state: dict, role: str, config: dict, ledger: Path) -> bool:
    failed = state[f"{role}_route"]
    for transition in workflow_config(config, state["unit"]).get("fallbacks", []):
        trigger = transition["on"]
        if (
            trigger["role"],
            trigger["provider"],
            trigger["model"],
        ) != (role, failed["provider"], failed["model"]):
            continue
        history = state.setdefault("fallbacks", [])
        if "to" not in transition:
            history.append({"role": role, "failed": failed, "status": "needs_user"})
            append_fallback(ledger, role, failed, state, True)
            return False
        author = dict(transition["to"]["author"])
        reviewer = dict(transition["to"]["reviewer"])
        if author["model"] == reviewer["model"] or transition["to"][role]["model"] == failed[
            "model"
        ]:
            raise ValueError("fallback would cause auto-review or retry the failed model")
        state.update(
            author_route=author,
            reviewer_route=reviewer,
            author_model=author["model"],
            reviewer_model=reviewer["model"],
            author_session=None,
            reviewer_session=None,
        )
        history.append(
            {
                "role": role,
                "failed": failed,
                "author": author,
                "reviewer": reviewer,
                "status": "fallback",
            }
        )
        append_fallback(ledger, role, failed, state, False)
        return True
    return False


def append_review(
    ledger: Path,
    round_number: int,
    target_hash: str,
    state: dict,
    review: dict,
) -> None:
    findings = review.get("findings", [])
    unit = state["unit"]
    lines = [
        f"## {unit} - round {round_number}",
        f"artifact: {unit}",
        f"target_hash: {target_hash}",
        f"author_model: {state['author_model']}",
        f"author_session: {state.get('author_session') or 'none'}",
        f"reviewer_model: {state['reviewer_model']}",
        f"reviewer_session: {state['reviewer_session']}",
        "",
        "### Review",
        f"status: {review['status']}",
        f"reviewed_hash: {review['reviewed_hash']}",
    ]
    if unit == "code":
        lines += [
            f"base_commit: {state['base_commit']}",
            f"diff_hash: {target_hash}",
            "tests: " + json.dumps(state.get("tests", []), separators=(",", ":")),
        ]
    elif unit == "validation.md":
        lines.append("tests: " + json.dumps(state.get("tests", []), separators=(",", ":")))
    lines.extend(
        f"- {finding.get('id', 'unknown')} {finding.get('severity', 'major')}: "
        f"{finding.get('issue', 'change required')}"
        for finding in findings
    )
    if review["status"] == "accepted":
        lines += [
            "",
            "### Decision",
            "status: accepted",
            f"accepted_hash: {target_hash}",
            "open_items: none",
        ]
        if unit == "code":
            lines.append(f"result_commit: {state['result_commit']}")
    elif review["status"] == "needs_user":
        lines += [
            "",
            "### Decision",
            "status: needs_user",
            "open_items: " + ",".join(item.get("id", "unknown") for item in findings),
        ]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as output:
        if ledger.stat().st_size:
            output.write("\n")
        output.write("\n".join(lines) + "\n")


def append_author_response(ledger: Path, findings: list) -> None:
    with ledger.open("a") as output:
        output.write("\n### Author response\n")
        for finding in findings:
            output.write(f"- {finding.get('id', 'unknown')} accepted: artifact updated\n")


def append_needs_user(ledger: Path, finding_id: str) -> None:
    with ledger.open("a") as output:
        output.write(
            "\n### Decision\nstatus: needs_user\n"
            f"open_items: {finding_id}\n"
        )


def needs_user(state: dict, finding: dict, resume_phase: str | None = None) -> dict:
    if "question" not in finding:
        finding["question"] = finding.get("issue", "Decisione utente richiesta")
    state.update(
        status="needs_user",
        finding=finding,
        phase="stopped",
        resume_phase=resume_phase,
    )
    return state


def public_state(state: dict, state_dir: Path) -> dict:
    result = {
        "status": state["status"],
        "unit": state["unit"],
        "review_round": state["review_round"],
        "target_hash": state.get("target_hash"),
        "state_dir": str(state_dir),
        "run_id": state.get("run_id"),
    }
    if state["unit"] == "code":
        result.update(
            base_commit=state.get("base_commit"),
            diff_hash=state.get("target_hash"),
            tests_run=len(state.get("tests", [])),
            result_commit=state.get("result_commit"),
        )
    elif state["unit"] == "validation.md":
        result["tests_run"] = len(state.get("tests", []))
        if state.get("artifact_commit"):
            result["artifact_commit"] = state["artifact_commit"]
    if state.get("finding"):
        finding = state["finding"]
        result.update(
            finding=finding.get("id", "unknown"),
            question=finding.get("question"),
            choices=finding.get("choices", []),
        )
        if state.get("resume_phase"):
            result["resume_command"] = (
                f"feature-flow resolve {result['finding']} --answer <answer> "
                f"--repo {state['repo']}"
            )
    return result


def resolve_finding(repo: Path, finding_id: str, answer: str) -> dict:
    answer = " ".join(answer.split())
    if not answer:
        raise ValueError("answer must not be empty")
    observed = inspect_repo(repo)
    if observed["state"] in {"idle", "corrupt"}:
        raise ValueError("repository has no active feature workflow")
    root = Path(observed["repo"])
    state_dir = Path(observed["state_dir"])
    state_file = state_dir / "state.json"
    if not state_file.is_file():
        raise ValueError("workflow state not found")
    with workflow_lock(state_dir):
        state = json.loads(state_file.read_text())
        finding = state.get("finding", {})
        if state.get("status") != "needs_user" or finding.get("id") != finding_id:
            raise ValueError(f"open finding not found: {finding_id}")
        if not state.get("resume_phase"):
            raise ValueError(f"finding cannot be resumed automatically: {finding_id}")
        resolved = dict(finding, user_answer=answer, status="resolved")
        state.setdefault("resolutions", []).append(resolved)
        state.update(
            status="running",
            phase=state.pop("resume_phase"),
            findings=[resolved],
        )
        state.pop("finding", None)
        write_json(state_file, state)

        ledger = root / "specs" / "features" / state["branch"] / "review-ledger.md"
        with ledger.open("a") as output:
            if ledger.stat().st_size:
                output.write("\n")
            unit = state["unit"]
            ledger_artifact = (
                "workflow-artifacts" if finding_id == "commit_artifacts" else unit
            )
            output.write(
                f"## {unit} - resolution {finding_id}\n"
                f"artifact: {ledger_artifact}\n"
                f"finding: {finding_id}\n"
                f"answer: {answer}\n"
                "status: resolved\n"
            )
    return {
        "status": "resolved",
        "finding": finding_id,
        "next": {"action": "run"},
        "resume_command": f"feature-flow run --repo {root}",
    }


def run_artifact(repo: Path, unit: str, provider=None) -> dict:
    observed = inspect_repo(repo, unit)
    if observed["state"] in {"idle", "corrupt"}:
        return {"status": observed["state"], "next": observed["next"]}
    if observed["state"] == f"{Path(unit).stem}_accepted":
        state_dir = Path(observed["state_dir"])
        state_file = state_dir / "state.json"
        if state_file.is_file():
            state = json.loads(state_file.read_text())
            if state.get("status") == "accepted" and state.get("target_hash") == observed[
                "artifact"
            ]["hash"]:
                return public_state(state, state_dir)
        return {
            "status": "accepted",
            "unit": unit,
            "review_round": 0,
            "target_hash": observed["artifact"]["hash"],
            "state_dir": observed["state_dir"],
            "run_id": None,
        }

    root = Path(observed["repo"])
    branch = observed["branch"]
    state_dir = Path(observed["state_dir"])
    artifact = None if unit == "code" else root / observed["artifact"]["path"]
    ledger = root / "specs" / "features" / branch / "review-ledger.md"
    config = load_config()
    workflow = workflow_config(config, unit)
    with workflow_lock(state_dir):
        state_file = state_dir / "state.json"
        state = json.loads(state_file.read_text()) if state_file.is_file() else {}
        if (state.get("repo"), state.get("branch"), state.get("unit")) != (
            str(root),
            branch,
            unit,
        ):
            base_commit = observed.get("base_commit")
            initial_hash = (
                code_hash(root, base_commit)
                if unit == "code"
                else sha256(artifact) if artifact.is_file() else None
            )
            initial_ready = bool(initial_hash) if unit == "code" else artifact_ready(artifact)
            state = {
                "version": 1,
                "repo": str(root),
                "branch": branch,
                "unit": unit,
                "status": "running",
                "phase": (
                    "author_needed"
                    if unit == "code"
                    else "review_needed" if initial_ready else "author_needed"
                ),
                "review_round": 0,
                "target_hash": initial_hash,
                "author_model": workflow["author"]["model"],
                "reviewer_model": workflow["reviewer"]["model"],
                "author_route": dict(workflow["author"]),
                "reviewer_route": dict(workflow["reviewer"]),
                "author_session": None,
                "reviewer_session": None,
                "findings": [],
                "fallbacks": [],
            }
            if unit in {"code", "validation.md"}:
                state.update(base_commit=base_commit, tests=[])
            if unit == "validation.md":
                state["accepted_code_hash"] = observed["code_hash"]
        state.setdefault("author_route", dict(workflow["author"]))
        state.setdefault("reviewer_route", dict(workflow["reviewer"]))
        state.setdefault("fallbacks", [])
        current_hash = lambda: (
            code_hash(root, state["base_commit"])
            if unit == "code"
            else sha256(artifact) if artifact.is_file() else None
        )
        if state["status"] == "needs_user":
            return public_state(state, state_dir)
        if state["status"] == "accepted" and state.get("target_hash") == current_hash():
            return public_state(state, state_dir)

        state.update(status="running", run_id=uuid4().hex[:12])
        run_dir = state_dir / "runs" / state["run_id"]
        run_dir.mkdir(parents=True)
        write_json(state_file, state)
        calls = 0
        stale_changes = 0

        while state["status"] == "running":
            if state["phase"] == "artifact_decision":
                resolution = next(
                    (
                        item
                        for item in reversed(state.get("resolutions", []))
                        if item.get("id") == "commit_artifacts"
                    ),
                    None,
                )
                answer = (resolution or {}).get("user_answer", "").strip().lower()
                if answer == "commit":
                    try:
                        artifact_commit = commit_artifacts(root, branch)
                    except ValueError:
                        needs_user(
                            state,
                            {
                                "id": "artifact_commit_failed",
                                "question": "Commit degli artifact fallito",
                            },
                        )
                        write_json(state_file, state)
                        break
                    state["artifact_commit"] = {
                        "status": "committed",
                        "commit": artifact_commit,
                    }
                elif answer == "leave uncommitted":
                    state["artifact_commit"] = {"status": "skipped"}
                else:
                    needs_user(
                        state,
                        {
                            "id": "commit_artifacts",
                            "question": "Rispondi con commit oppure leave uncommitted",
                            "choices": ["commit", "leave uncommitted"],
                        },
                        "artifact_decision",
                    )
                    write_json(state_file, state)
                    break
                state.update(status="accepted", phase="closed", findings=[])
                write_json(state_file, state)
                break

            if state["phase"] == "author_needed":
                open_findings = state.get("findings", [])
                calls += 1
                prompt = context_pack(
                    root,
                    artifact,
                    "author",
                    state["review_round"],
                    state.get("target_hash"),
                    state.get("findings", []),
                    state.get("base_commit"),
                )
                try:
                    result = call_provider(
                        provider,
                        state["author_route"],
                        prompt,
                        root,
                        state.get("author_session"),
                        run_dir / f"{calls:02d}-author.json",
                    )
                except ProviderFailure:
                    if apply_fallback(state, "author", config, ledger):
                        write_json(state_file, state)
                        continue
                    needs_user(
                        state,
                        {
                            "id": "author_provider",
                            "question": "Nessun fallback autore indipendente disponibile",
                        },
                    )
                    write_json(state_file, state)
                    break
                state["author_session"] = result["session_id"]
                if unit == "validation.md" and code_hash(
                    root, state["base_commit"]
                ) != state["accepted_code_hash"]:
                    needs_user(
                        state,
                        {"id": "e2e_code_changed", "question": "Il runner E2E ha modificato il codice"},
                    )
                    write_json(state_file, state)
                    break
                new_hash = current_hash()
                if not new_hash or (unit != "code" and not artifact_ready(artifact)):
                    needs_user(
                        state,
                        {"id": "author_output", "question": f"{unit} non valido"},
                        "author_needed",
                    )
                    write_json(state_file, state)
                    break
                if open_findings:
                    append_author_response(ledger, open_findings)
                if unit in {"code", "validation.md"}:
                    tests = result["response"].get("tests")
                    if not isinstance(tests, list) or not tests or not all(
                        isinstance(test, str) and test.strip() for test in tests
                    ):
                        needs_user(
                            state,
                            {"id": "author_tests", "question": "Test eseguiti non dichiarati"},
                            "author_needed",
                        )
                        write_json(state_file, state)
                        break
                    state["tests"] = state.get("tests", []) + tests
                if unit == "code":
                    try:
                        state["working_commit"] = commit_code(
                            root, branch, state["base_commit"]
                        )
                    except ValueError:
                        needs_user(
                            state,
                            {"id": "code_commit", "question": "Commit del codice fallito"},
                            "author_needed",
                        )
                        write_json(state_file, state)
                        break
                    new_hash = current_hash()
                state.update(
                    target_hash=new_hash, phase="review_needed", findings=[]
                )
                write_json(state_file, state)
                continue

            if state["review_round"] >= config["limits"]["max_reviews"]:
                needs_user(
                    state,
                    {
                        "id": "review_limit",
                        "question": "Tre review completate senza accettazione",
                        "choices": ["modifica manuale", "ferma workflow"],
                    },
                )
                write_json(state_file, state)
                break

            reviewed_hash = current_hash()
            state["target_hash"] = reviewed_hash
            calls += 1
            prompt = context_pack(
                root,
                artifact,
                "reviewer",
                state["review_round"] + 1,
                reviewed_hash,
                state.get("findings", []) + state.get("resolutions", []),
                state.get("base_commit"),
            )
            try:
                result = call_provider(
                    provider,
                    state["reviewer_route"],
                    prompt,
                    root,
                    state.get("reviewer_session"),
                    run_dir / f"{calls:02d}-reviewer.json",
                )
            except ProviderFailure:
                if apply_fallback(state, "reviewer", config, ledger):
                    write_json(state_file, state)
                    continue
                needs_user(
                    state,
                    {
                        "id": "reviewer_provider",
                        "question": "Nessun fallback reviewer indipendente disponibile",
                    },
                )
                write_json(state_file, state)
                break
            state["reviewer_session"] = result["session_id"]
            if state.get("author_session") == state["reviewer_session"]:
                raise ValueError("author and reviewer sessions must be separate")
            if unit == "validation.md" and code_hash(
                root, state["base_commit"]
            ) != state["accepted_code_hash"]:
                needs_user(
                    state,
                    {"id": "e2e_code_changed", "question": "Il codice e cambiato durante E2E"},
                )
                write_json(state_file, state)
                break
            if current_hash() != reviewed_hash:
                stale_changes += 1
                if stale_changes >= config["limits"]["max_reviews"]:
                    needs_user(
                        state,
                        {"id": "stale_hash", "question": "Artifact cambiato durante tre review"},
                    )
                    write_json(state_file, state)
                    break
                state["target_hash"] = current_hash()
                write_json(state_file, state)
                continue

            review = result["response"]
            if review.get("status") not in {"accepted", "needs_changes", "needs_user"}:
                raise ValueError("reviewer returned an invalid status")
            if review.get("reviewed_hash") != reviewed_hash:
                raise ValueError("reviewer returned the wrong hash")
            findings = review.get("findings", [])
            if not isinstance(findings, list) or (
                review["status"] == "needs_changes" and not findings
            ):
                needs_user(
                    state,
                    {
                        "id": "reviewer_output",
                        "question": "Review non conforme: findings incoerenti con status",
                    },
                    "review_needed",
                )
                write_json(state_file, state)
                break
            if review["status"] == "needs_changes" and all(
                finding.get("severity") == "minor" for finding in findings
            ):
                # ponytail: i minor restano nel ledger ma non costano un round di riscrittura
                review = {**review, "status": "accepted"}

            state["review_round"] += 1
            if review["status"] == "accepted" and unit == "code":
                state["result_commit"] = state["working_commit"]
            append_review(ledger, state["review_round"], reviewed_hash, state, review)
            if review["status"] == "accepted":
                if unit == "validation.md":
                    needs_user(
                        state,
                        {
                            "id": "commit_artifacts",
                            "question": "Vuoi committare gli artifact del workflow?",
                            "choices": ["commit", "leave uncommitted"],
                        },
                        "artifact_decision",
                    )
                    write_json(state_file, state)
                    break
                state.update(status="accepted", phase="closed", findings=[])
                write_json(state_file, state)
                break
            if review["status"] == "needs_user":
                decision = findings[0] if findings else {"id": "reviewer_decision"}
                state["findings"] = findings or [decision]
                needs_user(state, decision, "author_needed")
                write_json(state_file, state)
                break

            if state["review_round"] >= config["limits"]["max_reviews"]:
                finding_id = findings[0].get("id", "review_limit")
                append_needs_user(ledger, finding_id)
                needs_user(
                    state,
                    {
                        "id": finding_id,
                        "question": "Tre review completate senza accettazione",
                        "choices": ["modifica manuale", "ferma workflow"],
                    },
                )
                write_json(state_file, state)
                break

            state.update(phase="author_needed", findings=findings)
            write_json(state_file, state)
            continue

        return public_state(state, state_dir)


def run_requirements(repo: Path, provider=None) -> dict:
    return run_artifact(repo, "requirements.md", provider)


def run_docs(repo: Path, provider=None) -> dict:
    observed = inspect_repo(repo)
    if observed["state"] in {"idle", "corrupt"}:
        return {"status": observed["state"], "next": observed["next"]}
    return run_artifact(repo, Path(observed["artifact"]["path"]).name, provider)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="feature-flow")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("status", "run"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True, type=Path)
        if name == "run":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--fake", action="store_true")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("finding")
    resolve.add_argument("--answer", required=True)
    resolve.add_argument("--repo", type=Path, default=Path("."))
    validate = commands.add_parser("validate-config")
    validate.add_argument("--config", type=Path, default=CONFIG_PATH)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate-config":
            config = load_config(args.config)
            result = {
                "status": "valid",
                "config": str(args.config.resolve()),
                "version": config["version"],
                "workflows": list(WORKFLOW_NAMES),
            }
        elif args.command == "resolve":
            result = resolve_finding(args.repo, args.finding, args.answer)
        elif args.command == "run" and not args.dry_run:
            result = run_docs(args.repo, run_fake if args.fake else None)
        else:
            result = inspect_repo(args.repo)
        if args.command == "run":
            result["dry_run"] = args.dry_run
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.exit(main())
