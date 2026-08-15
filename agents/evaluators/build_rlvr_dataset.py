"""
Build an RLVR dataset in the schema SageMaker actually requires.

    python3 build_rlvr_dataset.py

Why this file exists
--------------------
The RLVR path on SageMaker does not accept a custom reward Lambda. The
`preset_reward_function` hyperparameter is required and takes exactly one of
`gsm8k`, `prime_code` or `prime_math`. So `reward_function_v2.py` cannot be wired
in here, and the dataset has to suit a preset verifier instead.

`gsm8k` verifies grade-school arithmetic by reading the number after `####`, so
every record below is an arithmetic word problem with an exact integer answer and a
prompt that asks for that marker.

Two constraints the earlier dataset failed
------------------------------------------
  * Schema. RLVR wants `prompt` as a message array plus `reward_model` carrying
    `ground_truth` and `style: rule`. A plain `messages` file is rejected with
    "File appears to be OpenAI_Messages format, but RLVR/RLAIF/MTRL format is
    expected for the selected technique."
  * Size. `global_batch_size` must be 128, 256, 512 or 1024. A 106-record file
    cannot fill even the smallest batch, so this generates 320.

Every answer is computed in Python, never written by hand, so the ground truth
cannot disagree with the question.
"""

import json
import os
import random

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "dataset_rlvr_math.jsonl")

DATA_SOURCE = "aws-ai-league/arithmetic"
ABILITY = "math"
SUFFIX = ' Let\'s think step by step and output the final answer after "####".'

TARGET = 320
SEED = 20260816

NAMES = ["Aisha", "Bruno", "Chen", "Dalia", "Eli", "Farah", "Gus", "Hana",
         "Ivan", "Jo", "Kiran", "Lena", "Mo", "Nia", "Omar", "Pia",
         "Quinn", "Rosa", "Sami", "Tara", "Uma", "Viktor", "Wen", "Yara"]
ITEMS = ["apples", "pencils", "stickers", "marbles", "cards", "cookies",
         "bottles", "tickets", "stamps", "coins", "books", "shells"]
PLACES = ["the market", "the fair", "school", "the shop", "the library"]


def r(rng, a, b):
    return rng.randint(a, b)


# Each builder returns (question_without_suffix, exact_integer_answer). The answer
# is always computed, so it cannot drift from the wording.

def t_sell_half(rng):
    name, item = rng.choice(NAMES), rng.choice(ITEMS)
    first = r(rng, 20, 90) * 2
    total = first + first // 2
    return ("%s sold %d %s in April, and half as many in May. How many %s did %s "
            "sell altogether?" % (name, first, item, item, name), total)


def t_buy_spend(rng):
    name, item = rng.choice(NAMES), rng.choice(ITEMS)
    count, price = r(rng, 3, 15), r(rng, 2, 12)
    return ("%s buys %d %s at %d each. How much does %s spend in total?"
            % (name, count, item, price, name), count * price)


def t_change(rng):
    name, item = rng.choice(NAMES), rng.choice(ITEMS)
    count, price = r(rng, 2, 9), r(rng, 3, 11)
    paid = count * price + r(rng, 1, 40)
    return ("%s pays %d for %d %s costing %d each. How much change does %s receive?"
            % (name, paid, count, item, price, name), paid - count * price)


def t_share(rng):
    name, item = rng.choice(NAMES), rng.choice(ITEMS)
    people = r(rng, 3, 12)
    each = r(rng, 2, 20)
    return ("%s shares %d %s equally among %d friends. How many does each friend get?"
            % (name, people * each, item, people), each)


def t_leftover(rng):
    item = rng.choice(ITEMS)
    people, each = r(rng, 3, 9), r(rng, 2, 12)
    extra = r(rng, 1, people - 1)
    total = people * each + extra
    return ("%d %s are shared equally among %d children. How many %s are left over?"
            % (total, item, people, item), extra)


def t_all_but(rng):
    total = r(rng, 12, 60)
    exception = r(rng, 2, total - 2)
    item = rng.choice(ITEMS)
    return ("A crate holds %d %s and all but %d are ripe. How many are ripe?"
            % (total, item, exception), exception)


def t_two_rates(rng):
    name = rng.choice(NAMES)
    per_day, days = r(rng, 3, 20), r(rng, 3, 14)
    bonus = r(rng, 5, 50)
    return ("%s saves %d each day for %d days, then finds %d more. How much does "
            "%s have?" % (name, per_day, days, bonus, name), per_day * days + bonus)


def t_percent(rng):
    base = r(rng, 4, 40) * 25
    pct = rng.choice([10, 20, 25, 50, 75])
    return ("A jacket costs %d and is reduced by %d percent. What is the new price?"
            % (base, pct), base - base * pct // 100)


def t_travel(rng):
    speed, hours = r(rng, 30, 90), r(rng, 2, 9)
    return ("A bus travels at %d km per hour for %d hours. How many kilometres does "
            "it cover?" % (speed, hours), speed * hours)


def t_minutes(rng):
    start_h, start_m = r(rng, 6, 10), rng.choice([0, 15, 30, 45])
    dur = r(rng, 20, 200)
    return ("A train leaves at %02d:%02d and arrives %d minutes later. How many "
            "minutes is the journey?" % (start_h, start_m, dur), dur)


def t_rows(rng):
    rows, per = r(rng, 3, 15), r(rng, 4, 18)
    removed = r(rng, 1, 10)
    return ("A hall has %d rows of %d chairs. If %d chairs are removed, how many "
            "remain?" % (rows, per, removed), rows * per - removed)


def t_difference(rng):
    a, b = rng.choice(NAMES), rng.choice(NAMES)
    while b == a:
        b = rng.choice(NAMES)
    item = rng.choice(ITEMS)
    x, y = r(rng, 20, 200), r(rng, 5, 19)
    return ("%s has %d %s and %s has %d. How many more does %s have?"
            % (a, x, item, b, y, a), x - y)


def t_double_then_add(rng):
    name = rng.choice(NAMES)
    start, extra = r(rng, 5, 60), r(rng, 3, 40)
    return ("%s has %d tokens, doubles them at %s, then wins %d more. How many "
            "tokens now?" % (name, start, rng.choice(PLACES), extra),
            start * 2 + extra)


def t_three_way(rng):
    item = rng.choice(ITEMS)
    a, b = r(rng, 10, 80), r(rng, 10, 80)
    c = r(rng, 5, 30)
    return ("A shop sells %d %s on Monday, %d on Tuesday and %d on Wednesday. How "
            "many in total?" % (a, item, b, c), a + b + c)


def t_remove_fraction(rng):
    item = rng.choice(ITEMS)
    total = r(rng, 4, 30) * 4
    return ("A box has %d %s. A quarter are taken out. How many remain?"
            % (total, item), total - total // 4)


def t_pairs(rng):
    item = rng.choice(ITEMS)
    pairs = r(rng, 6, 60)
    return ("%d pairs of %s are counted. How many individual %s is that?"
            % (pairs, item, item), pairs * 2)


BUILDERS = [t_sell_half, t_buy_spend, t_change, t_share, t_leftover, t_all_but,
            t_two_rates, t_percent, t_travel, t_minutes, t_rows, t_difference,
            t_double_then_add, t_three_way, t_remove_fraction, t_pairs]


def build():
    rng = random.Random(SEED)
    rows, seen = [], set()
    guard = 0
    while len(rows) < TARGET and guard < TARGET * 60:
        guard += 1
        question, answer = rng.choice(BUILDERS)(rng)
        if question in seen:
            continue
        seen.add(question)
        rows.append({
            "data_source": DATA_SOURCE,
            "prompt": [{"content": question + SUFFIX, "role": "user"}],
            "ability": ABILITY,
            "reward_model": {"ground_truth": str(answer), "style": "rule"},
        })
    return rows


def check(rows):
    problems = []
    if len(rows) < 128:
        problems.append("only %d records; global_batch_size starts at 128" % len(rows))
    seen = set()
    for i, row in enumerate(rows):
        if set(row) != {"data_source", "prompt", "ability", "reward_model"}:
            problems.append("row %d: unexpected keys %s" % (i, sorted(row)))
        prompt = row["prompt"]
        if not isinstance(prompt, list) or len(prompt) != 1:
            problems.append("row %d: prompt must be a one-element array" % i)
            continue
        turn = prompt[0]
        if set(turn) != {"content", "role"} or turn["role"] != "user":
            problems.append("row %d: prompt turn must be role user with content" % i)
        if not turn["content"].strip():
            problems.append("row %d: empty prompt" % i)
        if "####" not in turn["content"]:
            problems.append("row %d: prompt does not ask for the #### marker" % i)
        reward = row["reward_model"]
        if reward.get("style") != "rule":
            problems.append("row %d: reward_model.style must be rule" % i)
        truth = reward.get("ground_truth")
        if not isinstance(truth, str) or not truth.lstrip("-").isdigit():
            problems.append("row %d: ground_truth must be an integer string, got %r"
                            % (i, truth))
        if turn["content"] in seen:
            problems.append("row %d: duplicate prompt" % i)
        seen.add(turn["content"])
    return problems


def main():
    rows = build()
    problems = check(rows)
    if problems:
        print("REJECTED, %d problem(s):" % len(problems))
        for p in problems[:20]:
            print("  -", p)
        raise SystemExit(1)
    with open(OUT, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    truths = [int(r["reward_model"]["ground_truth"]) for r in rows]
    print("wrote %s" % os.path.basename(OUT))
    print("  records                : %d   (global_batch_size 128 needs >=128)" % len(rows))
    print("  unique prompts         : %d" % len({r["prompt"][0]["content"] for r in rows}))
    print("  ground truths          : min %d, max %d, all integers" % (min(truths), max(truths)))
    print("  every prompt asks for  : the #### marker gsm8k reads")
    print("  reward_model.style     : rule")


if __name__ == "__main__":
    main()
