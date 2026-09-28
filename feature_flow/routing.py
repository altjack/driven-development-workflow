"""Shadow classification of task difficulty for adaptive model/effort routing.

The classifier answers typed questions about a gate; the tier and the suggested
efforts are computed here with a versioned, deterministic formula. In shadow mode
nothing is applied: the result is only recorded for later calibration.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
TIERS = ("S", "M", "L", "XL")
ROUTING_MODES = {"off", "shadow"}
CLASSIFIER_PROVIDERS = {"openrouter"}
FORMULA_VERSION = 1
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
API_KEY_ENV = "FEATURE_FLOW_OPENROUTER_API_KEY"
THRESHOLDS = ("min_confidence", "underestimate_budget", "risk_threshold", "rounds_threshold")

QUESTIONS = {
    "scope": {
        "type": "score",
        "instructions": "How broad is the planned change within the repository?",
        "criteria": [
            "a single function or file",
            "a few files within one module",
            "several modules",
            "cross-cutting, architectural",
        ],
    },
    "ambiguity": {
        "type": "score",
        "instructions": "How ambiguous are the requirements for this change?",
        "criteria": [
            "complete and verifiable",
            "some points need interpretation",
            "open design decisions",
        ],
    },
    "novel_pattern": {
        "type": "noul",
        "instructions": "Does the change need patterns, libraries or infrastructure new to this repository?",
    },
    "tests_exist": {
        "type": "noul",
        "instructions": "Is the affected area already covered by automated tests?",
    },
    "risk_security": {
        "type": "noul",
        "instructions": "Does the change touch authentication, permissions or secrets?",
    },
    "risk_data_migration": {
        "type": "noul",
        "instructions": "Does the change require a data or schema migration?",
    },
    "risk_public_api": {
        "type": "noul",
        "instructions": "Does the change modify a public API or an external contract?",
    },
    "review_rounds": {
        "type": "score",
        "instructions": "How many review rounds will this gate likely need before acceptance?",
        "criteria": [
            "accepted at the first round",
            "needs a second round",
            "needs three rounds",
        ],
    },
}
RISK_QUESTIONS = {
    "risk_security": "security",
    "risk_data_migration": "data_migration",
    "risk_public_api": "public_api",
}


class ClassifierError(ValueError):
    pass


class ClassifierSkipped(ClassifierError):
    """The classifier is not configured locally; nothing is recorded in the ledger."""


def routing_error(path: str, message: str) -> ValueError:
    return ValueError(f"invalid workflow config at {path}: {message}")


def validate_routing(routing: object, workflow_names: tuple[str, ...]) -> None:
    path = "$.routing"
    if not isinstance(routing, dict):
        raise routing_error(path, "must be an object")
    required = {"mode", "classifier", "formula_version", "effort_by_tier", "reviewer_floor_on_risk", *THRESHOLDS}
    missing = required - set(routing)
    if missing:
        raise routing_error(path, f"missing keys: {', '.join(sorted(missing))}")
    unknown = set(routing) - required
    if unknown:
        raise routing_error(path, f"unknown keys: {', '.join(sorted(unknown))}")
    if routing["mode"] not in ROUTING_MODES:
        raise routing_error(f"{path}.mode", f"must be one of {sorted(ROUTING_MODES)}")
    classifier = routing["classifier"]
    if not isinstance(classifier, dict) or set(classifier) != {"provider", "model", "timeout_s"}:
        raise routing_error(f"{path}.classifier", "must define exactly provider, model and timeout_s")
    if classifier["provider"] not in CLASSIFIER_PROVIDERS:
        raise routing_error(
            f"{path}.classifier.provider", f"must be one of {sorted(CLASSIFIER_PROVIDERS)}"
        )
    if not isinstance(classifier["model"], str) or not classifier["model"].strip():
        raise routing_error(f"{path}.classifier.model", "must be a non-empty string")
    timeout = classifier["timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise routing_error(f"{path}.classifier.timeout_s", "must be a number in (0, 60]")
    if routing["formula_version"] != FORMULA_VERSION or isinstance(routing["formula_version"], bool):
        raise routing_error(f"{path}.formula_version", f"must be {FORMULA_VERSION}")
    for key in THRESHOLDS:
        value = routing[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise routing_error(f"{path}.{key}", "must be a number between 0 and 1")
    if routing["reviewer_floor_on_risk"] not in EFFORT_ORDER:
        raise routing_error(f"{path}.reviewer_floor_on_risk", f"must be one of {list(EFFORT_ORDER)}")
    table = routing["effort_by_tier"]
    if not isinstance(table, dict) or set(table) != set(workflow_names):
        raise routing_error(f"{path}.effort_by_tier", f"must define exactly {list(workflow_names)}")
    for name in workflow_names:
        tiers = table[name]
        if not isinstance(tiers, dict) or set(tiers) != set(TIERS):
            raise routing_error(f"{path}.effort_by_tier.{name}", f"must define exactly {list(TIERS)}")
        for tier in TIERS:
            efforts = tiers[tier]
            tier_path = f"{path}.effort_by_tier.{name}.{tier}"
            if not isinstance(efforts, dict) or set(efforts) != {"author", "reviewer"}:
                raise routing_error(tier_path, "must define exactly author and reviewer")
            for role, effort in efforts.items():
                if effort not in EFFORT_ORDER:
                    raise routing_error(f"{tier_path}.{role}", f"must be one of {list(EFFORT_ORDER)}")


def build_request(model: str, state: dict) -> dict:
    return {"model": model, "state": state, "questions": QUESTIONS}


def input_hash(request: dict) -> str:
    payload = json.dumps(
        {**request, "formula_version": FORMULA_VERSION}, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def probability(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ClassifierError(f"{where}: probability out of range")
    return float(value)


def parse_answers(raw: object) -> dict:
    """Validate a Decisions API response against QUESTIONS.

    The envelope is not yet confirmed against the official docs, so answers are
    accepted either at the top level or under "answers"/"decisions".
    """
    if not isinstance(raw, dict):
        raise ClassifierError("response must be an object")
    container = next(
        (raw[key] for key in ("answers", "decisions") if isinstance(raw.get(key), dict)),
        raw,
    )
    answers = {}
    for key, question in QUESTIONS.items():
        answer = container.get(key)
        if not isinstance(answer, dict):
            raise ClassifierError(f"missing answer: {key}")
        if question["type"] == "noul":
            value = answer.get("noul", answer.get("probability"))
            answers[key] = {"p": round(probability(value, key), 4)}
            continue
        levels = len(question["criteria"])
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict):
            raise ClassifierError(f"{key}: missing probabilities")
        distribution = [
            probability(probabilities.get(str(level), 0), f"{key}[{level}]") for level in range(levels)
        ]
        if set(map(str, probabilities)) - {str(level) for level in range(levels)}:
            raise ClassifierError(f"{key}: unknown levels")
        if abs(sum(distribution) - 1) > 0.02:
            raise ClassifierError(f"{key}: probabilities must sum to 1")
        answers[key] = {
            "probs": [round(value, 4) for value in distribution],
            "confidence": round(probability(answer.get("confidence"), f"{key}.confidence"), 4),
        }
    return answers


def raise_effort(effort: str, floor: str) -> str:
    return max(effort, floor, key=EFFORT_ORDER.index)


def classify(
    answers: dict,
    routing: dict,
    workflow_name: str,
    static: dict,
    deterministic_risks: set[str],
) -> dict:
    """Formula v1: scope distribution, shifted by one tier on complexity signals."""
    scope = answers["scope"]["probs"]
    shift = int(
        answers["novel_pattern"]["p"] > 0.5
        or answers["ambiguity"]["probs"][2] > 0.5
        or answers["tests_exist"]["p"] < 0.5
    )
    tier_probs = [0.0] * len(TIERS)
    for level, value in enumerate(scope):
        tier_probs[min(level + shift, len(TIERS) - 1)] += value
    tier_index = next(
        index
        for index in range(len(TIERS))
        if sum(tier_probs[index + 1 :]) <= routing["underestimate_budget"] + 1e-9
    )
    if sum(answers["review_rounds"]["probs"][1:]) > routing["rounds_threshold"]:
        tier_index = max(tier_index, TIERS.index("L"))
    tier = TIERS[tier_index]
    risk_flags = sorted(
        {
            flag
            for question, flag in RISK_QUESTIONS.items()
            if answers[question]["p"] > routing["risk_threshold"]
        }
        | deterministic_risks
    )
    confidence = answers["scope"]["confidence"]
    if confidence < routing["min_confidence"]:
        suggested = {"author": static["author"], "reviewer": static["reviewer"]}
        reason = "low_confidence"
    else:
        suggested = dict(routing["effort_by_tier"][workflow_name][tier])
        reason = "tier"
    if risk_flags:
        suggested["reviewer"] = raise_effort(suggested["reviewer"], routing["reviewer_floor_on_risk"])
    return {
        "tier": tier,
        "tier_probs": dict(zip(TIERS, (round(value, 4) for value in tier_probs))),
        "risk_flags": risk_flags,
        "confidence": confidence,
        "suggested_effort": suggested,
        "suggestion_basis": reason,
    }


def openrouter_classifier(request: dict, timeout: float) -> dict:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise ClassifierSkipped(f"{API_KEY_ENV} not set")
    http_request = urllib.request.Request(
        OPENROUTER_DECISIONS_URL,
        data=json.dumps(request).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ClassifierError(f"classifier request failed: {type(error).__name__}") from error


def fake_classifier(request: dict, timeout: float) -> dict:
    """Deterministic stand-in with the same envelope as the Decisions API."""
    signals = request["state"].get("signals", {})
    sensitive = bool(signals.get("sensitive_paths"))
    return {
        "scope": {"type": "score", "probabilities": {"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.1}, "confidence": 0.8},
        "ambiguity": {"type": "score", "probabilities": {"0": 0.7, "1": 0.2, "2": 0.1}, "confidence": 0.8},
        "novel_pattern": {"type": "noul", "noul": 0.2},
        "tests_exist": {"type": "noul", "noul": 0.9},
        "risk_security": {"type": "noul", "noul": 0.9 if sensitive else 0.05},
        "risk_data_migration": {"type": "noul", "noul": 0.05},
        "risk_public_api": {"type": "noul", "noul": 0.1},
        "review_rounds": {"type": "score", "probabilities": {"0": 0.7, "1": 0.2, "2": 0.1}, "confidence": 0.7},
    }
