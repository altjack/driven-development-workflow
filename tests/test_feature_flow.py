import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import feature_flow


class FeatureFlowTest(unittest.TestCase):
    def test_requirements_state_machine_and_dry_run_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, feature_dir = self._repo(directory)

            before = self._files(repo)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    feature_flow.main(["run", "--repo", str(repo), "--dry-run"]), 0
                )
            self.assertEqual(feature_flow.inspect_repo(repo)["next"]["action"], "author")
            self.assertEqual(before, self._files(repo))
            self.assertLess(len(output.getvalue().encode()), 1024)
            self.assertFalse(Path(json.loads(output.getvalue())["state_dir"]).exists())

            requirements = feature_dir / "requirements.md"
            requirements.write_text(
                "# Requirements\n## Problema\nTest\n## Scope\nTest\n"
                "## Decisioni di design\nTest\n"
                + "\n".join(f"Detail {number}" for number in range(20))
                + "\n"
            )
            review = feature_flow.inspect_repo(repo)
            self.assertEqual(review["next"]["action"], "reviewer")

            target_hash = feature_flow.sha256(requirements)
            (feature_dir / "review-ledger.md").write_text(
                "## requirements.md - round 1\n"
                "artifact: requirements.md\n"
                f"target_hash: {target_hash}\n"
                "### Decision\n"
                "status: accepted\n"
                f"accepted_hash: {target_hash}\n"
                "open_items: none\n"
            )
            self.assertEqual(
                feature_flow.inspect_repo(repo, "requirements.md")["state"],
                "requirements_accepted",
            )
            self.assertEqual(feature_flow.inspect_repo(repo)["state"], "plan_authoring")

    def test_plan_slice_uses_accepted_requirements_and_new_sessions(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                request = json.loads(prompt)
                calls.append(
                    (request["CONTRACT"]["unit"], request["CONTROL"]["role"], session_id)
                )
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                requirements_result = feature_flow.run_docs(repo, provider)
                state_file = Path(requirements_result["state_dir"]) / "state.json"
                requirements_state = json.loads(state_file.read_text())
                next_step = feature_flow.inspect_repo(repo)
                plan_result = feature_flow.run_docs(repo, provider)
                plan_state = json.loads(state_file.read_text())

            self.assertEqual(requirements_result["unit"], "requirements.md")
            self.assertEqual(next_step["next"]["unit"], "plan.md")
            self.assertEqual((plan_result["status"], plan_result["unit"]), ("accepted", "plan.md"))
            self.assertTrue(feature_flow.has_real_plan(feature_dir / "plan.md"))
            self.assertEqual(
                calls,
                [
                    ("requirements.md", "author", None),
                    ("requirements.md", "reviewer", None),
                    ("plan.md", "author", None),
                    ("plan.md", "reviewer", None),
                ],
            )
            sessions = {
                requirements_state["author_session"],
                requirements_state["reviewer_session"],
                plan_state["author_session"],
                plan_state["reviewer_session"],
            }
            self.assertEqual(len(sessions), 4)
            ledger = (feature_dir / "review-ledger.md").read_text()
            self.assertIn("## requirements.md - round 1", ledger)
            self.assertIn("## plan.md - round 1", ledger)

    def test_plan_cannot_start_before_requirements_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._repo(directory)
            with self.assertRaisesRegex(ValueError, "requires accepted requirements"):
                feature_flow.run_artifact(repo, "plan.md", feature_flow.run_fake)
            with self.assertRaisesRegex(ValueError, "requires accepted code"):
                feature_flow.run_artifact(repo, "validation.md", feature_flow.run_fake)

    def test_code_and_e2e_slices_hash_targets_and_keep_new_sessions(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            (repo / "app.py").write_text("VALUE = 1\n")
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Feature Flow Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Feature Flow Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "base",
                ],
                check=True,
                capture_output=True,
            )
            base_commit = feature_flow.git(repo, "rev-parse", "HEAD")
            code_calls = []
            code_prompts = []
            e2e_calls = []
            e2e_prompts = []

            def provider(model, effort, prompt, cwd, session_id=None):
                request = json.loads(prompt)
                if request["CONTRACT"]["unit"] == "code":
                    code_calls.append((request["CONTROL"]["role"], model, effort, session_id))
                    code_prompts.append(prompt)
                elif request["CONTRACT"]["unit"] == "validation.md":
                    e2e_calls.append((request["CONTROL"]["role"], model, effort, session_id))
                    e2e_prompts.append(prompt)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                feature_flow.run_docs(repo, provider)
                plan_result = feature_flow.run_docs(repo, provider)
                plan_state = json.loads(
                    (Path(plan_result["state_dir"]) / "state.json").read_text()
                )
                (repo / ".env.local").write_text("SECRET=do-not-copy\n")
                next_step = feature_flow.inspect_repo(repo)
                result = feature_flow.run_docs(repo, provider)
                state = json.loads((Path(result["state_dir"]) / "state.json").read_text())
                validation_step = feature_flow.inspect_repo(repo)
                validation_result = feature_flow.run_docs(repo, provider)
                validation_state = json.loads(
                    (Path(result["state_dir"]) / "state.json").read_text()
                )
                final_status = feature_flow.inspect_repo(repo)
                fake_change = repo / "feature-flow-fake-change.txt"
                fake_change.write_text(fake_change.read_text() + "tampered\n")
                regressed_status = feature_flow.inspect_repo(repo)

            self.assertEqual(next_step["next"]["unit"], "code")
            self.assertEqual(next_step["routing"]["author"]["model"], "gpt-5.6-terra")
            self.assertEqual((result["status"], result["unit"], result["review_round"]), ("accepted", "code", 1))
            self.assertEqual(result["base_commit"], base_commit)
            self.assertNotEqual(result["result_commit"], base_commit)
            self.assertEqual(result["diff_hash"], state["target_hash"])
            self.assertEqual(result["tests_run"], 1)
            self.assertLess(len(json.dumps(result).encode()), 1024)
            self.assertEqual(
                code_calls,
                [
                    ("author", "gpt-5.6-terra", "xhigh", None),
                    ("reviewer", "gpt-5.6-sol", "high", None),
                ],
            )
            self.assertNotIn("do-not-copy", "".join(code_prompts))
            self.assertNotIn(state["author_session"], {plan_state["author_session"], plan_state["reviewer_session"]})
            self.assertNotEqual(state["author_session"], state["reviewer_session"])
            ledger = (feature_dir / "review-ledger.md").read_text()
            self.assertIn("## code - round 1", ledger)
            self.assertIn(f"base_commit: {base_commit}", ledger)
            self.assertIn(f"diff_hash: {result['diff_hash']}", ledger)
            self.assertIn("python3 -m unittest: passed", ledger)
            self.assertIn(f"result_commit: {result['result_commit']}", ledger)
            self.assertEqual(validation_step["next"]["unit"], "validation.md")
            self.assertEqual(validation_step["routing"]["author"]["model"], "gpt-5.6-sol")
            self.assertEqual(
                (validation_result["status"], validation_result["unit"], validation_result["tests_run"]),
                ("accepted", "validation.md", 1),
            )
            self.assertTrue(feature_flow.has_real_validation(feature_dir / "validation.md"))
            self.assertEqual(
                e2e_calls,
                [
                    ("author", "gpt-5.6-sol", "high", None),
                    ("reviewer", "claude-fable-5", "high", None),
                ],
            )
            self.assertNotIn("do-not-copy", "".join(e2e_prompts))
            self.assertIn("accepted-code.diff", e2e_prompts[0])
            self.assertNotIn(
                validation_state["author_session"],
                {state["author_session"], state["reviewer_session"]},
            )
            self.assertNotEqual(
                validation_state["author_session"], validation_state["reviewer_session"]
            )
            self.assertEqual(final_status["state"], "validation_accepted")
            self.assertEqual(regressed_status["next"]["unit"], "code")
            ledger = (feature_dir / "review-ledger.md").read_text()
            self.assertIn("## validation.md - round 1", ledger)
            self.assertLess(len(json.dumps(validation_result).encode()), 1024)

    def test_fake_provider_resumes_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = json.dumps(
                {
                    "CONTROL": {"role": "reviewer"},
                    "TARGET": {"path": "requirements.md", "hash": "sha256:test"},
                }
            )
            first = feature_flow.run_fake("fake-model", "high", request, root)
            resumed = feature_flow.run_fake(
                "fake-model", "high", request, root, first["session_id"]
            )
            self.assertEqual(resumed["session_id"], first["session_id"])

    def test_fake_loop_persists_logs_and_accepts(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            completed = subprocess.run(
                [str(Path(feature_flow.__file__)), "run", "--repo", str(repo), "--fake"],
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": home},
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            result = json.loads(completed.stdout)
            requirements = feature_dir / "requirements.md"
            state_dir = Path(result["state_dir"])
            self.assertEqual((result["status"], result["review_round"]), ("accepted", 1))
            self.assertTrue(feature_flow.has_real_requirements(requirements))
            state = json.loads((state_dir / "state.json").read_text())
            self.assertNotEqual(state["author_session"], state["reviewer_session"])
            self.assertNotIn("session", completed.stdout)
            self.assertTrue(
                feature_flow.gate_accepted(
                    feature_dir / "review-ledger.md", requirements, feature_flow.sha256(requirements)
                )
            )
            self.assertEqual(len(list((state_dir / "runs" / result["run_id"]).glob("*.json"))), 2)
            self.assertLess(len(completed.stdout.encode()), 1024)
            with mock.patch.dict(os.environ, {"HOME": home}):
                resumed = feature_flow.run_requirements(repo)
            self.assertEqual(
                (resumed["status"], resumed["review_round"], resumed["run_id"]),
                ("accepted", 1, result["run_id"]),
            )

    def test_three_reviews_stop_without_a_fourth(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)
            reviews = 0

            def provider(model, effort, prompt, cwd, session_id=None):
                nonlocal reviews
                request = json.loads(prompt)
                if request["CONTROL"]["role"] == "author":
                    return feature_flow.run_fake(model, effort, prompt, cwd, session_id)
                reviews += 1
                return {
                    "provider": "fake",
                    "model": model,
                    "effort": effort,
                    "session_id": session_id or "review-session",
                    "response": {
                        "status": "needs_changes",
                        "reviewed_hash": request["TARGET"]["hash"],
                        "findings": [{"id": "C1", "severity": "major", "issue": "revise"}],
                    },
                    "returncode": 0,
                }

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
                status = feature_flow.inspect_repo(repo)
                with self.assertRaisesRegex(ValueError, "cannot be resumed"):
                    feature_flow.resolve_finding(repo, "C1", "Esegui una quarta review")
            self.assertEqual((result["status"], result["review_round"], reviews), ("needs_user", 3, 3))
            self.assertEqual(status["state"], "needs_user")

    def test_context_pack_uses_an_explicit_safe_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, feature_dir = self._repo(directory)
            (repo / ".env").write_text("SECRET=do-not-copy\n")
            pack = feature_flow.context_pack(
                repo, feature_dir / "requirements.md", "author", 0, None, []
            )
            self.assertNotIn("do-not-copy", pack)
            self.assertIn("Modifica soltanto", pack)
            self.assertIn("{{FEATURE_NAME}}", pack)

    def test_every_configured_fallback_pair_prevents_auto_review(self):
        config = feature_flow.load_config()
        pairs = []
        for name in ("docs", "code", "e2e"):
            workflow = config[name]
            pairs.append((workflow["author"], workflow["reviewer"]))
            pairs.extend(
                (transition["to"]["author"], transition["to"]["reviewer"])
                for transition in workflow["fallbacks"]
                if "to" in transition
            )
        self.assertTrue(all(author["model"] != reviewer["model"] for author, reviewer in pairs))

    def test_configured_providers_drive_the_requirements_loop(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)

            def author(model, effort, prompt, cwd, session_id=None):
                result = feature_flow.run_fake(model, effort, prompt, cwd, session_id)
                result.update(ok=True, provider="claude")
                return result

            def reviewer(model, effort, prompt, cwd, sandbox, session_id=None):
                request = json.loads(prompt)
                return {
                    "ok": True,
                    "provider": "codex",
                    "model": model,
                    "effort": effort,
                    "session_id": session_id or "codex-session",
                    "response": {
                        "status": "accepted",
                        "reviewed_artifact": request["TARGET"]["path"],
                        "reviewed_hash": request["TARGET"]["hash"],
                        "findings": [],
                    },
                    "returncode": 0,
                }

            with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.object(
                feature_flow, "run_claude", side_effect=author
            ) as claude, mock.patch.object(feature_flow, "run_codex", side_effect=reviewer) as codex:
                result = feature_flow.run_requirements(repo)

            self.assertEqual((result["status"], result["review_round"]), ("accepted", 1))
            self.assertEqual(claude.call_count, 1)
            self.assertEqual(codex.call_args.args[4], "read-only")

    def test_author_fallback_changes_both_roles_without_consuming_review(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                role = json.loads(prompt)["CONTROL"]["role"]
                calls.append((role, model))
                if model == "claude-fable-5":
                    return self._failure("claude", model)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
            state = json.loads((Path(result["state_dir"]) / "state.json").read_text())
            self.assertEqual(
                calls,
                [
                    ("author", "claude-fable-5"),
                    ("author", "gpt-5.6-sol"),
                    ("reviewer", "claude-opus-4-8"),
                ],
            )
            self.assertEqual((result["status"], result["review_round"]), ("accepted", 1))
            self.assertNotEqual(state["author_model"], state["reviewer_model"])
            self.assertIn("failed_model: claude-fable-5", (feature_dir / "review-ledger.md").read_text())

    def test_reviewer_fallback_uses_terra_without_counting_failed_sol(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                role = json.loads(prompt)["CONTROL"]["role"]
                calls.append((role, model))
                if role == "reviewer" and model == "gpt-5.6-sol":
                    return self._failure("codex", model)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
            self.assertEqual(
                calls,
                [
                    ("author", "claude-fable-5"),
                    ("reviewer", "gpt-5.6-sol"),
                    ("reviewer", "gpt-5.6-terra"),
                ],
            )
            self.assertEqual((result["status"], result["review_round"]), ("accepted", 1))

    def test_author_fallback_chain_reaches_terra_then_opus(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                role = json.loads(prompt)["CONTROL"]["role"]
                calls.append((role, model))
                if role == "author" and model in {"claude-fable-5", "gpt-5.6-sol"}:
                    return self._failure("claude" if model.startswith("claude") else "codex", model)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
            self.assertEqual(
                calls,
                [
                    ("author", "claude-fable-5"),
                    ("author", "gpt-5.6-sol"),
                    ("author", "gpt-5.6-terra"),
                    ("reviewer", "claude-opus-4-8"),
                ],
            )
            self.assertEqual((result["status"], result["review_round"]), ("accepted", 1))

    def test_terminal_fallback_stops_on_opus_failure(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, _ = self._repo(directory)
            calls = []

            def provider(model, effort, prompt, cwd, session_id=None):
                role = json.loads(prompt)["CONTROL"]["role"]
                calls.append((role, model))
                if model in {"claude-fable-5", "claude-opus-4-8"}:
                    return self._failure("claude", model)
                return feature_flow.run_fake(model, effort, prompt, cwd, session_id)

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
                status = feature_flow.inspect_repo(repo)
            self.assertEqual(result["status"], "needs_user")
            self.assertEqual(result["review_round"], 0)
            self.assertEqual(calls[-1], ("reviewer", "claude-opus-4-8"))
            self.assertEqual(calls.count(("reviewer", "claude-opus-4-8")), 1)
            self.assertEqual(status["routing"]["reviewer"]["model"], "claude-opus-4-8")

    def test_resolve_records_answer_and_resumes_both_sessions(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            reviewer_sessions = []
            reviewer_findings = []
            author_sessions = []
            reviews = 0

            def provider(model, effort, prompt, cwd, session_id=None):
                nonlocal reviews
                request = json.loads(prompt)
                if request["CONTROL"]["role"] == "author":
                    author_sessions.append(session_id)
                    return feature_flow.run_fake(model, effort, prompt, cwd, session_id)
                reviewer_sessions.append(session_id)
                reviewer_findings.append(request["OPEN FINDINGS"])
                reviews += 1
                status = "needs_user" if reviews == 1 else "accepted"
                findings = (
                    [
                        {
                            "id": "C2",
                            "severity": "major",
                            "owner": "user",
                            "evidence": "roadmap",
                            "issue": "Scegliere il comportamento mancante",
                            "required_change": "Registrare la scelta",
                            "why": "Il requisito e ambiguo",
                        }
                    ]
                    if reviews == 1
                    else []
                )
                return {
                    "provider": "fake",
                    "model": model,
                    "effort": effort,
                    "session_id": session_id or "review-session",
                    "response": {
                        "status": status,
                        "reviewed_artifact": request["TARGET"]["path"],
                        "reviewed_hash": request["TARGET"]["hash"],
                        "findings": findings,
                    },
                    "returncode": 0,
                }

            with mock.patch.dict(os.environ, {"HOME": home}):
                paused = feature_flow.run_requirements(repo, provider)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        feature_flow.main(
                            [
                                "resolve",
                                "C2",
                                "--answer",
                                "Mantieni il comportamento fail-closed",
                                "--repo",
                                str(repo),
                            ]
                        ),
                        0,
                    )
                resumed = feature_flow.run_requirements(repo, provider)

            self.assertEqual(paused["status"], "needs_user")
            self.assertIn("feature-flow resolve C2", paused["resume_command"])
            self.assertEqual(json.loads(output.getvalue())["status"], "resolved")
            self.assertLess(len(output.getvalue().encode()), 1024)
            self.assertEqual((resumed["status"], resumed["review_round"]), ("accepted", 2))
            self.assertEqual(reviewer_sessions, [None, "review-session"])
            self.assertEqual(
                reviewer_findings[1][0]["user_answer"],
                "Mantieni il comportamento fail-closed",
            )
            self.assertIsNone(author_sessions[0])
            self.assertIsNotNone(author_sessions[1])
            ledger = (feature_dir / "review-ledger.md").read_text()
            self.assertIn("resolution C2", ledger)
            self.assertIn("Mantieni il comportamento fail-closed", ledger)

    def test_stale_review_is_discarded_without_consuming_round(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            repo, feature_dir = self._repo(directory)
            reviews = 0

            def provider(model, effort, prompt, cwd, session_id=None):
                nonlocal reviews
                request = json.loads(prompt)
                if request["CONTROL"]["role"] == "author":
                    return feature_flow.run_fake(model, effort, prompt, cwd, session_id)
                reviews += 1
                if reviews == 1:
                    artifact = cwd / request["TARGET"]["path"]
                    artifact.write_text(artifact.read_text() + "\nConcurrent edit.\n")
                return {
                    "provider": "fake",
                    "model": model,
                    "effort": effort,
                    "session_id": session_id or "review-session",
                    "response": {
                        "status": "accepted",
                        "reviewed_hash": request["TARGET"]["hash"],
                        "findings": [],
                    },
                    "returncode": 0,
                }

            with mock.patch.dict(os.environ, {"HOME": home}):
                result = feature_flow.run_requirements(repo, provider)
            ledger = (feature_dir / "review-ledger.md").read_text()
            self.assertEqual((result["status"], result["review_round"], reviews), ("accepted", 1, 2))
            self.assertEqual(ledger.count("## requirements.md - round"), 1)

    def test_lock_allows_only_one_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with feature_flow.workflow_lock(state_dir):
                with self.assertRaisesRegex(ValueError, "already running"):
                    with feature_flow.workflow_lock(state_dir):
                        pass

    def test_claude_provider_logs_and_normalizes_new_and_resumed_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            output = json.dumps(
                {
                    "session_id": "claude-session",
                    "result": "secret transcript",
                    "structured_output": {"status": "accepted"},
                }
            )
            completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="secret stderr")
            author_prompt = json.dumps({"CONTROL": {"role": "author"}})
            with mock.patch.object(feature_flow, "active_run_dir", return_value=run_dir), mock.patch.object(
                feature_flow.subprocess, "run", return_value=completed
            ) as execute:
                result = feature_flow.run_claude(
                    "claude-fable-5", "high", author_prompt, Path(directory)
                )
                resumed = feature_flow.run_claude(
                    "claude-fable-5",
                    "high",
                    author_prompt,
                    Path(directory),
                    "claude-session",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["response"], {"status": "accepted"})
            self.assertNotIn("secret transcript", json.dumps(result))
            self.assertEqual(Path(result["logs"]["stdout"]).read_text(), output)
            self.assertEqual(Path(result["logs"]["stderr"]).read_text(), "secret stderr")
            self.assertNotIn("shell", execute.call_args_list[0].kwargs)
            self.assertNotIn("--resume", execute.call_args_list[0].args[0])
            self.assertIn("acceptEdits", execute.call_args_list[0].args[0])
            self.assertIn("--resume", execute.call_args_list[1].args[0])
            self.assertEqual(resumed["session_id"], "claude-session")

    def test_codex_provider_logs_and_normalizes_new_and_resumed_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "runs" / "run-1"
            run_dir.mkdir(parents=True)

            def execute(command, **kwargs):
                response_path = Path(command[command.index("-o") + 1])
                response_path.write_text('{"status":"accepted"}')
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"type":"thread.started","thread_id":"codex-session"}\n',
                    stderr="secret stderr",
                )

            with mock.patch.object(feature_flow, "active_run_dir", return_value=run_dir), mock.patch.object(
                feature_flow.subprocess, "run", side_effect=execute
            ) as invoked:
                result = feature_flow.run_codex(
                    "gpt-5.6-sol", "xhigh", "prompt", Path(directory), "read-only"
                )
                resumed = feature_flow.run_codex(
                    "gpt-5.6-sol",
                    "xhigh",
                    "follow-up",
                    Path(directory),
                    "read-only",
                    "codex-session",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["response"], {"status": "accepted"})
            self.assertNotIn("secret stderr", json.dumps(result))
            self.assertEqual(Path(result["logs"]["stderr"]).read_text(), "secret stderr")
            schema = json.loads((run_dir / "reviewer-schema.json").read_text())
            self.assertFalse(schema["additionalProperties"])
            self.assertFalse(schema["properties"]["findings"]["items"]["additionalProperties"])
            new_command = invoked.call_args_list[0].args[0]
            resume_command = invoked.call_args_list[1].args[0]
            self.assertNotIn("shell", invoked.call_args_list[0].kwargs)
            self.assertIn("-s", new_command)
            self.assertIn("read-only", new_command)
            self.assertEqual(resume_command[:3], ["codex", "exec", "resume"])
            self.assertIn('sandbox_mode="read-only"', resume_command)
            self.assertEqual(resumed["session_id"], "codex-session")

    def test_provider_invalid_output_stays_out_of_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            completed = subprocess.CompletedProcess([], 0, stdout="not json", stderr="details")
            with mock.patch.object(feature_flow, "active_run_dir", return_value=run_dir), mock.patch.object(
                feature_flow.subprocess, "run", return_value=completed
            ):
                result = feature_flow.run_claude("model", "high", "prompt", Path(directory))
            self.assertEqual((result["ok"], result["error"], result["response"]), (False, "invalid_output", None))
            self.assertNotIn("not json", json.dumps(result))

    @staticmethod
    def _files(repo: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(repo)): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

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

    @staticmethod
    def _failure(provider: str, model: str) -> dict:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "session_id": None,
            "response": None,
            "returncode": 1,
            "error": "process_failed",
        }


if __name__ == "__main__":
    unittest.main()
