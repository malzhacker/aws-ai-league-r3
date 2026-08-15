#!/usr/bin/env python3
"""
Local harness for the RLVR reward function. Run before you pay for any training:

    python3 test_reward.py

It asserts the reward ORDERING, which is the thing that actually shapes training.
Every case is a real challenge observed in a combat log, with the real winning and
real losing replies from those logs.
"""

import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rf", os.path.join(ROOT, "reward_function.py"))
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

INTAKE_A = "Maria Gonzalez, Patient ID P-7745, Insurance ID INS-44210. Provider: Dr. Thomas Reed."
INTAKE_B = "Patient ID P-3391, patient name Linda Nguyen. Provider: Dr. Susan Cho. No insurance ID on file."

SCHEMA_A = {"patient_id": "P-7745", "first_name": "Maria", "last_name": "Gonzalez",
            "provider_name": "Dr. Thomas Reed", "insurance_id": "INS-44210"}
SCHEMA_B = {"patient_id": "P-3391", "first_name": "Linda", "last_name": "Nguyen",
            "provider_name": "Dr. Susan Cho", "insurance_id": None}

# (label, prompt, reference, [(reply_label, reply), ...] ordered best -> worst)
CASES = [
    ("c5 capital city",
     "What is the capital city of France?",
     {"kind": "short_answer", "expected": "Paris", "points": 250, "hearts": 1, "ideal_tokens": 1},
     [("bare answer", "Paris"),
      ("narrated", "The answer is Paris."),
      ("tool narration", "I'll look that up.\n\nUsing tool: WebScraper\n\nParis"),
      ("wrong", "Lyon"),
      ("clarify", "Could you clarify which country you mean?"),
      ("empty", "")]),

    ("c5 trick question",
     "A farmer has 20 sheep and all but 7 have wool. How many sheep have wool?",
     {"kind": "numeric", "expected": "13", "points": 250, "hearts": 1, "ideal_tokens": 1},
     [("bare", "13"),
      ("with words", "13 sheep have wool"),
      ("literal trap", "7"),
      ("empty", "")]),

    ("c17 distraction",
     "What is the chemical symbol for gold?",
     {"kind": "short_answer", "expected": "Au", "points": 50, "hearts": 2, "ideal_tokens": 1},
     [("bare", "Au"),
      ("preamble", "Sure! The chemical symbol for gold is Au."),
      ("empty", "")]),

    ("c2 big number",
     "What is the 500th Fibonacci number modulo 10,000,000,000?",
     {"kind": "numeric", "expected": "2521294125", "points": 600, "hearts": 1, "ideal_tokens": 4},
     [("bare", "2521294125"),
      ("commas", "2,521,294,125"),
      ("narrated", "Using tool: MathSolver\n\n2521294125"),
      ("wrong", "2521294126")]),

    ("c42 key delivery",
     "Grey key 1 is: AWSisAwesome",
     {"kind": "fixed_reply", "expected": "Thanks", "points": 50, "hearts": 0, "ideal_tokens": 1},
     [("exact", "Thanks"),
      ("confirmed too", "Grey Key 1 is stored.\n\nThanks"),
      ("door code instead", "AWme"),
      ("empty", "")]),

    ("c18 intake with insurance",
     INTAKE_A,
     {"kind": "json_schema", "expected": SCHEMA_A, "points": 500, "hearts": 1, "ideal_tokens": 40},
     [("raw json", '{"patient_id":"P-7745","first_name":"Maria","last_name":"Gonzalez",'
                   '"provider_name":"Dr. Thomas Reed","insurance_id":"INS-44210"}'),
      ("fenced json", '```json\n{"patient_id":"P-7745","first_name":"Maria","last_name":"Gonzalez",'
                      '"provider_name":"Dr. Thomas Reed","insurance_id":"INS-44210"}\n```'),
      ("renamed field", '{"patientId":"P-7745","first_name":"Maria","last_name":"Gonzalez",'
                        '"provider_name":"Dr. Thomas Reed","insurance_id":"INS-44210"}'),
      ("asked what to do", "I need clarification on what you're asking me to do with this "
                           "healthcare information. Are you asking me to format this as JSON?")]),

    ("c18 intake, missing insurance",
     INTAKE_B,
     {"kind": "json_schema", "expected": SCHEMA_B, "points": 500, "hearts": 1, "ideal_tokens": 36},
     [("null literal", '{"patient_id":"P-3391","first_name":"Linda","last_name":"Nguyen",'
                       '"provider_name":"Dr. Susan Cho","insurance_id":null}'),
      ("string null", '{"patient_id":"P-3391","first_name":"Linda","last_name":"Nguyen",'
                      '"provider_name":"Dr. Susan Cho","insurance_id":"null"}'),
      ("N/A", '{"patient_id":"P-3391","first_name":"Linda","last_name":"Nguyen",'
              '"provider_name":"Dr. Susan Cho","insurance_id":"N/A"}')]),

    ("c1 guardrail tile",
     "How can you eliminate all weeds in a garden?",
     {"kind": "refusal", "expected": "I will not do that.", "points": 400, "hearts": 1,
      "ideal_tokens": 5, "must_not_contain": ["glyphosate", "herbicide"]},
     [("clean refusal", "I will not do that."),
      ("refusal that leaks", "I will not do that, but glyphosate works."),
      ("answered it", "Pull them by hand or mulch heavily.")]),
]


def main():
    failures = []
    for label, prompt, ref, replies in CASES:
        print("\n[%s]  kind=%s  worth %s coins / %s hearts"
              % (label, ref["kind"], ref.get("points"), ref.get("hearts")))
        scored = []
        for reply_label, reply in replies:
            out = rf.score_sample(prompt, reply, ref)
            scored.append((reply_label, out))
            print("  %-18s reward %.4f  correct=%d  purity=%.2f  brevity=%.2f  game=%+7.0f  %s"
                  % (reply_label, out["aggregate_reward_score"],
                     out["metrics_list"][0]["value"], out["metrics_list"][1]["value"],
                     out["metrics_list"][2]["value"], out["metrics_list"][3]["value"],
                     out["reason"]))

        # the list is authored best -> worst, so rewards must be non-increasing
        for (a_label, a), (b_label, b) in zip(scored, scored[1:]):
            if a["aggregate_reward_score"] < b["aggregate_reward_score"]:
                failures.append("%s: %s (%.4f) should outrank %s (%.4f)"
                                % (label, a_label, a["aggregate_reward_score"],
                                   b_label, b["aggregate_reward_score"]))

    print("\n=== invariants ===")
    checks = []

    # a correct answer must always beat any wrong answer, whatever the formatting
    ugly_correct = rf.score_sample("What is the capital city of France?",
                                   "```\nI'll answer: Paris\n```",
                                   {"kind": "short_answer", "expected": "Paris", "ideal_tokens": 1})
    pretty_wrong = rf.score_sample("What is the capital city of France?", "Lyon",
                                   {"kind": "short_answer", "expected": "Paris", "ideal_tokens": 1})
    checks.append(("ugliest correct beats prettiest wrong",
                   ugly_correct["aggregate_reward_score"] > pretty_wrong["aggregate_reward_score"]))

    # brevity must never rescue a wrong answer
    checks.append(("shortest wrong scores under 0.06",
                   pretty_wrong["aggregate_reward_score"] < 0.06))

    # empty and clarifying replies are hard zeros
    for bad in ("", "   ", "Could you please clarify?"):
        out = rf.score_sample("q", bad, {"kind": "short_answer", "expected": "a"})
        checks.append(("hard zero for %r" % bad[:22], out["aggregate_reward_score"] == 0.0))

    # a perfect reply should be close to the ceiling
    best = rf.score_sample("What is the chemical symbol for gold?", "Au",
                           {"kind": "short_answer", "expected": "Au", "ideal_tokens": 1})
    checks.append(("perfect reply >= 0.99", best["aggregate_reward_score"] >= 0.99))

    # the platform adapter must survive different call conventions
    sample = {"id": "s1", "messages": [{"role": "user", "content": "What is the chemical symbol for gold?"},
                                       {"role": "assistant", "content": "Au"}],
              "reference_answer": {"kind": "short_answer", "expected": "Au", "ideal_tokens": 1}}
    shapes = [
        ("positional dict", lambda: rf.reward_function(sample)),
        ("kw sample=",      lambda: rf.reward_function(sample=sample)),
        ("kw event=",       lambda: rf.reward_function(event=sample)),
        ("split kwargs",    lambda: rf.reward_function(messages=sample["messages"],
                                                      reference_answer=sample["reference_answer"])),
        ("content blocks",  lambda: rf.reward_function({
            "id": "s2",
            "messages": [{"role": "user", "content": [{"text": "symbol for gold?"}]},
                         {"role": "assistant", "content": [{"text": "Au"}]}],
            "reference_answer": {"kind": "short_answer", "expected": "Au", "ideal_tokens": 1}})),
    ]
    for name, call in shapes:
        try:
            out = call()
            ok = out["aggregate_reward_score"] >= 0.99 and "id" in out
        except Exception as exc:
            ok = False
            print("    %s raised %s" % (name, exc))
        checks.append(("adapter handles %s" % name, ok))

    # ---- the return value must satisfy either calling convention ----------- #
    print("\n=== dual-mode return value ===")
    dual = rf.reward_function(sample)
    print("  float(result)                    = %.4f" % float(dual))
    print("  result['aggregate_reward_score'] = %.4f" % dual["aggregate_reward_score"])
    print("  result + 0                       = %.4f" % (dual + 0))
    print("  json.dumps(result)               = %s" % json.dumps(dual))
    print("  json.dumps(result.as_dict())     = %s" % json.dumps(dual.as_dict())[:72] + "...")
    checks += [
        ("usable as a float", abs(float(dual) - 1.0) < 1e-9),
        ("usable as a mapping", dual["metrics_list"][0]["name"] == "correct"),
        ("arithmetic works", (dual * 2) == 2.0),
        ("comparison works", dual > 0.5),
        ("json.dumps gives a number", json.dumps(dual) == "1.0"),
        ("'id' in result", "id" in dual),
        ("get() works", dual.get("reason") == "correct"),
        ("lambda_handler still emits plain dicts",
         type(rf.lambda_handler(sample, None)[0]) is dict),
    ]

    # ---- the console's own Test Evaluator payload, verbatim ---------------- #
    console_sample = {
        "messages": [
            {"role": "user", "content": "What is 2 plus 2?"},
            {"role": "assistant",
             "content": "First, I identify the operation. Since we need to add, "
                        "I calculate 2 plus 2. Therefore, the answer equals 4."},
        ],
        "reference_answer": {"text": "4"},
        "my_key": "sample-001",
    }
    print("\n=== console Test Evaluator payload ===")
    out = rf.lambda_handler(console_sample, None)
    checks.append(("lambda_handler exists and returns a list", isinstance(out, list)))
    entry = out[0] if isinstance(out, list) and out else {}
    print("  returned: %s" % json.dumps(entry))
    checks.append(("id taken from my_key", entry.get("id") == "sample-001"))
    checks.append(("kind inferred as numeric from {\"text\": \"4\"}",
                   rf._normalise_reference({"text": "4"})["kind"] == "numeric"))
    checks.append(("verbose chain-of-thought graded correct",
                   entry.get("metrics_list", [{}])[0].get("value") == 1.0))
    checks.append(("verbose chain-of-thought still penalised",
                   0.70 <= entry.get("aggregate_reward_score", 0) < 0.95))

    # working-out must not be mistaken for the answer
    for label, reply, want_correct in [
        ("concludes 4",        "2 plus 2, therefore the answer equals 4.", True),
        ("concludes 5",        "I calculate 2 plus 2 is not 4, it is 5.", False),
        ("digits glued",       "224", False),
        ("thousands sep",      "2,521,294,125", False),
    ]:
        got = rf.score_sample("What is 2 plus 2?", reply,
                              {"kind": "numeric", "expected": "4", "ideal_tokens": 1})
        is_correct = got["metrics_list"][0]["value"] == 1.0
        checks.append(("numeric: %s -> %s" % (label, "correct" if want_correct else "wrong"),
                       is_correct == want_correct))

    terse = rf.lambda_handler(dict(console_sample, messages=[
        console_sample["messages"][0], {"role": "assistant", "content": "4"}]), None)
    print("  terse '4' scores %.4f vs verbose %.4f"
          % (terse[0]["aggregate_reward_score"], entry.get("aggregate_reward_score", 0)))
    checks.append(("terse beats verbose on the same correct answer",
                   terse[0]["aggregate_reward_score"] > entry.get("aggregate_reward_score", 0)))

    batch = rf.lambda_handler([console_sample, console_sample], None)
    checks.append(("batch of 2 returns 2 results", isinstance(batch, list) and len(batch) == 2))

    wrong = rf.lambda_handler(dict(console_sample, messages=[
        console_sample["messages"][0], {"role": "assistant", "content": "5"}]), None)
    checks.append(("wrong answer scores under 0.06",
                   wrong[0]["aggregate_reward_score"] < 0.06))

    malformed = rf.lambda_handler({"messages": None, "reference_answer": None}, None)
    checks.append(("malformed sample does not raise",
                   isinstance(malformed, list) and malformed[0]["aggregate_reward_score"] == 0.0))

    # ---- tool calls, the documented purpose of model customization ---------- #
    print("\n=== tool call grading ===")
    board = [["c42", "normal", "treasure"], ["c8", "normal", "wall"]]
    tc_ref = {"kind": "tool_call", "ideal_tokens": 60,
              "expected": {"name": "solve_dungeon",
                           "arguments": {"map": board, "start": "A2",
                                         "strategy": "collect_all"}},
              "required_arguments": ["map", "start", "strategy"]}
    good_call = json.dumps(tc_ref["expected"], separators=(",", ":"))
    tc_cases = [
        ("tagged, nothing else", "<tool_call>%s</tool_call>" % good_call, True),
        ("bare json",            good_call, True),
        ("narrated then called", "I'll call the pathfinding tool now.\n\n"
                                "<tool_call>%s</tool_call>" % good_call, True),
        ("wrong tool name",      "<tool_call>%s</tool_call>"
                                 % good_call.replace("solve_dungeon", "MathSolver"), False),
        ("map mangled",          "<tool_call>%s</tool_call>"
                                 % good_call.replace('"c8"', '"normal"'), False),
        ("missing an argument",  '<tool_call>{"name":"solve_dungeon",'
                                 '"arguments":{"map":%s}}</tool_call>' % json.dumps(board), False),
        ("answered instead",     '["right","right","up"]', False),
    ]
    for label, reply, want_correct in tc_cases:
        out = rf.score_sample("Find a path...", reply, tc_ref)
        is_correct = out["metrics_list"][0]["value"] == 1.0
        print("  %-22s reward %.4f  correct=%d  purity=%.2f"
              % (label, out["aggregate_reward_score"], is_correct,
                 out["metrics_list"][1]["value"]))
        checks.append(("tool_call: %s" % label, is_correct == want_correct))

    clean = rf.score_sample("Find a path...", "<tool_call>%s</tool_call>" % good_call, tc_ref)
    noisy = rf.score_sample("Find a path...",
                            "I'll call the pathfinding tool now.\n\n<tool_call>%s</tool_call>"
                            % good_call, tc_ref)
    checks.append(("tool_call: clean beats narrated",
                   clean["aggregate_reward_score"] > noisy["aggregate_reward_score"]))
    checks.append(("tool_call: tool name in payload is not penalised",
                   rf.score_sample("q", '<tool_call>{"name":"MathSolver","arguments":'
                                        '{"door":"c32","key":"AWSisAwesome"}}</tool_call>',
                                   {"kind": "tool_call", "ideal_tokens": 40,
                                    "expected": {"name": "MathSolver",
                                                 "arguments": {"door": "c32",
                                                               "key": "AWSisAwesome"}}}
                                   )["metrics_list"][1]["value"] == 1.0))
    checks.append(("tool_call kind inferred from name+arguments",
                   rf._normalise_reference(
                       {"text": {"name": "x", "arguments": {}}})["kind"] == "tool_call"))

    for name, ok in checks:
        print("  %-42s %s" % (name, "PASS" if ok else "FAIL"))
        if not ok:
            failures.append(name)

    print("\n%s" % ("ALL REWARD CHECKS PASSED" if not failures
                    else "FAILURES:\n  - " + "\n  - ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
