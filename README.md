# AWS AI League — round 3

Every artefact for the dungeon agent: three Lambda tools, the supervisor and
navigation prompts, the guardrail spec, the fine-tuning datasets, and the RLVR
reward function.

Standard library only. Each Lambda deploys as a single `.py` file with no layer and
no build step.

## Where to start

| I want to | Read |
| --- | --- |
| Set up a fresh account from zero | `agents/SETUP.md` |
| Build every artefact in order | `agents/BUILD.md` |
| Understand what was learned and why | `agents/STATE.md` |
| Know the tools' input and output shapes | `agents/README.md` |

`.kiro/steering/aws-ai-league.md` holds the facts that must never be re-derived:
the scoring formula solved exactly, the configuration that must not regress, and
the prompt-writing rules earned through failed runs.

## Layout

```
agents/
├── pathfinding/lambda.py   <- Pathfinder
├── mathsolver/lambda.py    <- Calculator, also resolves door codes
├── webscraper/lambda.py    <- Researcher
├── verify.py               <- runs every handler against the fixtures
├── examples/               <- JSON fixtures, test input only, never imported
├── evaluators/             <- datasets and the RLVR reward function
├── NAVIGATION_PROMPT.md
├── SUPERVISOR_PROMPT_GENERIC.md   <- the one to use: no board data
├── SUPERVISOR_PROMPT.md           <- superseded, contains board data
└── SUBAGENT_ARCHITECTURE.md
```

## No board data in any prompt or handler

No map, no tile IDs, no reward table, no answer table. Everything arrives in the
payload, comes from an environment variable, or is derived from the challenge text
at runtime. The evaluation map differs from the practice map on purpose, so a
hardcoded answer would fail there and risks disqualification.

The one place board data is allowed is Lambda configuration, because it never
reaches the prompt. Door transformations go in the MathSolver environment:

```
DOOR_RULES = c32=edges:2,2; c33=chars:5,7
```

MathSolver resolves a door rule in three tiers: the door's own wording first, then
`DOOR_RULES`, then a per-request `door_rules` field. Without one of them a door tile
returns `door_unresolved` and the tile is lost.

## Verifying before deploying

```bash
cd agents
python3 verify.py                      # every handler against every fixture
cd evaluators
python3 test_reward_v2.py              # 74 adversarial reward checks
python3 reward_function_v2.py dataset_answerer_rft.jsonl \
        dataset_extractor_rft.jsonl dataset_toolcaller_rft.jsonl
```

The last command scores each dataset row against its own reference answer. Every
row must come back at 1.000: a reference that cannot score full marks is a bug in
the reward function, not in the model.

## Deploying

Runtime `python3.12`, handler `lambda.lambda_handler`. WebScraper must be deployed
**with no VPC config**, or it has no route to the internet.

The reward function is the exception: its handler is
`lambda_function.lambda_handler`, and the whole file must be pasted including
`lambda_handler` itself.

## The reward function contract, as documented

Confirmed against [Implementing reward functions](https://docs.aws.amazon.com/nova/latest/nova2-userguide/nova-implementing-reward-functions.html)
in the Amazon Nova user guide. Content below is paraphrased from that page for
licensing compliance.

`body` is a **JSON string**, not a list. The working example on that page returns
`{"statusCode": 200, "body": json.dumps(results)}`, which is what this repo does.
The evaluator console displays `body` as a nested array because it parses the
string for display, not because the service wants the parsed form.

The container transforms each dataset row before calling the Lambda: it generates a
model response, appends it as the `assistant` turn, and **injects its own `id`**.
The output `id` must match that input `id`. `my_key` is only the dataset's own key,
so the platform `id` takes precedence and `my_key` is the fallback for local and
console testing where no `id` exists. Getting this backwards silently misattributes
every sample during training.

The event arrives as a bare list of samples, or as a single sample object.
`metrics_list` is optional, and each entry's `type` is either `Reward` or `Metric`.

Operational limits from the same page: 15 minutes maximum per invocation, and the
function must tolerate `rollout_worker_replicas * 64` concurrent requests, so it
stays pure string and regex work with no network calls. Failed rows return 0.0
rather than raising, which is why one malformed row cannot abort a batch.

## A note on copying these files

Prefer `git clone` over copy and paste. Two literal sequences have each corrupted a
file in transit through a chat window and a console editor:

- a triple backtick closed the surrounding markdown fence and truncated the file;
- `\n` inside a string literal became a real line break, producing
  `Runtime.UserCodeSyntaxError: unterminated string literal`.

`agents/evaluators/reward_function_v2.py` therefore builds every special character
with `chr()` and contains no literal backtick, and test 15 in `test_reward_v2.py`
reads the source back to assert both properties still hold.

## Scoring, solved exactly

```
totalScore    = coinsEarned + lifeBonus + tokenBonus + treasureBonus
lifeBonus     = 250 x livesRemaining        (max 1250)
treasureBonus = 1000
coinsEarned   = 10750 on a full clear
tokenBonus    = 1000 - avgTokens x (1 - penaltyReduction)
```

`penaltyReduction` is 0% with no custom models, 50% with one, 70% with two. Two is
the cap. `tokensUsed` counts **output tokens only**, so prompts, tool responses and
challenge text are free and prompt length costs nothing.

`customModelCount` counts **registered** models, not invoked ones. Two fine-tuned
models attached and never called is optimal. Model quality does not appear in the
formula anywhere, which means the reward function in this repo is worth zero
leaderboard points; it earns its place by diagnosing failures and by being the
RLVR artefact itself.
