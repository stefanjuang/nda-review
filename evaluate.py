"""Compare saved findings with a reference locally; this is not legal validation."""
import argparse
import json
from pathlib import Path

from redline import (check_playbook, check_shape, normalize, read_document,
                     validate_candidate, write_new)

EXACT_MATCH = "exact after quote/whitespace normalization"
CONTAINMENT_MATCH = "containment (review quote boundaries)"
DRAFT_NOTICE = (
    "UNAPPROVED ASSEMBLED DRAFT - OPTIONAL HUMAN INSPECTION COPY\n"
    "Source/shape checks only. No legal approval, completeness or correctness guarantee.\n"
    "This is extracted plain text, not the original document format.\n"
    "Everything after the next line is the assembled document body.\n"
    "----- BEGIN ASSEMBLED DOCUMENT -----\n"
)


def assemble_draft(document, predictions, playbook):
    """Validate actual saved edits, then replace original spans from last to first."""
    check_playbook(playbook)
    issues = validate_candidate({"issues": predictions, "questions": []}, document, playbook)["issues"]
    edits = []
    for issue in issues:
        start = document.index(issue["text_snippet"])
        edits.append((start, start + len(issue["text_snippet"]), issue["suggested_fix"]))
    assembled = document
    # Later spans first: length changes cannot shift any earlier saved offset.
    for start, end, replacement in sorted(edits, reverse=True):
        assembled = assembled[:start] + replacement + assembled[end:]
    return assembled


def match_kind(predicted, expected):
    """The rule name must be literal; only snippet quotes/whitespace normalize."""
    if predicted["playbook_clause_reference"] != expected["playbook_clause_reference"]:
        return None
    a = normalize(predicted["text_snippet"])
    b = normalize(expected["text_snippet"])
    if a == b:
        return EXACT_MATCH
    # A text-only hint, never a scored match or a semantic judgment.
    if a in b or b in a:
        return CONTAINMENT_MATCH
    return None


def compare(predictions, expected):
    """Pair exact matches in input order, using each reference at most once."""
    check_shape(predictions)
    check_shape(expected)
    matches = []
    used_predictions = set()
    used_expected = set()
    for p, prediction in enumerate(predictions):
        for e, reference in enumerate(expected):
            if e not in used_expected and match_kind(prediction, reference) == EXACT_MATCH:
                matches.append((p, e, EXACT_MATCH))
                used_predictions.add(p)
                used_expected.add(e)
                break

    unmatched = [p for p in range(len(predictions)) if p not in used_predictions]
    missed = [e for e in range(len(expected)) if e not in used_expected]
    possible = [
        (p, e) for p in unmatched for e in range(len(expected))
        if match_kind(predictions[p], expected[e]) == CONTAINMENT_MATCH
    ]
    matched_count = len(matches)
    total_count = len(predictions) + len(expected)
    return {
        "matches": matches,
        "possible": possible,
        "unmatched": unmatched,
        "missed": missed,
        "precision": matched_count / len(predictions) if predictions else None,
        "recall": matched_count / len(expected) if expected else None,
        "f1": 2 * matched_count / total_count if total_count else None,
        "replacement_exact_count": sum(
            predictions[p]["suggested_fix"] == expected[e]["suggested_fix"]
            for p, e, _ in matches
        ),
    }


def render_report(predictions, expected, result):
    def percent(value):
        return "N/A (empty denominator)" if value is None else "{:.1%}".format(value)

    matched = len(result["matches"])
    identical_fixes = result["replacement_exact_count"]
    lines = [
        "Saved-file reference comparison",
        "Prediction origin and currentness are unverified; no new generation is confirmed.",
        "Semantic and legal correctness are NOT EVALUATED.",
        "",
        "Predictions: {} | Reference findings: {} | Matched: {}".format(
            len(predictions), len(expected), matched),
        "Exact-reference precision: " + percent(result["precision"]),
        "Exact-reference recall: " + percent(result["recall"]),
        "Exact-reference F1: " + percent(result["f1"]),
        "Matching uses literal rule names and quote/whitespace-normalized snippets, one-to-one.",
        "Pairs are chosen in input order. Containment candidates receive no credit.",
        "Containment is text-only; short or broad quotes may give unhelpful hints.",
        "These are reference-agreement scores, not defect-detection or legal-quality scores.",
        "Unmatched predictions are UNJUDGED, not established false positives.",
        "A missed reference means no exact pair, not necessarily an undetected problem.",
        "Wider source quotes can remove contradictions yet lose exact-reference credit.",
        "",
        "LITERAL REPLACEMENT AGREEMENT (separate from passage scores)",
        "Identical suggested_fix: {}/{} matched findings ({})".format(
            identical_fixes, matched, percent(identical_fixes / matched if matched else None)),
        "Different wording may be valid; identical wording is not legal approval.",
        "",
        "MATCHED FINDINGS AND REPLACEMENTS",
    ]
    for p, e, _ in result["matches"]:
        prediction, reference = predictions[p], expected[e]
        agreement = "identical" if prediction["suggested_fix"] == reference["suggested_fix"] else "different; review"
        lines += [
            "- Prediction {} -> reference {}: {}".format(p + 1, e + 1, reference["playbook_clause_reference"]),
            "  Snippet: " + prediction["text_snippet"],
            "  Literal replacement agreement: " + agreement,
            "  Predicted replacement: " + prediction["suggested_fix"],
            "  Reference replacement: " + reference["suggested_fix"],
        ]
    if not result["matches"]:
        lines.append("- None")
    lines += ["", "POSSIBLE CONTAINMENT MATCHES (UNSCORED; verify source boundaries)"]
    for p, e in result["possible"]:
        lines += [
            "- Prediction {} may correspond to reference {}: {}".format(p + 1, e + 1, expected[e]["playbook_clause_reference"]),
            "  Predicted snippet: " + predictions[p]["text_snippet"],
            "  Reference snippet: " + expected[e]["text_snippet"],
        ]
    if not result["possible"]:
        lines.append("- None")
    for heading, indices, findings in [
        ("MISSED REFERENCES (no exact match)", result["missed"], expected),
        ("UNMATCHED PREDICTIONS (UNJUDGED; may be valid issues absent from the reference)", result["unmatched"], predictions),
    ]:
        lines += ["", heading]
        for i in indices:
            finding = findings[i]
            lines += [
                "- {}: {}".format(i + 1, finding["playbook_clause_reference"]),
                "  Snippet: " + finding["text_snippet"],
                "  Replacement: " + finding["suggested_fix"],
            ]
        if not indices:
            lines.append("- None")
    return "\n".join(lines) + "\n"


def read_findings(path, label):
    """Report the failing input without exposing its contents in errors."""
    try:
        findings = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except UnicodeError:
        raise ValueError(label + " must be UTF-8 JSON (a BOM is allowed).") from None
    except json.JSONDecodeError:
        raise ValueError(label + " is not valid JSON.") from None
    except OSError:
        raise ValueError("Cannot read " + label.lower() + ". Check the file path and permissions.") from None
    try:
        check_shape(findings)
    except ValueError:
        raise ValueError(label + " must be an array of findings with exactly three nonempty string fields: text_snippet, playbook_clause_reference, suggested_fix.") from None
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", default="redline_output.json")
    parser.add_argument("--expected", default="data/expected_output.json")
    parser.add_argument("--output", default="evaluation.txt")
    parser.add_argument("--assembled-draft", help="Optional new text file; prominently labeled UNAPPROVED.")
    parser.add_argument("--document", default="data/bad_document.txt.rtf", help="Source for optional draft assembly.")
    parser.add_argument("--playbook", default="data/playbook.json", help="Rules for optional draft validation.")
    args = parser.parse_args()
    try:
        output = Path(args.output)
        draft_path = Path(args.assembled_draft) if args.assembled_draft else None
        if draft_path and draft_path.resolve() == output.resolve():
            raise ValueError("Report and assembled draft need different new output paths.")
        for path in [output] + ([draft_path] if draft_path else []):
            if path.exists() or path.is_symlink():
                raise FileExistsError()
            if not path.parent.is_dir():
                raise OSError()
        predictions = read_findings(args.predictions, "Predictions")
        expected = read_findings(args.expected, "Expected reference")
        report = render_report(predictions, expected, compare(predictions, expected))
        if draft_path:
            document = read_document(args.document)
            playbook = json.loads(Path(args.playbook).read_text(encoding="utf-8-sig"))
            draft = DRAFT_NOTICE + assemble_draft(document, predictions, playbook)
            draft.encode("utf-8")  # Fail before any writes on invalid Unicode.
            report += "\nOptional UNAPPROVED assembled copy: {}\n".format(draft_path)
            report += "Its source spans were validated. Inspect its full body and the separate review questions.\n"
        report.encode("utf-8")
        # No transaction: if the report save fails, an unapproved copy can remain.
        if draft_path:
            write_new(draft_path, draft)
        write_new(output, report)
    except UnicodeError:
        parser.exit(1, "Error: Finding text contains characters that cannot be written as UTF-8.\n")
    except FileExistsError:
        parser.exit(1, "Error: Output already exists; choose new report/draft paths. Nothing was overwritten.\n")
    except OSError:
        parser.exit(1, "Error: Cannot write report or read/save optional draft inputs. Check paths and permissions.\n")
    except ImportError:
        parser.exit(1, "Error: Missing dependency; install requirements.txt.\n")
    except json.JSONDecodeError:
        parser.exit(1, "Error: Playbook is not valid JSON.\n")
    except ValueError as error:
        parser.exit(1, "Error: {}\n".format(error))
    print(report, end="")


if __name__ == "__main__":
    main()
