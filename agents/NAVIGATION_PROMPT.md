# Strategy — paste this into the "Navigation Prompt" box

The pathfinding Lambda knows no tile IDs. It learns the taxonomy from whatever it
is told, and this box is the channel. Two ways to fill it in — pick one.

## Option A — declare the taxonomy (short, explicit)

```text
collect_all.
avoid: wall, c8
keys: c42 -> c32, c43 -> c33

Visit the nearest unvisited challenge first using a 4-direction safe shortest path,
then repeat. A locked door is impassable until its matching key is collected.
Treasure is terminal: while any reachable challenge remains, treat treasure as an
impassable wall. Enter treasure exactly once, only after all reachable challenges
are complete, and make it the final coordinate. Return only the ordered path.
```

Update the `avoid:` and `keys:` lines if the board changes — that is the only edit
a new dungeon ever needs, and it is a text edit, not a code change.

## Option B — paste the challenge list and let it derive everything

Append the game's own challenge descriptions and drop the `avoid:`/`keys:` lines.
The handler works out the taxonomy itself:

```text
collect_all. Nearest unvisited challenge first. Keys before doors. Treat treasure
as an impassable wall until every reachable challenge is complete; then enter it
exactly once as the final coordinate. Return only the ordered path.

(c42) a Grey Key - You must find this key before entering the grey door. +50
(c32) a Grey Door - To solve this challenge you must find the grey key. +1000 ❤ -5
(c43) a Yellow Key - You must find this key before entering the yellow door. +50
(c33) a Yellow Door - To solve this challenge you must find the yellow key. +1000 ❤ -5
(c8)  a Spike trap - obstacles meant to be avoided, or lose 1 health. ❤ -1
(c7)  some Coins - pass over these and gain coins. +25
... one line per challenge
```

Verified: Option A and Option B produce the identical 69-step route on the current
board (`python3 verify.py` runs both).

## How the derivation works

| Signal in the text | What the handler concludes |
| --- | --- |
| `avoid: <tiles>`, "never step on …" | those tiles are hazards |
| a tile whose entry costs health and awards no points | hazard, even if the word "spike" never appears |
| `<id> a <colour> Key` / `<id> a <colour> Door` | key / door, paired on the colour word |
| `c42 -> c32`, `c42 before c32` | explicit key→door pair |
| `+1000` inside a tile's entry | that tile's value (tie-breaks only) |
| `❤ -5`, `-1 heart`, `lose 1 health` | that tile's heart cost |
| a door with no discoverable key | stays impassable, and a warning is returned |

Classification reads the tile's **name**, by whichever noun comes first — so
"a Crystal Gate — needs the crystal key" is a door, while "a Yellow Key — find it
before the yellow door" is a key. Entries are cut at the next tile ID and at the
document boundary, so one tile can never absorb its neighbour's reward badge.

## Strategy keywords

| Phrase | Behaviour |
| --- | --- |
| `collect_all` / `all challenges` | visit every challenge tile, treasure last (default) |
| `swift` / `direct` | ignore challenges, shortest safe route to the treasure |
| `coins only` | only the risk-free low-value tiles, plus keys/doors, then treasure |
| `safe` / `preserve hearts` | skip every tile that can cost a heart |
| `budget=250` / `max_steps 250` | stop collecting once the step budget would be blown, but still reach the chest |

Same settings are accepted as payload fields (`strategy`, `avoid`, `key_doors`,
`values`, `max_steps`) and as Lambda env vars (`STRATEGY`, `AVOID_TILES`,
`KEY_DOOR_PAIRS`, `TILE_VALUES`, `STEP_BUDGET`) if you would rather not put them in
the prompt at all.

## Result on the current board

`A5` start → 30 objectives → chest at `J1`: **69 steps, 0 hazards, 0 walls,
~6820 points before answer bonuses.**

```
D4(c5) D2(c2) D1(c1) B1(c18) A1(c42 grey key) A2(c4) F1(c7) F3(c43 yellow key)
J4(c33 yellow door) J6(c32 grey door) J7(c1) I7 H7 G7 F7 F8(c17) G8 H8 I8 J8
J10 I10 H10 G10 F10 E10(c5) D10(c18) D8(c4) A8(c2) → J1(treasure)
```

Spikes `A7, A10, D6, E5, G4` are never entered. `A1` (step 10) precedes door `J6`
(step 26); `F3` (step 19) precedes door `J4` (step 24).

If the 5:00 clock binds before the walking does, add `budget=45`: 21 objectives in
43 steps, chest still opened.
