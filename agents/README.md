# AWS AI League — dungeon agent tools

Three standalone Lambda handlers plus one navigation strategy. Standard library
only, so each function deploys as a single `.py` file with no layer or build step.

**No dungeon data is baked into any handler.** No map, no tile IDs, no reward
table, no answer table, no limits you cannot override. Everything comes from the
payload, from environment variables, or is derived from the prompt at runtime.

```
agents/
├── NAVIGATION_PROMPT.md    <- the strategy to paste in the Navigation Prompt box
├── verify.py               <- runs every handler against the fixtures and asserts
├── examples/               <- JSON fixtures (test input only, never imported)
├── pathfinding/lambda.py
├── mathsolver/lambda.py
└── webscraper/lambda.py
```

Handler for all three: `lambda.lambda_handler`, runtime `python3.12`.

## 1. `pathfinding/lambda.py`

Finds the map in whatever prompt it is handed, works out what the tiles mean, then
plans the route.

Behaviour that is fixed (this is the strategy, not dungeon data):

- never enters a wall or a hazard,
- always walks to the **nearest** unvisited objective,
- never enters a door before its key is held,
- takes the **treasure last**,
- stops collecting when continuing would leave it unable to reach the chest,
- re-validates its own output — `issues` is empty only when the path is legal.

Everything else is resolved at runtime, in this order:

1. **payload** — `avoid`, `key_doors`, `values`, `risks`, `walls`, `treasures`,
   `strategy`, `max_steps`, `start`, `map`
2. **environment** — `AVOID_TILES`, `KEY_DOOR_PAIRS`, `TILE_VALUES`, `RISKY_TILES`,
   `WALL_TILES`, `FLOOR_TILES`, `TREASURE_TILES`, `STRATEGY`, `STEP_BUDGET`
3. **the prompt text** — see the derivation table in
   [`NAVIGATION_PROMPT.md`](./NAVIGATION_PROMPT.md)

If no hazards can be identified from any channel, the response carries a warning
instead of silently walking over traps.

Input — any of these work:

```json
{"prompt": "Find a path from position A5 ... map: [[\"c42\", ...]]", "navigation_prompt": "avoid: c8 ..."}
{"map": [["..."]], "start": "A5", "strategy": "swift", "avoid": "c8", "key_doors": "c42->c32"}
{"messageVersion":"1.0","actionGroup":"pathfinding","parameters":[{"name":"prompt","value":"..."}]}
```

Output:

```json
{
  "strategy": "collect_all",
  "start": "A5",
  "path": ["A5","B5","C5","D5","D4","..."],
  "directions": ["right","right","right","up","..."],
  "steps": 69,
  "order": [{"tile":"c5","cell":"D4","steps":4}, "..."],
  "keys_collected": ["c42","c43"],
  "estimated_points": 6820,
  "skipped": [],
  "treasure_reached": true,
  "config": {"hazards":["c8"], "key_doors":{"c42":"c32","c43":"c33"}, "derived_from":["..."]},
  "warnings": [],
  "issues": []
}
```

`config.derived_from` explains every inference it made, so a wrong route is
debuggable without reading the code. Timeout 10 s, memory 256 MB.

## 2. `mathsolver/lambda.py`

For a code/computation challenge — *"the 3000th fibonacci number, last 10 digits"*
→ `6709796000`. No answers are stored; everything is computed.

- `{"question": "..."}` alone often suffices: fibonacci, factorial, nth prime,
  primality, digit sums, powers, base conversion and bare arithmetic are pattern
  matched, and `last/first N digits` post-processing is applied automatically.
- `{"code": "result = last_digits(fact(1000), 12)"}` runs arbitrary Python in an
  AST sandbox — no imports, no attribute access, no builtins beyond a number-theory
  toolbox (`fib`, `fact`, `is_prime`, `nth_prime`, `primes_up_to`, `divisors`,
  `prime_factors`, `collatz_length`, `to_base`, `gcd`, `lcm`, `comb`, `isqrt`,
  `digit_sum`, `last_digits`), `**` routed through a size guard, and a `SIGALRM`
  wall-clock timeout.
- Always returns a bare `answer` string, so the agent can echo it with no prose.

Ceilings are env-tunable: `EXEC_TIMEOUT_SECONDS`, `MAX_EXEC_TIMEOUT_SECONDS`,
`MAX_POW_EXPONENT`, `MAX_POW_BITS`, `MAX_FACTORIAL`, `MAX_INT_STR_DIGITS`.
Timeout 30 s, memory 512 MB (big-int work is CPU-bound).

## 3. `webscraper/lambda.py`

For a web-search challenge — *"According to https://… what is their favourite …"*

- Pulls the URL out of the question when it is not passed separately.
- Real User-Agent, gzip, charset sniffing, redirects, size cap.
- Strips script/style/markup, unescapes entities, returns readable text plus
  keyword-ranked `snippets` so the model can quote the exact answer.
- No URL anywhere → falls back to a search endpoint and auto-fetches the top hit.

Env-tunable: `SEARCH_ENDPOINTS` (comma separated, `%s` = the encoded query),
`HTTP_USER_AGENT`, `FETCH_TIMEOUT_SECONDS`, `MAX_FETCH_TIMEOUT_SECONDS`,
`MAX_RESPONSE_BYTES`, `DEFAULT_MAX_CHARS`, `HARD_MAX_CHARS`.

**Deploy this one without a VPC config**, otherwise it has no internet egress.
Timeout 30 s, memory 512 MB.

## Verify locally

```bash
python3 verify.py
```

Runs both taxonomy channels (declared vs. auto-derived), the swift route and the
budget cap, then asserts: no hazard tile is ever entered, no wall is entered, no
door is entered before its key, the treasure is the final tile, and nothing
reachable is missed. Also runs the solver cases — including four that **must** be
rejected (`import`, dunder access, an oversized power, an infinite loop) — and one
live page fetch.

Each handler is also a CLI:

```bash
python3 pathfinding/lambda.py examples/pathfinding_event.json
python3 mathsolver/lambda.py  examples/mathsolver_events.json
echo '{"url": "https://example.com"}' | python3 webscraper/lambda.py
```

## Deploy

```bash
for fn in pathfinding mathsolver webscraper; do
  (cd "$fn" && zip -q "../$fn.zip" lambda.py)
  aws lambda create-function \
    --function-name "dungeon-$fn" \
    --runtime python3.12 --handler lambda.lambda_handler \
    --role "arn:aws:iam::$ACCOUNT:role/service-role/dungeon-lambda-role" \
    --timeout 30 --memory-size 512 \
    --zip-file "fileb://$fn.zip"
done
```

Update with
`aws lambda update-function-code --function-name dungeon-<fn> --zip-file fileb://<fn>.zip`.

Then register `mathsolver` and `webscraper` as AgentCore / Bedrock action-group
tools, point the game's pathfinding tool at `dungeon-pathfinding`, and paste the
strategy from [`NAVIGATION_PROMPT.md`](./NAVIGATION_PROMPT.md) into the Navigation
Prompt box.
