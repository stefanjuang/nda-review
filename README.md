# NDA review

Python CLI for reviewing the supplied NDA against its playbook. It extracts the document, sends the full text and playbook in one structured request, validates the response, and saves proposed replacements plus review questions. A separate local evaluator compares the findings with the supplied reference; the generator never reads that reference.

## Setup

Tested with Python 3.12.14; targets Python 3.8+.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`. Offline commands need no API key. For live use, copy `.env.example` to a new plain-text `.env` beside `redline.py` and enter `OPENAI_API_KEY` locally. An existing environment variable takes precedence. Keep `.env` private and out of version control.

## Run

From this folder, check the code and reproduce the submitted evaluation without an API call:

```sh
python tests/run_offline.py
python -O tests/run_offline.py
mkdir -p outputs
python evaluate.py --predictions redline_output.json --output outputs/evaluation.txt
```

Try the complete flow with a canned response:

```sh
python redline.py --mock-response fixtures/canned_response.json --output outputs/demo.json
python evaluate.py --predictions outputs/demo.json --output outputs/demo-evaluation.txt
```

For a new live run, which sends the document and playbook to OpenAI and incurs API usage charges:

```sh
python redline.py --live --model gpt-6.1-sol --max-output-tokens 25000 --output outputs/new-review.json
python evaluate.py --predictions outputs/new-review.json --output outputs/new-evaluation.txt
```

Use new output filenames for each run; existing files are never overwritten.

## Files and results

- `redline.py` and `instructions.txt`: generation, prompt, validation, and saving.
- `evaluate.py`: local reference comparison.
- `redline_output.json`: 19 saved findings, each with `text_snippet`, `playbook_clause_reference`, and `suggested_fix`.
- `redline_output.json.review.json`: 12 review questions and a rule-review checklist.
- `evaluation.txt`: comparison of the submitted output with the eight reference findings.
- `data/`: supplied document, playbook, and reference, unchanged.
- `tests/` and `fixtures/`: offline tests and a deliberately incomplete mock response.

### Results

The submitted output has **0/8 exact-reference matches** and seven unscored containment candidates. Matching requires the same rule name and equal snippets after quote/whitespace normalization. Different quote boundaries lose exact-match credit. This measures reference agreement, not legal correctness; unmatched findings are unjudged.

The draft addresses several reference concerns, but policy conflicts and missing context still require review.

In one API run on the supplied NDA and playbook, the request took approximately 133 seconds and cost approximately $0.12 based on recorded usage. Latency and cost vary by model, input size and caching. This is a single-run measurement; cost is an estimate, not an invoice.

## Scope and limitations

The 83 offline tests check mechanics and failure handling, not model accuracy. Validation checks structure, rule names, unique source quotes, and nonoverlapping edits. It does not establish policy compliance or completeness. Conflicting playbook fields and missing business context remain questions for review; the output is an unapproved draft.

The RTF reader is scoped to the supplied document and rejects unsupported notes, tracked changes, and hidden text. The client makes one attempt with a 600-second timeout and no automatic retry or repair. The output-token limit is not a spending cap. A failed command may leave partial files; use its exit status to confirm completion.
