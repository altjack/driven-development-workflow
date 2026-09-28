import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import feature_flow
from feature_flow import routing


def answers(scope, confidence=0.8, rounds=(0.8, 0.15, 0.05), **noul):
    values = {
        "novel_pattern": 0.1,
        "tests_exist": 0.9,
        "risk_security": 0.05,
        "risk_data_migration": 0.05,
        "risk_public_api": 0.05,
        **noul,
    }
    return {
        "scope": {"probs": list(scope), "confidence": confidence},
        "ambiguity": {"probs": [0.8, 0.15, 0.05], "confidence": 0.8},
        "review_rounds": {"probs": list(rounds), "confidence": 0.8},
        **{key: {"p": value} for key, value in values.items()},
    }


def api_response(scope=(0.05, 0.55, 0.30, 0.10)):
    return {
        "scope": {
            "type": "score",
            "score": 1.45,
            "probabilities": {str(level): value for level, value in enumerate(scope)},
            "confidence": 0.8,
        },
        "ambiguity": {"type": "score", "probabilities": {"0": 0.8, "1": 0.2}, "confidence": 0.9},
        "novel_pattern": {"type": "noul", "noul": 0.1},
        "tests_exist": {"type": "noul", "noul": 0.9},
        "risk_security": {"type": "noul", "noul": 0.02},
        "risk_data_migration": {"type": "noul", "noul": 0.02},
        "risk_public_api": {"type": "noul", "noul": 0.02},
        "review_rounds": {"type": "score", "probabilities": {"0": 0.9, "1": 0.1}, "confidence": 0.9},
    }


class RoutingFormulaTest(unittest.TestCase):
    def setUp(self):
        self.routing = feature_flow.load_config()["routing"]
        self.static = {"author": "xhigh", "reviewer": "high"}

    def classify(self, values, risks=frozenset()):
        return routing.classify(values, self.routing, "code", self.static, set(risks))

    def test_tier_uses_the_distribution_not_the_argmax(self):
        result = self.classify(answers([0.05, 0.55, 0.30, 0.10]))
        self.assertEqual(result["tier"], "L")
        self.assertEqual(result["suggested_effort"], self.routing["effort_by_tier"]["code"]["L"])

        result = self.classify(answers([0.30, 0.60, 0.08, 0.02]))
        self.assertEqual(result["tier"], "M")

    def test_complexity_signals_shift_the_distribution_up(self):
        result = self.classify(answers([0.9, 0.1, 0, 0], tests_exist=0.2))
        self.assertEqual(result["tier_probs"], {"S": 0.0, "M": 0.9, "L": 0.1, "XL": 0.0})
        self.assertEqual(result["tier"], "M")

        result = self.classify(answers([0, 0, 0.2, 0.8], novel_pattern=0.9))
        self.assertEqual(result["tier_probs"]["XL"], 1.0)

    def test_expected_review_rounds_set_a_floor(self):
        result = self.classify(answers([0.9, 0.1, 0, 0], rounds=(0.3, 0.5, 0.2)))
        self.assertEqual(result["tier"], "L")

    def test_low_confidence_keeps_the_static_efforts(self):
        result = self.classify(answers([1, 0, 0, 0], confidence=0.3))
        self.assertEqual(result["suggested_effort"], self.static)
        self.assertEqual(result["suggestion_basis"], "low_confidence")

    def test_risk_raises_only_the_reviewer_floor(self):
        result = self.classify(answers([1, 0, 0, 0], risk_public_api=0.4))
        self.assertEqual(result["risk_flags"], ["public_api"])
        self.assertEqual(result["suggested_effort"], {"author": "medium", "reviewer": "xhigh"})

        result = self.classify(answers([1, 0, 0, 0]), {"data_migration"})
        self.assertEqual(result["risk_flags"], ["data_migration"])
        self.assertEqual(result["suggested_effort"]["reviewer"], "xhigh")


class ParseAnswersTest(unittest.TestCase):
    def test_accepts_top_level_and_wrapped_answers(self):
        parsed = routing.parse_answers(api_response())
        self.assertEqual(parsed["scope"], {"probs": [0.05, 0.55, 0.3, 0.1], "confidence": 0.8})
        self.assertEqual(parsed["ambiguity"]["probs"], [0.8, 0.2, 0.0])
        self.assertEqual(parsed["risk_security"], {"p": 0.02})
        self.assertEqual(routing.parse_answers({"answers": api_response()}), parsed)

    def test_rejects_non_conforming_responses(self):
        missing = api_response()
        del missing["tests_exist"]
        unnormalized = api_response(scope=(0.5, 0.5, 0.5, 0))
        out_of_range = api_response()
        out_of_range["risk_security"]["noul"] = 1.5
        unknown_level = api_response()
        unknown_level["ambiguity"]["probabilities"]["7"] = 0
        for response, message in (
            (missing, "missing answer: tests_exist"),
            (unnormalized, "must sum to 1"),
            (out_of_range, "out of range"),
            (unknown_level, "unknown levels"),
            ([], "must be an object"),
        ):
            with self.subTest(message), self.assertRaisesRegex(routing.ClassifierError, message):
                routing.parse_answers(response)


class RoutingConfigTest(unittest.TestCase):
    def test_routing_is_optional_and_validated(self):
        config = feature_flow.load_config()
        without = copy.deepcopy(config)
        del without["routing"]
        feature_flow.validate_config(without)

        enforced = copy.deepcopy(config)
        enforced["routing"]["mode"] = "enforced"
        with self.assertRaisesRegex(ValueError, "routing.mode"):
            feature_flow.validate_config(enforced)

        missing_tier = copy.deepcopy(config)
        del missing_tier["routing"]["effort_by_tier"]["code"]["XL"]
        with self.assertRaisesRegex(ValueError, "effort_by_tier.code"):
            feature_flow.validate_config(missing_tier)

        bad_threshold = copy.deepcopy(config)
        bad_threshold["routing"]["risk_threshold"] = 2
        with self.assertRaisesRegex(ValueError, "risk_threshold"):
            feature_flow.validate_config(bad_threshold)

        bad_effort = copy.deepcopy(config)
        bad_effort["routing"]["effort_by_tier"]["docs"]["S"]["author"] = "turbo"
        with self.assertRaisesRegex(ValueError, "effort_by_tier.docs.S.author"):
            feature_flow.validate_config(bad_effort)

    def test_schema_describes_routing(self):
        schema = json.loads(feature_flow.CONFIG_SCHEMA_PATH.read_text())
        self.assertEqual(
            set(schema["$defs"]["routing"]["required"]),
            set(feature_flow.load_config()["routing"]),
        )


class OpenRouterClassifierTest(unittest.TestCase):
    request = {"model": "~typesafe/jev-latest", "state": {}, "questions": routing.QUESTIONS}

    def test_missing_key_skips_without_network(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "urllib.request.urlopen"
        ) as urlopen:
            with self.assertRaises(routing.ClassifierSkipped):
                routing.openrouter_classifier(self.request, 5)
        urlopen.assert_not_called()

    def test_posts_the_request_with_timeout(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(api_response()).encode()
        with mock.patch.dict(os.environ, {routing.API_KEY_ENV: "sk-test"}), mock.patch(
            "urllib.request.urlopen", return_value=response
        ) as urlopen:
            result = routing.openrouter_classifier(self.request, 5)
        http_request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)
        self.assertEqual(http_request.full_url, routing.OPENROUTER_DECISIONS_URL)
        self.assertEqual(http_request.get_header("Authorization"), "Bearer sk-test")
        self.assertEqual(json.loads(http_request.data)["questions"]["scope"]["type"], "score")
        self.assertEqual(result, api_response())

    def test_network_errors_become_classifier_errors(self):
        with mock.patch.dict(os.environ, {routing.API_KEY_ENV: "sk-test"}), mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            with self.assertRaisesRegex(routing.ClassifierError, "URLError"):
                routing.openrouter_classifier(self.request, 5)


class ShadowRunTest(unittest.TestCase):
    def test_shadow_classification_is_recorded_but_not_applied(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                calls.append(effort)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_docs(repo, provider)
                state = json.loads((Path(result["state_dir"]) / "state.json").read_text())
                logs = list((Path(result["state_dir"]) / "classifications").iterdir())
            ledger = (feature_dir / "review-ledger.md").read_text()

        docs = feature_flow.load_config()["docs"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["classification"], {"status": "classified", "tier": "L"})
        self.assertEqual(calls, [docs["author"]["effort"], docs["reviewer"]["effort"]])
        self.assertFalse(state["classification"]["applied"])
        self.assertEqual(len(logs), 1)
        self.assertIn("## requirements.md - classification", ledger)
        self.assertIn("status: classified", ledger)
        self.assertIn("applied: false", ledger)
        self.assertIn(f"input_hash: {state['classification']['input_hash']}", ledger)

    def test_classifier_failures_never_stop_the_gate(self):
        def broken(request, timeout):
            raise routing.ClassifierError("classifier request failed: URLError")

        def malformed(request, timeout):
            return {"scope": {}}

        for classifier, reason in (
            (broken, "URLError"),
            (malformed, "scope: missing probabilities"),
        ):
            with self.subTest(reason), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
                repo, feature_dir = self._repo(directory)
                with mock.patch.dict(os.environ, {"HOME": home}):
                    result = feature_flow.run_docs(repo, feature_flow.run_fake, classifier)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["classification"]["status"], "error")
                ledger = (feature_dir / "review-ledger.md").read_text()
                self.assertIn("status: error", ledger)
                self.assertIn(reason, ledger)

    def test_missing_api_key_skips_silently(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            environment = {key: value for key, value in os.environ.items() if key != routing.API_KEY_ENV}
            with mock.patch.dict(os.environ, {**environment, "HOME": home}, clear=True):
                result = feature_flow.run_docs(
                    repo, feature_flow.run_fake, routing.openrouter_classifier
                )
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["classification"], {"status": "skipped", "tier": None})
            self.assertNotIn("classification", (feature_dir / "review-ledger.md").read_text())

    def test_mode_off_does_not_call_the_classifier(self):
        config = feature_flow.load_config()
        config["routing"]["mode"] = "off"
        classifier = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)
            with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.object(
                feature_flow, "load_config", return_value=config
            ):
                result = feature_flow.run_docs(repo, feature_flow.run_fake, classifier)
        classifier.assert_not_called()
        self.assertNotIn("classification", result)

    def test_code_signals_come_from_the_accepted_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, feature_dir = self._repo(directory)
            (feature_dir / "plan.md").write_text(
                "- File: `backend/auth/login.py`\n- File: `db/migrations/0002_token.sql`\n"
                "- File: `web/src/upload.ts`\n- File: `.env.local`\n"
            )
            ledger = feature_dir / "review-ledger.md"
            ledger.write_text("## plan.md - round 1\n\n## plan.md - round 2\n")
            state, risks = feature_flow.classification_state(
                repo, "16-07-2026_feature-flow", "code", None, ledger
            )
        self.assertEqual(risks, {"security", "data_migration"})
        self.assertEqual(
            state["signals"],
            {
                "previous_gate_rounds": 2,
                "planned_files": 3,
                "languages": [".py", ".sql", ".ts"],
                "sensitive_paths": ["backend/auth/login.py", "db/migrations/0002_token.sql"],
            },
        )

    @staticmethod
    def _repo(directory: str) -> tuple[Path, Path]:
        repo = Path(directory)
        branch = "16-07-2026_feature-flow"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
        )
        feature_dir = repo / "specs" / "features" / branch
        feature_dir.mkdir(parents=True)
        (repo / "specs" / "roadmap.md").write_text("- [~] Build feature flow\n")
        return repo, feature_dir


if __name__ == "__main__":
    unittest.main()
