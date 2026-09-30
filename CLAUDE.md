# LoRa Messenger — an MFruit OS app

A chat between two radios (SX126X LoRa) on a Whisplay HAT: speech to
text on the device, quick replies, typing. Raspberry Pi Zero 2 W and
Orange Pi Zero 2W. Python 3.9+.

- **Follow `.claude/rules/mfruit-os-app.md`** (MFruit OS app rules: one
  input controller, the same controls and look as MFruit OS, lifecycle).
- Input: `mfruit_sdk.input.InputController` → `Messenger._on_action`
  (`main.py`): `_on_button` and `_on_key`. The chat is a talk screen
  (`talk=`: reply list closed and speech recognition available); the
  quick replies are an MFruit OS list. Footer hints come from `_hints()`,
  states worth a coloured bar from `_status()` -- keep both in step with
  the handlers.
- Screen: `display/whisplay.py` `render(View)` is pure; MFruit OS's status
  bar and footer via the SDK. Frames are pushed only on change: every
  push deafens the radio for ~11 ms (the LCD's DC line is its M1).
- `mfruit_sdk/` is vendored from MFruit OS -- never edit it here; change
  `~/MFruitOS/mfruitos/sdk` and run `~/MFruitOS/scripts/sdk-sync.sh .`.
- Tests: `python3 -m pytest -q` (no hardware). Previews: `python3 tools/preview.py`.

---

# Skill: Write Tests, Debug, and Update Documentation

## Purpose
Handle code changes with a complete quality loop:
- implement the fix or feature
- add or update tests
- debug root causes systematically
- document changes in the README or relevant project docs

## Required workflow

### 1. Implement the requested change
- Make the smallest correct change that addresses the problem.
- Keep code consistent with the surrounding style.
- Avoid unrelated refactors or scope creep.

### 2. Write or update tests
- Add or update tests for the changed behavior.
- Prefer the narrowest test that checks the bug or feature directly.
- Cover edge cases and regression scenarios.
- Keep tests reliable and deterministic.
- If no test framework exists, clearly document the validation method used.

### 3. Debug before finalizing
- Reproduce the issue when possible.
- Read the failure output, logs, and stack traces.
- Identify the actual root cause, not just the symptom.
- Validate assumptions with targeted checks.
- If debugging is stuck, reduce scope and inspect the smallest relevant code path.

### 4. Validate the fix
- Run the smallest relevant command set:
  - unit tests for the changed area
  - targeted build or lint checks
  - focused manual verification if necessary
- Record the outcome honestly.
- If a check cannot run in the current environment, note that limitation explicitly.

### 5. Update documentation
When behavior, setup, usage, troubleshooting, or configuration changes, update the README or the most relevant project document.

Include:
- what changed
- why it changed
- any migration or follow-up steps
- validation notes when relevant

Use short, practical sections such as:

#### Update summary
- Brief summary of the fix or feature.

#### What changed
- Bullet list of key updates.

#### Validation
- Commands run
- Result or known caveat

#### Notes
- Manual steps, environment constraints, or follow-up tasks

## Quality standard
Before finishing, confirm all of the following:
- the requested change is implemented
- relevant tests are added or updated
- the bug or behavior is validated with targeted checks
- the README or relevant docs reflect the change when user-facing impact exists

## Example documentation entry
- Fixed the bug in X by updating Y logic.
- Added regression coverage for the failing scenario.
- Verified with: `command-name`.
- Updated the README usage notes for the new behavior.