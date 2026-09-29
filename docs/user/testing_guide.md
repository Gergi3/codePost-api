---
key: testing-guide
path: testing-guide
title: Testing Guide
category: Reference
order: 19
---

# Testing Guide

This guide is the source of truth for script-based test syntax in codePost.

It is written to match the current backend parser behavior.

## Where to find testing in the UI

1. Go to **Admin Console > Assignments > Environment**.
2. Select an assignment from **Environment & Tests**.
3. Select a **Test Category** (or create one).
4. Open the **Test Script** editor.
5. Use **Code**, **Split Preview**, or **Builder** view.

## Quick workflow

1. Set the assignment **Environment Language** first.
2. Create/open a **Test Category**.
3. Add script tests in **Test Script**.
4. Select a **Target File** for context-aware suggestions and helpers. (This is required as it will determine which file the test will run against and what context the AI will use for suggestions.)
5. Confirm parsing in **Split Preview**.
6. Save category and verify generated test cases.

> [!IMPORTANT]
> You can only create tests after an assignment environment exists in **Environment & Tests**. An environment is usually created automatically when you upload assignment files. The assignment language determines how the script is parsed, so set that first.

> [!TIP]
> Save early and often while writing tests. The parser syncs test cases from your script.

## How script parsing works

- codePost parses your script by language and creates/updates test cases automatically.
- Each test should define at least:
  - a **name**
  - a **points** value
- Category **max points** is computed as the sum of parsed `points` values.
- On save, codePost syncs test cases by parsed function identity:
  - creates new tests
  - updates existing tests
  - removes tests no longer present in the script

> [!IMPORTANT]
> Keep test names stable and unique in a category so updates are predictable.

![The instructor test interface](/assets/docs/instructor_tests.png)

## What fields are extracted

The parser extracts (when present):

- `functionName` (internal identifier)
- `name` (display title)
- `description` (optional)
- `points`
- `timeout` (optional; defaults to `30` seconds when omitted)
- `objectives` (optional; for learning outcome tracking)
- `hidden` (optional; if true, test is hidden from students)

### Hidden tests and objectives syntax

- **Python**: use decorator keywords directly (for example, `hidden=True`, `objectives=["LO1", "LO2"]`).
- **Java**: use annotation parameters directly (for example, `hidden=true`, `objectives={"LO1", "LO2"}`).
- **R, C/C++, Node/JS/TS, Ruby, PHP**: add a parser directive comment directly above the test definition:
  - `@codepost hidden`
  - `@codepost objectives=LO1,LO2`
  - Combined: `@codepost hidden objectives=LO1,LO2`

The directive is recognized on `//`, `#`, `--`, and `/* */` comment lines, and may appear up to three lines above the test definition (the parser stops at the first non-comment line).

> [!NOTE]
> For non-Python/Java languages, `hidden` and `objectives` are parser metadata (sync-time), not runtime test-function parameters. They are read once when the test category is saved and persisted on the test case record.

## Hidden tests

A **hidden** test contributes to a student's score exactly like a normal test, but the student-facing test results endpoint strips its name, logs, and explanation before responding. Graders and admins continue to see full output in the code-review panel.

Use hidden tests when you want to grade against behavior that should not be reverse-engineered from output — anti-cheating probes, edge-cases reserved for after publishing, or autograder traps.

### Marking a test hidden

```python
# Python — decorator keyword
@test(name="Reject negative input", points=2, hidden=True)
def test_reject_negative(): ...
```

```java
// Java — annotation parameter
@Test(name = "Reject negative input", points = 2, hidden = true)
public void testRejectNegative() { ... }
```

```js
// JS / TS — @codepost directive above the test
// @codepost hidden
test("Reject negative input", 2, "...", function () { ... });
```

### What students see

- The test counts toward the category's `maxPoints` exactly as it would otherwise.
- The student receives **pass/fail** and the resulting point delta only.
- Test name, logs, captured stdout/stderr, and explanation are blanked.

## Learning Objectives

**Learning Objectives** let you communicate *what* a student demonstrated — not just which tests passed. Each objective is defined per assignment and linked to one or more test cases. The student feedback panel shows objectives alongside test results, gated by the objective's visibility settings.

### Defining an objective

You can create objectives explicitly through the assignment UI, or implicitly by referencing them in a test script. The first time a test script references an objective `shortId` that does not yet exist on the assignment, codePost auto-creates the objective with a default display name derived from the `shortId` — you can then edit its name, description, visibility, and aggregation in the Learning Objectives UI.

```python
@test(name="Reverses a list of length n", points=3, objectives=["recursion", "lists"])
def test_reverse(): ...
```

```js
// @codepost objectives=recursion,lists
test("Reverses a list of length n", 3, "...", function () { ... });
```

### Visibility modes

Each objective has a **visibility mode** controlling when students see it:

| Mode        | When students see this objective                              |
| ----------- | ------------------------------------------------------------- |
| `always`    | On every submission, regardless of pass/fail.                 |
| `on_pass`   | Only when **all** linked tests for this objective pass.       |
| `on_fail`   | Only when **at least one** linked test for this objective fails. |
| `never`     | Hidden from students entirely — visible only to staff.        |

Staff (graders, course admins) always see every objective, regardless of visibility.

### Aggregation modes

Each objective also has an **aggregation mode** controlling how its `met` status and `score` are computed across linked tests:

| Mode              | Met when…                                              | Score                                       |
| ----------------- | ------------------------------------------------------ | ------------------------------------------- |
| `all`             | Every linked test passes.                              | `1.0` if all pass, otherwise fraction passed |
| `any`             | At least one linked test passes.                       | `1.0` if any pass, otherwise `0.0`           |
| `percentage`      | 100% of linked tests pass.                             | `passed_count / total_count`                |
| `points_weighted` | All weighted points earned.                            | `earned_points / total_points` across linked tests |

### What the API returns

`GET /submissions/{id}/submissionTestResults/` now includes a `learningObjectives` array in the response:

```json
{
  "submissionTests": [...],
  "logs": "...",
  "learningObjectives": [
    {
      "id": 12,
      "shortId": "recursion",
      "name": "Recursion",
      "description": "Uses recursion to solve list-processing problems.",
      "met": false,
      "score": 0.5,
      "aggregationMode": "percentage"
    }
  ]
}
```

For student views, objectives whose `visibilityMode` does not match the submission's state are filtered out server-side.

### Managing objectives via the API

- `GET /assignments/{id}/learningObjectives/` — list all objectives for an assignment (course staff).
- `GET/POST/PATCH/DELETE /learningObjectives/` — CRUD for objectives (course admin for writes; course staff for reads).

## Language syntax reference

The table below shows **recognized parser formats**.

| Language   | Recognized format                                                | Notes                                                                                |
| ---------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Python     | `@test(...)` on `def`                                            | Supports positional first arg for `name`; `points` should be keyword (`points=...`). |
| Java       | `@Test(...)` on `public` method                                  | Regex-based; expects annotation + `public <type> <methodName>`.                      |
| R          | `run_test("Name", points, "desc", function() { ... }, timeout)`  | Current parser expects `run_test(...)` (not roxygen `#' @test`).                     |
| C/C++      | `TEST`, `TEST_DESC`, `TEST_TIMEOUT`, `TEST_DESC_TIMEOUT`         | Macro name becomes internal identifier.                                              |
| Node/JS/TS | `test("Name", points, "desc", fn, timeout);`                     | Parser expects semicolon-terminated `test(...)` statements.                          |
| Ruby       | `run_test("Name", points, "desc", timeout)`                      | Timeout optional in parser.                                                          |
| PHP        | `Tester::test("Name", points, "desc", function(){...}, timeout)` | Parser extracts name/points/description; timeout may not be extracted.               |

## Supported script formats

Choose a language to see the exact parser-compatible format.
Each example includes separate test functions showing common return patterns (full credit, message returns, and partial credit).

[[SCRIPT_FORMAT_SELECTOR]]

## Scoring and points behavior

- `points` in script become each test case's **pointsPass** value.
- Category `maxPoints` is auto-updated from total parsed points.
- If parsing misses a test, its points will not contribute to category totals.

## Return patterns (full vs partial, with/without message)

Return patterns are shown directly inside each language script example above.

> [!NOTE]
> In all languages, score values are clamped to the valid range: `0 <= score <= points`.
> If a return shape is not recognized, the test usually defaults to full-credit pass unless an assertion/exception fails.

## Parser gotchas (important)

- **R:** use `run_test(...)` for parser compatibility.
- **Java:** method must match the parser-friendly `@Test(...)` + `public` signature.
- **Node/JS/TS:** terminate tests with `;` to match current parser pattern.
- **Python:** using `points` as a keyword is safest.
- **Cross-language:** malformed syntax may silently reduce parsed tests.

## Troubleshooting checklist

### If preview shows no tests or fewer tests than expected:

1. Confirm assignment **Environment Language** is correct.
2. Check syntax against this guide's exact pattern.
3. Verify each test has a parseable `name` and `points`.
4. Ensure your script is saved in the category.
5. Re-open Split Preview after edits.

### If totals look wrong:

1. Search for tests missing `points`.
2. Look for syntax errors that stop pattern matching.
3. Check if renamed/removed tests were intentionally changed.

### If tests are failing unexpectedly:

1. Is the students code causing exceptions that the test doesn't handle?
2. Look for syntax issues in the test script that could cause parsing errors.
3. Check if the test timeout is too short for the execution.

## Tips

- Use **Split Preview** to verify parsing and points.
- Set the **Target File** so helpers/AI suggestions use the right context.
- Start with Builder view if you want scaffolding, then refine in Code view.
- Globals can be defined outside test functions for shared setup, but be mindful of test isolation and side effects. Make sure these are static and do not rely on mutable state that could cause tests to interfere with each other.

## Best Practices

### Isolation

- Tests should not depend on each other.
- Do not assume `Test A` runs before `Test B`.
- Don't mutate shared state in globals between `Test A` and `Test B`, outcomes may be non-deterministic.

### Resource Cleanup

- If your test creates temporary files, delete them in a `finally` block or tear-down step.
- The environment persists for the duration of the run session, so safe cleanup prevents side effects.

### Robustness

- Handle potential exceptions in student code.
- Use timeouts to prevent infinite loops from hanging the entire suite.

## Notes

- Parsing and preview behavior depend on the assignment environment language.
- If script syntax is invalid, preview may return no tests or an error hint.
