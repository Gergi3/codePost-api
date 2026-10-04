---
key: instructor-mcp-agent
path: instructor-mcp-agent
title: AI Agents (MCP)
category: Instructor Workflows
order: 13
---

# Managing your course with an AI agent

codePost has a built-in [MCP](https://modelcontextprotocol.io) server, so you can connect
an AI assistant — Claude Code, Claude Desktop, or any MCP client — to your course and ask
for things in plain language:

- *"Who hasn't submitted HW3, and are they all in the same section?"*
- *"Set up HW4 like HW3 but due two weeks later, and write starter code for it."*
- *"Create a 5-question quiz on recursion attached to HW3, available after students submit."*
- *"Give this student time-and-a-half on quizzes."*
- *"What needs my attention this week?"*

The agent works through the same permission system as everything else in codePost: it can
never see another course, and it can never do more than the credential you hand it allows.

## Connecting

**1. Create an API key.** In *Course Settings → Course API Keys*, create a key and choose
its scope:

| Scope | What the agent can do |
| --- | --- |
| `read` *(default)* | Look things up: roster, grading progress, gradebook, submissions, quizzes, analytics. Nothing else. |
| `write` | Everything in `read`, plus course setup: create and edit assignments and quizzes, author autograder tests, upload starter files, manage the rubric, sections, and grading assignments, publish work and release feedback (each with a preview step). |
| `admin` | Everything in `write`, plus the destructive operations — deleting things, resetting quiz attempts, removing roster members, emailing students. **Every one of these also requires your explicit approval** (see below). |

The key is shown once — copy it immediately.

**2. Connect your client.**

**Claude Desktop / claude.ai (easiest):** add a custom connector pointing at
`https://codepost-api.cs.rutgers.edu/mcp`. Claude opens a codePost sign-in in your
browser (your university single sign-on works), shows you exactly which permissions the
agent is asking for, and connects — no keys to copy. Approving as yourself means the
agent asks which of your courses to work on and can switch mid-conversation. If your
client asks for an OAuth Client ID (Advanced settings), leave it blank — registration is
automatic; a fallback client id is available from your codePost admins if needed.

**Claude Code**, with a course key:

```bash
claude mcp add --transport http codepost https://codepost-api.cs.rutgers.edu/mcp \
  --header "Authorization: CourseKey cpk_..."
```

…or with no header at all — Claude Code will run the same browser sign-in flow as
Desktop. Course keys remain the right choice for headless or scripted use.

You can also connect with your **personal API token** (*Account → Request API Token*)
as an `Authorization: Token …` header — like the OAuth path, the agent then asks which
course to work on.

## How destructive actions are protected

Anything the agent cannot undo — deleting an assignment, resetting quiz attempts,
emailing every student — needs your explicit approval, no matter the key scope.

**In Claude Code and Claude Desktop**, your client pops a native approval dialog right in
the chat showing exactly what the agent wants to do and how much it affects. Click
**Approve** and the operation runs; click **Decline** and the agent is told to stop. The
AI model itself never sees or answers that dialog — only you can.

**On clients that can't show the dialog**, the request appears under *Course Settings →
Pending agent actions* instead (the agent hands you a direct link). Review the plan there
and press **Approve** — the agent proceeds on its next attempt — or **Deny**, which makes
the agent stop asking. Requests work once, expire after 10 minutes, and die automatically
if the situation changes between the preview and your approval (for example, a TA
finalizes more submissions in the meantime). The agent cannot approve anything itself —
the panel refuses the agent's own credential — so nothing irreversible happens without a
human in the loop.

Less dangerous changes (publishing an assignment, releasing feedback) use a lighter
protection: the agent's first call is always a preview of exactly what students would
gain or lose, and it must call again explicitly to apply.

## Good to know

- Every change an agent makes is recorded in the course **Activity Log**, marked as
  agent-initiated, alongside the normal audit events.
- Revoking the API key (or deactivating it) cuts the agent off immediately. OAuth
  connections are revoked from your AI assistant's connector settings, and their tokens
  expire on their own within an hour of revocation.
- The agent never sees student email addresses. Students appear as stable, course-specific
  aliases such as `student-3f9a1c2d40` (a student has a different alias in each course);
  graders and admins still appear by email. You can pass an alias back to the agent, and
  resolve one to a real student under **Course Settings > Student Aliases** — that page is
  only available to a signed-in course admin, never to the agent's own key.
- The agent can still see grades and submission metadata with a `read`-or-above key — treat
  the key like you would treat your own login.
- Agent traffic is rate-limited per course, so a runaway agent cannot affect the rest of
  codePost.
