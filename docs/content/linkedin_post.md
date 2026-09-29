<!--
BEFORE POSTING (delete this block):
- Every team member posts their own version; change at least the first line and one takeaway so they aren't identical.
- The link assumes the repo is github.com/SomeswararaoTellakula/nevertwice — change it if yours differs. Length: 798 characters (limit 800).
- Do NOT mention the event or competition, and no student-related tags.
- Make sure both quoted outputs (without / with memory) match what YOUR run actually showed; edit them if not.
- Post the FIRST COMMENT right after publishing (below).
-->

My on-call agent's best feature is refusing to repeat a fix that failed.

Same alert, same model (gpt-oss-120b):
Without memory: "raise the pool to 80, restart the pods", citing an incident that doesn't exist.
With Hindsight agent memory: cited 4 real past incidents and flagged the fixes that caused outages before.

What I learned building NeverTwice:
- Store fixes by outcome (worked/failed/made worse) as tagged memories
- Recall isn't enough: the model still suggested a restart that memory said failed
- A backfire guard asks memory to check the plan, moving known-bad steps out
- Measured: 11/12 answers passed with memory, 2/12 without; without it, a known-bad fix came back 10/12 times

Code: https://github.com/SomeswararaoTellakula/nevertwice

#AIAgents #AgentMemory #Hindsight #LLM #SRE

---

**First comment (post immediately):**

Memory layer: Hindsight by Vectorize — https://github.com/vectorize-io/hindsight
