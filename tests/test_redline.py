"""Offline structural tests. A fake HTTP response is not evidence of model quality."""
import ast
import copy
from http.client import HTTPResponse, IncompleteRead
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import redline as r
import evaluate

ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = "1. Alpha obligation.\n2. Beta obligation.\n"
PLAYBOOK = [{"clause": "Required rule", "is_required": True},
            {"clause": "Optional rule ", "is_required": False}]
KEY = "SYNTHETIC_KEY_NOT_A_CREDENTIAL"


def issue(quote="Alpha obligation.", rule="Required rule", fix="Alpha protected obligation."):
    return dict(zip(r.FIELDS, (quote, rule, fix)))


def candidate():
    return {"issues": [issue()], "questions": []}


def response(text=None, **changes):
    value = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(candidate()) if text is None else text}]}]}
    value.update(changes)
    return value


class ValidationTests(unittest.TestCase):
    def test_real_rtf_preserves_quotes_sections_and_ending(self):
        text = r.read_document(ROOT / "data/bad_document.txt.rtf")
        self.assertTrue(text.startswith("NON‑DISCLOSURE AGREEMENT"))
        self.assertIn("“Confidential Information”", text)
        for section in range(1, 13):
            self.assertRegex(text, r"(?m)^" + str(section) + r"\. ")
        self.assertIn("Date: _____________________________", text)
        self.assertNotIn("\\rtf", text)
        self.assertGreater(len(text), 7000)

    def test_rtf_rejects_unsupported_controls_including_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.rtf"
            for control in ("header", "headerl", "headerr", "headerf", "footer", "footerl", "footerr",
                            "footerf", "footnote", "annotation", "revised", "deleted", "v"):
                for parameter in ("", "0", "1", "12", "-1"):
                    path.write_text("{\\rtf1 Body {\\" + control + parameter + " hidden}}")
                    with self.subTest(control=control, parameter=parameter), self.assertRaisesRegex(ValueError, "export reviewed"):
                        r.read_document(path)

    def test_rtf_literal_escape_and_control_prefix_are_not_false_positives(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "literal.rtf"
            for control in ("header", "footnote", "revised1", "deleted0", "v1"):
                path.write_text(r"{\rtf1 Body \\" + control + " literal}")
                self.assertEqual(r.read_document(path), "Body \\" + control + " literal")
            for control in ("viewkind4", "vern1", "revisedextra1", "headery720", "footery720"):
                path.write_text("{\\rtf1\\" + control + " Body}")
                self.assertEqual(r.read_document(path), "Body")
            path.write_text(r"{\rtf1 Body " + "\\" * 3 + "revised1 hidden}")
            with self.assertRaises(ValueError):
                r.read_document(path)

    def test_plain_text_bom_and_empty_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.txt"
            path.write_text("\ufeffBody")
            self.assertEqual(r.read_document(path), "Body")
            path.write_text(" \n ")
            with self.assertRaisesRegex(ValueError, "empty"):
                r.read_document(path)

    def test_malformed_rtf_has_a_controlled_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "malformed.rtf"
            path.write_text(r"{\rtf1\uc bad}")
            with self.assertRaisesRegex(ValueError, "export reviewed plain text"):
                r.read_document(path)

    def test_source_quote_preserves_typography(self):
        document = 'Before. “Secret”\u00a0information\n is protected. After.'
        self.assertEqual(r.source_quote('"Secret" information is protected.', document),
                         '“Secret”\u00a0information\n is protected.')
        self.assertEqual(r.source_quote("\u00a0information\n ", document), "\u00a0information\n ")

    def test_source_quote_rejects_invented_words_and_ambiguity(self):
        for quote, document in [("Recipient may disclose.", "Recipient may not disclose."),
                                ('"A"', '“A” then “A”'), ("same", "same then same"), ("", "source")]:
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                r.source_quote(quote, document)

    def test_issue_shape_exact_keys_and_nonempty_strings(self):
        for bad in ({}, ["bad"], [{"text_snippet": "x"}], [dict(issue(), extra="x")],
                    [issue(fix=12)], [issue(quote=" ")]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.check_shape(bad)

    def test_playbook_rejects_bad_rules_and_duplicate_names(self):
        for bad in ([], {}, [1], [{}], [{"clause": "", "is_required": True}],
                    [{"clause": "Rule", "is_required": 1}], PLAYBOOK * 2):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.check_playbook(bad)

    def test_candidate_shape_questions_and_rule_names(self):
        bad_values = [[], {}, dict(candidate(), extra=1), {"issues": [], "questions": "?"},
                      {"issues": [], "questions": [""]}, {"issues": [], "questions": [1]},
                      {"issues": [issue(rule="Unknown")], "questions": []}]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                r.validate_candidate(bad, DOCUMENT, PLAYBOOK)
        exact_name = {"issues": [issue(rule="Optional rule ")], "questions": []}
        self.assertEqual(r.validate_candidate(exact_name, DOCUMENT, PLAYBOOK), exact_name)

    def test_duplicate_and_partial_overlap_fail_but_adjacent_ranges_pass(self):
        for issues in ([issue(), issue()], [issue("Alpha Beta"), issue("Beta Gamma")],
                       [issue("Alpha Beta"), issue("Alpha Beta", rule="Optional rule ")]):
            with self.subTest(issues=issues), self.assertRaisesRegex(ValueError, "overlap"):
                r.validate_candidate({"issues": issues, "questions": []}, DOCUMENT + "Alpha Beta Gamma", PLAYBOOK)
        value = {"issues": [issue("Alpha"), issue(" Beta")], "questions": []}
        self.assertEqual(r.validate_candidate(value, "Alpha Beta", PLAYBOOK), value)

    def test_validation_does_not_mutate_candidate(self):
        value = {"issues": [issue('"Alpha" obligation.')], "questions": ["Context needed: Which purpose?"]}
        before = copy.deepcopy(value)
        validated = r.validate_candidate(value, '“Alpha” obligation.', PLAYBOOK)
        self.assertEqual(value, before)
        self.assertEqual(validated["issues"][0]["text_snippet"], '“Alpha” obligation.')

    def test_mechanical_acceptance_is_not_semantic_review(self):
        value = {"issues": [issue(fix="Should this be stronger?")], "questions": ["Unlabeled"]}
        self.assertEqual(r.validate_candidate(value, DOCUMENT, PLAYBOOK), value)
        self.assertEqual(r.validate_candidate({"issues": [], "questions": []}, DOCUMENT, PLAYBOOK),
                         {"issues": [], "questions": []})

    def test_request_preserves_all_input_fields_and_uses_small_strict_schema(self):
        playbook = [dict(PLAYBOOK[0], extra="All fields, even ones unknown to this script.")]
        payload = r.build_request(DOCUMENT, playbook, "OFFLINE_TEST_ONLY", 1500)
        self.assertEqual(json.loads(payload["input"]), {"original_document": DOCUMENT, "playbook": playbook})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_output_tokens"], 1500)
        schema = payload["text"]["format"]["schema"]
        self.assertEqual(set(schema["properties"]), {"issues", "questions"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["issues"]["items"]["required"]), set(r.FIELDS))
        self.assertEqual(payload["instructions"], (ROOT / "instructions.txt").read_text())

    def test_request_requires_explicit_positive_token_cap_and_model(self):
        for cap in (None, 0, -1, True, "5"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                r.build_request(DOCUMENT, PLAYBOOK, "MODEL", cap)
        for model in (None, "", " "):
            with self.subTest(model=model), self.assertRaises(ValueError):
                r.build_request(DOCUMENT, PLAYBOOK, model, 5)

    def test_request_excludes_expected_answers_and_review_rationale(self):
        case = {"document": DOCUMENT, "playbook": PLAYBOOK,
                "expected_handling": "EXPECTED_ANSWER_NOT_FOR_THE_MODEL",
                "rationale": "REVIEW_RATIONALE_NOT_FOR_THE_MODEL"}
        payload = r.build_request(case["document"], case["playbook"], "OFFLINE_TEST_ONLY", 1000)
        self.assertEqual(json.loads(payload["input"]),
                         {"original_document": DOCUMENT, "playbook": PLAYBOOK})
        for field in ("expected_handling", "rationale"):
            self.assertNotIn(field, payload["input"])
            self.assertNotIn(case[field], json.dumps(payload))

    def test_prompt_keeps_fallback_noisy_policy_and_review_choices(self):
        text = " ".join((ROOT / "instructions.txt").read_text().split())
        for phrase in ("The ideal is preferred", "an explicitly permitted, relevant acceptable fallback also satisfies the rule",
                       "Do not require an edit solely because such a fallback differs from the ideal",
                       "do not apply a blanket field-precedence order", "do not infer missing facts or authority",
                       "Never treat arbitrary or irrelevant text", "A local policy uncertainty does not prevent",
                       "A weaker permitted fallback is not a reason to remove", "is_required=false means",
                       "Policy conflict:", "Context needed:", "Material risk:", "Optional stronger protection:",
                       "only in questions", "Do not invent purpose", "A broad license, use permission, assignment, waiver"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_python_38_grammar(self):
        for path in list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py")):
            ast.parse(path.read_text(), feature_version=(3, 8))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.document, self.playbook, self.canned = [self.root / name for name in ("document.txt", "playbook.json", "canned.json")]
        self.output = self.root / "result.json"
        self.document.write_text(DOCUMENT)
        self.playbook.write_text(json.dumps(PLAYBOOK))
        self.canned.write_text(json.dumps(candidate()))

    def invoke(self, value=None, fail=False, live=False, error=None, extra=None):
        args = ["redline.py", "--document", str(self.document), "--playbook", str(self.playbook), "--output", str(self.output)]
        args += ["--live", "--model", "OFFLINE_TEST_ONLY", "--max-output-tokens", "1000"] if live else ["--mock-response", str(self.canned)]
        args += extra or []
        value = response() if value is None else value
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        with patch("sys.argv", args), patch.dict(os.environ, {"OPENAI_API_KEY": KEY}), \
                patch("redline.urlopen", return_value=BytesIO(raw), side_effect=error) as http, \
                patch("sys.stdout", new_callable=StringIO) as stdout, patch("sys.stderr", new_callable=StringIO) as stderr:
            if fail:
                with self.assertRaises(SystemExit) as stopped:
                    r.main()
                self.assertEqual(stopped.exception.code, 1)
            else:
                r.main()
        self.assertLessEqual(http.call_count, 1)
        self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
        return http, stdout.getvalue() + stderr.getvalue()

    def test_mock_writes_only_issues_and_human_review_sidecar(self):
        value = candidate()
        value["questions"] = ["Optional stronger protection: Would the client prefer a fixed term?"]
        self.canned.write_text(json.dumps(value))
        http, _ = self.invoke()
        http.assert_not_called()
        self.assertEqual(json.loads(self.output.read_text()), value["issues"])
        review = json.loads(Path(str(self.output) + ".review.json").read_text())
        self.assertEqual(review["mode"], "mock")
        self.assertEqual(review["questions"], value["questions"])
        self.assertTrue(review["manual_review_required"])
        self.assertEqual([row["status"] for row in review["manual_review_checklist"]], ["not_reviewed"] * 2)
        self.assertEqual(len(list(self.root.glob("result*"))), 2)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_supplied_mock_matches_readme_scores_and_keeps_question_separate(self):
        self.document = ROOT / "data/bad_document.txt.rtf"
        self.playbook = ROOT / "data/playbook.json"
        self.canned = ROOT / "fixtures/canned_response.json"
        http, _ = self.invoke()
        http.assert_not_called()
        saved = json.loads(self.output.read_text())
        reference = json.loads((ROOT / "data/expected_output.json").read_text())
        result = evaluate.compare(saved, reference)
        self.assertEqual((len(saved), len(reference), len(result["matches"])), (1, 8, 1))
        self.assertEqual(result["precision"], 1)
        self.assertEqual(result["recall"], 1 / 8)
        self.assertAlmostEqual(result["f1"], 2 / 9)
        self.assertEqual(result["replacement_exact_count"], 0)
        self.assertEqual(set(saved[0]), set(r.FIELDS))
        review = json.loads(Path(str(self.output) + ".review.json").read_text())
        self.assertEqual(review["mode"], "mock")
        self.assertEqual(review["questions"], json.loads(self.canned.read_text())["questions"])
        self.assertEqual(len(review["questions"]), 1)
        self.assertTrue(review["questions"][0].startswith("Policy conflict: Notices and domicile"))
        self.assertEqual([row["status"] for row in review["manual_review_checklist"]], ["not_reviewed"] * 16)

    def test_simulated_live_saves_raw_model_text_before_local_validation(self):
        raw = json.dumps(candidate(), indent=3)
        http, message = self.invoke(response(raw), live=True)
        http.assert_called_once()
        saved = list(self.root.glob("result.json.unaccepted-*.txt"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), raw)
        self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(str(saved[0]), message)
        self.assertEqual(json.loads(self.output.read_text()), candidate()["issues"])

    def test_malformed_model_json_is_preserved_without_a_draft(self):
        _, message = self.invoke(response('{"issues":'), live=True, fail=True)
        self.assertFalse(self.output.exists())
        self.assertEqual(next(self.root.glob("*.unaccepted-*.txt")).read_text(), '{"issues":')
        self.assertIn("Malformed JSON", message)

    def test_wrong_shape_and_bad_source_are_preserved_without_a_draft(self):
        for value in ({}, {"issues": [issue(quote="Invented")], "questions": []}):
            with self.subTest(value=value):
                self.invoke(response(json.dumps(value)), live=True, fail=True)
                self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.root.glob("*.unaccepted-*.txt"))), 2)

    def test_old_output_and_review_block_before_any_request(self):
        for path in (self.output, Path(str(self.output) + ".review.json")):
            path.write_text("old result")
            http, message = self.invoke(live=True, fail=True)
            http.assert_not_called()
            self.assertIn("choose a new", message)
            self.assertEqual(path.read_text(), "old result")
            path.unlink()

    def test_symlink_hardlink_and_dangling_symlink_outputs_never_overwrite(self):
        self.output.symlink_to(self.document)
        self.invoke(fail=True)
        self.assertEqual(self.document.read_text(), DOCUMENT)
        self.output.unlink()
        os.link(self.document, self.output)
        self.invoke(fail=True)
        self.assertEqual(self.document.read_text(), DOCUMENT)
        self.output.unlink()
        self.output.symlink_to(self.root / "missing")
        self.invoke(fail=True)
        self.assertFalse((self.root / "missing").exists())

    def test_existing_input_output_path_is_rejected_before_request(self):
        self.output = self.document
        http, _ = self.invoke(live=True, fail=True)
        http.assert_not_called()
        self.assertEqual(self.document.read_text(), DOCUMENT)

    def test_missing_directory_invalid_inputs_and_settings_make_no_request(self):
        self.output = self.root / "missing" / "out.json"
        http, _ = self.invoke(live=True, fail=True)
        http.assert_not_called()
        self.output = self.root / "result.json"
        self.playbook.write_text("[]")
        http, _ = self.invoke(live=True, fail=True)
        http.assert_not_called()
        self.playbook.write_text(json.dumps(PLAYBOOK))
        http, _ = self.invoke(live=True, fail=True, extra=["--max-output-tokens", "0"])
        http.assert_not_called()

    def test_empty_issues_still_leave_all_rules_unreviewed(self):
        self.canned.write_text(json.dumps({"issues": [], "questions": []}))
        self.invoke()
        self.assertEqual(json.loads(self.output.read_text()), [])
        review = json.loads(Path(str(self.output) + ".review.json").read_text())
        self.assertEqual(len(review["manual_review_checklist"]), 2)

    def test_mock_failure_does_not_duplicate_existing_canned_file(self):
        self.canned.write_text("{")
        self.invoke(fail=True)
        self.assertEqual(list(self.root.glob("result*")), [])
        self.assertEqual(self.canned.read_text(), "{")

    def test_failure_to_preserve_response_stops_before_draft(self):
        with patch("redline.save_response", side_effect=OSError("SENSITIVE ERROR BODY")):
            _, message = self.invoke(live=True, fail=True)
        self.assertFalse(self.output.exists())
        self.assertNotIn("SENSITIVE ERROR BODY", message)

    def test_failed_checkpoint_flush_reports_salvage_path_without_error_body(self):
        with patch("os.fsync", side_effect=OSError("SENSITIVE ERROR BODY")):
            _, message = self.invoke(live=True, fail=True)
        saved = next(self.root.glob("*.unaccepted-*.txt"))
        self.assertIn(str(saved), message)
        self.assertIn("possibly partial", message)
        self.assertNotIn("SENSITIVE ERROR BODY", message)
        self.assertFalse(self.output.exists())

    def test_failed_output_write_keeps_saved_response_and_marks_no_success(self):
        actual_write = r.write_new
        def fail_output(path, text):
            if path == self.output:
                raise OSError("PRIVATE BODY")
            actual_write(path, text)
        with patch("redline.write_new", side_effect=fail_output):
            _, message = self.invoke(live=True, fail=True)
        self.assertFalse(self.output.exists())
        self.assertTrue(Path(str(self.output) + ".review.json").exists())
        self.assertEqual(len(list(self.root.glob("*.unaccepted-*.txt"))), 1)
        self.assertNotIn("Saved 1 issues", message)
        self.assertNotIn("PRIVATE BODY", message)

    def test_exclusive_write_refuses_racing_existing_file(self):
        self.output.write_text("existing")
        with self.assertRaises(FileExistsError):
            r.write_new(self.output, "replacement")
        self.assertEqual(self.output.read_text(), "existing")

    def test_partial_write_is_removed_on_failure(self):
        with patch("os.fsync", side_effect=OSError("full disk")), self.assertRaises(OSError):
            r.write_new(self.output, "partial")
        self.assertFalse(self.output.exists())

    def test_real_generation_inputs_never_read_reference_file(self):
        self.document = ROOT / "data/bad_document.txt.rtf"
        self.playbook = ROOT / "data/playbook.json"
        canned = (ROOT / "fixtures/canned_response.json").read_text()
        read_text, read_bytes = Path.read_text, Path.read_bytes
        def guarded_text(path, *args, **kwargs):
            self.assertNotEqual(path.name, "expected_output.json")
            return read_text(path, *args, **kwargs)
        def guarded_bytes(path, *args, **kwargs):
            self.assertNotEqual(path.name, "expected_output.json")
            return read_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded_text), patch.object(Path, "read_bytes", guarded_bytes):
            http, _ = self.invoke(response(canned), live=True)
        payload = json.loads(http.call_args[0][0].data)
        self.assertEqual(json.loads(payload["input"]), {"original_document": r.read_document(self.document),
                                                       "playbook": json.loads(self.playbook.read_text())})
        self.assertEqual(len(json.loads(Path(str(self.output) + ".review.json").read_text())["manual_review_checklist"]), 16)


class TransportTests(unittest.TestCase):
    def call(self, value=None, error=None):
        value = response() if value is None else value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        with patch("redline.urlopen", return_value=BytesIO(body), side_effect=error) as http:
            try:
                return r.call_model({}, KEY)
            finally:
                self.assertEqual(http.call_count, 1)

    def test_completed_output_text_only(self):
        self.assertEqual(self.call(), json.dumps(candidate()))

    def test_refusal_incomplete_malformed_and_invalid_envelopes_fail(self):
        bad = [[], b"{", response(status="incomplete"), response(output=None), response(output=[1]),
               response(output=[{"type": "message", "content": None}]),
               response(output=[{"type": "message", "content": [1]}]),
               response(output=[{"type": "message", "content": [{"type": "refusal", "refusal": KEY}]}]),
               response(output=[{"type": "message", "content": [{"type": "output_text", "text": None}]}]),
               response(output=[])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                self.call(value)
            self.assertNotIn(KEY, str(error.exception))

    def test_http_connection_timeout_and_incomplete_read_errors_are_sanitized(self):
        errors = [HTTPError("https://invalid/" + KEY, 429, KEY, {}, BytesIO(KEY.encode())),
                  URLError(KEY), TimeoutError(KEY), IncompleteRead(KEY.encode())]
        for failure in errors:
            with self.subTest(error=failure), self.assertRaises(ValueError) as error:
                self.call(error=failure)
            self.assertNotIn(KEY, str(error.exception))
            self.assertIn("no retry", str(error.exception))

    def test_bad_keys_are_rejected_before_transport(self):
        for key in (None, "", "bad key", "key\n", "é"):
            with patch("redline.urlopen") as http, self.assertRaises(ValueError):
                r.call_model({}, key)
            http.assert_not_called()

    def test_real_urllib_rejects_every_redirect_status_without_following(self):
        for status in range(300, 400):
            body = json.dumps(response()).encode()
            wire = ("HTTP/1.1 {} Redirect\r\nLocation: https://other.invalid/{}\r\nContent-Length: {}\r\n\r\n".format(
                status, KEY, len(body))).encode() + body
            connection = Mock(sock=None)
            def getresponse():
                parsed = HTTPResponse(Mock(makefile=Mock(return_value=BytesIO(wire))))
                parsed.begin()
                return parsed
            connection.getresponse.side_effect = getresponse
            with self.subTest(status=status), patch("http.client.HTTPSConnection", return_value=connection) as https, \
                    patch("http.client.HTTPConnection") as http, patch("urllib.request.proxy_bypass", return_value=True):
                with self.assertRaises(ValueError) as error:
                    r.call_model({}, KEY)
                self.assertNotIn(KEY, str(error.exception))
                self.assertEqual(https.call_count, 1)
                self.assertEqual(connection.request.call_count, 1)
                http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
