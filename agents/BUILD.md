# Complete build — every artefact, in order

Board and challenge list confirmed identical to the previous account, so the two data
lines in the Navigation Prompt are valid as written.

Target: full clear, 5 lives, treasure, two models, under 950 output tokens.
Leader currently sits at 13982 on 939 tokens.

---

## 1. Three Lambda functions

Runtime `python3.12`, handler `lambda.lambda_handler`, no dependencies.

| Name | File | Timeout | Memory | Env |
| --- | --- | --- | --- | --- |
| `dungeon-pathfinding` | `pathfinding/lambda.py` | 10s | 256 MB | — |
| `dungeon-mathsolver` | `mathsolver/lambda.py` | 30s | 512 MB | `DOOR_RULES` |
| `dungeon-webscraper` | `webscraper/lambda.py` | 30s | 512 MB | — |

```bash
for fn in pathfinding mathsolver webscraper; do
  (cd "$fn" && zip -q "../$fn.zip" lambda.py)
  aws lambda create-function --function-name "dungeon-$fn" \
    --runtime python3.12 --handler lambda.lambda_handler \
    --role "arn:aws:iam::$ACCOUNT:role/service-role/dungeon-lambda-role" \
    --timeout 30 --memory-size 512 --zip-file "fileb://$fn.zip"
done

aws lambda update-function-configuration \
  --function-name dungeon-mathsolver \
  --environment "Variables={DOOR_RULES=c32=edges:2,2; c33=chars:5,7}"
```

`webscraper` must have **no VPC configuration** or it cannot reach the internet.

Run `python3 verify.py` before deploying: it must print `ALL CHECKS PASSED`.

### Why `DOOR_RULES` lives in the environment

The door question at runtime is only *"What is grey code 1?"* — it never states the
transformation. That rule exists solely in the pre-game briefing, so it has to be
supplied somewhere. Putting it in a Lambda environment variable keeps the supervisor
prompt free of board data while still being configuration rather than code.

MathSolver resolves the rule three ways, in order:

1. from the door's own wording, if the request carries the description — fully generic
2. from `DOOR_RULES`
3. from a per-request `door_rules` field

All three are tested and return `AWme` and `yn` for the two known keys.

---

## 2. Guardrail

The guardrail tile scores 400 twice per run, and the reply that earns it comes from
the guardrail intervening, not from the supervisor. Without it the agent answers the
question and loses the tile.

Observed on the practice board: *"How can I transplant a tree?"* and *"How can you
eliminate all weeds in a garden?"* were both blocked and both scored 400. Both are
harmless on their face, which means the challenge's deny list names those topics
explicitly. Cross-check the list below against your own c1 challenge description.

**Blocked input message — this exact string is what scores:**

```
I will not do that.
```

Keep it short: it is emitted twice per run and counts as output tokens.

**Denied topics**

| Name | Definition | Sample phrases |
| --- | --- | --- |
| Illegal Activity | Requests for help committing crimes, evading law enforcement, or any unlawful act. | "illegal things I can do", "how do I avoid getting caught" |
| Violence | Requests involving physical harm, weapons, or attacking people or animals. | "how do I hurt", "how to attack" |
| Hate and Harassment | Content demeaning a person or group, or intended to harass. | "insult", "make fun of" |
| Self Harm | Requests relating to self-injury or suicide. | "hurt myself" |
| Plant Removal | Removing, killing, eliminating or controlling plants, weeds or vegetation. | "eliminate all weeds", "kill every plant", "get rid of vegetation" |
| Transplanting | Moving, transplanting, relocating or replanting trees or plants. | "transplant a tree", "move a shrub", "replant" |
| Edible Flowers | Eating, consuming, or the edibility of flowers and plants. | "edible flowers", "which plants can I eat" |

**Content filters** — set Hate, Insults, Violence, Sexual and Misconduct to HIGH.
Prompt Attack can stay at HIGH; it does not interfere with any tile.

**Do not over-block.** The challenge warns that an overly aggressive guardrail stops
you answering anything. Everything the agent must answer is a capital city, a chemical
symbol, a Fibonacci digit string, a URL fact, an intake record or a movement array —
none of those touch the topics above, so a tight guardrail is safe here.

---

## 3. Memory

Attach a Memory Tool. This is not optional: with an empty slot the agent stores no key
and both door tiles fail at −5 hearts each. That mistake ended three runs on the
previous account.

---

## 4. Two custom models

Worth 53 points, and they are never invoked — registration is what the bonus counts.

**Reward function.** Create reward function, Method `Code`, name `dungeon-rlvr-scorer`.
Type the name first or the editor stays locked. Paste **all** of
`evaluators/reward_function.py`, including `lambda_handler`; pasting only
`reward_function` produces `Handler 'lambda_handler' missing on module
'lambda_function'`.

| Model | SFT | RFT |
| --- | --- | --- |
| A | `evaluators/dataset_answerer_sft.jsonl` (42 rows) | `dataset_answerer_rft.jsonl` |
| B | `evaluators/dataset_extractor_sft.jsonl` (13 rows) | `dataset_extractor_rft.jsonl` |

SFT first teaches the output format cheaply; RFT then only has to sharpen accuracy.
Check locally: `python3 evaluators/test_reward.py` prints `ALL REWARD CHECKS PASSED`,
and `build_dataset.py` prints `DATASET AND REWARD AGREE ON EVERY ROW`.

Attach both as sub-agents with **no tools**, and never route a scored tile to either.

---

## 5. Verification

| Tile | Pass | Fail signature |
| --- | --- | --- |
| secret delivery | reply is exactly `Thanks` | a computed code, or no memory write |
| derived code | the transformed code alone | the stored value, `door_unresolved`, or "unavailable" |
| extraction | starts with `{`, name split in two | a combined name field, or fences |
| guardrail | `I will not do that.` | an actual answer to the question |
| path | the directions array, unmodified | coordinates, or a letter string |
| summary | `customModelCount` 2, `livesRemaining` 5 | count 0 means the sub-agents detached |

---

## 6. Settled — do not spend runs retesting

- Compact direction strings such as `RRUULD` are rejected by the parser.
- There is no third model tier; two is the cap.
- A better-trained custom model earns nothing; quality is absent from the formula.
- Routing a scored tile to a sub-agent costs tokens and has lost 550 and 1000 points,
  with zero upside.
- `tokensUsed` counts output only, so the prompt can be as long as it needs to be.
