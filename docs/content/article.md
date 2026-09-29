<!--
BEFORE PUBLISHING (delete this block):
1. Everything below comes from real runs on 28–29 Sept (Groq gpt-oss-120b + Hindsight Cloud), including the final
   scores (11/12 vs 2/12). If you re-run the scoring, update those numbers to match.
2. Rewrite the opening and at least one lesson in your own words — every team member must publish their own article.
3. Publish on Medium, Dev.to, Hashnode, Substack or LinkedIn Articles (public link). Never share via Google Drive.
4. Do NOT mention the event or competition anywhere (title, body, tags) — it disqualifies the content.
5. Add 3–4 PNG screenshots, tightly cropped: the Compare view, the "caught by guard" card, the timeline, the scores.
Length: ~1,550 words including code.
-->

# Hindsight taught my on-call agent which fixes backfire

At 11:06 on a Monday, checkout starts failing. The alert says `HikariPool-1 - Connection is not available, request timed
out after 30000ms`. I gave that alert to `gpt-oss-120b` with no history. Its first recommendation: raise the connection
pool from 40 to 80 per pod and restart the pods.

At this company, that is how you cause an outage. Five months earlier, a restart bought four minutes before the pool
exhausted again, and scaling out blew through the database's 500-connection limit and took payments down with it.
Twelve pods at 80 connections is 960. It's all in a postmortem that nobody reads at 11:06 with a SEV1 going off.

So I built **NeverTwice**, an on-call copilot that reads the postmortems for you, using
[Hindsight](https://github.com/vectorize-io/hindsight) as its memory. This post is about the design decision that mattered
most: remembering *what didn't work* as carefully as what did — and then making memory enforce it.

## The setup

NeverTwice triages alerts for BasketBolt, a fictional 10-minute grocery delivery company in Hyderabad. I wrote six months of
history for it: 15 postmortems across checkout (Spring Boot + HikariCP), payments (UPI through two payment providers),
Kafka consumers, Redis, OpenSearch, SMS OTPs and Kubernetes. Each has real-shaped logs, a timeline, a root cause, open
action items and every remediation attempt with its outcome: *worked*, *didn't work*, *made it worse*.

The stack is small: Python/Starlette, a vanilla JS UI, Groq's `gpt-oss-120b`, and Hindsight for everything the agent
knows. Hindsight extracts facts and entities from what you `retain`, searches them four ways at once on `recall`
(semantic, keyword, entity graph, time), consolidates repeated facts into observations, and answers questions with
`reflect`. Loading the 63 memory items took about a minute of extraction.

## Decision 1: store outcomes as separate, tagged memories

If a postmortem is retained as one document, the restart that failed and the fix that worked sit in the same blob, and
"restart" is such a natural first step that a model can hand it straight back. So each incident is split by outcome:

```python
items.append({
    "content": "\n".join(lines),   # "At 11:15 IST the team tried: Rolling restart ... Outcome: DID NOT WORK. ..."
    "context": f"{iid} remediation — {OUTCOME_LABEL[outcome]} ({svc})",
    "timestamp": ts,
    "document_id": f"{iid}:remediation:{outcome}",
    "tags": base_tags + ["kind:remediation", f"outcome:{outcome}"],
    "metadata": {"incident_id": iid, "service": svc, "kind": "remediation", "outcome": outcome},
})
```

For every alert, NeverTwice runs four recalls in parallel — similar incidents, fixes that worked, fixes that failed or
backfired, and on-call rules — and the outcome recalls are scoped by tag:

```python
("failed", "Recalling fixes that failed",
 dict(query=f"remediation that failed or made things worse for {symptom}", budget="low", max_tokens=900,
      tags=["outcome:failed", "outcome:made_worse"], tags_match="any", types=["world", "experience"])),
```

Results are numbered `M1..Mn`, and the model must cite them or incident IDs as evidence.

## Before and after

Same model, same prompt, same alert; the only difference is memory.

**Without memory**, the model recommended raising the HikariCP pool to 80, restarting the pods, and said it had "seen this
before" in **INC-2025** — an incident that doesn't exist. NeverTwice checks every cited ID against what recall actually
returned, so that chip shows up red as unverified.

**With Hindsight**, it recalled 28 memories and cited four real incidents (INC-2104, INC-2291, INC-2317, INC-2282). Its
first step was a verification query, not a mitigation. It said to cut the new reconcile workers back and terminate
idle-in-transaction sessions, and under *Don't do this* it listed scaling checkout to 20 replicas (INC-2104) and changing
the database instance mid-incident (INC-2282).

[Screenshot: the Compare view]

## The dead end: memory that informs isn't enough

Then I read the memory answer closely. Step 4 was: *"If latency remains high, perform a rolling restart of checkout-api
pods."* Memory had recalled that restarts relapsed in two incidents. The model even cited INC-2317 as support — but the
fix there was a rollback, not a restart. Recall worked; the model still ignored it.

Prompting harder helps a little. What fixed it was treating memory as a check, not just context. After the model
answers, a **backfire guard** looks at every risky step — restarts, scaling out, raising timeouts, database limit
changes — and asks Hindsight directly whether that kind of fix failed before for the same service:

```python
query = f"{name} of {service} did not work or made things worse"
for group in ("failed", "worked"):
    for raw in await self.memory.search(query, group):
        ...
moved = backfire_guard(report, MemoryContext(items=ctx.items + extra), service)
```

If failures outnumber successes, the step moves to *Don't do this* with the proof and a "caught by guard" badge. The
"outnumber" rule matters: in one incident a restart *was* the right fix (a rotated secret is only read at startup), so
the guard can't be a blanket ban.

[Screenshot: the "caught by guard" card]

## Decision 2: let it learn in front of you

One alert is a failure BasketBolt has never seen: Telugu and Hindi searches return nothing because new OpenSearch nodes
were built without the `analysis-icu` plugin. NeverTwice has nothing relevant to recall and says so, with low confidence.

The responder clicks **Resolve & teach**, records what worked, what didn't, and whether NeverTwice's advice was right.
That becomes new memories — including an experience fact about NeverTwice's own advice. When the same error hits a
new node five days later, it recalls the incident it was just taught and gives the exact fix.

Hindsight also maintains four **mental models** that act as living runbooks — *Recurring failure patterns*, *Fixes that
backfired*, *Open action items*, *On-call rules* — refreshed automatically after consolidation. Three **directives** keep
`reflect` honest: cite incident IDs, flag destructive actions, separate what worked from what backfired.

## Measuring it

I didn't want "it feels smarter", so each alert has a checklist: steps it must recommend, known-bad steps it must not,
incidents it should cite. A script triages each alert three times with and without memory and grades every answer.

On the payments alert, memory passed 3/3. Without memory, the model recommended raising the payment-provider timeout in
3/3 runs — the exact change that turned a degraded provider into an outage in INC-2150. On the checkout alert, the
no-memory answer recommended a bigger connection pool 3/3 times.

Across all four alerts, three runs each:

| | Passed the checklist | Recommended a fix that already failed |
| --- | --- | --- |
| With Hindsight memory | **11 / 12** | 1 / 12 |
| Without memory | 2 / 12 | 10 / 12 |

Without memory, the model's instincts were consistent and wrong: bigger connection pools, longer timeouts, more
replicas, reset the consumer offsets. It never once found the dead-letter-queue fix for the crash-looping Kafka
consumers, because that fix only exists in this team's history.

The one memory miss is the interesting one. On the DNS alert, it suggested scaling CoreDNS "if the DaemonSet is already
present". Memory holds two answers to that: scaling CoreDNS fixed INC-2160 and failed in INC-2305, where the real cause
was a missing node-local DNS cache. With one success and one failure, the guard correctly doesn't veto it; the model
has to read *why* each one happened. That is where I'd push next: remembering the conditions under which a fix works,
not just its outcome.

My first checklist was wrong, too: it failed two good answers because "scale the reconcile workers down to 4" matched my
"no scaling" rule. Grading your grader is part of the job.

## What didn't work, and what I'd change

- **Tool calling breaks in new ways.** On the first live run, `gpt-oss-120b` called a tool named `json` that didn't exist,
  and Groq rejected the call. The model's arguments were fine, just under the wrong name, so NeverTwice now salvages
  them from the failed generation instead of paying for another call.
- **Free-tier limits shaped the design.** Groq's free tier allows 8K tokens per minute on `gpt-oss-120b`, and a
  side-by-side comparison makes two calls. Four small tag-scoped recalls plus runbook excerpts keep a prompt around 3K tokens.
- **Keyword grading is crude.** It undercounts answers phrased differently; an LLM judge would be fairer. I kept keywords
  because they're deterministic and I can read exactly why an answer failed.
- **The data is synthetic.** The next step is importing real postmortems with the same schema.

## Try it

The code, data and scripts are on GitHub: https://github.com/SomeswararaoTellakula/nevertwice. You need a Hindsight Cloud
key (or a local Hindsight container) and a Groq key; `./start.sh` sets everything up and starts the app.

The most valuable thing it did wasn't finding the right fix. It was stopping the wrong one.

**Links:** [Hindsight on GitHub](https://github.com/vectorize-io/hindsight) · [Hindsight docs](https://hindsight.vectorize.io/)
· [What is agent memory?](https://vectorize.io/what-is-agent-memory)
