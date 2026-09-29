# Submission form — ready-to-paste answers

Replace anything in [brackets]. Keep links public and test each one in an incognito window before submitting.

**Project name:** NeverTwice

**One-line description:**
An on-call incident copilot that remembers every past incident — what fixed it and what made it worse — using Hindsight
agent memory, so the same outage never costs a team twice.

**Problem (2–3 sentences):**
Most production outages are repeats, but the knowledge to fix them is buried in postmortems nobody reads during a SEV1.
Engineers — and stateless AI assistants — repeat fixes that already failed: in our tests, an LLM without memory
recommended raising a payment-provider timeout in 3 of 3 runs, the exact change that turned a provider hiccup into an
outage in a past incident.

**How Hindsight memory is used:**
Every postmortem is retained into a Hindsight bank split by outcome — fixes that worked, failed, or made things worse —
with tags, timestamps and incident metadata. For each live alert, NeverTwice runs four tag-scoped recalls in parallel
(similar incidents, fixes that worked, fixes that backfired, on-call rules) and gives the model numbered memories it must
cite. After the model answers, a backfire guard queries Hindsight again for each risky step and moves any fix that failed
before for that service into "Don't do this". Four mental models act as living runbooks that refresh after
consolidation, three directives keep reflect answers grounded, and "Ask memory" uses reflect. When an incident is
resolved in the app, the fix, the failed attempts and feedback on NeverTwice's own advice are retained, so the next similar
alert is answered from experience — shown live in the demo (ALT-505 taught, ALT-506 recalled).

**Results:**
With memory 11/12 answers passed vs 2/12 without memory; a fix that had already failed or caused an outage was
recommended in 1/12 answers with memory vs 10/12 without (4 alerts × 3 runs, 29 Sept) — graded by
`scripts/eval_alerts.py` against per-alert checklists, gpt-oss-120b on Groq, real Hindsight Cloud bank.

**Tech stack:** Python (Starlette, httpx, Pydantic), vanilla JS UI, Hindsight Cloud, Groq (openai/gpt-oss-120b with
gpt-oss-20b fallback), 72 offline tests in GitHub Actions.

**GitHub:** https://github.com/SomeswararaoTellakula/nevertwice
**Demo video (YouTube, public):** [link]
**Articles (one per member):** [links]
**Social posts (one per member):** [links]

## Final checklist

- [ ] `python scripts/check_secrets.py` says "safe to push" (use `--fix` to blank keys in `.env.example`)
- [ ] Repo is public; `.env` is not in it (`git ls-files | grep -x .env` prints nothing)
- [ ] GitHub Actions badge is green
- [ ] One real screenshot at the top of the README
- [ ] Video: public on YouTube, 2–5 min, thumbnail, no Drive links
- [ ] Every member: article + LinkedIn post (+ first comment with the Hindsight GitHub link)
- [ ] No mention of the event/competition in any title, body or hashtag
- [ ] Every member filled the Profile Review Form
- [ ] One final submission only
