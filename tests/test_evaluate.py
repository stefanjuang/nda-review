"""Local, deterministic reference-comparison tests. No model or network calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate import EXACT_MATCH, compare, render_report


def finding(snippet="The receiving party must protect all information.", rule="Confidentiality", fix="Protect confidential information."):
    return {"text_snippet": snippet, "playbook_clause_reference": rule, "suggested_fix": fix}


class ComparisonTests(unittest.TestCase):
    def test_quotes_and_whitespace_normalize_but_fix_is_literal(self):
        predicted = [finding('  The “recipient”\n must protect information. ', fix="Protect  information.")]
        expected = [finding('The "recipient" must protect information.', fix="Protect information.")]
        result = compare(predicted, expected)
        self.assertEqual(result["matches"], [(0, 0, EXACT_MATCH)])
        self.assertEqual((result["precision"], result["recall"], result["f1"]), (1, 1, 1))
        self.assertEqual(result["replacement_exact_count"], 0)

    def test_rule_names_case_and_snippet_punctuation_are_literal(self):
        expected = [finding()]
        for prediction in [finding(rule="confidentiality"), finding(rule="Confidentiality "), finding("The receiving party must protect all information!")]:
            with self.subTest(prediction=prediction):
                self.assertEqual(compare([prediction], expected)["matches"], [])

    def test_duplicates_cannot_reuse_a_reference(self):
        result = compare([finding(), finding()], [finding()])
        self.assertEqual(result["matches"], [(0, 0, EXACT_MATCH)])
        self.assertEqual(result["unmatched"], [1])
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 1)
        self.assertAlmostEqual(result["f1"], 2 / 3)

    def test_duplicate_references_pair_in_input_order(self):
        predicted = [finding(fix="second"), finding(fix="first")]
        expected = [finding(fix="first"), finding(fix="second"), finding()]
        result = compare(predicted, expected)
        self.assertEqual(result["matches"], [(0, 0, EXACT_MATCH), (1, 1, EXACT_MATCH)])
        self.assertEqual(result["missed"], [2])
        self.assertEqual(result["replacement_exact_count"], 0)

    def test_containment_is_only_a_review_candidate(self):
        result = compare([finding("The receiving party must protect all information. Forever.")], [finding()])
        self.assertEqual(result["possible"], [(0, 0)])
        self.assertEqual(result["matches"], [])
        self.assertEqual((result["precision"], result["recall"], result["f1"]), (0, 0, 0))
        self.assertEqual(result["unmatched"], [0])
        self.assertEqual(result["missed"], [0])

    def test_containment_has_no_length_threshold_but_never_earns_credit(self):
        for snippet in ["The receiving party", "The receiving party must protect all information. " + "Extra unrelated text. " * 20]:
            for predictions, expected in [([finding(snippet)], [finding()]), ([finding()], [finding(snippet)])]:
                result = compare(predictions, expected)
                self.assertEqual(result["possible"], [(0, 0)])
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["unmatched"], [0])
                self.assertEqual(result["missed"], [0])
                self.assertEqual([result[k] for k in ("precision", "recall", "f1")], [0, 0, 0])

    def test_containment_still_requires_same_rule_and_remains_unmatched(self):
        expected = [finding()]
        self.assertEqual(compare([finding("The receiving party", rule="Other")], expected)["possible"], [])
        result = compare(expected + [finding("The receiving party")], expected)
        self.assertEqual(result["matches"], [(0, 0, EXACT_MATCH)])
        self.assertEqual(result["possible"], [(1, 0)])
        self.assertEqual(result["unmatched"], [1])
        self.assertEqual(result["precision"], 0.5)

    def test_empty_denominators(self):
        for predicted, expected, scores in [([], [], (None, None, None)), ([], [finding()], (None, 0, 0)), ([finding()], [], (0, None, 0))]:
            with self.subTest(scores=scores):
                result = compare(predicted, expected)
                self.assertEqual(tuple(result[key] for key in ("precision", "recall", "f1")), scores)

    def test_reject_invalid_shapes(self):
        for bad in [{}, [None], [{"text_snippet": "x"}], [finding(fix=" ")], [finding(rule=42)], [dict(finding(), extra="secret")]]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compare(bad, [])
                with self.assertRaises(ValueError):
                    compare([], bad)

    def test_report_discloses_limits_and_all_replacements(self):
        predicted = [finding(), finding("Another prediction", fix="Predicted unmatched replacement")]
        expected = [finding(), finding("Another reference", fix="Missed reference replacement")]
        report = render_report(predicted, expected, compare(predicted, expected))
        for text in ["Saved-file", "origin and currentness are unverified", "Semantic and legal correctness are NOT EVALUATED", "Identical suggested_fix: 1/1", "Predicted replacement:", "Reference replacement:", "Predicted unmatched replacement", "Missed reference replacement", "MISSED REFERENCES", "UNMATCHED PREDICTIONS"]:
            self.assertIn(text, report)
        self.assertNotIn("similarity", report)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.predictions = self.root / "predictions.json"
        self.expected = self.root / "expected.json"
        self.output = self.root / "report.txt"
        self.predictions.write_text(json.dumps([finding()]), encoding="utf-8")
        self.expected.write_text(json.dumps([finding()]), encoding="utf-8")

    def run_cli(self, output=None):
        return subprocess.run([sys.executable, str(ROOT / "evaluate.py"), "--predictions", str(self.predictions), "--expected", str(self.expected), "--output", str(output or self.output)], capture_output=True, text=True, timeout=10)

    def test_writes_report_and_refuses_overwrite(self):
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        saved = self.output.read_text(encoding="utf-8")
        self.assertEqual(saved, first.stdout)
        second = self.run_cli()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), saved)

    def test_refuses_output_aliasing_inputs(self):
        for target in [self.predictions, self.expected]:
            before = target.read_bytes()
            result = self.run_cli(output=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), before)

    def test_refuses_output_symlink(self):
        self.output.symlink_to(self.predictions)
        before = self.predictions.read_bytes()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.predictions.read_bytes(), before)

    def test_accepts_utf8_bom(self):
        for path in [self.predictions, self.expected]:
            path.write_text(json.dumps([finding("The “recipient” must protect information.")], ensure_ascii=False), encoding="utf-8-sig")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Matched: 1", result.stdout)

    def test_does_not_use_sidecars_to_claim_origin(self):
        for suffix in [".meta.json", ".run.json", ".review.json"]:
            Path(str(self.predictions) + suffix).write_text('{"mode":"live","run_status":"succeeded"}', encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("origin and currentness are unverified", result.stdout)
        self.assertNotIn("succeeded", result.stdout)

    def test_invalid_inputs_are_clear_and_do_not_echo_contents(self):
        for path in [self.predictions, self.expected]:
            original = path.read_bytes()
            for data, message in [(b'{"private_marker":', "not valid JSON"), (b'\xffprivate_marker', "UTF-8"), (b'{"private_marker": 3}', "array of findings")]:
                with self.subTest(path=path.name, data=data):
                    path.write_bytes(data)
                    result = self.run_cli()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("private_marker", result.stderr)
                    self.assertFalse(self.output.exists())
            path.write_bytes(original)

    def test_escaped_invalid_unicode_has_clear_error(self):
        self.predictions.write_text(json.dumps([finding(fix="\ud800")]), encoding="utf-8")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be written as UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_input_and_missing_output_directory(self):
        result = self.run_cli(output=self.root / "absent" / "report.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot write report", result.stderr)
        self.predictions.unlink()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read predictions", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
