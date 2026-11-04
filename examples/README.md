# Examples

## Solve a bug with real verification

From this directory:

```bash
export HELIX_WORKSPACE=$PWD
helix serve
```

Then in the **Solve** tab, or via curl:

```bash
curl -s -X POST localhost:8000/api/problems -H 'Content-Type: application/json' -d '{
  "statement": "calc.py divides by zero when the divisor is 0. It should raise a clear ValueError instead.",
  "criteria": [
    {"description": "the fix is present", "kind": "contains",
     "value": "ValueError", "path": "calc.py", "pattern": "raise ValueError"},
    {"description": "behaviour is correct", "kind": "python",
     "value": "import calc\nassert calc.divide(6, 3) == 2\ntry:\n    calc.divide(1, 0)\nexcept ValueError:\n    print(\"correct\")\n"}
  ]
}'
```

The `criteria` are the guard: `solved` is only `true` once both actually pass, with
the raw evidence attached.

## Review a diff

Any change the agent proposes shows up in the **Diffs** tab (or `helix diffs`). Toggle
individual hunks, apply, and roll back if you change your mind:

```bash
helix diffs
helix preview <id>       # dry-run + conflict check
helix apply <id>         # apply all accepted hunks
helix rollback <id>      # restore the pre-change state
```

## Run without a model

Every example works offline with the deterministic mock:

```bash
helix solve "check the project layout" --check "test -d helix" --mock
```
