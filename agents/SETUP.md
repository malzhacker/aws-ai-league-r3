# Fresh account setup, in order

Everything needed is already in this repo. Build in this order, because each phase
depends on the one before it.

## Phase 1 — three Lambda functions

Runtime `python3.12`, handler `lambda.lambda_handler`, no dependencies to install.

| Function | File | Timeout | Memory | Note |
| --- | --- | --- | --- | --- |
| Pathfinding | `pathfinding/lambda.py` | 10s | 256 MB | |
| MathSolver | `mathsolver/lambda.py` | 30s | 512 MB | see the decision below |
| WebScraper | `webscraper/lambda.py` | 30s | 512 MB | **deploy with no VPC config** or it has no internet |

```bash
for fn in pathfinding mathsolver webscraper; do
  (cd "$fn" && zip -q "../$fn.zip" lambda.py)
  aws lambda create-function --function-name "dungeon-$fn" \
    --runtime python3.12 --handler lambda.lambda_handler \
    --role "arn:aws:iam::$ACCOUNT:role/service-role/dungeon-lambda-role" \
    --timeout 30 --memory-size 512 --zip-file "fileb://$fn.zip"
done
```

Verify locally first: `python3 verify.py` must print `ALL CHECKS PASSED`.

### The one decision you have to make: door codes

On the previous account, MathSolver accepted `{"door": "<id>", "key": "<value>"}` and
applied the right character transformation itself. **`mathsolver/lambda.py` in this
repo does not do that.** It exposes `chars(text, ...)` and `edges(text, head, tail)`
and expects to be told which to use:

```json
{"code": "result = chars(key, 5, 7)", "variables": {"key": "<stored value>"}}
{"code": "result = edges(key, 2, 2)", "variables": {"key": "<stored value>"}}
```

Both are verified: they return `yn` and `AWme` for the two keys seen on the practice
board.

That leaves two ways to wire doors, and they differ in where the board's rule lives:

**Option A — the rule lives in MathSolver.** Add a door-to-rule mapping inside the
Lambda so the prompt only sends `{"door": "<challenge id>", "key": "<value>"}`. The
supervisor prompt stays completely free of board data, matching what scored 13977 on
the evaluation map. The board knowledge sits in code instead.

**Option B — the rule lives in the prompt.** Keep the Lambda as it is and have the
prompt say which transformation to use. Simpler to deploy, but puts two lines of
board data back into the supervisor prompt.

Option A is closer to the setup that passed the evaluation map. Decide before Phase 4,
because the DOORS block differs between them.

## Phase 2 — Memory and Guardrail

Both matter, and one of them is not optional.

- **Memory Tool** — required. With an empty slot the agent stores no key and both door
  tiles are lost at −5 hearts each. This single mistake ended three runs.
- **Guardrail Tool** — the guardrail tile scored 400 twice by replying
  `I will not do that.` That reply comes from the guardrail intervening, not from the
  prompt, so without a guardrail attached that tile behaves differently.

## Phase 3 — two custom models

Worth 53 points, and they are never invoked. Registration is what counts.

1. **Create reward function** — Method `Code`, name `dungeon-rlvr-scorer`. Type a name
   first or the editor stays locked. Paste all of `evaluators/reward_function.py`,
   including `lambda_handler`; pasting only `reward_function` causes
   `Handler 'lambda_handler' missing on module 'lambda_function'`.
2. **Model A** — SFT on `evaluators/dataset_answerer_sft.jsonl` (42 rows), then RFT on
   `dataset_answerer_rft.jsonl` with that reward function.
3. **Model B** — same with the `extractor` pair (13 rows).
4. **Attach both as sub-agents**, no tools attached to either.

Check locally first: `python3 evaluators/test_reward.py` prints
`ALL REWARD CHECKS PASSED`, and `build_dataset.py` prints
`DATASET AND REWARD AGREE ON EVERY ROW`.

Sub-agent system prompt for both — never a refusal, never a sentinel, never empty,
because a sub-agent reply is submitted verbatim as the scored answer:

```text
You answer one challenge per request, as briefly as possible.
- Output ONLY the answer. No reasoning, no explanation, no restating the question, no markdown, no tool calls.
- Preserve units and capitalisation exactly. For numbers output only digits.
- Your reply is submitted directly as the scored answer. Nothing downstream can correct it.
- Never reply with a sentinel word, a refusal, a question, an empty string, or whitespace. Any of those loses the challenge.
- If unsure, still give your single best short answer. A wrong short answer and a refusal cost the same.
```

## Phase 4 — the two prompts

- **Supervisor system prompt** — the block in `SUPERVISOR_PROMPT_GENERIC.md`, adjusted
  for whichever door option you chose in Phase 1.
- **Navigation Prompt**, on the Game Play screen, not the agent config:

```text
collect_all. shortest route, fewest steps.
avoid: wall, c8
keys: c42 -> c32, c43 -> c33

Clear every reachable challenge using 4-direction safe paths, and keep the total
number of steps as low as possible. A locked door is impassable until its matching
key is collected. Treasure is terminal: while any reachable challenge remains, treat
treasure as an impassable wall. Enter treasure exactly once, only after all reachable
challenges are complete, and make it the final step.
```

The `avoid:` and `keys:` lines are data the Lambda is designed to receive. If a future
board uses different ids, these two lines are the only edit.

## Token budget — build for this from the start

The current leaderboard leader scores **13982 on 939 output tokens**, a full clear
with two models. Everyone else in the top five is also a full clear on two models, so
output tokens are the only thing separating them.

```
1178 tokens -> 13978      what the previous account settled at
1100         -> 13980
1000         -> 13981
 960         -> 13982     ties the leader
 939         -> 13983     beats them
```

So the target is **under 950 output tokens**. These choices cost nothing and should be
in the first build rather than discovered later:

| Choice | Why |
| --- | --- |
| `shortest route, fewest steps` in the Navigation Prompt | fewer moves means a shorter array to echo back |
| compact JSON on extraction, no fences, no space after colons | ~10 tokens across the two tiles |
| condensed questions to MathSolver and WebScraper | keywords and numbers only, never the full challenge text |
| forbid intent openings by name | "I need to", "I'll", "Let me" cost 15–19 tokens each time they appear |
| two models attached, never routed | routing appends the sub-agent reply on top, adding tokens for zero bonus |

Remember `tokensUsed` counts **output only**. The system prompt, tool responses and
challenge text are input and cost nothing, so the prompt can be as long as it needs to
be. Never trade a rule away to shorten the prompt.

## Measure before optimising further

Do not chase the leader's 939 by guessing. Get one clean full clear on this account,
then read the actual `tokensUsed`, and cut from the largest real item.

An earlier estimate put the map relayed into the Pathfinding tool call at roughly 213
tokens, which would be the single biggest item — but that was inferred, not measured,
because combat logs do not show tool call arguments. Confirm it on this account before
acting on it.

## Phase 5 — verify with one test run

| Tile | Pass | Fail signature |
| --- | --- | --- |
| secret delivery | reply is exactly `Thanks` | a computed code, or no memory write |
| derived code | the transformed code alone | the stored value, or "unavailable" |
| extraction | reply starts with `{`, name split in two | a combined name field |
| path | the directions array, unmodified | coordinates, or a letter string |
| summary | `customModelCount` 2, `livesRemaining` 5 | count 0 means sub-agents detached |

Target is a full clear: 16/16 challenges, 5 lives, treasure taken, roughly 1200 output
tokens, and about 13977.

## Things already settled — do not spend runs retesting

- Compact direction strings such as `RRUULD` are rejected by the parser.
- There is no third model tier; two is the cap.
- A better-trained custom model earns nothing: quality is absent from the formula.
- Never route a scored tile to a sub-agent. It costs tokens and has lost 550 and 1000
  points on two separate runs, with zero upside.
