# DraftRefine prompt registry

All runtime prompts should live in this directory instead of being hardcoded in Python code.

## Lookup order

For a call such as category `rewrite`, action `academic-rewrite`, language `zh`, DraftRefine loads prompts in this order:

1. `rewrite/academic-rewrite.zh.yaml`
2. `rewrite/academic-rewrite.en.yaml`
3. `rewrite/default.zh.yaml`
4. `rewrite/default.en.yaml`

The same rule applies to `diagnose`, `comment-map`, and `review`.

## Editable fields

- `version`: Bump this when you change wording or constraints.
- `purpose`: Human-readable purpose.
- `system_prompt`: Main instruction sent to the model.
- `schema_hint`: JSON shape the model must return.
- `guardrails`: Human-readable safety and quality rules.
- `output_schema`: Documentation for expected fields.

Keep `system_prompt` and `schema_hint` aligned. The backend expects JSON outputs, so do not change required key names unless you also update parser code and tests.
