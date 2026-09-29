---
key: mcp-agents
path: mcp-agents
title: AI Agents (MCP)
category: Reference
order: 22
---

# AI Agents (MCP)

codePost exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server so you can connect an AI assistant — Claude Code, Claude Desktop, or any MCP-capable client — directly to your course. Once connected, the assistant can answer questions about grading progress, rosters, and analytics, and (if you allow it) manage assignments, rubrics, quizzes, and sections on your behalf.

The endpoint is:

```
https://codepost-api.cs.rutgers.edu/mcp
```

It speaks MCP Streamable HTTP in stateless mode — every request is a plain POST, so it works from any standard MCP client.

## Credentials

Two kinds of credentials can connect. Both are sent as an `Authorization` header.

### Course API key (recommended)

Create one under **Course Settings > API Keys** — see the [Course Setup guide](/docs/instructor-course-setup) for the full walkthrough of creating, scoping, and revoking keys.

```
Authorization: CourseKey cpk_<course_id>_<secret>
```

A course key connects **pinned** to the course that issued it: the assistant sees only that course, no tool ever asks for a course id, and the key cannot reach anything else. This is the safest way to hand an agent access, because revoking the key cuts it off completely.

![The API Keys tab in Course Settings](/assets/docs/instructor_api_keys.png)

### Personal API token

Your personal token (the same one the Python SDK uses, from **Settings** in the dashboard):

```
Authorization: Token <your_token>
```

A personal token connects **unpinned**: the assistant gets an extra `codepost_list_courses` tool, and every course-bound tool takes a `courseId` argument. Each call is checked against your own staff membership in that course — the assistant can only touch courses where you are an instructor or admin.

## Scopes

What the assistant is allowed to do is governed by scope. Tools the credential's scope doesn't cover are **not shown to the assistant at all** — a read-only key never even sees the tools that would change anything.

| Scope | What the assistant can do |
| ----- | ------------------------- |
| **Read** | Answer questions: course overview, roster, grading progress, gradebook, analytics, submissions, quiz status, activity log. |
| **Write** | Also create and edit assignments, rubrics, files, test cases, and quizzes; manage sections and the roster; move assignments through their lifecycle; release feedback; run the autograder. |
| **Admin** | Also the most destructive actions, such as removing people from the roster. |

A course API key carries the scope you chose when creating it. A personal token connects at full scope (your own permissions still bound every call). Either way, you can **narrow** a connection by adding `?scope=read` or `?scope=write` to the endpoint URL — narrowing only; a read key asking for more stays read:

```
https://codepost-api.cs.rutgers.edu/mcp?scope=read
```

Connecting read-only is a good default when you mainly want the assistant to answer questions.

## Connecting from Claude Code

```bash
claude mcp add --transport http codepost \
  https://codepost-api.cs.rutgers.edu/mcp \
  --header "Authorization: CourseKey cpk_<course_id>_<secret>"
```

Then ask things like *"How far along is grading on Assignment 3?"* or *"Which students haven't submitted the current quiz?"*. Other MCP clients work the same way: point them at the endpoint URL with the `Authorization` header set.

## What the assistant can do

All tools are prefixed `codepost_`. The main groups:

| Area | Read | Write |
| ---- | ---- | ----- |
| **Orientation** | Course overview, roster lookup, a composed to-do view (deadlines, grading debt, pending regrades) | — |
| **Assignments** | Assignment detail, rubric, grading progress, per-assignment analytics | Create, update, clone; move through the draft → published lifecycle; release or retract feedback; edit the rubric |
| **Submissions & grading** | List and inspect submissions, gradebook, audit log | Assign graders, finalize/unfinalize, manage regrade requests |
| **Course content** | — | Manage assignment files, test cases; trigger autograder runs |
| **Quizzes** | Quiz status | Create and update quizzes, edit questions, per-student accommodations |
| **People** | — | Roster changes, section management |

Long-running work (autograder runs, bulk operations) returns a job id the assistant polls with `codepost_poll_job` rather than blocking.

## Safety model

- **Your permissions still apply.** Every tool call is executed through the same permission checks as the regular app and API — a connected assistant can never do something the credential's owner couldn't do themselves.
- **Previews before risky changes.** Bulk or destructive write tools run as a preview first: the assistant sees exactly what would change and must explicitly confirm — with a short-lived token bound to that exact plan — before anything is applied. If the underlying data changes between preview and confirm, the confirmation is rejected and the assistant must re-preview.
- **New assignments land hidden.** Anything the assistant creates starts as a hidden draft — students see nothing until you (or the assistant, with write scope, at your instruction) publish it.
- **Feature gates apply.** Tools for features disabled in your course (for example quizzes) are hidden from the assistant.
- **Rate limits.** Tool calls are rate-limited per course, so one runaway agent loop can't degrade the platform or other courses.
- **Student privacy.** The assistant never sees a student's email address, NetID or username. Every student is shown as a stable, course-specific alias such as `student-3f9a1c2d40` — in rosters, submission lists, the gradebook, quiz results, the activity log, and even inside free text like regrade requests. You can hand those aliases back to the assistant ("what did student-3f9a1c2d40 submit?"), and resolve one to a real student under **Course Settings > Student Aliases**. That lookup only works for a signed-in course admin; the assistant's own credential is refused, so it can never de-anonymize its own output. Staff (graders, admins) still appear by email.

> [!WARNING]
> A connected assistant with a read key can see grades and submission metadata (students appear as aliases, never as emails). Treat the key like a password: don't paste it into shared configuration, prefer one key per integration, and revoke keys you no longer use under **Course Settings > API Keys**.

## Troubleshooting

- **401 Unauthorized** — the header is malformed or the key was revoked/disabled. Recreate the key and reconnect.
- **The assistant says a tool doesn't exist** — the credential's scope (or a `?scope=` narrowing) hides it. Reconnect with a broader key if you actually want writes.
- **"course … not in scope"** — a course key was asked about a different course. Course keys are pinned; use a personal token for multi-course work.
