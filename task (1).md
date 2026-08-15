# CodeOracle - Remaining Tasks (Final)

Rebalanced against `PRD.md` for actual effort, not just task count. Original 8/8/8 split
was even in numbers but uneven in weight (A had the heaviest backend + testing load, B had
the lightest UI-only load, C was dense UI work). Changes made:

- **Confirmed: `llm.py` uses the Google Gemini API** (not Anthropic Claude, as the PRD's tech
  stack, prompt section, and cost estimates assume). This is an intentional deviation from
  `PRD.md` — flagged once here so no one "fixes" it back, and folded into Member A's retry-audit
  task so someone explicitly checks that the rest of `llm.py` (response parsing, retry wrapper,
  fenced-block extraction for refactor output) matches Gemini's SDK response shape rather than
  the Claude shape the PRD documents in Section 10.
- Added the missing screen-transition / API-wiring task to Member B (implied but never assigned).
- Merged C's duplicate-pair tasks (tests tab + copy button; refactor layout + highlighting) and
  backfilled with two small data/empty-state tasks so C isn't left light after the merge.
- Moved scalability testing into a **Shared** section — flexible owner, not dedicated solo work,
  since the PRD treats scale-testing as a Q&A talking point (Section 16/19), not a live-demo
  requirement.
- Priority tags (`Critical` / `Important` / `Cut-safe`) come from PRD Section 18 — do Critical
  items first if you're short on time.

Effort tag key: `S` = small, `M` = medium, `L` = large.

---

## 🤝 Shared / Team Tasks (do these together, not solo)

- [ ] **Scalability validation** `L` *(flexible owner — A leads, B/C help once their lists are
  done)* — run the pipeline against a ~7,000-line and ~10,000-line codebase, confirm it stays
  within the 8-minute / no-timeout targets (Section 12), document actual timings. *Cut-safe as a
  live demo — it's a Q&A talking point (Section 16), not something judges need to see run.*
- [ ] **Final end-to-end rehearsal** *(all three, right before demo)* — run the exact judge demo
  flow from Section 16 start to finish, on the real demo file, at least once. Note during
  rehearsal: since prompts and cost estimates in PRD Section 5/10 were written for Claude, sanity
  check the actual explanation/test/refactor output quality against Gemini's real responses — the
  wording of "bad vs good" examples in the prompts may need light tuning for Gemini's style.

---

## 👤 Member A — Backend Pipeline & Reliability

Focus: multi-language support, real-time progress, demo caching, safety guarantees.

- [ ] **LLM retry logic + Gemini response-shape audit** `M` — `Critical` — verify `llm.py`
  implements 2 retries with exponential backoff (1s, 2s), falling back to `"Analysis failed for
  this function"` without crashing (Feature 5). Since the project uses the **Gemini API**, confirm
  response parsing pulls text from Gemini's actual response object (not the Claude
  `response.content[0].text` shape the PRD documents), and confirm the refactor step's fenced
  python/json block extraction (Feature 7, Step 2) still works against Gemini's raw text output.
  Also confirm `ANTHROPIC_API_KEY` references in setup docs/env vars have been swapped for the
  correct Gemini API key variable.
- [ ] **Pipeline-level catch-all error handling** `S` — `Critical` — wrap the full background task so
  a total crash still updates job status to `"error"` with a message instead of leaving the job
  stuck "processing" (Section 13, Level 4).
- [ ] **Coverage retry logic** `M` — `Critical` — in `coverage_runner.py`, if a function's coverage
  comes back below 60%, retry test generation once with a prompt targeting the uncovered lines
  (Feature 6, Step 5).
- [ ] **Refactor safety verification** `M` — `Critical` — confirm/implement
  `run_tests_against_refactor()`: run the previously generated tests against refactored code and
  set `tests_verified: true/false` based on the actual pytest result (Feature 7).
- [ ] **SSE progress endpoint** `M` — `Cut-safe` (fallback: 3s polling) — implement
  `GET /progress/{job_id}` as a Server-Sent Events stream, updating every 2s with the
  emoji-prefixed progress messages (Section 8), closing on `complete`/`error`.
- [ ] **Re-add `/demo` and `/demo/file` endpoints** `S` — `Cut-safe` (fallback: manual ZIP upload) —
  tracker notes `/demo` was removed from `main.py`; re-implement with result caching (first call
  processes, subsequent calls return cached result in <100ms) per Feature 9.
- [ ] **JavaScript parsing (Feature 3)** `L` — `Cut-safe` (Python alone still satisfies the
  "2 language" requirement per Section 18) — integrate `esprima` via a Node.js subprocess; output
  must match the same dict shape as `parser.py`'s Python output; add fallback ("JS parsing
  unavailable, using raw analysis") if Node/esprima isn't available.

---

## 👤 Member B — Frontend Shell & UX Flow

Focus: the three main screens, the state that connects them, and global UI rules.

- [ ] **Upload screen polish** `M` — `Critical` — drag-and-drop zone with dashed border, file
  name/size display after selection, "Remove file" (X) button, "Analyze Codebase" button disabled
  until a file is selected, "Try Demo →" link.
- [ ] **Upload validation messaging** `S` — `Important` — red inline error text for non-ZIP files
  or files over 50MB (client-side, before hitting the API).
- [ ] **Screen state management & API wiring** `L` — `Critical` *(new — was implied but unowned)* —
  connect `POST /analyze` response (`job_id`) to the Processing screen, open the SSE connection
  (or polling fallback) to `/progress/{job_id}`, and transition to the Results screen on `complete`
  or to the error card on `error`. This is the glue between every other screen task.
- [ ] **Processing screen** `M` — `Important` — animated progress bar (fake-timed 0→90% over ~3
  min, jumps to 100% on complete), live SSE progress message display, "Estimated time: 2–5
  minutes" note.
- [ ] **Function/file discovery display** `S` — `Important` — show "Found {n} functions across
  {n} files" on the processing screen as soon as parsing completes.
- [ ] **Cancel button** `S` — `Cut-safe` — resets UI to upload screen (does not need to cancel the
  actual background job).
- [ ] **Results screen shell** `M` — `Critical` — dark theme (`bg-gray-900`), summary banner with
  4 metric cards (Files, Functions, Coverage, Breaking Changes), tab bar with blue underline on
  active tab.
- [ ] **Global UI safety rules + error state** `M` — `Critical` — no `undefined`/`null`/
  `[object Object]` ever rendered; wrap all dynamic values as `{value || "N/A"}`; loading states
  on every async action; red error card with message + "Try Again" button that resets to the
  upload screen.

---

## 👤 Member C — Results Tabs, Highlighting & Export

Focus: the content inside each of the 4 result tabs, plus export.

- [ ] **Explanation tab** `L` — `Critical` (highest rubric weight) — search box to filter functions
  by name (client-side), "Collapse All / Expand All" toggle, expandable function cards (collapsed
  = name + first sentence, expanded = all 3 lines), warning icon if explanation contains "bug" /
  "risk" / "caution" / "careful" / "dangerous".
- [ ] **Dependency Graph tab** `M` — `Important` — node/edge count display, Mermaid diagram render
  from `graph.mermaid`, "No dependencies found" fallback for zero nodes, scrollable container with
  min-height 400px.
- [ ] **Generated Tests tab (incl. code display + copy)** `L` — `Critical` (hard numeric constraint)
  *(merged from 2 tasks)* — coverage bar (green ≥60%, red <60%), coverage % number, pass/fail
  badge, red left border below threshold, "X of Y functions meet the 60% threshold" summary line,
  monospace/dark code block with a working "Copy" button.
- [ ] **Refactored Code tab (incl. highlighting)** `L` — `Important` *(merged from 2 tasks)* —
  two-column layout (Original: gray bg | Refactored: dark blue tint bg), "Tests Verified ✅" /
  "⚠️ Unverified" badge at top, `highlight.js` (CDN) wired up on both code blocks, "Copy refactored
  code" button.
- [ ] **Breaking Changes table** `M` — `Critical` (rubric item) — columns Change | Risk | Why |
  Line, risk badges (red/yellow/green), sorted high-risk first, "No breaking changes ✅" fallback
  for empty array.
- [ ] **Export Report button** `S` — `Cut-safe` — pure frontend feature: build the plain-text report
  string (files, functions, avg coverage, explanations, test coverage, breaking changes) and
  trigger download via `Blob` + `URL.createObjectURL` as `codeoracle_report.txt`.
- [ ] **Metric card data wiring** `S` — `Important` *(new — backfilled after merge)* — compute and
  feed the four summary numbers (files, functions, avg coverage, breaking changes) from the
  `/results/{job_id}` response into Member B's summary banner cards.
- [ ] **Tab-level empty/loading states** `S` — `Important` *(new — backfilled after merge)* — apply
  the global "no undefined/null" rule specifically inside each tab (e.g. a function list that
  hasn't loaded yet, zero tests generated) so no tab ever renders blank or broken before data
  arrives.

---

## Task Count & Weight Check

| Member | Tasks | Mix (S / M / L) |
|---|---|---|
| A — Backend Pipeline & Reliability | 7 | 2S / 3M / 2L |
| B — Frontend Shell & UX Flow | 8 | 4S / 3M / 1L |
| C — Results Tabs, Highlighting & Export | 8 | 3S / 2M / 3L |
| Shared | 3 | — |

Roughly even in total effort now (A's count dropped by one but kept its two largest items;
B gained one large task to offset having had the lightest load; C's count held steady after
merging duplicates and backfilling with smaller tasks).
