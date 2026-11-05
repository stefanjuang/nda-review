"""Optional copy checks, including real-file counterexamples. No model calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import evaluate as e
import redline as r

ROOT = Path(__file__).resolve().parents[1]
RULES = [{"clause": "Rule", "is_required": True}]


def edit(source, replacement="Replacement.", rule="Rule"):
    return dict(zip(r.FIELDS, (source, rule, replacement)))


class AssemblyTests(unittest.TestCase):
    def test_preserves_every_untargeted_character_and_uses_actual_edits(self):
        before, between, after = "  Prefix\r\n\t", "\u00a0 \r\nUntouched “quotes”\n", "\r\nTail\t  "
        document = before + "First target." + between + "Second target." + after
        predictions = [edit("Second target.", "Short."), edit("First target.", "A longer new first paragraph.\nExtra line.")]
        expected = before + predictions[1]["suggested_fix"] + between + predictions[0]["suggested_fix"] + after
        self.assertEqual(e.assemble_draft(document, predictions, RULES), expected)

    def test_new_text_is_never_reinterpreted_as_an_original_target(self):
        document = "Alpha target.\nBeta target."
        predictions = [edit("Alpha target.", "Beta target. with new context."), edit("Beta target.", "Final beta.")]
        self.assertEqual(e.assemble_draft(document, predictions, RULES), "Beta target. with new context.\nFinal beta.")

    def test_whitespace_and_quote_normalization_only_locates_source(self):
        document = '  Before\r\n“Secret”\u00a0information\n is protected.\t After\r\n'
        self.assertEqual(e.assemble_draft(document, [edit('"Secret" information is protected.', "New\ntext.")], RULES),
                         '  Before\r\nNew\ntext.\t After\r\n')

    def test_verbatim_boundary_whitespace_remains_part_of_target(self):
        self.assertEqual(e.assemble_draft("Before\n target\t After", [edit("\n target\t", " X ")], RULES), "Before X  After")

    def test_empty_edits_are_identity_not_approval(self):
        document = "Original\r\n body\t  "
        self.assertEqual(e.assemble_draft(document, [], RULES), document)

    def test_no_mutation(self):
        predictions = [edit('"Secret" information.', "Fixed.")]
        before = copy.deepcopy(predictions)
        e.assemble_draft('“Secret” information.', predictions, RULES)
        self.assertEqual(predictions, before)

    def test_exact_and_normalized_ambiguity_rejected(self):
        cases = [("same", "same same"), ('"A"', '"A" then “A”'),
                 ("A B", "A B then A\n B"), ("A\tB", "A\tB then A B"),
                 ("A B", "A\nB then A\tB")]
        for quote, document in cases:
            with self.subTest(quote=quote, document=document), self.assertRaisesRegex(ValueError, "ambiguous"):
                e.assemble_draft(document, [edit(quote)], RULES)

    def test_malformed_missing_unknown_duplicate_and_overlap_rejected(self):
        bad = [None, {}, [None], [dict(edit("Alpha"), extra="x")], [edit("Alpha", " ")],
               [edit("Alpha", 4)], [edit(" ")], [edit("Unknown")], [edit("Alpha", rule="Unknown")],
               [edit("Alpha"), edit("Alpha")], [edit("Alpha Beta"), edit("Beta Gamma")]]
        for predictions in bad:
            with self.subTest(predictions=predictions), self.assertRaises(ValueError):
                e.assemble_draft("Alpha Beta Gamma", predictions, RULES)

    def test_normalized_overlaps_rejected(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            e.assemble_draft('“Alpha”\n Beta Gamma', [edit('"Alpha" Beta'), edit("Beta Gamma")], RULES)

    def test_adjacent_edits_are_allowed(self):
        self.assertEqual(e.assemble_draft("AlphaBeta", [edit("Alpha", "A"), edit("Beta", "B")], RULES), "AB")

    def test_bad_playbook_rejected(self):
        for rules in [[], None, [{}], RULES * 2]:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                e.assemble_draft("Alpha", [edit("Alpha")], rules)

    def test_actual_reference_boundary_counterexample(self):
        document = r.read_document(ROOT / "data/bad_document.txt.rtf")
        playbook = json.loads((ROOT / "data/playbook.json").read_text())
        reference = json.loads((ROOT / "data/expected_output.json").read_text())
        narrow = e.assemble_draft(document, reference, playbook)
        broadened = copy.deepcopy(reference)
        for index, number in [(2, "5.1"), (3, "6.1")]:
            full = next(line for line in document.splitlines() if line.strip().startswith(number + " "))
            broadened[index]["text_snippet"] = full.split(number + " ", 1)[1]
        coordinated = e.assemble_draft(document, broadened, playbook)
        for waiver in ["Recipient shall not be obligated to provide any certification",
                       "Recipient shall have no obligation to notify Discloser or cooperate"]:
            self.assertIn(waiver, narrow)
            self.assertNotIn(waiver, coordinated)
        for untouched in ["Recipient agrees to exercise at least the same degree of care",
                          "derivatives, adaptations, or modifications thereof",
                          "selected by Recipient in its sole discretion"]:
            self.assertIn(untouched, coordinated)
        self.assertEqual(e.compare(reference, reference)["f1"], 1)
        scores = e.compare(broadened, reference)
        self.assertEqual(len(scores["matches"]), 6)
        self.assertEqual([scores[k] for k in ("precision", "recall", "f1")], [0.75] * 3)
        self.assertEqual(scores["possible"], [(2, 2), (3, 3)])
        self.assertEqual(scores["replacement_exact_count"], 6)
        self.assertEqual([i["suggested_fix"] for i in broadened], [i["suggested_fix"] for i in reference])


class AssemblyCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.document = self.root / "document.txt"
        self.playbook = self.root / "playbook.json"
        self.predictions = self.root / "predictions.json"
        self.expected = self.root / "expected.json"
        self.report = self.root / "evaluation.txt"
        self.draft = self.root / "copy.txt"
        self.document.write_bytes(b"Before\r\nOriginal target.\tAfter\r\n")
        self.playbook.write_text(json.dumps(RULES))
        self.predictions.write_text(json.dumps([edit("Original target.", "ACTUAL saved replacement.\nLine two.")]))
        self.expected.write_text(json.dumps([edit("Original target.", "REFERENCE replacement must not be applied.")]))

    def invoke(self, draft=None):
        flags = ["-O"] if not __debug__ else []
        return subprocess.run([sys.executable] + flags + [str(ROOT / "evaluate.py"),
            "--document", str(self.document), "--playbook", str(self.playbook),
            "--predictions", str(self.predictions), "--expected", str(self.expected),
            "--output", str(self.report), "--assembled-draft", str(draft or self.draft)],
            capture_output=True, text=True, timeout=10)

    def test_writes_labeled_copy_with_exact_body_bytes(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        body = b"Before\r\nACTUAL saved replacement.\nLine two.\tAfter\r\n"
        self.assertEqual(self.draft.read_bytes(), e.DRAFT_NOTICE.encode("utf-8") + body)
        self.assertEqual(self.draft.stat().st_mode & 0o777, 0o600)
        self.assertIn("UNAPPROVED", self.draft.read_text())
        self.assertIn("UNJUDGED", self.report.read_text())
        self.assertIn(str(self.draft), self.report.read_text())
        self.assertEqual(self.report.read_text(), result.stdout)

    def test_no_assembly_unless_requested(self):
        result = subprocess.run([sys.executable, str(ROOT / "evaluate.py"), "--predictions", str(self.predictions),
            "--expected", str(self.expected), "--output", str(self.report)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.draft.exists())

    def test_existing_outputs_and_input_aliases_are_untouched(self):
        for target in [self.document, self.playbook, self.predictions, self.expected]:
            before = target.read_bytes()
            result = self.invoke(draft=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse(self.report.exists())
        self.draft.write_text("old copy")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.draft.read_text(), "old copy")
        self.assertFalse(self.report.exists())
        self.draft.unlink()
        self.report.write_text("old report")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report.read_text(), "old report")
        self.assertFalse(self.draft.exists())

    def test_symlink_hardlink_and_dangling_symlink_are_untouched(self):
        before = self.document.read_bytes()
        for kind in ["symlink", "hardlink", "dangling"]:
            if kind == "hardlink":
                os.link(self.document, self.draft)
            else:
                self.draft.symlink_to(self.document if kind == "symlink" else self.root / "missing")
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.document.read_bytes(), before)
            self.assertFalse(self.report.exists())
            self.assertFalse((self.root / "missing").exists())
            self.draft.unlink()

    def test_same_new_output_path_is_rejected(self):
        result = self.invoke(draft=self.report.parent / "." / self.report.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different new output paths", result.stderr)
        self.assertFalse(self.report.exists())

    def test_malformed_edits_and_bad_source_produce_no_outputs(self):
        for value in ["{", "{}", json.dumps([edit("invented quote")]), json.dumps([edit("Original target.", "\ud800")])]:
            self.predictions.write_text(value)
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(self.report.exists())
            self.assertFalse(self.draft.exists())

    def test_bad_playbook_produces_controlled_error_without_outputs(self):
        for value in ["{", "[]", '{"private_marker": 1}']:
            self.playbook.write_text(value)
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("private_marker", result.stderr)
            self.assertFalse(self.report.exists())
            self.assertFalse(self.draft.exists())

    def test_partial_copy_write_is_removed(self):
        with patch("redline.os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            r.write_new(self.draft, e.DRAFT_NOTICE + "partial")
        self.assertFalse(self.draft.exists())


if __name__ == "__main__":
    unittest.main()
