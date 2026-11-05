"""Review one NDA against a playbook. Save an unapproved draft for human review."""
import argparse
import json
import os
import re
import tempfile
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

FIELDS = ("text_snippet", "playbook_clause_reference", "suggested_fix")
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def read_document(path):
    """Extract plain text; refuse RTF features this small reader cannot review."""
    raw = Path(path).read_bytes()
    if raw.lstrip().startswith(b"{\\rtf"):
        rtf = raw.decode("cp1252")
        unsupported = {
            "header", "headerl", "headerr", "headerf",
            "footer", "footerl", "footerr", "footerf",
            "footnote", "annotation", "revised", "deleted", "v",
        }
        # Consume escaped symbols too, so a literal \\header is not a control.
        controls = re.finditer(r"\\(?:([a-zA-Z]+)|[^a-zA-Z])", rtf)
        if any(control.group(1) in unsupported for control in controls):
            raise ValueError("RTF contains headers, notes, tracked changes, or hidden text; "
                             "export reviewed plain text first.")
        from striprtf.striprtf import rtf_to_text
        try:
            text = rtf_to_text(rtf)
        except (ValueError, TypeError, IndexError):
            raise ValueError("Could not extract this RTF; export reviewed plain text first.") from None
    else:
        text = raw.decode("utf-8-sig")
    if not text.strip():
        raise ValueError("The document is empty after text extraction.")
    return text


def normalize(text):
    """Normalize quote style and whitespace, never words or punctuation."""
    return " ".join(text.translate(QUOTES).split())


def source_quote(snippet, document):
    """Return source typography; reject missing or ambiguous normalized quotes."""
    characters, positions = [], []
    # Map each normalized character back to its original document offset.
    for position, char in enumerate(document.translate(QUOTES)):
        if char.isspace():
            if not characters or characters[-1] == " ":
                continue
            char = " "
        characters.append(char)
        positions.append(position)
    normalized_document = "".join(characters)
    quote = normalize(snippet)
    start = normalized_document.find(quote)
    if not quote or start < 0:
        raise ValueError("text_snippet is not a quote from the extracted document.")
    if normalized_document.find(quote, start + 1) >= 0:
        raise ValueError("text_snippet is ambiguous; quote more context.")
    # Even an exact quote must be unique after allowed normalization.
    # Retain explicitly quoted boundary whitespace when it matches verbatim.
    if snippet in document:
        return snippet
    source_start = positions[start]
    source_end = positions[start + len(quote) - 1] + 1
    return document[source_start:source_end]


def check_shape(issues):
    """Require an array of objects, each with three named, nonempty string fields."""
    if not isinstance(issues, list):
        raise ValueError("Issues must be a JSON array.")
    for index, issue in enumerate(issues, 1):
        if not isinstance(issue, dict) or set(issue) != set(FIELDS):
            raise ValueError("Issue {} must have exactly these keys: {}".format(index, FIELDS))
        if any(not isinstance(issue[field], str) or not issue[field].strip() for field in FIELDS):
            raise ValueError("Issue {} fields must be nonempty strings.".format(index))


def check_playbook(playbook):
    if not isinstance(playbook, list) or not playbook:
        raise ValueError("Playbook must be a nonempty array of named rules with boolean is_required.")
    names = set()
    for rule in playbook:
        if not isinstance(rule, dict) or not isinstance(rule.get("clause"), str) \
                or not rule["clause"].strip() or type(rule.get("is_required")) is not bool:
            raise ValueError("Playbook rules need a nonempty clause name and boolean is_required.")
        if rule["clause"] in names:
            raise ValueError("Playbook rule names must be unique.")
        names.add(rule["clause"])


def validate_candidate(candidate, document, playbook):
    """Check the issues/questions response envelope, not legal or policy quality."""
    if not isinstance(candidate, dict) or set(candidate) != {"issues", "questions"}:
        raise ValueError("Expected a JSON object with exactly issues and questions.")
    check_shape(candidate["issues"])
    questions = candidate["questions"]
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("Questions must be an array of nonempty strings.")
    rule_names = {rule["clause"] for rule in playbook}
    issues, spans = [], []
    for index, issue in enumerate(candidate["issues"], 1):
        if issue["playbook_clause_reference"] not in rule_names:
            raise ValueError("Issue {} has an unknown playbook clause name.".format(index))
        quote = source_quote(issue["text_snippet"], document)
        start = document.index(quote)
        end = start + len(quote)
        # End offsets are exclusive, so adjacent quotes do not overlap.
        for previous, previous_start, previous_end in spans:
            if start < previous_end and previous_start < end:
                raise ValueError("Issues {} and {} overlap; combine their required changes.".format(previous, index))
        spans.append((index, start, end))
        issues.append(dict(issue, text_snippet=quote))
    return {"issues": issues, "questions": list(questions)}


def build_request(document, playbook, model, max_output_tokens):
    """The reference answer is deliberately absent from this request."""
    check_playbook(playbook)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Choose an authorized model explicitly.")
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError("max_output_tokens must be a positive integer chosen explicitly.")
    return {
        "model": model,
        "store": False,
        "service_tier": "default",
        "max_output_tokens": max_output_tokens,
        "instructions": Path(__file__).with_name("instructions.txt").read_text(encoding="utf-8"),
        "input": json.dumps({"original_document": document, "playbook": playbook}, ensure_ascii=False),
        "text": {"format": {
            "type": "json_schema", "name": "nda_draft", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["issues", "questions"],
                "properties": {
                    "issues": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": list(FIELDS),
                        "properties": {field: {"type": "string"} for field in FIELDS},
                    }},
                    "questions": {"type": "array", "items": {"type": "string"}},
                },
            },
        }},
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the key or silently make another request.


urlopen = build_opener(NoRedirect()).open


def read_api_key(path=None):
    """Prefer the environment; otherwise read one literal key from a local .env."""
    if "OPENAI_API_KEY" in os.environ:
        return os.environ["OPENAI_API_KEY"]
    path = Path(path) if path is not None else Path(__file__).with_name(".env")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return None
    values = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if name.strip() != "OPENAI_API_KEY":
            continue
        value = value.strip()
        if not separator or not value:
            raise ValueError("Set OPENAI_API_KEY in the environment or a plain-text .env file.")
        if value[0] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("OPENAI_API_KEY has unmatched quotes in .env.")
            value = value[1:-1]
        values.append(value)
    if len(values) > 1:
        raise ValueError("Use one OPENAI_API_KEY assignment in .env.")
    return values[0] if values else None


def call_model(payload, api_key):
    """Return completed model text. One attempt, no redirects or automatic retry."""
    if not isinstance(api_key, str) or not api_key or any(not 33 <= ord(c) <= 126 for c in api_key):
        raise ValueError("OPENAI_API_KEY must contain only non-whitespace printable ASCII characters.")
    request = Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=600) as response:
            result = json.load(response)
    except HTTPError as error:
        raise ValueError("API HTTP {}; no retry was made.".format(error.code)) from None
    except (URLError, OSError, HTTPException):
        raise ValueError("API connection failed or timed out. Outcome may be uncertain; no retry was made.") from None
    except ValueError:
        raise ValueError("API response was not valid JSON; no output was accepted.") from None
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise ValueError("API response was not completed; no output was accepted.")
    output = result.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ValueError("API output must be a list of objects.")
    text_parts = []
    for item in output:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
            raise ValueError("API message content must be a list of objects.")
        for part in content:
            if part.get("type") == "refusal":
                raise ValueError("Model refused the request; no output was accepted.")
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("API output text must be a string.")
                text_parts.append(part["text"])
    if not text_parts:
        raise ValueError("API response contains no valid output text.")
    return "".join(text_parts)


def write_new(path, text):
    """Exclusive creation prevents overwriting originals, old results, or aliases."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink()
        raise


def save_response(output, text):
    """Keep paid model text before parsing or validation, separate from the draft."""
    fd, name = tempfile.mkstemp(prefix=output.name + ".unaccepted-", suffix=".txt", dir=str(output.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ValueError("Response save failed. A possibly partial unaccepted response remains at {}. "
                         "No draft was accepted.".format(name)) from None
    return Path(name)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", default="data/bad_document.txt.rtf")
    parser.add_argument("--playbook", default="data/playbook.json")
    parser.add_argument("--output", default="redline_output.json", help="New filename; existing files are never overwritten.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock-response", help="Read canned issues/questions JSON, without an API call.")
    mode.add_argument("--live", action="store_true", help="Send private inputs to OpenAI; requires separate authorization.")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, help="Required in live mode; not a dollar cap.")
    args = parser.parse_args()
    output = Path(args.output)
    review_path = Path(str(output) + ".review.json")
    try:
        # Refuse reuse before a paid request. Exclusive writes also catch races/aliases.
        for path in (output, review_path):
            if path.exists() or path.is_symlink():
                raise ValueError("Output or review file already exists; choose a new --output filename.")
        if not output.parent.is_dir():
            raise ValueError("Output directory does not exist; create it before running.")
        document = read_document(args.document)
        playbook = json.loads(Path(args.playbook).read_text(encoding="utf-8-sig"))
        check_playbook(playbook)
        if args.live:
            payload = build_request(document, playbook, args.model, args.max_output_tokens)
            text = call_model(payload, read_api_key())
            saved = save_response(output, text)
            print("Unaccepted model response saved to {}. This is not a validated draft.".format(saved))
        else:
            text = Path(args.mock_response).read_text(encoding="utf-8-sig")
        candidate = validate_candidate(json.loads(text), document, playbook)
        review = {
            "mode": "live" if args.live else "mock", "manual_review_required": True,
            "questions": candidate["questions"],
            "manual_review_checklist": [{"playbook_clause_reference": rule["clause"], "status": "not_reviewed"}
                                        for rule in playbook],
        }
        # Save the draft last. A failed save may leave a review file; choose a new name.
        write_new(review_path, json_text(review))
        # The required output is only the issue array, not the response envelope.
        write_new(output, json_text(candidate["issues"]))
        print("Saved {} issues to {}. Human review required; see {}.".format(
            len(candidate["issues"]), output, review_path))
    except (OSError, ValueError, ImportError) as error:
        # JSON and Unicode decoding errors also inherit from ValueError.
        # Match only plain ValueError so their raw details cannot leak.
        if type(error) is ValueError:
            message = str(error)  # Only our controlled validation messages.
        elif isinstance(error, json.JSONDecodeError):
            message = "Malformed JSON; no draft was accepted."
        elif isinstance(error, ImportError):
            message = "Missing dependency; install requirements.txt."
        else:
            message = "Could not read or save files; check paths, encoding, permissions and free space."
        parser.exit(1, "Error: {}\n".format(message))


if __name__ == "__main__":
    main()
