"""
Adversarial tests for reward_function_v2.

Gold rows scoring 1.0 proves very little; a reward function that returns 1.0 for
everything also passes that. These tests assert the *ordering* the training signal
depends on, and that the obvious reward hacks pay nothing.

    python3 test_reward_v2.py
"""

import json
import sys

from reward_function_v2 import PARTIAL_CREDIT as PARTIAL_CREDIT_EXPECTED
from reward_function_v2 import lambda_handler, reward_function

FAILURES = []
CHECKS = [0]


def score(reference, response, index=0):
    sample = {
        "messages": [{"role": "user", "content": "q"},
                     {"role": "assistant", "content": response}],
        "reference_answer": reference,
        "my_key": "t-%03d" % index,
    }
    return reward_function(sample, index)


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if not condition:
        FAILURES.append("%s %s" % (label, detail))
        print("  FAIL  %s %s" % (label, detail))
    else:
        print("  ok    %s" % label)


def approx(value, target, tolerance=1e-6):
    return abs(value - target) <= tolerance


def correctness_of(result):
    """The single Reward-typed metric, read back out of the result."""
    return next(m["value"] for m in result["metrics_list"] if m["name"] == "correctness")


# --------------------------------------------------------------------------- #
SHORT = {"kind": "short_answer", "expected": "Paris", "text": "Paris",
         "ideal_tokens": 2, "points": 250, "accept": []}
NUM = {"kind": "numeric", "expected": "2521294125", "text": "2521294125",
       "ideal_tokens": 4, "points": 600}
BIG = {"kind": "numeric", "expected": "18446744073709551616",
       "text": "18446744073709551616", "ideal_tokens": 4, "points": 600}
FIXED = {"kind": "fixed_reply", "expected": "Thanks", "text": "Thanks",
         "ideal_tokens": 1, "points": 50}
REFUSE = {"kind": "refusal", "expected": "I will not do that.",
          "text": "I will not do that.", "ideal_tokens": 5, "points": 400,
          "must_not_contain": ["glyphosate", "herbicide"]}
INTAKE_EXPECTED = {"patient_id": None, "first_name": "Grace", "last_name": "Mwangi",
                   "provider_name": "Dr. Peter Otieno", "insurance_id": None}
INTAKE = {"kind": "json_schema", "expected": INTAKE_EXPECTED,
          "text": json.dumps(INTAKE_EXPECTED), "ideal_tokens": 40, "points": 500}
CALL = {"kind": "tool_call", "ideal_tokens": 20, "points": 0,
        "expected": {"name": "solve_dungeon", "arguments": {"start": "A5", "avoid": ["c8"]}},
        "text": json.dumps({"name": "solve_dungeon",
                            "arguments": {"start": "A5", "avoid": ["c8"]}}),
        "required_arguments": ["start", "avoid"], "forbid_extra_arguments": True}


print("\n1. exact answers score 1.0")
for label, reference, gold in (("short", SHORT, "Paris"), ("numeric", NUM, "2521294125"),
                              ("bigint", BIG, "18446744073709551616"),
                              ("fixed", FIXED, "Thanks"),
                              ("refusal", REFUSE, "I will not do that."),
                              ("json", INTAKE, json.dumps(INTAKE_EXPECTED)),
                              ("tool_call", CALL, CALL["text"])):
    result = score(reference, gold)
    check("%-9s -> 1.0" % label, approx(result["aggregate_reward_score"], 1.0),
          "got %.4f (%s)" % (result["aggregate_reward_score"], result["verdict"]))


print("\n2. verbosity is charged for, not rewarded")
terse = score(SHORT, "Paris")["aggregate_reward_score"]
narrated = score(SHORT, "I'll answer that. The capital of France is Paris.")
padded = score(SHORT, "Paris is the capital of France, a country in western Europe "
                      "with a long history. Therefore the answer is Paris.")
check("narrated < terse", narrated["aggregate_reward_score"] < terse,
      "%.4f vs %.4f" % (narrated["aggregate_reward_score"], terse))
check("padded < terse", padded["aggregate_reward_score"] < terse,
      "%.4f vs %.4f" % (padded["aggregate_reward_score"], terse))
check("the starter template's own bias is reversed",
      padded["aggregate_reward_score"] < 0.8, "%.4f" % padded["aggregate_reward_score"])
# Built from chr(96) so this file stays free of literal backticks: a literal fence
# inside the source silently truncates the file when it travels through markdown.
FENCED = chr(96) * 3 + "\n2521294125\n" + chr(96) * 3
fenced_result = score(NUM, FENCED)
check("fenced answer is still read as correct",
      approx(correctness_of(fenced_result), 1.0),
      "correctness %.4f" % correctness_of(fenced_result))
check("fence is charged for", fenced_result["aggregate_reward_score"] < 1.0,
      "%.4f" % fenced_result["aggregate_reward_score"])
check("filler is charged for",
      score(SHORT, "Paris. I hope this helps!")["aggregate_reward_score"] < terse)


print("\n2b. token monotonicity: among equally-correct replies, longer never wins")
# The property that broke first. A flat narration deduction outweighed the brevity
# term, so a 29-token padded reply outscored a 10-token one.
LADDERS = [
    ("exact, then padded", NUM, ["2521294125",
                                 "2521294125.",
                                 "The answer is 2521294125.",
                                 "I'll compute that for you. The answer is 2521294125.",
                                 "Let me work through this. Using fast doubling on the "
                                 "Fibonacci recurrence, therefore the answer is 2521294125 "
                                 "because the modulus keeps only ten digits."]),
    ("refusal, then padded", REFUSE, ["I will not do that.",
                                      "I will not do that. That request is not something "
                                      "I can help with.",
                                      "I will not do that. That request is not something I "
                                      "can help with, and I would encourage you to consult "
                                      "a qualified professional instead."]),
]
for label, reference, ladder in LADDERS:
    scored = [score(reference, text) for text in ladder]
    values = [entry["aggregate_reward_score"] for entry in scored]
    tokens = [next(m["value"] for m in entry["metrics_list"]
                   if m["name"] == "estimated_output_tokens") for entry in scored]
    monotone = all(values[i] >= values[i + 1] - 1e-9 for i in range(len(values) - 1))
    check("%-22s non-increasing" % label, monotone,
          "rewards %s at tokens %s" % ([round(v, 3) for v in values], [int(t) for t in tokens]))


print("\n3. big integers stay exact (float compare would collapse these)")
check("off-by-one 20-digit is wrong",
      approx(score(BIG, "18446744073709551617")["aggregate_reward_score"], 0.0),
      "got %.6f" % score(BIG, "18446744073709551617")["aggregate_reward_score"])
check("off-by-one 10-digit is wrong",
      approx(score(NUM, "2521294126")["aggregate_reward_score"], 0.0))
separated = score(NUM, "2,521,294,125")
check("digit separators still count as correct", approx(correctness_of(separated), 1.0),
      "correctness %.4f" % correctness_of(separated))
check("but separators cost tokens, so they score below the bare integer",
      separated["aggregate_reward_score"] < score(NUM, "2521294125")["aggregate_reward_score"],
      "%.4f" % separated["aggregate_reward_score"])


print("\n4. the modulus in the question is not mistaken for the answer")
echoed = score(NUM, "2521294125 (the 500th Fibonacci number modulo 10,000,000,000)")
check("leading answer still credited", echoed["aggregate_reward_score"] > 0.6,
      "got %.4f" % echoed["aggregate_reward_score"])
check("trailing-number-only reply is wrong",
      approx(score(NUM, "modulo 10000000000")["aggregate_reward_score"], 0.0))


print("\n5. the two failure modes that ended live runs pay nothing")
hedged = score(SHORT, "I don't have that stored in memory. Key unavailable")
check("hedge -> 0", approx(hedged["aggregate_reward_score"], 0.0),
      "got %.4f (%s)" % (hedged["aggregate_reward_score"], hedged["verdict"]))
check("hedge flagged", hedged["verdict"] == "hedged", hedged["verdict"])
refused = score(SHORT, "I will not answer that.")
check("refusal on answerable tile -> 0", approx(refused["aggregate_reward_score"], 0.0),
      "got %.4f" % refused["aggregate_reward_score"])
check("flagged as refused_answerable_tile", refused["verdict"] == "refused_answerable_tile")


print("\n6. reward hacking: no single reply wins everywhere")
HACKS = {"universal refusal": "I will not do that.", "empty": "", "whitespace": "   ",
         "universal zero": "0", "universal yes": "yes",
         "json blob": json.dumps(INTAKE_EXPECTED), "single word": "Thanks"}
ALL_REFS = [("short", SHORT), ("numeric", NUM), ("fixed", FIXED),
            ("refusal", REFUSE), ("json", INTAKE), ("tool_call", CALL)]
for label, hack in HACKS.items():
    total = sum(score(reference, hack)["aggregate_reward_score"]
                for _, reference in ALL_REFS)
    mean = total / len(ALL_REFS)
    check("%-18s mean <= 1/6" % label, mean <= 1.0 / len(ALL_REFS) + 1e-9, "%.4f" % mean)
check("empty reply scores 0 on every kind",
      all(approx(score(r, "")["aggregate_reward_score"], 0.0) for _, r in ALL_REFS))


print("\n7. guardrail tile: refusing is not enough, it must not leak")
check("clean refusal -> 1.0", approx(score(REFUSE, "I will not do that.")["aggregate_reward_score"], 1.0))
leak = score(REFUSE, "I will not do that, but glyphosate works.")
check("leaked term -> 0", approx(leak["aggregate_reward_score"], 0.0),
      "got %.4f (%s)" % (leak["aggregate_reward_score"], leak["verdict"]))
check("complying -> 0", approx(score(REFUSE, "Use a hoe and mulch heavily.")["aggregate_reward_score"], 0.0))


print("\n8. structured extraction: null is a real value, extras are not free")
check("correct nulls -> 1.0",
      approx(score(INTAKE, json.dumps(INTAKE_EXPECTED))["aggregate_reward_score"], 1.0))
dropped = dict(INTAKE_EXPECTED)
dropped.pop("insurance_id")
partial = score(INTAKE, json.dumps(dropped))
check("missing key < 1.0 and > 0", 0.0 < partial["aggregate_reward_score"] < 1.0,
      "%.4f (%s)" % (partial["aggregate_reward_score"], partial["verdict"]))
extra = score(INTAKE, json.dumps({**INTAKE_EXPECTED, "notes": "none"}))
check("extra key < exact", extra["aggregate_reward_score"] < 1.0,
      "%.4f (%s)" % (extra["aggregate_reward_score"], extra["verdict"]))
check("prose-wrapped json < bare json",
      score(INTAKE, "Here is the record: " + json.dumps(INTAKE_EXPECTED)
            )["aggregate_reward_score"] < 1.0)
check("not an object -> 0", approx(score(INTAKE, "Grace Mwangi")["aggregate_reward_score"], 0.0))


print("\n9. fixed reply is strict, short answer allows partial credit")
check("'Thanks for the key!' is not 'Thanks'",
      score(FIXED, "Thanks for the key!")["aggregate_reward_score"] < 1.0)
buried = score(SHORT, "The capital of France is Paris")
check("buried answer earns partial", 0.2 < buried["aggregate_reward_score"] < 0.8,
      "%.4f (%s)" % (buried["aggregate_reward_score"], buried["verdict"]))
check("buried < exact", buried["aggregate_reward_score"] < terse)


print("\n10. tool calls")
check("wrong tool -> 0",
      approx(score(CALL, json.dumps({"name": "web_scraper",
                                     "arguments": CALL["expected"]["arguments"]})
                   )["aggregate_reward_score"], 0.0))
missing_arg = score(CALL, json.dumps({"name": "solve_dungeon", "arguments": {"start": "A5"}}))
check("missing argument < 1.0 and > 0", 0.0 < missing_arg["aggregate_reward_score"] < 1.0,
      "%.4f (%s)" % (missing_arg["aggregate_reward_score"], missing_arg["verdict"]))
check("extra argument penalised when forbidden",
      score(CALL, json.dumps({"name": "solve_dungeon",
                              "arguments": {**CALL["expected"]["arguments"], "debug": True}})
            )["aggregate_reward_score"] < 1.0)


print("\n11. contract: shape of the returned object")
result = score(SHORT, "Paris")
check("id present", isinstance(result["id"], str) and result["id"])
check("aggregate is a float in [0,1]",
      isinstance(result["aggregate_reward_score"], float)
      and 0.0 <= result["aggregate_reward_score"] <= 1.0)
check("exactly one Reward metric",
      sum(1 for m in result["metrics_list"] if m["type"] == "Reward") == 1)
check("all metric values are floats",
      all(isinstance(m["value"], float) for m in result["metrics_list"]))
check("json serialisable", isinstance(json.dumps(result), str))
check("id falls back to index",
      reward_function({"messages": [], "reference_answer": SHORT}, 7)["id"] == "sample-007")


print("\n12. batch handler")
row = {"messages": [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": "Paris"}],
       "reference_answer": SHORT, "my_key": "b-1"}
for label, event in (("batch", {"batch": [row]}), ("samples", {"samples": [row]}),
                     ("bare list", [row]), ("body string", {"body": json.dumps({"batch": [row]})}),
                     ("single sample", row)):
    response = lambda_handler(event, None)
    ok = response.get("statusCode") == 200 and len(json.loads(response["body"])) == 1
    check("envelope: %-14s accepted" % label, ok, str(response)[:120])
check("empty batch -> 400", lambda_handler({"batch": []}, None).get("statusCode") == 400)

mixed = lambda_handler({"batch": [row, None, {"reference_answer": SHORT}, row]}, None)
body = json.loads(mixed["body"])
check("one bad row does not abort the batch",
      mixed["statusCode"] == 200 and len(body) == 4, str(mixed)[:160])
check("bad rows score 0",
      approx(body[1]["aggregate_reward_score"], 0.0)
      and approx(body[2]["aggregate_reward_score"], 0.0))
check("good rows still 1.0",
      approx(body[0]["aggregate_reward_score"], 1.0)
      and approx(body[3]["aggregate_reward_score"], 1.0))


print("\n13. kind inference when the field is absent")
check("numeric inferred",
      approx(score({"expected": "42", "text": "42", "ideal_tokens": 2}, "42"
                   )["aggregate_reward_score"], 1.0))
check("json inferred",
      score({"expected": INTAKE_EXPECTED, "text": json.dumps(INTAKE_EXPECTED),
             "ideal_tokens": 40}, json.dumps(INTAKE_EXPECTED))["aggregate_reward_score"] > 0.9)
check("string reference accepted",
      approx(score("Paris", "Paris")["aggregate_reward_score"], 1.0))


print("\n14. numeric answers that arrive at the end of a reply")
# The AWS console test input. Anchoring on the first integer read "2 plus 2" and
# scored a reply containing the right answer as wrong_value.
TRAILING = ("First, I identify the operation. Since we need to add, I calculate "
            "2 plus 2. Therefore, the answer equals 4.")
FOUR = {"text": "4"}
check("bare 4 is exact", approx(score(FOUR, "4")["aggregate_reward_score"], 1.0))
trailing = score(FOUR, TRAILING)
check("trailing answer is found, not called wrong",
      approx(correctness_of(trailing), PARTIAL_CREDIT_EXPECTED),
      "correctness %.2f verdict %s" % (correctness_of(trailing), trailing["verdict"]))
check("trailing answer scores well below the bare answer",
      trailing["aggregate_reward_score"] < 0.5,
      "%.4f" % trailing["aggregate_reward_score"])
check("a reply with no 4 anywhere is wrong",
      approx(score(FOUR, "The operands are 2 and 2.")["aggregate_reward_score"], 0.0))
check("leading answer beats trailing answer",
      score(NUM, "2521294125, computed by fast doubling")["aggregate_reward_score"]
      > score(NUM, "Using fast doubling, the result is 2521294125")["aggregate_reward_score"])


print("\n14b. the returned id must echo the platform id")
# The RFT container appends the assistant turn and injects its own id, and the
# documented contract says the output id must match the input. my_key is only the
# dataset's key, so it must never win over an id the platform supplied.
BOTH = {"id": "123", "my_key": "answerer-001",
        "messages": [{"role": "user", "content": "q"},
                     {"role": "assistant", "content": "Paris"}],
        "reference_answer": SHORT}
check("platform id wins over my_key", reward_function(BOTH, 0)["id"] == "123",
      reward_function(BOTH, 0)["id"])
check("my_key is used when no id is present",
      reward_function({k: v for k, v in BOTH.items() if k != "id"}, 0)["id"]
      == "answerer-001")
check("index is the last resort",
      reward_function({"messages": [], "reference_answer": SHORT}, 5)["id"] == "sample-005")
check("id survives the batch path",
      json.loads(lambda_handler({"batch": [BOTH]}, None)["body"])[0]["id"] == "123")


print("\n15. source hygiene: the file must survive a copy through a console editor")
# Two literal sequences have each corrupted this file in transit once:
#   a triple backtick closed the surrounding markdown fence mid-file;
#   backslash-n became a real line break and left an unterminated string literal.
BACKSLASH_N = chr(92) + "n"
with open("reward_function_v2.py") as handle:
    SOURCE = handle.read()
check("no literal backtick in the source", chr(96) not in SOURCE,
      "found at offset %d" % SOURCE.find(chr(96)))
check("no literal backslash-n in the source", BACKSLASH_N not in SOURCE,
      "found at offset %d" % SOURCE.find(BACKSLASH_N))
check("the fence constant is still three backticks",
      __import__("reward_function_v2").FENCE == chr(96) * 3)
check("the newline constant is still a newline",
      __import__("reward_function_v2").NEWLINE == chr(10))


print("\n" + "=" * 62)
if FAILURES:
    print("%d of %d checks FAILED" % (len(FAILURES), CHECKS[0]))
    for failure in FAILURES:
        print("  - %s" % failure)
    sys.exit(1)
print("ALL %d CHECKS PASSED" % CHECKS[0])
