# NeverTwice demo script (≈3 minutes)

**Before you present**

1. `python scripts/check_setup.py` → all green.
2. `python scripts/seed_memory.py --reset` → fresh bank, learning demo not yet taught.
3. `python scripts/eval_alerts.py --baseline --runs 3` once (≈20 min) so the Learning tab shows the *Answer quality*
   scores. (Optional, if you have time: `python scripts/eval_learning_curve.py --baseline` for the replay curve.)
4. `python run.py`, open <http://127.0.0.1:8000>. Rehearse with <http://127.0.0.1:8000/?notes=1> (shows presenter notes).
5. Warm up once: run **Compare** on ALT-501 so the first live call isn't the first call of the day. Triage never
   changes memory (only **Resolve** does), so no reset is needed afterwards.
6. Zoom the browser to 110–125% so judges can read the chips.

---

### 0:00 – 0:25 · The problem

> "Most production outages are repeats. The fix already exists, in a postmortem from three months ago, in the head of
> someone who's asleep. We built NeverTwice, an on-call copilot for a quick-commerce company, that remembers every incident
> using Hindsight agent memory — including the fixes that made things worse."

Show the alert list. Click **ALT-501 — checkout-api 5xx 13.7%**.

### 0:25 – 1:15 · Before / after on the same alert

Click **Compare with no memory**.

- **Left (no memory):** "Same model, same prompt, no history. It says restart the pods and scale out — reasonable, and
  exactly what hurt this team twice."
- **Right (Hindsight memory):** point at the four recall steps ("fixes that worked", "fixes that failed" are tag-scoped
  recalls). Then the report: INC-2104 and INC-2291 cited, **Don't do this** lists the restart and the scale-out with the
  incidents that prove it, and it spots that today's deploy moved reconcile to a DB role without the timeout fix.
- **Impact strips:** point at the numbers under each lane — *0 memories · 0 incidents · 0 fixes flagged* on the left
  versus the memory lane. "That's the difference memory makes, in one line."
- **Guard:** if a card says **caught by guard**, say: "the model still suggested this; memory vetoed it."
- **Timeline:** "Watch the sweep — it looked back six months, and these amber dots are the incidents it cited."
- Click the **INC-2104** chip → the drawer shows the original postmortem. "Every claim is traceable. If the model invents an
  incident ID, it shows up red."

### 1:15 – 2:20 · Watch it learn

Click **ALT-505 — search returns zero results for Telugu and Hindi**. Click **Triage with memory**.

> "This failure has never happened here. Memory has nothing matching, so confidence is low and advice is generic.
> That's honest."

Click **Resolve & teach NeverTwice** → **Use the suggested resolution** → mark the first recommendation *no* → **Save to memory**.

> "This is retained into Hindsight: the root cause, the fix that worked, the restart that didn't, and a note that
> NeverTwice's own suggestion was wrong. You can see Hindsight extracting the facts."

Click **Now triage ALT-506** (same failure five days later on a new node).

> "Same failure mode, different node, different index. Now it recalls the incident we just taught it, gives the exact fix
> — exclude the node, check `_cat/plugins` — and warns that restarts don't help."

Point at the new dashed dot on the timeline.

### 2:20 – 2:45 · Memory that maintains itself

Open **Runbooks & memory** → *Fixes that backfired*.

> "These runbooks are Hindsight mental models. Nobody writes them: they're regenerated after new incidents are
> consolidated. Directives keep reflect honest — cite incident IDs, flag destructive actions."

Optional: **Ask memory** → "Which fixes have backfired on us, and why?"

### 2:45 – 3:00 · Proof and close

Open **Learning** → *Answer quality*.

> "Every alert graded three times: with memory 11 of 12 passed, without memory 2 of 12. Without memory it
> recommended a fix that had already failed or caused an outage in 10 of 12 answers."

> "NeverTwice: your on-call rotation's memory, so the same outage never costs you twice."

---

### If something goes wrong live

| Symptom | What to say / do |
| --- | --- |
| "Rate limited … retrying" warning | Normal on the free tier; NeverTwice waits or switches to the fallback model automatically. |
| A recall step shows `!` | That recall failed; the others still run. Mention graceful degradation. |
| Resolve is slow | Hindsight is extracting facts with an LLM; it usually takes 5–30 s. Talk over the progress steps. |
| Hindsight banner error | Check `.env`, then `python scripts/check_setup.py`. |
