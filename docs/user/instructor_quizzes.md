---
key: instructor-quizzes
path: instructor-quizzes
title: Quizzes
category: Instructor Workflows
order: 12
---

# Quizzes

codePost includes a full quiz system for building, delivering, and grading quizzes alongside your
assignments. This page is the end-to-end instructor workflow. For how the other roles experience
quizzes, see the [Student Guide](/docs/student) and [Grader Guide](/docs/grader).

A quiz can live one of two ways:

- **Standalone** — it opens and closes on its own dates, independent of any assignment.
- **Attached to an assignment** — it opens and closes relative to the assignment's lifecycle (for
  example, "after the student submits" or "a week after feedback is released"), and shows up on the
  assignment card for students. Its score is always kept **separate** from the assignment grade.

## How quizzes fit together

The building blocks flow in one direction:

1. **Question banks** — a course-level, reusable pool of questions. Every question lives in exactly
   one bank.
2. **Questions** — the individual items (multiple choice, code, essay, etc.), authored in a bank.
3. **Quizzes** — a container that pulls questions together, sets the rules (timing, attempts,
   scoring), and is optionally attached to an assignment.

A quiz can receive questions three ways: **fixed questions** you pick from a bank, **random draws**
(a Canvas-style group that pulls *N* random questions from a bank per student), and **per-student
AI-generated sections** (a fresh set generated for each student from their own submission).

> [!IMPORTANT]
> **You are always the author.** Even when AI drafts a question, nothing becomes a real quiz
> question until you accept or publish it — and **students never see any signal that a question was
> AI-assisted.** Questions always read as instructor-authored.

## Open the Quizzes hub

In the admin navigation, open **Quizzes**. The hub has two tabs:

- **Question Banks** — author and organize your reusable questions.
- **Quizzes** — assemble, configure, publish, and grade quizzes.

## Author question banks and questions

1. On the **Question Banks** tab, create a bank (for example, "Midterm concepts" or "Week 3").
2. Add questions to the selected bank. Each question has a **type** (see the table below), a point
   value, and — for choice questions — a set of answer options with the correct one(s) marked.
3. Question text and descriptions support **Markdown and images**, so you can format prompts and
   embed diagrams.
4. **Code** questions use a full code editor, with optional starter code shown to the student.

Banks can be linked to assignments, which is what lets AI suggestions and per-student generation
draw on the right submissions.

### Question types

| Type | Graded | Notes |
| --- | --- | --- |
| Multiple Choice (one correct) | Automatic | Exactly one correct option. |
| Multiple Answers (several correct) | Automatic | All-or-nothing, or partial credit if enabled. |
| True / False | Automatic | |
| Short Answer | Automatic | Case-insensitive, trimmed match against accepted answers. |
| Numerical | Automatic | Parsed value within an optional ± tolerance. |
| Essay | Manual | Free-text response, graded by a person. |
| Code | Manual | Written in a code editor, graded by a person. |

## Import from Canvas

If you already have quizzes in Canvas (or another LMS), export them as **QTI / Common Cartridge**
(`.zip`, `.imscc`, or `.xml`) and use the **Import** button on the Question Banks tab.

- By default the importer brings in **questions only**, into a bank of your choice.
- Check **"also import quizzes"** to recreate the quizzes as well.
- Imports are **deduplicated and idempotent** — re-importing the same file won't create duplicates.

## Generate questions with AI

> [!NOTE]
> AI question generation requires the course's **AI question generation** feature (on by default).
> See the [AI Settings & Usage Guide](/docs/ai-guide).

### Suggest new questions for a bank

From a bank, use **Suggest questions** to have codePost draft candidate questions from an attached
assignment. Choose the question type and how many to generate, then **review each suggestion** and
**Accept** the ones you want. Accepting is the authoring step — the accepted question becomes a
normal question you can freely edit.

### Refresh a question across semesters

Each question has a **Suggest update** action. codePost drafts a refreshed version (optionally
guided by instructions you provide); when you **Accept**, it updates the question **in place** —
keeping its identity and bank/quiz memberships. This is handy for reusing a bank in a new term.

## Build a quiz

On the **Quizzes** tab, **New Quiz** opens a short setup workflow — **Basics**, **Availability**,
**Attempts**, **Results**, **Security**, **AI questions**, and a final **Review** — so the quiz is
created already configured the way you want it. Only a title is required: **Skip & create** on any
step creates the quiz immediately with the defaults, and the Review step lets you **create as a
draft** or **create & publish** in one go. Every choice remains editable afterwards in the builder's
**Settings** tab.

After creating the quiz, work through the quiz builder's four tabs: **Settings**, **Questions**,
**Grading**, and **Review**.

### Settings

- **Attach to an assignment** (optional) and choose the **open trigger** — when an attached quiz
  becomes available: *during the assignment*, *after the assignment closes*, *after the student
  submits*, *after feedback is released*, or *after each student's feedback is released*.
- **Close event** — when the quiz closes: *never*, *at the assignment due date*, *after submission*,
  *when feedback is released*, or a *fixed date* — with an optional **offset** (e.g. "a week after
  feedback"). **End attempts at close** makes the close a hard deadline that auto-submits any
  in-progress attempt; otherwise the close only blocks *new* attempts.
- **Per-student feedback caveat** — if the assignment's feedback flow is **Per student**, there is
  no single feedback-release moment, so the *after feedback is released* trigger and the
  *when feedback is released* close event can't be used (codePost blocks the combination in both
  directions). Use *after each student's feedback is released* instead — it's the self-paced
  equivalent.
- **Late access code** — optional. Generate a code and share it with students who missed the
  deadline: entering it lets them start the quiz **after it closes**, with the normal time limit.
  Nothing else about availability changes, and the code is never shown to students. You can
  **copy**, **rotate** ("New code"), or **remove** the code at any time — rotating or removing
  immediately invalidates the old one. Late starts are flagged on the attempt and recorded in the
  [Activity Log](/docs/activity-log).
- **Time limit** (leave blank for untimed).
- **Attempts allowed** (0 = unlimited).
- **Shuffle questions** — randomize order per attempt.
- **Sequential mode** — show **one question at a time**, and optionally **allow backtracking**.
- **Passing score** — an optional threshold, as a percentage or raw points.
- **Scoring policy** — when a student has multiple attempts, which one counts: *highest*, *latest*,
  or *average*.
- **Publish** — an unpublished quiz is a **draft** only you can see. Students only see published
  quizzes.
- **Require Safe Exam Browser** — lock the quiz down to
  [Safe Exam Browser](https://safeexambrowser.org) (see below).

### What students see after submitting

The **After submission** settings control what students can see about their results:

- **When results are released** — *as soon as they submit*, or *after the quiz closes*. Holding
  results until close keeps scores, points, and the answer key hidden — on the quiz card and on
  review — until the quiz closes for that student. (This option requires the quiz to actually
  close; the builder warns if you seal results on a quiz that never closes.)
- **Let students reopen submitted attempts** — off means students get a submission confirmation
  only; their score still appears on the quiz card once results are released.
- **Results students see** — on review, *scores + their answers* or *scores only* (score without
  the questions or their answers).
- **Show correct answers** — *never*, *after submit*, or *after the quiz closes*.

### Questions

Assemble the quiz content in one unified table:

- **Add fixed questions** from a bank (with per-question **points override** and drag-to-reorder).
- **Add a random draw** — pick *N* random questions from a bank, each worth the same points.
- **Add an AI-generated section** — a per-student set (see below).
- **Preview as a student** to see exactly what a student will experience, including simulated
  random draws and a "show correct answers" toggle.

## Per-student AI-generated questions

An AI section generates a **different set of questions for each student**, grounded in that
student's own work. A common use is a **post-submission "understanding check"**: after a student
submits, generate questions from *their* code to confirm they understand what they turned in.

- You write a **prompt** using template variables such as `{submission_files}` and
  `{submission_test_results}`. The generator sees **only** the material your variables reference.
- **Start from a template.** When you add a section, a **"Start from a template"** picker offers
  ready-made starter prompts you can then edit:
  - **Retasking** — quote the student's own code and ask them to rewrite it for different inputs or
    parameters (proves they understand it, not just that it runs).
  - **Manual evaluation** — give a small sample of data and ask them to hand-compute what their own
    code would return. A **Sample size** control sets how many rows to include.
  - **Understanding check** — a mix of retasking and manual-evaluation questions.
  - **Explain your code** — quote a block of their code and ask them to explain what it does and why.
- **Reference files in your prompt.** Variables like `{assignment_file:spec.pdf}` and
  `{course_file:rubric.pdf}` insert file contents into the prompt. **PDFs are converted to text and
  notebooks (`.ipynb`) to readable cells automatically** — you never have to paste raw content. If
  the assignment uses [per-student dataset variants](/docs/instructor-environment-testing),
  `{student_dataset}` inserts the specific data assigned to that student, so questions can reference
  their actual numbers.
- **Images.** `{assignment_files}` includes text files only — images are skipped, with a note in the
  resolved prompt. Use `{assignment_files_with_images}` to also attach images (PNG, JPEG, GIF, WebP)
  for the model to look at, or name one directly with `{assignment_file:plot.png}` or
  `{course_file:diagram.png}`. Each image adds to the prompt's token cost, and at most 10 are attached
  per prompt. With a self-hosted Ollama model, only a vision model (e.g. `llava`, `gemma3`) can see
  them.
- **Unchanged files aren't sent twice.** If the same content appears more than once in a prompt — for
  example a starter file or spec PDF in `{assignment_files}` that the student submitted untouched in
  `{submission_files}` — it is included once, and later occurrences are replaced with a short
  "identical to … above" note. This keeps the model's context window for what actually differs.
- **Generation is manual by default** (Settings → AI-generated questions → **Generate question
  sets manually**): you create sets from the Review tab — **Generate missing** (the count of
  affected students is shown first, since generation costs AI tokens) or per student. You can also
  set a **scheduled generation time**: a one-time run that generates sets for every student who has
  a submission but no set yet — for example, right after the assignment deadline, so each set
  reflects the student's final submission. Students who submit after the run appear in the
  **Generate missing** count, and moving the time later re-runs it for newly missing students.
- Turning the manual toggle **off** makes questions generate automatically whenever a student
  submits — convenient for self-paced quizzes, but note that every resubmission regenerates the
  student's set (until it's approved), spending AI tokens each time.
- On the **Review** tab, review each student's set: edit questions inline, **regenerate**, or
  **approve**. A student's quiz only opens for them once their set is approved — until then they see
  a neutral **"Your quiz is being prepared."**
- **Answer keys.** Each generated question carries a grader-only **answer key** (the correct
  answer/working code, plus worked steps for hand-computation questions). It's shown — and is
  editable — on the Review tab and again while grading. **Students never see it.**
- **Auto-publish generated sets** skips the manual review gate. **Graders can review generated**
  lets your quiz graders (not just admins) review these sets, and the nested **Graders may also
  generate missing question sets** additionally lets them run **Generate missing** — so a grader
  can generate, review, and release the quiz for their section without an admin. Per-student
  generate/regenerate stays admin-only.

> [!IMPORTANT]
> Per-student AI generation requires the course's **AI-generated quiz questions** feature, which is
> **off by default** because it spends AI tokens on every submission. When it's off, the builder
> hides the "Add AI questions" option.

## Safe Exam Browser lockdown

For high-stakes quizzes, turn on **Require Safe Exam Browser** (Settings → Exam security).
[Safe Exam Browser](https://safeexambrowser.org) (SEB) is a free locked-down browser for
**Windows, macOS, and iPad** that blocks other apps, copy/paste, and navigation away from
the quiz while it runs.

![The Exam security section of the quiz settings](/assets/docs/instructor_quiz_seb_settings.png)

With the requirement on, the server rejects every quiz interaction — starting, reading an
in-progress attempt, answering, submitting — that doesn't come from inside SEB. A student
who opens the quiz in a normal browser sees an explanation screen instead of the quiz:

- **Launch in Safe Exam Browser** — one click generates a personal exam configuration and
  opens it in the student's installed SEB, which lands directly back on the quiz, already
  signed in. There is nothing for you to configure or distribute.
- **Download Safe Exam Browser** — for students who don't have SEB installed yet.

When the student quits SEB they're returned to their normal browser session.

A few operational notes:

- **Exemptions** — SEB doesn't run on Linux or ChromeOS. Exempt individual students in
  **Roster → Manage Students** (the **SEB exempt** checkbox); they take the quiz in a
  normal browser. Exemptions are course-wide, like time accommodations.
- **Verification flag** — each attempt records whether it was actually taken under SEB.
  Attempts by exempt students (or started before the requirement was switched on) show as
  not verified to staff.
- **Blocked attempts** are recorded in the [Activity Log](/docs/activity-log), so you can
  see who tried to open a locked-down quiz outside SEB.
- **Custom configurations** — if your department distributes its own `.seb` file instead
  of the built-in launch, paste its **Config Key** (from the SEB Config Tool) into the
  quiz's Exam security settings; the built-in launch keeps working alongside it.

> [!IMPORTANT]
> Turning the requirement on mid-quiz blocks in-progress attempts until students re-enter
> through SEB — their saved answers are kept. Prefer setting it before the quiz opens.

## Accommodations

To give a specific student extra time on **every** timed quiz in the course, set their **Quiz time**
multiplier in the roster (Roster → Manage Students). A multiplier of 1× means no accommodation.
The roster is also where you mark a student **SEB exempt** for
[locked-down quizzes](#safe-exam-browser-lockdown).

## Grade quizzes

Auto-graded questions (multiple choice, true/false, short answer, numerical) are scored the moment a
student submits. **Essay** and **Code** questions need a human — grade them on the quiz builder's
**Grading** tab:

1. Open the **Grading** tab (its label shows a count of responses awaiting grading).
2. If the course has sections, use the **section filter** to narrow the queue to your own
   students. Your choice is remembered per course, so you come back to the same section next time.
3. Pick an attempt to open the **focused grader** — a full-height workspace showing one response
   at a time.
4. Enter **points** and optional **feedback** for the student, then press **Next** (**Finish** on
   the last one) or `⌘/Ctrl + Enter` to save and advance.
5. **Reopen** undoes a saved grade and sends it back to the queue (feedback is kept as a draft).

![The focused quiz grader, showing one response with the points and feedback panel](/assets/docs/instructor_quiz_focused_grader.png)

Inside the focused grader:

- The header carries the student, attempt number, running score, and your position in the queue
  ("Response 3 of 12 to grade").
- The numbered **chips** under the header jump to any question in the same attempt — useful for
  context before you score. A dot means it still needs grading, a check means it is done.
- **Grade below** / **Grade beside** moves the points-and-feedback panel under the question or next
  to it. The choice and the panel's size are remembered for you across quizzes.
- **Use as official grade** pins this attempt as the student's official one, overriding the quiz's
  scoring policy (highest, latest, and so on). **Unpin official grade** hands it back to the policy.
  A student can have at most one pinned attempt per quiz.
- **Skip** moves on without grading.
- **Queue** returns to the attempt list.

> [!IMPORTANT]
> **Your draft is saved when you navigate.** Moving to another response — Next, a chip, the queue,
> or closing the grader — saves a grade you have typed but not yet submitted. If the save fails you
> stay on the response so nothing is lost. The one case that cannot be saved is feedback with no
> valid points: that draft is discarded with a warning, so enter points to record the grade.

> [!TIP]
> **Run a student's code.** On a **Code** response, use **Run code** to execute the student's answer
> in the course's sandbox and see its output (stdout, errors, and any plots) right in the grading
> view. The run uses the attached assignment's environment and stages the student's submission files
> and datasets — including their assigned dataset variant — so answers that reference "their" data
> work. This is grader-only and never shown to the student.

> [!NOTE]
> **Who can grade quizzes.** By default **every grader on the course can**, alongside course admins.
> To restrict it, turn off **Graders Can Grade Quizzes** in Course Settings → Behavior; a **Quiz
> Grader** column then appears in Roster → Graders so you can grant quiz grading one grader at a
> time. Course admins can always grade quizzes. See the [Grader Guide](/docs/grader).

## Track grading progress

**Quizzes → Grading Progress** shows where a grading push stands across the whole course, without
opening each quiz. Rows are graders; there is one column per published quiz that has manually
graded (essay/code) responses.

![The quiz grading progress page, one row per grader and one column per quiz](/assets/docs/instructor_quiz_grading_progress.png)

- A banner reports how many responses are still awaiting grading across all quizzes.
- **Responses Graded** and **% of All Grading** show each grader's share of the work, and **Last
  Graded** how recently they worked.
- Each quiz column shows that grader's `graded / total` with a progress bar.
- Graders who have not started yet appear with zeros, and graders who have since left the course
  keep their rows — the page is a record of who graded what, not a roster.

## Review results

- **Results** — a per-student table of official scores and pass/fail, exportable to **CSV**.
- **Item analysis** — per-question statistics (average score, how often each choice was picked, how
  many responses are still pending) to spot confusing questions.
- **Reset attempts** — deletes **all** attempts for the quiz. **Course admins only.**

Quiz activity is recorded in the course [Activity Log](/docs/activity-log): attempt starts
(including **late starts** via access code), submissions and auto-submissions, grading actions,
access-code changes, and generated-set approvals — useful when investigating a dispute about
timing or access.

## Related docs

- [Assignment Workflow](/docs/instructor-assignment-workflow)
- [Grading, Release & Exports](/docs/instructor-grading-publishing)
- [AI Settings & Usage](/docs/ai-guide)
- [Student Guide](/docs/student)
- [Grader Guide](/docs/grader)
