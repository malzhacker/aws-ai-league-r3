"""
RLVR reward function for the AWS AI League dungeon agent.

Shape follows the AWS starter template: reward_function(sample, index) returns
{id, aggregate_reward_score, metrics_list}, and lambda_handler maps a batch.
Everything inside is rebuilt for this game.

Why it differs from the starter template
----------------------------------------
The template's compute_reasoning_quality pays out for length (len > 120) and
for connectives such as "because" and "therefore". In this game the only cost
term in the score is output tokens:

    tokenBonus = 1000 - avgTokens x (1 - penaltyReduction)

so that reward trains the model to throw away points. Brevity is inverted here
and measured against the ideal_tokens recorded on each sample.

Four further corrections:

  * float() on the reference answer is lossy past 2^53, and this board asks for
    "the 500th Fibonacci number modulo 10,000,000,000". Integers are compared as
    integers, never as floats.
  * "return the last number found" picks the modulus out of that same question.
    Answers are graded by kind, anchored at the start of the reply.
  * Style terms are gated behind correctness, so a well-formatted wrong answer
    scores zero instead of collecting the presentation weight.
  * One malformed row no longer aborts the whole batch.

Verification is fully deterministic: no model judges another model, which is what
makes the signal RLVR-valid.
"""

import json
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Weights. They sum to 1.0, and the style terms are multiplied by correctness.
# --------------------------------------------------------------------------- #

W_CORRECT = 0.60
W_BREVITY = 0.30
W_FORMAT = 0.10

# Narration is charged as the tokens it wastes rather than as a flat deduction. A
# flat deduction outweighed the brevity term, which let a 29-token padded reply
# outscore a 10-token one: the exact lesson this reward must never teach.
NARRATION_TOKEN_SURCHARGE = 8
GRACE_TOKENS = 2            # slack before brevity starts to decay
PARTIAL_CREDIT = 0.5        # right answer, buried in prose


# =========================================================================== #
# SECTION 1: normalisation and token accounting
# =========================================================================== #

# Every literal character this module needs is built with chr(). Escape sequences
# such as backslash-n do not survive every paste path into a console editor: one
# of them turned into a real line break and left this file with an unterminated
# string literal at import time. chr() cannot be mangled that way.
BACKTICK = chr(96)
FENCE = BACKTICK * 3
NEWLINE = chr(10)
CARRIAGE_RETURN = chr(13)
TAB = chr(9)
NUL = chr(0)
QUOTE = chr(34)
APOSTROPHE = chr(39)

# Curly quotes and dashes, folded to their ASCII equivalents before comparison.
_UNICODE_FOLD = (
    (chr(0x2018), APOSTROPHE), (chr(0x2019), APOSTROPHE),
    (chr(0x201C), QUOTE), (chr(0x201D), QUOTE),
    (chr(0x2013), "-"), (chr(0x2014), "-"),
)

# Edge characters stripped before comparison. Hyphen and plus are absent on
# purpose: stripping them would corrupt negative answers.
_PUNCT_EDGE = (" " + TAB + CARRIAGE_RETURN + NEWLINE + ".,;:!?*_()[]{}<>"
               + QUOTE + APOSTROPHE + BACKTICK)


def normalize(text: Any) -> str:
    """Casefold, fold unicode punctuation, collapse whitespace, strip edges.

    Deliberately conservative: it does not drop interior words, so "Paris" and
    "Paris is the capital" stay distinguishable. That distinction is what the
    partial-credit path relies on.
    """
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    for fancy, plain in _UNICODE_FOLD:
        text = text.replace(fancy, plain)
    text = re.sub(r"\s+", " ", text)
    return text.strip(_PUNCT_EDGE).casefold()


def strip_wrapper(text: str) -> str:
    """Remove markdown fences and a leading 'Answer:' label."""
    if not text:
        return ""
    text = re.sub(r"^\s*" + FENCE + r"[a-zA-Z0-9_+-]*\s*", "", text)
    text = re.sub(r"\s*" + FENCE + r"\s*$", "", text)
    text = re.sub(r"^\s*(?:the\s+)?(?:final\s+)?(?:answer|result|solution|output)\s*[:=]\s*",
                  "", text, flags=re.I)
    return text.strip()


_WORD_RE = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")


def estimate_tokens(text: str) -> int:
    """Approximate BPE output length.

    An approximation, not a tokenizer: roughly four characters per word-piece and
    three digits per numeric piece, one token per standalone symbol. It only ever
    feeds a *ratio* against ideal_tokens, so a constant bias cancels out.
    """
    if not text or not text.strip():
        return 0
    total = 0
    for piece in _WORD_RE.findall(text):
        if piece.isdigit():
            total += max(1, math.ceil(len(piece) / 3))
        elif piece.isalpha():
            total += max(1, math.ceil(len(piece) / 4))
        else:
            total += 1
    return max(1, total)


def brevity_score(response: str, ideal_tokens: int, gold: Optional[str] = None,
                  charged_tokens: Optional[int] = None) -> Tuple[float, int]:
    """1.0 up to the allowance, then decays as allowance/actual.

    The allowance is the larger of ideal_tokens and the reference answer's own
    length. Without that floor the reward penalises the very text it is teaching:
    a 20-digit integer and a five-field JSON object both cost more tokens than a
    hand-written ideal_tokens estimate suggests.
    """
    used = estimate_tokens(response) if charged_tokens is None else charged_tokens
    if used == 0:
        return 0.0, 0
    allowance = max(1, int(ideal_tokens or 1))
    if gold:
        allowance = max(allowance, estimate_tokens(gold))
    allowance += GRACE_TOKENS
    if used <= allowance:
        return 1.0, used
    return round(allowance / used, 6), used


# A number introduced by a conclusion keyword. Used only after an exact and a
# leading match have both failed, so it cannot override a clean answer.
_KEYED_NUMBER_RE = re.compile(
    r"(?:answer|result|solution|total|output|equals?|equal\s+to|is|are|=|:)"
    r"\s*(?:is\s+|to\s+|be\s+)?([-+]?\d[\d,_]*)", re.I)


def _to_int(text: Any) -> Optional[int]:
    """Parse an integer, tolerating digit separators. None if not purely an int."""
    stripped = re.sub(r"[,_\s]", "", str(text).strip())
    try:
        return int(stripped)
    except ValueError:
        return None


def extract_integer(text: str) -> Optional[int]:
    """First integer in the reply, digit separators removed, exact.

    Anchored at the front because a correct reply to these tiles *starts* with the
    answer. Scanning from the end would return the modulus quoted in the question.
    """
    if not text:
        return None
    cleaned = strip_wrapper(text)
    match = re.search(r"[-+]?\d[\d,_\s]*", cleaned)
    if not match:
        return None
    digits = re.sub(r"[,_\s]", "", match.group(0))
    try:
        return int(digits)
    except ValueError:
        return None


def extract_number(text: str) -> Optional[float]:
    """Float fallback for non-integral numeric answers only."""
    if not text:
        return None
    match = re.search(r"[-+]?\d[\d,_]*(?:\.\d+)?", strip_wrapper(text))
    if not match:
        return None
    try:
        return float(re.sub(r"[,_]", "", match.group(0)))
    except ValueError:
        return None


def extract_json(text: str) -> Optional[Any]:
    """Parse the first balanced JSON object or array in the reply."""
    if not text:
        return None
    candidate = strip_wrapper(text)
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start < 0:
            continue
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:index + 1])
                    except ValueError:
                        break
    return None


# =========================================================================== #
# SECTION 2: behavioural detectors
# =========================================================================== #

# Announced intent, by name. "No narration" as a rule does not catch these; each
# one below was observed in a real run that leaked tokens.
NARRATION_RE = re.compile(
    r"^\s*(?:"
    # (?!\s*not\b) matters: "I will not do that." is the correct reply on a
    # guardrail tile, and must not be read as announced intent.
    r"i'?ll\b(?!\s*not\b)|i\s+will\b(?!\s*not\b)|i\s+need\s+to\b|"
    r"i'?m\s+going\s+to\b|i\s+should\b|"
    r"let\s+me\b|let'?s\b|first,?\s+i\b|now\s+i\b|"
    r"sure[,!.]|certainly[,!.]|of\s+course[,!.]|okay[,!.]|alright[,!.]|"
    r"here\s+(?:is|are)\b|here'?s\b|based\s+on\b|according\s+to\s+my\b|"
    r"to\s+(?:answer|solve|find|do)\s+this\b|the\s+answer\s+to\s+your\b"
    r")", re.I)

FILLER_RE = re.compile(
    r"\b(?:i\s+hope\s+this\s+helps|let\s+me\s+know\s+if|feel\s+free\s+to|"
    r"as\s+an\s+ai|i\s+apolog|great\s+question)\b", re.I)

# Declining a task, for the guardrail tiles. Deliberately narrower than "any
# negative sentence": it keys on the verb that follows, because "I don't provide
# that" is a refusal while "I don't have that" is the hedge defined below.
_GIVE_UP_VERBS = r"find|locate|retrieve|determine|recall|remember|see|know|have"
REFUSAL_RE = re.compile(
    r"\b(?:i\s+will\s+not\b|i\s+won'?t\b|i\s+refuse\b|refuse\s+to\b|"
    r"i\s+(?:must\s+)?decline\b|"
    r"i\s+do\s*n[o']?t\s+(?:provide|assist|help|answer|do|engage)\b|"
    r"i\s+cannot\s+(?!" + _GIVE_UP_VERBS + r")|"
    r"i\s+can\s*n[o']?t\s+(?!" + _GIVE_UP_VERBS + r")|"
    r"i'?m\s+(?:not\s+able|unable)\s+to\s+(?:help|assist|provide|answer|do)\b|"
    r"un(?:able)?\s*to\s+(?:help|assist|provide)\b)", re.I)

# Giving up when an answer was available. This exact shape ended a live run:
# "I don't have the yellow key stored in memory." then a wrong guess, -5, dead.
HEDGE_RE = re.compile(
    r"\b(?:i\s+do\s*n[o']?t\s+(?:have|know|see|recall|remember)|"
    r"not\s+(?:stored|available|found|in\s+memory)|unavailable|"
    r"no\s+(?:record|memory|entry)\s+of|"
    r"(?:could|can)\s*n[o']?t\s+(?:" + _GIVE_UP_VERBS + r")|"
    r"unsure|not\s+sure)\b", re.I)


def has_narration(response: str) -> bool:
    return bool(NARRATION_RE.search(response or "")) or bool(FILLER_RE.search(response or ""))


def is_refusal(response: str) -> bool:
    return bool(REFUSAL_RE.search(response or ""))


def is_hedge(response: str) -> bool:
    return bool(HEDGE_RE.search(response or ""))


def format_score(response: str, kind: str) -> float:
    """Shape compliance, independent of whether the content is right."""
    if not response or not response.strip():
        return 0.0
    score = 1.0
    if response != response.strip():
        score -= 0.1
    if FENCE in response:
        score -= 0.4
    if FILLER_RE.search(response):
        score -= 0.3
    if kind in ("json_schema", "tool_call"):
        if extract_json(response) is None:
            score -= 0.6
        elif strip_wrapper(response)[:1] not in ("{", "["):
            score -= 0.3          # valid JSON, but wrapped in prose
    else:
        if response.count(NEWLINE) > 1:
            score -= 0.2
        if len(re.findall(r"[.!?]", response)) > 1:
            score -= 0.2          # more than one sentence for a one-word tile
    return max(0.0, min(1.0, score))


# =========================================================================== #
# SECTION 3: verifiers, one per reference kind
# =========================================================================== #

def verify_numeric(response: str, reference: Dict[str, Any]) -> Tuple[float, str]:
    expected_raw = str(reference.get("expected", reference.get("text", ""))).strip()
    expected_int = extract_integer(expected_raw)
    if expected_int is not None and re.fullmatch(r"[-+]?[\d,_\s]+", expected_raw):
        cleaned = strip_wrapper(response)
        if not re.search(r"\d", cleaned):
            return 0.0, "no_number"

        # Tier 1: the reply is the number and nothing else.
        if _to_int(cleaned) == expected_int:
            return 1.0, "exact"

        # Tier 2: the reply opens with the number, then rambles. The answer is
        # unambiguous, so it is fully correct; brevity charges for the rambling.
        leading = re.match(r"\s*([-+]?\d[\d,_\s]*)", cleaned)
        if leading and _to_int(leading.group(1)) == expected_int:
            return 1.0, "leading"

        # Tier 3: the number sits after a conclusion keyword, as in "therefore the
        # answer equals 4". The last such match wins, because the conclusion comes
        # last. Anchoring at the front would read "2 plus 2" and call this wrong.
        keyed = _KEYED_NUMBER_RE.findall(cleaned)
        if keyed and _to_int(keyed[-1]) == expected_int:
            return PARTIAL_CREDIT, "buried_in_prose"

        # Tier 4: the number appears somewhere as a standalone token. Weaker
        # evidence, same partial credit.
        if any(_to_int(token) == expected_int
               for token in re.findall(r"[-+]?\d[\d,_]*", cleaned)):
            return PARTIAL_CREDIT, "buried_in_prose"

        return 0.0, "wrong_value"
    expected_float = extract_number(expected_raw)
    actual_float = extract_number(response)
    if expected_float is None or actual_float is None:
        return 0.0, "no_number"
    if abs(actual_float - expected_float) <= 1e-9 * max(1.0, abs(expected_float)):
        return 1.0, "exact"
    return 0.0, "wrong_value"


def verify_text(response: str, reference: Dict[str, Any], strict: bool) -> Tuple[float, str]:
    """Shared by short_answer (strict=False) and fixed_reply (strict=True)."""
    candidates = [reference.get("expected", reference.get("text", ""))]
    candidates.extend(reference.get("accept") or [])
    stripped = normalize(strip_wrapper(response))
    for candidate in candidates:
        target = normalize(candidate)
        if not target:
            continue
        if stripped == target:
            return 1.0, "exact"
    if strict:
        return 0.0, "not_exact"
    for candidate in candidates:
        target = normalize(candidate)
        if target and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(target), stripped):
            return PARTIAL_CREDIT, "buried_in_prose"
    return 0.0, "wrong_value"


def verify_refusal(response: str, reference: Dict[str, Any]) -> Tuple[float, str]:
    if not is_refusal(response):
        return 0.0, "complied"
    low = (response or "").casefold()
    for banned in reference.get("must_not_contain") or []:
        if str(banned).casefold() in low:
            return 0.0, "leaked_%s" % banned
    return 1.0, "refused"


class _Missing:
    """Distinguishes an absent key from a key whose value is null."""


_MISSING = _Missing()


def _flatten(value: Any) -> str:
    if value is _MISSING:
        return NUL + "missing"
    if value is None:
        return NUL + "null"
    if isinstance(value, bool):
        return NUL + ("true" if value else "false")
    if isinstance(value, (dict, list)):
        return normalize(json.dumps(value, sort_keys=True))
    return normalize(value)


def verify_json_schema(response: str, reference: Dict[str, Any]) -> Tuple[float, str]:
    """Per-field credit, but a missing or extra key is never free."""
    expected = reference.get("expected")
    if isinstance(expected, str):
        expected = extract_json(expected)
    if not isinstance(expected, dict):
        return 0.0, "bad_reference"
    actual = extract_json(response)
    if not isinstance(actual, dict):
        return 0.0, "not_an_object"

    lowered = {str(k).strip().casefold(): v for k, v in actual.items()}
    matched = 0
    for key, value in expected.items():
        # A key that is present and legitimately null must count as a match. The
        # intake tile carries records with no insurance or no patient ID, so
        # null is the correct value there, not a missing answer.
        got = lowered.get(str(key).strip().casefold(), _MISSING)
        if got is not _MISSING and _flatten(got) == _flatten(value):
            matched += 1
    extra = len(lowered) - len(expected)
    fraction = matched / float(len(expected))
    if matched == len(expected) and extra <= 0:
        return 1.0, "exact"
    if extra > 0:
        fraction *= max(0.0, 1.0 - 0.25 * extra)
    return round(min(fraction, 0.95), 6), "fields_%d_of_%d" % (matched, len(expected))


def verify_tool_call(response: str, reference: Dict[str, Any]) -> Tuple[float, str]:
    expected = reference.get("expected") or {}
    if isinstance(expected, str):
        expected = extract_json(expected) or {}
    actual = extract_json(response)
    if not isinstance(actual, dict):
        return 0.0, "not_a_call"

    def pick(obj, *names):
        for name in names:
            if name in obj:
                return obj[name]
        return None

    want_name = normalize(pick(expected, "name", "tool", "function") or "")
    got_name = normalize(pick(actual, "name", "tool", "function") or "")
    if want_name and got_name != want_name:
        return 0.0, "wrong_tool"

    want_args = pick(expected, "arguments", "args", "parameters", "input") or {}
    got_args = pick(actual, "arguments", "args", "parameters", "input") or {}
    if not isinstance(want_args, dict) or not isinstance(got_args, dict):
        return (0.5, "no_arguments") if want_name else (0.0, "bad_arguments")

    required = reference.get("required_arguments") or list(want_args.keys())
    lowered = {str(k).casefold(): v for k, v in got_args.items()}
    def argument_matches(key: Any) -> bool:
        # Sentinels on both sides, so two absent arguments never match each other.
        got = lowered.get(str(key).casefold(), _MISSING)
        want = want_args.get(key, _MISSING)
        if got is _MISSING or want is _MISSING:
            return False
        return _flatten(got) == _flatten(want)

    matched = sum(1 for key in required if argument_matches(key))
    if not required:
        return 1.0, "name_only"
    fraction = matched / float(len(required))
    if reference.get("forbid_extra_arguments"):
        extra = [k for k in lowered if k not in {str(r).casefold() for r in required}]
        if extra:
            fraction *= max(0.0, 1.0 - 0.25 * len(extra))
    if fraction >= 1.0:
        return 1.0, "exact"
    return round(fraction * 0.9, 6), "args_%d_of_%d" % (matched, len(required))


VERIFIERS = {
    "numeric": verify_numeric,
    "short_answer": lambda r, ref: verify_text(r, ref, strict=False),
    "fixed_reply": lambda r, ref: verify_text(r, ref, strict=True),
    "refusal": verify_refusal,
    "json_schema": verify_json_schema,
    "tool_call": verify_tool_call,
}


def infer_kind(reference: Dict[str, Any]) -> str:
    """Fall back to the reference's shape when kind is absent."""
    expected = reference.get("expected", reference.get("text"))
    if isinstance(expected, dict):
        return "tool_call" if {"name", "arguments"} & set(expected) else "json_schema"
    text = str(expected or "")
    if is_refusal(text):
        return "refusal"
    if re.fullmatch(r"[-+]?[\d,_\s]*\d[\d,_\s]*", text.strip()):
        return "numeric"
    return "short_answer"


# =========================================================================== #
# SECTION 4: reward function
# =========================================================================== #

def reward_function(sample: Dict[str, Any], index: int) -> Dict[str, Any]:
    messages = sample.get("messages") or []
    reference = sample.get("reference_answer") or {}
    if isinstance(reference, str):
        reference = {"expected": reference, "text": reference}

    response = ""
    for message in messages:
        role = str(message.get("role", "")).casefold()
        if role == "assistant":
            response = message.get("content") or ""
    if not response:
        response = sample.get("completion") or sample.get("response") or ""
    if isinstance(response, list):        # content-block form
        response = " ".join(part.get("text", "") for part in response
                            if isinstance(part, dict))
    response = str(response)

    kind = str(reference.get("kind") or "").strip().casefold() or infer_kind(reference)
    verifier = VERIFIERS.get(kind, VERIFIERS["short_answer"])
    correctness, detail = verifier(response, reference)

    # ---- hard gates ------------------------------------------------------- #
    # A refusal on an answerable tile, or a hedge instead of an answer, are the
    # two failure modes that ended real runs. Neither may collect any weight.
    # Hedging is tested first: "I don't have it" reads as a near-refusal, but the
    # accurate diagnosis is the one that names the behaviour to train away.
    hedged = kind != "refusal" and is_hedge(response)
    refused_wrongly = kind != "refusal" and not hedged and is_refusal(response)
    if hedged and correctness < 1.0:
        correctness, detail = 0.0, "hedged"
    elif refused_wrongly:
        correctness, detail = 0.0, "refused_answerable_tile"

    ideal_tokens = int(reference.get("ideal_tokens") or 8)
    gold = reference.get("text")
    if not isinstance(gold, str):
        gold = json.dumps(gold) if gold is not None else ""
    shape = format_score(response, kind)
    # A correct refusal is allowed to say "I will not"; only unearned narration
    # is charged for.
    narration = has_narration(response) and not (kind == "refusal" and correctness >= 1.0)

    tokens_used = estimate_tokens(response)
    effective_tokens = tokens_used + (NARRATION_TOKEN_SURCHARGE if narration else 0)
    brevity, _ = brevity_score(response, ideal_tokens, gold, effective_tokens)

    # Every term is multiplied by correctness, so style and brevity pay out only
    # in proportion to being right. A beautifully formatted wrong answer earns
    # nothing, and brevity can never be farmed by answering tersely and wrongly.
    aggregate = correctness * (W_CORRECT + W_BREVITY * brevity + W_FORMAT * shape)
    aggregate = max(0.0, min(1.0, aggregate))

    metrics: List[Dict[str, Any]] = [
        {"name": "correctness", "value": float(correctness), "type": "Reward"},
        {"name": "brevity", "value": float(brevity), "type": "Metric"},
        {"name": "format_compliance", "value": float(shape), "type": "Metric"},
        {"name": "narration_free", "value": 0.0 if narration else 1.0, "type": "Metric"},
        {"name": "estimated_output_tokens", "value": float(tokens_used), "type": "Metric"},
        {"name": "token_overrun", "value": float(max(0, tokens_used - ideal_tokens)),
         "type": "Metric"},
        {"name": "answer_present", "value": 1.0 if response.strip() else 0.0, "type": "Metric"},
        {"name": "refused_answerable_tile", "value": 1.0 if refused_wrongly else 0.0,
         "type": "Metric"},
        {"name": "hedged", "value": 1.0 if hedged else 0.0, "type": "Metric"},
        # Board points this reply would have banked, so a training curve can be
        # read in the game's own units instead of in normalised reward.
        {"name": "board_points",
         "value": float(reference.get("points", 0)) * (1.0 if correctness >= 1.0 else 0.0),
         "type": "Metric"},
    ]

    return {
        "id": str(sample.get("my_key") or sample.get("id") or "sample-%03d" % index),
        "aggregate_reward_score": float(round(aggregate, 6)),
        "metrics_list": metrics,
        "verdict": detail,
    }


# =========================================================================== #
# SECTION 5: Lambda entry point
# =========================================================================== #

def _extract_batch(event: Any) -> List[Dict[str, Any]]:
    """Accept every batch envelope the training service may send."""
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except ValueError:
            return []
    if isinstance(event, list):
        return event
    if not isinstance(event, dict):
        return []
    for key in ("batch", "samples", "records", "input", "inputs", "data"):
        value = event.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_batch(value)
            if nested:
                return nested
    body = event.get("body")
    if body is not None:
        return _extract_batch(body)
    if "messages" in event:
        return [event]
    return []


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    try:
        batch = _extract_batch(event)
        if not batch:
            return {"statusCode": 400,
                    "body": json.dumps({"error": "Missing or empty batch"})}

        results = []
        for index, sample in enumerate(batch):
            # Per-sample isolation. The starter template returns on the first
            # exception, which discards an entire training batch over one row.
            try:
                results.append(reward_function(sample, index))
            except Exception as exc:                      # noqa: BLE001
                results.append({
                    "id": str((sample or {}).get("my_key") or "sample-%03d" % index),
                    "aggregate_reward_score": 0.0,
                    "metrics_list": [{"name": "correctness", "value": 0.0, "type": "Reward"}],
                    "verdict": "error: %s: %s" % (type(exc).__name__, exc),
                })

        return {"statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(results)}
    except Exception as exc:                              # noqa: BLE001
        return {"statusCode": 400, "body": json.dumps({"error": str(exc)})}


if __name__ == "__main__":
    import sys

    paths = sys.argv[1:] or ["dataset_answerer_rft.jsonl"]
    for path in paths:
        with open(path) as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        # Score each row's own reference as if the model had emitted it: a
        # correct dataset must self-score at 1.0.
        total = 0.0
        for index, row in enumerate(rows):
            reference = row["reference_answer"]
            gold = reference.get("text")
            if not isinstance(gold, str):
                gold = json.dumps(gold)
            probe = {**row, "messages": list(row["messages"]) +
                     [{"role": "assistant", "content": gold}]}
            scored = reward_function(probe, index)
            total += scored["aggregate_reward_score"]
            flag = "" if scored["aggregate_reward_score"] >= 0.999 else "   <-- below 1.0"
            print("%-18s %.4f  %s%s" % (scored["id"], scored["aggregate_reward_score"],
                                        scored["verdict"], flag))
        print("%s: mean %.4f over %d rows" % (path, total / max(1, len(rows)), len(rows)))
        print("")
