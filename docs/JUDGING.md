# Getting NeverTwice into the top 60

Read this before you submit. Nobody can promise a top-60 place — it depends on the other teams' work and the judges —
but you can make sure NeverTwice scores as high as it can on every criterion, and that nothing gets it disqualified.

## 1. The zip alone is not a submission

The problem statement requires **all** of these, and the final form accepts **only one submission per team**:

| Required | Status in this repo | What you must do |
| --- | --- | --- |
| GitHub repository with clean, documented code | Code, README, tests ready | Push to a **public** repo under your account. Never commit `.env`. |
| Demo video | Script in `docs/content/video_script.md` | Record 2–5 min, 1080p, upload to YouTube as **public**, thumbnail from Nano Banana. |
| Live project demo to judges | Script in `docs/DEMO_SCRIPT.md` | Rehearse it twice with real keys. |
| Article, social post and video — **every team member** | Drafts in `docs/content/` | Each member rewrites and publishes their own article and post. |
| Explanation of how Hindsight is used | README → *How Hindsight memory is used* | Paste that section into the submission form. |
| Profile Review Form | – | Every member fills it; the team lead checks. |

Content is disqualified if it mentions the event/competition by name, is shared via Google Drive, or has broken links.
Open every link in an incognito window before you submit.

## 2. How the judges will score it

| Criterion | Weight | What NeverTwice already has | What moves the score further |
| --- | --- | --- | --- |
| Innovation | 30% | Incident response is on the organisers' idea list, so expect several similar teams. NeverTwice's differentiators: **fixes that backfired are first-class memories** (tagged by outcome), **evidence verification** (invented incident IDs shown in red), a **live teach-and-recall loop**, the **look-back timeline**, and a **replay benchmark**. | Lead with the differentiators, not "an incident chatbot". First sentence of the video and form: *"Most outages are repeats; NeverTwice remembers what fixed them — and what made them worse."* |
| Use of Hindsight memory | 25% | retain (async, tags, timestamps, metadata, document_id upserts), 4 tag-scoped recalls, reflect, 4 mental models with `refresh_after_consolidation`, 3 directives, mission + disposition, experience facts, operations polling. | Show the **caught by guard** badge: memory vetoing a bad step is the strongest proof memory is central. In the video, **open the Hindsight Cloud console** and show the bank's facts and mental models after you resolve ALT-505. That proves the memory is real, not a prompt. |
| Technical implementation | 20% | Clean modules, 72 offline tests run in GitHub Actions, one-command `./start.sh`, retries/fallbacks for rate limits, malformed tool calls (including a real `json`-tool bug found live), oversized prompts and Hindsight errors. | Push so the green CI badge shows; add one real screenshot at the top of the README. |
| User experience | 15% | Redesigned console: before/after lanes with an impact strip, look-back sweep, clickable evidence, one-click resolve & teach. | Practise the 3-minute flow until it's smooth. Browser zoom 110–125%. Close other tabs. |
| Real-world impact | 10% | Realistic postmortems, Indian context (UPI PSPs, TRAI DLT, Telugu/Hindi search), on-call conventions. | Say who pays: SRE teams already pay for PagerDuty, incident.io, FireHydrant. Mention the adoption path below. |

### Path to adoption (use this in the pitch)

1. Import existing postmortems (Confluence/Jira/Google Docs) with the same retain schema the seed script uses.
2. Receive alerts from a PagerDuty or Grafana webhook instead of the demo inbox.
3. Post the triage into the incident Slack channel; the **Resolve** step becomes a Slack button.
4. One Hindsight bank per team, tags per service; self-hosted Hindsight for companies that can't send logs out.

## 3. Your plan for the next 24 hours

| When | Task | Time |
| --- | --- | --- |
| First | Keys in `.env` → `python scripts/check_setup.py` → `python scripts/seed_memory.py` → `python run.py` | 30 min |
| | Try every alert once. If an answer looks wrong, note it — it's material for "honest lessons". | 20 min |
| | `python scripts/eval_learning_curve.py --baseline`; copy the real numbers into the README, article and video script | 15 min |
| | Take the six screenshots in `docs/screenshots/README.md`; add one to the top of the README | 15 min |
| | Push to GitHub (public), add topics: `hindsight`, `agent-memory`, `sre`, `incident-response` | 15 min |
| | Record the video (talking head + screen), upload public to YouTube with thumbnail | 1.5–2 h |
| Each member | Personalise and publish the article; publish the LinkedIn post + first comment | ~1 h each |
| Last | Every member's Profile Review Form; then the single final submission form; test every link in incognito | 20 min |

Before the live demo: rehearse twice, keep a backup screen recording in case the venue network fails, and bring a
phone hotspot. If Groq rate-limits you, NeverTwice falls back to `openai/gpt-oss-20b` automatically; a second Groq key in
`.env` is a good spare.

## 4. Questions judges are likely to ask

**How is this different from RAG over postmortems?**
RAG retrieves similar text chunks. Hindsight extracts facts, entities and time from each postmortem, searches four ways
at once (semantic, keyword, entity graph, temporal), consolidates repeated facts into observations, and maintains mental
models that rewrite themselves. NeverTwice also writes back: every resolution — including whether NeverTwice's own advice was
right — becomes new memory.

**Where exactly is memory used in the code?**
`nevertwice/memory.py`: `gather_context` runs the four recalls (two of them tag-scoped to `outcome:worked` and
`outcome:failed/made_worse`), `setup_mental_models` creates the living runbooks, `ask` calls reflect, `retain_and_wait`
stores resolutions. `nevertwice/dataset.py` shows how incidents are split into memories by outcome.

**What if memory is wrong or out of date?**
Three guards: the prompt tells the model to verify when the alert differs from the remembered incident (the dataset's
INC-2317 is exactly that trap); every incident ID in a report is checked against what recall returned; and the bank's
disposition is set to skeptical. Hindsight's observations keep history instead of overwriting it.

**What if the model still suggests a fix that failed before?**
The backfire guard checks every risky step against Hindsight after the model answers and moves known-bad steps to
*Don't do this* with the proof. Show the "caught by guard" badge in the demo.

**How do you know the answers are good?**
`scripts/eval_alerts.py` grades each alert against a checklist (must recommend / must not recommend / must cite) over
several runs, with and without memory. Quote the pass rates.

**Does the model hallucinate incident IDs?**
It can. NeverTwice flags any ID that recall didn't return in red as unverified, and the count appears under the report.

**Is the data real?**
It's synthetic — a fictional company — written in the format of real postmortems so the demo is realistic. Real
postmortems can be imported with the same schema.

**What does it cost to run?**
A triage prompt is about 3K tokens; recall budgets are low/mid. It runs on Groq's free tier and Hindsight Cloud
credits. Self-hosted Hindsight is supported for teams that can't send logs out.

**Does it run commands automatically?**
No. It recommends; a human executes. Destructive steps are marked as needing incident-commander approval by a Hindsight
directive.

**How do you know it gets better?**
The replay benchmark: 15 incidents in date order, each triaged before it's remembered, with and without memory, scored
on whether the real fix appears in the top two actions. Quote your actual numbers.

**What would you build next?**
PagerDuty/Grafana webhook intake, a Slack bot, auto-drafted postmortems from the resolve step, and an LLM-judge
evaluation to replace the keyword metric.

## 5. Make it yours

Judges and recruiters can tell when a team can't explain its own project. Before the live round:

- Each member should be able to walk through `memory.py` and `agent.py` and explain one design decision in their own words.
- Change anything you'd do differently — alert names, the company, a new alert from a failure you've actually seen.
- Rewrite the article and post in your own voice; the drafts are a starting point, not the final text.
