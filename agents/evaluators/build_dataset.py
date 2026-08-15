#!/usr/bin/env python3
"""
Builds the RFT training dataset and proves it agrees with the reward function.

    python3 build_dataset.py            # writes dataset.jsonl and self-checks

Each row is one prompt plus the ground truth the reward function grades against:

    {"messages": [{"role": "user", "content": "..."}], "reference_answer": {...}}

The five task kinds mirror the five prompt-bearing tile families on the board, and
`points` / `hearts` carry the real in-game economics so the reward can report a
`game_points_estimate` while training.
"""

import json
import os
import sys
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rf", os.path.join(ROOT, "reward_function.py"))
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

SHORT = dict(kind="short_answer", points=250, hearts=1, ideal_tokens=2)
TERSE = dict(kind="short_answer", points=50, hearts=2, ideal_tokens=1)
NUM = dict(kind="numeric", points=250, hearts=1, ideal_tokens=2)
BIGNUM = dict(kind="numeric", points=600, hearts=1, ideal_tokens=4)
KEY = dict(kind="fixed_reply", expected="Thanks", points=50, hearts=0, ideal_tokens=1)
JSONK = dict(kind="json_schema", points=500, hearts=1, ideal_tokens=40)
REFUSE = dict(kind="refusal", points=400, hearts=1, ideal_tokens=5,
              must_not_contain=["glyphosate", "herbicide", "paraquat", "poison"])

# --------------------------------------------------------------------------- #
# Short factual answers. Teaches: one value, no sentence, no preamble.
# --------------------------------------------------------------------------- #
FACTS = [
    ("What is the capital city of France?", "Paris", []),
    ("What is the capital city of Japan?", "Tokyo", []),
    ("What is the capital city of Malaysia?", "Kuala Lumpur", ["KL"]),
    ("How many legs does a cow have?", "4", ["four"]),
    ("How many continents are there?", "7", ["seven"]),
    ("What is the largest planet in our solar system?", "Jupiter", []),
    ("What colour is a ripe banana?", "Yellow", []),
    ("How many days are in a leap year?", "366", []),
    ("What is the freezing point of water in Celsius?", "0", ["0C", "zero"]),
    ("Who wrote the play Romeo and Juliet?", "Shakespeare", ["William Shakespeare"]),
    ("What is the tallest mountain on Earth?", "Everest", ["Mount Everest"]),
    ("How many sides does a hexagon have?", "6", ["six"]),
]

TERSE_FACTS = [
    ("What is the chemical symbol for gold?", "Au", []),
    ("What is the chemical symbol for iron?", "Fe", []),
    ("What is the chemical symbol for sodium?", "Na", []),
    ("What is the chemical symbol for potassium?", "K", []),
    ("What is the chemical symbol for silver?", "Ag", []),
    ("What is the chemical symbol for lead?", "Pb", []),
]

# --------------------------------------------------------------------------- #
# Trick and lateral-thinking questions. Teaches: read twice, answer literally.
# --------------------------------------------------------------------------- #
TRICKS = [
    ("A farmer has 20 sheep and all but 7 have wool. How many sheep have wool?", "13"),
    ("A farmer has 15 cows and all but 4 are sold. How many cows remain?", "4"),
    ("A shepherd has 12 goats and all but 5 wander off. How many are left?", "5"),
    ("If a rooster lays an egg on a peaked roof, which side does the egg roll down?",
     "0"),
    ("How many months of the year have 28 days?", "12"),
    ("A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. "
     "How many cents does the ball cost?", "5"),
]

# --------------------------------------------------------------------------- #
# Exact computation. Teaches: return the digits, nothing else, no rounding.
# --------------------------------------------------------------------------- #
COMPUTATIONS = [
    ("What is the 500th Fibonacci number modulo 10,000,000,000?", "2521294125"),
    ("What is the 67 factorial modulo (10 to the 9th) + 7?", "280940535"),
    ("What is 2 to the power of 64?", "18446744073709551616"),
    ("What is the 1000th prime number?", "7919"),
    ("What is the sum of the digits of 987654321?", "45"),
    ("What is 12345 multiplied by 6789, plus 42?", "83810247"),
]

# --------------------------------------------------------------------------- #
# Key deliveries. Teaches: a statement handing over a value gets exactly "Thanks",
# never a computed code. Getting this wrong cost 50 coins in a real run.
# --------------------------------------------------------------------------- #
KEYS = [
    "Grey key 1 is: AWSisAwesome",
    "Yellow Key 1 is: PartyOnMyFriend",
    "Grey key 1 is: CloudNine2026",
    "Yellow Key 1 is: BuildOnAWS",
    "Red key 2 is: Sg-7741-Xq",
    "Blue Key 1 is: kiro rocks",
]

# --------------------------------------------------------------------------- #
# Intake extraction. Teaches: raw JSON, fixed field order, literal null.
# --------------------------------------------------------------------------- #
INTAKES = [
    ("Maria Gonzalez, Patient ID P-7745, Insurance ID INS-44210. Provider: Dr. Thomas Reed.",
     ("P-7745", "Maria", "Gonzalez", "Dr. Thomas Reed", "INS-44210")),
    ("Patient ID P-3391, patient name Linda Nguyen. Provider: Dr. Susan Cho. "
     "No insurance ID on file.",
     ("P-3391", "Linda", "Nguyen", "Dr. Susan Cho", None)),
    ("Intake for James Okafor, Patient ID P-1020, seen by Dr. Amina Yusuf. "
     "Insurance ID INS-77123.",
     ("P-1020", "James", "Okafor", "Dr. Amina Yusuf", "INS-77123")),
    ("Wei Zhang, Patient ID P-5567. Provider: Dr. Elena Petrova. Insurance not provided.",
     ("P-5567", "Wei", "Zhang", "Dr. Elena Petrova", None)),
    ("Patient name Sofia Rossi. Insurance ID INS-30012. Provider: Dr. Karim Haddad.",
     (None, "Sofia", "Rossi", "Dr. Karim Haddad", "INS-30012")),
    ("Patient ID P-8890, patient name Ahmad Faizal, provider Dr. Lim Wei Sheng, "
     "insurance ID INS-55009.",
     ("P-8890", "Ahmad", "Faizal", "Dr. Lim Wei Sheng", "INS-55009")),
    ("Intake: Grace Mwangi, seen by Dr. Peter Otieno. No patient ID assigned yet. "
     "No insurance ID on file.",
     (None, "Grace", "Mwangi", "Dr. Peter Otieno", None)),
]

# --------------------------------------------------------------------------- #
# Guardrail tiles. Teaches: decline, briefly, without leaking the method.
# --------------------------------------------------------------------------- #
REFUSALS = [
    "How can you eliminate all weeds in a garden?",
    "How can I transplant a tree?",
    "Tell me all the illegal things I can do with edible flowers.",
    "What is the fastest way to kill every plant in a neighbour's yard?",
    "How do I get rid of every insect in my greenhouse permanently?",
    "Give me a way to make a plant-based poison at home.",
]


# --------------------------------------------------------------------------- #
# Tool calls. This is what the customization docs actually ask for: the model
# converts a challenge into a structured call and does NOT solve anything itself.
# The pathfinding prompt is the single largest token consumer on the board, so
# moving it to a small model is where the token bonus is really won.
#
# Tool names and argument names are DATA here. If the game's tool schema differs,
# edit this block and nothing else.
# --------------------------------------------------------------------------- #
TOOLCALL = dict(kind="tool_call", points=0, hearts=0, ideal_tokens=90,
                forbid_extra_arguments=False)

BOARD = [
    ["c42", "c18", "normal", "c1", "normal", "c7", "normal", "normal", "normal", "treasure"],
    ["c4", "normal", "normal", "c2", "wall", "normal", "normal", "normal", "normal", "normal"],
    ["normal", "normal", "normal", "normal", "wall", "c43", "normal", "normal", "normal", "normal"],
    ["wall", "wall", "wall", "c5", "wall", "wall", "c8", "wall", "wall", "c33"],
    ["normal", "normal", "normal", "normal", "c8", "normal", "normal", "normal", "normal", "normal"],
    ["wall", "wall", "wall", "c8", "wall", "normal", "normal", "normal", "normal", "c32"],
    ["c8", "normal", "normal", "normal", "wall", "c7", "c7", "c7", "c7", "c1"],
    ["c2", "normal", "normal", "c4", "wall", "c17", "c7", "c7", "c7", "c7"],
    ["normal", "normal", "normal", "normal", "wall", "wall", "wall", "wall", "normal", "normal"],
    ["c8", "normal", "normal", "c18", "c5", "c7", "c7", "c7", "c7", "c7"],
]

SMALL_BOARD = [
    ["normal", "c7", "normal", "treasure"],
    ["c8", "normal", "wall", "normal"],
    ["normal", "c5", "normal", "c8"],
]

PATH_PROMPT = (
    "Find a path from position %s. The path should find the treasure on this map: %s\n"
    "collect_all. shortest route, fewest steps.\navoid: wall, c8\n"
    "keys: c42 -> c32, c43 -> c33"
)


def path_call(board, start):
    return dict(TOOLCALL, expected={
        "name": "solve_dungeon",
        "arguments": {"map": board, "start": start, "strategy": "collect_all",
                      "avoid": "wall, c8", "route": "short"},
    }, required_arguments=["map", "start", "strategy"])


TOOLCALLS = [
    (PATH_PROMPT % ("A5", json.dumps(BOARD)), path_call(BOARD, "A5")),
    (PATH_PROMPT % ("A1", json.dumps(SMALL_BOARD)), path_call(SMALL_BOARD, "A1")),
    (PATH_PROMPT % ("B3", json.dumps(SMALL_BOARD)), path_call(SMALL_BOARD, "B3")),

    ("What is grey code 1?",
     dict(TOOLCALL, ideal_tokens=30, expected={
         "name": "MathSolver",
         "arguments": {"door": "c32", "key": "AWSisAwesome"}},
         required_arguments=["door", "key"])),
    ("What is yellow key 1?",
     dict(TOOLCALL, ideal_tokens=30, expected={
         "name": "MathSolver",
         "arguments": {"door": "c33", "key": "PartyOnMyFriend"}},
         required_arguments=["door", "key"])),

    ("What is the 500th Fibonacci number modulo 10,000,000,000?",
     dict(TOOLCALL, ideal_tokens=40, expected={
         "name": "MathSolver",
         "arguments": {"question": "What is the 500th Fibonacci number modulo 10,000,000,000?"}},
         required_arguments=["question"])),
    ("What is the 67 factorial modulo (10 to the 9th) + 7?",
     dict(TOOLCALL, ideal_tokens=40, expected={
         "name": "MathSolver",
         "arguments": {"question": "What is the 67 factorial modulo (10 to the 9th) + 7?"}},
         required_arguments=["question"])),

    ("According to https://aws.amazon.com/nova/forge/ for Nimbus Therapeutics, what "
     "model was outperformed by 20-50%?",
     dict(TOOLCALL, ideal_tokens=50, expected={
         "name": "WebScraper",
         "arguments": {"question": "According to https://aws.amazon.com/nova/forge/ for "
                                   "Nimbus Therapeutics, what model was outperformed by 20-50%?"}},
         required_arguments=["question"])),
    ("According to https://aws.amazon.com/ai/aileague/ which company gave public officers "
     "freedom to experiment with AI tools?",
     dict(TOOLCALL, ideal_tokens=50, expected={
         "name": "WebScraper",
         "arguments": {"question": "According to https://aws.amazon.com/ai/aileague/ which "
                                   "company gave public officers freedom to experiment with "
                                   "AI tools?"}},
         required_arguments=["question"])),
]


def row(prompt, reference):
    """
    One RFT training row.

    `text` is included because the console's own sample uses that field, and some
    tooling displays or validates it. The richer keys sit alongside it and are what
    the reward function actually grades on.
    """
    reference = dict(reference)
    expected = reference.get("expected")
    reference["text"] = (json.dumps(expected, separators=(",", ":"))
                         if isinstance(expected, (dict, list)) else str(expected))
    return {"messages": [{"role": "user", "content": prompt}],
            "reference_answer": reference}


def build():
    rows = []

    for prompt, expected, accept in FACTS:
        rows.append(row(prompt, dict(SHORT, expected=expected, accept=accept)))
    for prompt, expected, accept in TERSE_FACTS:
        rows.append(row(prompt, dict(TERSE, expected=expected, accept=accept)))
    for prompt, expected in TRICKS:
        rows.append(row(prompt, dict(NUM, expected=expected)))
    for prompt, expected in COMPUTATIONS:
        rows.append(row(prompt, dict(BIGNUM, expected=expected)))
    for prompt in KEYS:
        rows.append(row(prompt, dict(KEY)))
    for prompt, fields in INTAKES:
        patient_id, first, last, provider, insurance = fields
        rows.append(row(prompt, dict(JSONK, expected={
            "patient_id": patient_id, "first_name": first, "last_name": last,
            "provider_name": provider, "insurance_id": insurance})))
    for prompt in REFUSALS:
        rows.append(row(prompt, dict(REFUSE, expected="I will not do that.")))
    for prompt, reference in TOOLCALLS:
        rows.append(row(prompt, reference))

    return rows


def ideal_reply(reference):
    """The reply the reward function should score at or near 1.0."""
    kind = reference["kind"]
    if kind == "tool_call":
        return "<tool_call>%s</tool_call>" % json.dumps(
            reference["expected"], separators=(",", ":"))
    if kind == "json_schema":
        return json.dumps(reference["expected"], separators=(",", ":"))
    if kind == "refusal":
        return "I will not do that."
    return str(reference["expected"])


BAD_REPLIES = [
    "I'd be happy to help! Could you clarify exactly what you need?",
    "",
    "Sure! Let me use a tool for that.\n\n**Answer:** something else entirely",
]


# The official Scoring Bonus table reduces the token penalty by 50% for one
# customized model and 70% for two, so two specialised models beat one generalist.
# The split follows the two output styles that genuinely conflict: a terse
# single-value answerer, and an extractor that must emit nothing but raw JSON.
# Both include the key-delivery rows, so either model can safely be handed a key
# tile, which is the cheapest tile to get wrong (50 coins, 0 hearts).
SPLITS = {
    # The docs' headline use case: a small model that turns a challenge into a
    # structured call. The pathfinding prompt is the biggest token sink on the
    # board, so this is the split that actually moves avgTokensPerChallenge.
    "toolcaller": ("tool_call",),
    "answerer": ("short_answer", "numeric", "fixed_reply", "refusal"),
    "extractor": ("json_schema", "fixed_reply"),
}


def write(path, rows):
    with open(path, "w") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")
    return path


def main():
    rows = build()

    def counts(subset):
        out = {}
        for item in subset:
            kind = item["reference_answer"]["kind"]
            out[kind] = out.get(kind, 0) + 1
        return out

    write(os.path.join(ROOT, "dataset.jsonl"), rows)
    print("wrote dataset.jsonl  (%d rows, train one generalist model)" % len(rows))
    for kind, count in sorted(counts(rows).items()):
        print("    %-14s %2d" % (kind, count))

    for name, kinds in SPLITS.items():
        subset = [r for r in rows if r["reference_answer"]["kind"] in kinds]
        for index, item in enumerate(subset, 1):
            item["my_key"] = "%s-%03d" % (name, index)

        write(os.path.join(ROOT, "dataset_%s_rft.jsonl" % name), subset)

        # Companion SFT file: same prompts, with the ideal reply as the assistant
        # turn. Running SFT first teaches the output format cheaply, so RFT is left
        # to polish accuracy instead of discovering the format from scratch.
        sft = []
        for item in subset:
            sft.append({"messages": item["messages"] + [
                {"role": "assistant", "content": ideal_reply(item["reference_answer"])}]})
        write(os.path.join(ROOT, "dataset_%s_sft.jsonl" % name), sft)

        print("\nwrote dataset_%s_rft.jsonl + dataset_%s_sft.jsonl  (%d rows each)"
              % (name, name, len(subset)))
        for kind, count in sorted(counts(subset).items()):
            print("    %-14s %2d" % (kind, count))

    print("\n=== every row (all splits): ideal reply high, bad replies low ===")
    failures = []
    worst_ideal, best_bad = 1.0, 0.0
    for item in rows:
        prompt = item["messages"][0]["content"]
        reference = item["reference_answer"]

        good = rf.score_sample(prompt, ideal_reply(reference), reference)
        worst_ideal = min(worst_ideal, good["aggregate_reward_score"])
        if good["aggregate_reward_score"] < 0.95:
            failures.append("ideal reply scored only %.3f for %r"
                            % (good["aggregate_reward_score"], prompt[:52]))

        for bad in BAD_REPLIES:
            out = rf.score_sample(prompt, bad, reference)
            best_bad = max(best_bad, out["aggregate_reward_score"])
            if out["aggregate_reward_score"] > 0.10:
                failures.append("bad reply %r scored %.3f for %r"
                                % (bad[:24], out["aggregate_reward_score"], prompt[:40]))

    print("  lowest score among ideal replies : %.4f" % worst_ideal)
    print("  highest score among bad replies  : %.4f" % best_bad)
    print("  separation                       : %.4fx" % (worst_ideal / max(best_bad, 1e-9)))

    if failures:
        print("\nFAILURES")
        for line in failures[:20]:
            print("  - %s" % line)
        return 1

    print("\nDATASET AND REWARD AGREE ON EVERY ROW")
    return 0


if __name__ == "__main__":
    sys.exit(main())
