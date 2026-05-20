# Phase 0 — Findings

Recorded while running Phase 0 of [flowboard_modification_plan.md](flowboard_modification_plan.md).

## Environment notes

- Python venv at `agent/.venv/` uses **Python 3.14.4** (created by `uv`), not the 3.11 the plan suggests. All deps install fine and all tests pass against 3.14.
- The `uv`-created venv ships **without `pip`**. Bootstrap via `python -m ensurepip --upgrade`. `pytest` and `pytest-asyncio` are NOT in `requirements.txt` and were added manually for the test run.
  - **Phase 1 action**: add `pytest`, `pytest-asyncio`, and a `pip` bootstrap note to `requirements.txt` (or split into `requirements-dev.txt`).
- Test count: **387 passed in 26.02s** (plan estimate was 190; README badge says 333 — repo grew). 3 `DeprecationWarning`s for `asyncio.get_event_loop_policy` (Python 3.16 removal). Not blocking.

## Q1 (FLOWBOARD_ANALYSIS.md §L item 1) — Storyboard capitalisation bug

**Verdict**: real bug, not a bypass. The `Storyboard` node type has NEVER worked through `POST /api/nodes`.

### Evidence

[agent/flowboard/routes/nodes.py:13](../agent/flowboard/routes/nodes.py):
```python
NodeType = Literal["character", "image", "video", "prompt", "note", "visual_asset"]
```

Direct Pydantic validation rejects both casings:
```
NodeCreate(board_id=1, type='Storyboard') -> ValidationError (literal_error)
NodeCreate(board_id=1, type='storyboard') -> ValidationError (literal_error)
```

FastAPI TestClient against the live app, end-to-end:
```
POST /api/nodes type='image'       -> 200
POST /api/nodes type='Storyboard'  -> 422 Unprocessable Entity
POST /api/nodes type='storyboard'  -> 422 Unprocessable Entity
POST /api/nodes type='foo'         -> 422 Unprocessable Entity
```

### Why the frontend doesn't error visibly

[frontend/src/store/board.ts:482](../frontend/src/store/board.ts):
```ts
} catch {
  // surface silently for now
}
return null;
```

`addNodeOfType` swallows the HTTP failure and returns null. The user clicks the Storyboard chip in `AddNodePalette`, the request 422s, nothing happens, no error toast. The worker handler `_handle_gen_storyboard` exists but is unreachable through normal flow because no Storyboard node row gets created.

### Resolution plan (deferred to Phase 4.4)

Per the plan, Phase 4.4 rewrites the type registry on both sides. The fix is to extend the Literal:

```python
NodeType = Literal[
    "character", "image", "video", "prompt", "note", "visual_asset",
    "Storyboard",                                         # fix the existing bug
    "script", "bible_ref", "master_shot", "approval_gate" # new anime kinds
]
```

Mirror in `frontend/src/api/client.ts`. **Casing choice**: keep `"Storyboard"` capitalised because frontend already uses that casing and renaming it would break the existing worker handler dispatcher key. New types use snake_case.

Also: replace the silent `catch {}` in `store/board.ts:482` with a visible error surface (toast). Right now the same failure mode could hide future bugs.

## Dreamina API contract — outstanding

Phase 0.5 says "Document Dreamina API contract" — this requires the user's Dreamina/Volcengine account credentials to test against the real API. A skeleton doc is at [dreamina_api_contract.md](dreamina_api_contract.md) for the user to fill in before Phase 5.
