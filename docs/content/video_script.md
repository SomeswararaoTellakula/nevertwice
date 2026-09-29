# Video script (target 3:30, max 5:00)

Record at 1080p or higher. Browser zoom 125%, terminal font enlarged. Talking head in a corner is preferred; narration
only is acceptable. Speak conversationally — this is a guide, not a teleprompter. Upload to YouTube as **public** (never
Google Drive) with a thumbnail made in Google Nano Banana (prompt in `thumbnail_prompt.md`).
Do not mention the event or competition in the video, title or description.

---

## 0:00–0:30 · Intro (who, what, why)

**On screen:** you, then the NeverTwice alert list.

> "Hi, I'm [name]. Most production outages are repeats — the fix is sitting in a postmortem from months ago, and the
> person who knows it is asleep. So I built NeverTwice: an on-call copilot that remembers every incident using Hindsight agent
> memory, including the fixes that made things worse."

## 0:30–1:00 · The problem: an agent without memory

**On screen:** select ALT-501 (checkout-api 5xx, HikariCP timeouts). Click **No-memory only**.

> "Here's a real-looking page: checkout is failing with connection pool timeouts. Without memory, the model does what
> anyone would: restart the pods, scale out, raise the pool size. Reasonable — and at this company, both of those have
> already caused outages."

## 1:00–3:00 · Live demo: retain and recall, before and after

**1:00 — Compare.** Click **Compare with no memory**.

> "Same model, same prompt, now with Hindsight. Watch the steps: four recalls — similar incidents, fixes that worked,
> fixes that failed, and on-call rules. The outcome recalls are scoped with tags, because I store every fix separately as
> worked, failed or made-it-worse."

Point at: cited incidents, the **Don't do this** list, the timeline dots.

> "Without memory it says raise the pool to 80 and restart — and look, it cites INC-2025, which doesn't exist, so it's
> shown in red. With memory it cites real incidents, and under *Don't do this* it lists the fixes that caused outages
> before. Every ID is clickable —" *(click INC-2104)* "— this is the postmortem it came from, five months back."

If the run shows a **caught by guard** badge, point at it:

> "Here the model still suggested a restart. The backfire guard asked Hindsight whether restarts had failed before for
> checkout-api — they had, twice — so it moved the step out of the plan. Memory doesn't just inform the answer; it vetoes
> known-bad steps."

**On screen, briefly:** `nevertwice/memory.py`, the `plans` list with `tags=["outcome:worked"]` — "this is the recall plan."

**1:50 — Learning.** Click ALT-505 (Telugu/Hindi search returns nothing) → **Triage with memory**.

> "This one has never happened. No relevant memory, low confidence, generic advice. Now the incident gets fixed, and I
> teach it." *(Resolve & teach → Use suggested resolution → mark 'no' → Save to memory)* "That's a Hindsight retain:
> root cause, what worked, the restart that didn't, and feedback that NeverTwice's own suggestion was wrong."

**2:30 —** Click **Now triage ALT-506**.

> "Five days later, a new node, same failure. Now it recalls the incident I just taught it and gives the exact fix, and
> warns that restarts don't help. That's the before and after."

**2:50 — Runbooks.** Open **Runbooks & memory** → *Fixes that backfired*.

> "These runbooks are Hindsight mental models — they rewrite themselves after new incidents are consolidated."

## 3:00–3:30 · Key takeaway (what surprised me)

**On screen:** the **Learning** tab → *Answer quality* card.

> "I graded every alert three times. With memory it passed 11 of 12; without memory, 2 of 12 — and without memory it
> recommended a fix that had already failed or caused an outage in 10 of 12 answers. On payments it said 'raise the
> timeout' every single time. What surprised me most: recall alone wasn't enough. The model still slipped in a fix memory said had
> failed, until I made memory check the plan. The code's on GitHub — link below. Thanks for watching."

---

**YouTube description template**

NeverTwice is an on-call incident copilot that remembers every past incident — what fixed it and what made it worse —
using Hindsight agent memory.
Code: https://github.com/SomeswararaoTellakula/nevertwice
Hindsight: https://github.com/vectorize-io/hindsight · Docs: https://hindsight.vectorize.io/
