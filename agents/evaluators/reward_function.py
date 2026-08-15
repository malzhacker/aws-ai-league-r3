"""
RLVR reward function for the AWS AI League custom model.

Paste this whole file into the "Function code" editor (Method: Code). It defines
BOTH reward_function() and lambda_handler(), because pasting over the template
removes the template's handler and the runtime then fails with:

    Handler 'lambda_handler' missing on module 'lambda_function'

REFERENCE ANSWER FORMAT
-----------------------
The platform's own sample uses a `text` field:

    "reference_answer": {"text": "4"}

That is supported as-is, and the task kind is inferred from the value. Richer
grading is available by adding fields, which is what dataset.jsonl does:

    "reference_answer": {
        "text": "Paris",          # what the platform shows
        "kind": "short_answer",   # short_answer | numeric | json_schema
                                  # | fixed_reply | refusal
        "expected": "Paris",      # optional, defaults to `text`
        "accept": ["paris"],      # optional alternatives
        "points": 250,            # in-game coins, for the monitoring metric
        "hearts": 1,              # in-game heart cost of getting it wrong
        "ideal_tokens": 2         # length the brevity term treats as free
    }

WHY THIS SHAPE
--------------
The reward mirrors the game's own economics, which are known exactly from six runs:

    totalScore = coins + lifeBonus + tokenBonus + treasureBonus
    tokenBonus = (1000 - avgTokensPerChallenge) + customModelBonus
    customModelBonus = round(avg / 2)    when a custom model ran

So a correct answer is worth 250-1000 coins plus 250 per heart preserved, while
every saved token is worth about half a point. Correctness therefore has to
dominate, and brevity may only ever be a tie-breaker. A reward that traded
accuracy for shortness would train the model straight into losing tiles.

Consequences baked into the scoring below:

  * correctness is BINARY. The game pays nothing for "close", so partial credit
    would teach a lie.
  * a wrong answer still earns a sliver of credit for clean formatting, purely to
    keep a usable gradient among failures. The gap to a correct answer is ~14x.
  * an empty reply scores a hard zero. An empty reply is submitted verbatim as the
    answer and has already cost 2 hearts in a real run.
  * asking a clarifying question scores a hard zero. Also observed, also cost
    1000 coins and 2 hearts.
"""

import json
import re
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Weights. correct >> everything else, on purpose.
# --------------------------------------------------------------------------- #

W_CORRECT = 0.70
W_PURITY = 0.18
W_BREVITY = 0.12
W_WRONG_RESIDUAL = 0.05   # max credit a wrong answer can earn, for formatting only

# What lambda_handler returns. The AWS docs show two shapes on the same page, so
# this is a one-word switch if the runtime rejects the default.
#   "list"   -> [ {id, aggregate_reward_score, metrics_list}, ... ]   (documented contract)
#   "single" -> the bare dict when a single sample came in
#   "http"   -> {"statusCode": 200, "body": "<json list>"}           (LLM-judge example)
RESPONSE_ENVELOPE = "list"

# Rough token estimate. Good enough for a monotonic brevity signal.
CHARS_PER_TOKEN = 4

NARRATION_OPENERS = (
    "i'll", "i will", "i need to", "i have to", "let me", "looking at",
    "i have ", "i am going to", "i'm going to", "sure", "certainly", "of course",
    "here is", "here's", "based on", "to answer", "first,", "okay", "ok,",
    "the answer is", "i can", "i would", "as requested", "understood",
)

TOOL_MENTIONS = re.compile(
    r"\b(using tool|tool call|calling|invoke|lambda|mathsolver|webscraper|"
    r"pathfinding|i'?ll call|let me call)\b", re.I)

CLARIFY = re.compile(
    r"(could you (please )?(clarify|provide|specify)|please (clarify|provide|specify)|"
    r"what (exactly )?(do|would) you (mean|like|want)|can you clarify|"
    r"need (more )?(clarification|information|details)|are you asking|"
    r"which of the following|i'?m not sure what you|outside (of )?my|"
    r"doesn'?t match any of those)", re.I)

REFUSAL = re.compile(
    r"(i will not|i won'?t|i cannot|i can'?t|unable to|not able to|"
    r"i must decline|i'?m not able)", re.I)

FENCE = re.compile(r"```")
LIST_MARKER = re.compile(r"^\s*(\d+[.)]\s|[-*+]\s)", re.M)
BOLD = re.compile(r"\*\*|__")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text or "") / CHARS_PER_TOKEN))


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for lenient compare."""
    text = (text or "").strip().lower()
    text = re.sub(r"[`*_#>]", "", text)
    text = re.sub(r"[\s]+", " ", text)
    return text.strip(" .!,;:\"'()[]")


def _digits_only(text: str) -> str:
    return re.sub(r"[^\d]", "", text or "")


def _extract_json(text: str) -> Optional[Any]:
    """Parse a JSON object out of a reply, tolerating fences so purity can score it."""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        raw = match.group(0)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _purity(reply: str, prompt: str, kind: str, ref: Optional[Dict[str, Any]] = None) -> float:
    """1.0 for a bare answer. Deductions for anything the game does not pay for."""
    text = reply or ""
    low = text.strip().lower()
    score = 1.0

    # For a tool-call task the tool name belongs in the payload, so narration is
    # only looked for in whatever sits OUTSIDE the call.
    narration_scope = text
    if kind == "tool_call":
        narration_scope = TOOL_CALL_TAG.sub("", text)

    if FENCE.search(text):
        score -= 0.40
    if BOLD.search(text):
        score -= 0.15
    if LIST_MARKER.search(text):
        score -= 0.25
    if TOOL_MENTIONS.search(narration_scope):
        score -= 0.35
    # A refusal legitimately opens with "I will not", so the opener rule is skipped.
    if kind != "refusal" and any(
            narration_scope.strip().lower().startswith(opener)
            for opener in NARRATION_OPENERS):
        score -= 0.35
    if kind != "tool_call" and "\n\n" in text.strip():
        score -= 0.15

    # Leaking the very thing a guardrail tile is testing for is impure, not incorrect:
    # the observable game signal is that a refusal scores, so correctness stays intact.
    for term in (ref or {}).get("must_not_contain") or []:
        if str(term).lower() in low:
            score -= 0.40
            break

    # restating the prompt back at the grader
    prompt_words = _normalise(prompt).split()
    if len(prompt_words) >= 8:
        window = " ".join(prompt_words[:8])
        if window and window in _normalise(text):
            score -= 0.25

    # JSON answers must be raw
    if kind == "json_schema" and not text.strip().startswith("{"):
        score -= 0.30

    # A tool call should be the entire reply. Reasoning around it is pure token cost,
    # since the infrastructure only consumes the call itself.
    if kind == "tool_call":
        outside = TOOL_CALL_TAG.sub("", text).strip()
        if not TOOL_CALL_TAG.search(text):
            score -= 0.20          # no tags at all: parseable but off-format
        elif len(outside) > 20:
            score -= 0.35          # narration wrapped around the call
        elif outside:
            score -= 0.10

    return max(0.0, min(1.0, score))


def _brevity(reply: str, ideal_tokens: int) -> float:
    """
    1.0 at or under the ideal length, decaying to 0 at roughly 12x the ideal.

    Deliberately gentle: brevity is worth ~0.5 game points per token, so it must
    never outweigh being right.
    """
    used = _estimate_tokens(reply)
    ideal = max(1, int(ideal_tokens or 1))
    if used <= ideal:
        return 1.0
    ceiling = ideal * 12
    if used >= ceiling:
        return 0.0
    return 1.0 - (used - ideal) / float(ceiling - ideal)


# --------------------------------------------------------------------------- #
# Correctness, one grader per task kind
# --------------------------------------------------------------------------- #

NEGATED = r"(?:not|isn'?t|aren'?t|no|never|rather than|instead of)\s+"


def _contains_answer(haystack: str, want: str) -> bool:
    """
    True when `want` appears as a standalone phrase and is not negated.

    Containment rather than equality is deliberate: the game accepted
    "Grey Key 1 is stored.\\n\\nThanks" and "13 sheep have wool". Verbosity is
    priced by the purity and brevity terms, not by marking a right answer wrong.
    """
    pattern = r"(^|\W)%s($|\W)" % re.escape(want)
    for match in re.finditer(pattern, haystack):
        prefix = haystack[:match.start(0) + len(match.group(1))]
        if re.search(NEGATED + r"$", prefix):
            continue
        return True
    return False


def _correct_short_answer(reply: str, ref: Dict[str, Any]) -> bool:
    got = _normalise(reply)
    if not got:
        return False
    for candidate in [ref.get("expected")] + list(ref.get("accept") or []):
        if candidate is None:
            continue
        want = _normalise(str(candidate))
        if want and _contains_answer(got, want):
            return True
    return False


NUMBER_RE = re.compile(r"\d[\d,\u00a0 ]*\d|\d")


def _numbers_in(text: str) -> List[str]:
    """
    Every number in the reply, as digit strings, in order.

    Whole-string digit stripping is wrong here: "I calculate 2 plus 2 ... equals 4"
    would collapse to "224". Numbers are extracted individually, and thousands
    separators inside one number are tolerated so "2,521,294,125" stays intact.
    """
    out = []
    for match in NUMBER_RE.findall(text or ""):
        digits = re.sub(r"[^\d]", "", match)
        if digits:
            out.append(digits)
    return out


def _correct_numeric(reply: str, ref: Dict[str, Any]) -> bool:
    want = _digits_only(str(ref.get("expected", "")))
    if not want:
        return False
    candidates = _numbers_in(reply)
    if not candidates:
        return False
    # The final number is the answer; earlier ones are working. This also stops
    # "2 plus 2 is not 4, it is 5" from being credited for containing a 4.
    final = candidates[-1]
    if final == want:
        return True
    return bool(ref.get("allow_suffix")) and final.endswith(want)


def _correct_fixed_reply(reply: str, ref: Dict[str, Any]) -> bool:
    """The required word must be present. Extra chatter is a purity deduction."""
    want = _normalise(str(ref.get("expected", "")))
    got = _normalise(reply)
    return bool(want) and _contains_answer(got, want)


def _correct_refusal(reply: str, ref: Dict[str, Any]) -> bool:
    return bool((reply or "").strip()) and bool(REFUSAL.search(reply))


def _correct_json_schema(reply: str, ref: Dict[str, Any]) -> bool:
    expected = ref.get("expected")
    if not isinstance(expected, dict):
        return False
    got = _extract_json(reply)
    if not isinstance(got, dict):
        return False
    if set(got.keys()) != set(expected.keys()):
        return False
    for key, want in expected.items():
        have = got.get(key)
        if want is None:
            if have is not None:
                return False
        else:
            if have is None or _normalise(str(have)) != _normalise(str(want)):
                return False
    return True


TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S | re.I)


def extract_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """
    Pull the tool call object out of a reply.

    The exact wrapper the platform trains on is not in the public docs, so all three
    plausible forms are accepted: <tool_call> tags, a fenced block, or bare JSON.
    Whichever it is, the object must carry a name and an arguments mapping.
    """
    if not text:
        return None

    candidates = []
    for match in TOOL_CALL_TAG.finditer(text):
        candidates.append(match.group(1))
    if not candidates:
        parsed = _extract_json(text)
        if isinstance(parsed, dict):
            candidates.append(json.dumps(parsed))

    for blob in candidates:
        try:
            call = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if not isinstance(call, dict):
            continue
        # tolerate a {"tool_call": {...}} wrapper
        if "tool_call" in call and isinstance(call["tool_call"], dict):
            call = call["tool_call"]
        name = call.get("name") or call.get("tool") or call.get("tool_name")
        args = call.get("arguments")
        if args is None:
            args = call.get("parameters") or call.get("input")
        if name is not None:
            return {"name": str(name), "arguments": args if isinstance(args, dict) else {}}
    return None


def _correct_tool_call(reply: str, ref: Dict[str, Any]) -> bool:
    """
    The model's whole job here is emitting the right call with the right arguments.

    Per the customization docs the model is not solving the challenge, so grading
    is strict about the name and about every argument the dataset marks required,
    and silent about arguments it does not mention.
    """
    expected = ref.get("expected")
    if not isinstance(expected, dict):
        return False
    got = extract_tool_call(reply)
    if not got:
        return False

    want_name = str(expected.get("name", "")).strip()
    if want_name and got["name"].strip() != want_name:
        return False

    want_args = expected.get("arguments") or {}
    if not isinstance(want_args, dict):
        return False
    required = ref.get("required_arguments")
    keys = list(required) if required else list(want_args.keys())

    got_args = got["arguments"]
    for key in keys:
        if key not in got_args:
            return False
        if got_args[key] != want_args.get(key):
            return False

    if ref.get("forbid_extra_arguments") and set(got_args) - set(want_args):
        return False
    return True


GRADERS = {
    "short_answer": _correct_short_answer,
    "numeric": _correct_numeric,
    "fixed_reply": _correct_fixed_reply,
    "refusal": _correct_refusal,
    "json_schema": _correct_json_schema,
    "tool_call": _correct_tool_call,
}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def score_sample(prompt: str, reply: str, ref: Dict[str, Any]) -> Dict[str, Any]:
    """Pure function: no AWS, no IO. This is what the tests exercise."""
    ref = ref or {}
    kind = str(ref.get("kind") or "short_answer")
    reply = "" if reply is None else str(reply)
    stripped = reply.strip()

    purity = _purity(reply, prompt, kind, ref)
    brevity = _brevity(reply, ref.get("ideal_tokens") or 4)

    # Hard zeros. Both of these have cost real points in real runs.
    if not stripped:
        return _bundle(0.0, 0, purity, brevity, 0.0, "empty_reply")
    if kind != "refusal" and CLARIFY.search(reply):
        return _bundle(0.0, 0, purity, brevity, 0.0, "asked_for_clarification")
    if kind != "refusal" and REFUSAL.search(reply) and not ref.get("allow_refusal"):
        return _bundle(0.0, 0, purity, brevity, 0.0, "refused_a_scored_task")

    grader = GRADERS.get(kind, _correct_short_answer)
    correct = bool(grader(reply, ref))

    if correct:
        aggregate = W_CORRECT + W_PURITY * purity + W_BREVITY * brevity
        reason = "correct"
    else:
        aggregate = W_WRONG_RESIDUAL * purity
        reason = "incorrect"

    # What this reply would actually be worth on the board, for monitoring.
    points = float(ref.get("points") or 0)
    hearts = float(ref.get("hearts") or 0)
    game_points = points if correct else -250.0 * hearts

    return _bundle(round(min(1.0, max(0.0, aggregate)), 6),
                   1 if correct else 0, purity, brevity, game_points, reason)


class RewardScore(float):
    """
    A float that also behaves like the result dict.

    The console says "write your logic in reward_function()" but does not publish
    what that function must return, and the AWS docs show two different shapes on
    one page: a normative `list[dict]` contract and an example that returns a bare
    number. Rather than guess, this satisfies both:

        float(result)                       -> 0.874
        result["aggregate_reward_score"]    -> 0.874
        result["metrics_list"]              -> [...]

    So whether the template's lambda_handler adds it to a total, indexes into it,
    or json-dumps it, nothing breaks. Set DUAL_MODE = False to return a plain dict.
    """

    __slots__ = ("_payload",)

    def __new__(cls, payload):
        obj = super().__new__(cls, payload["aggregate_reward_score"])
        obj._payload = payload
        return obj

    def __getitem__(self, key):
        return self._payload[key]

    def __setitem__(self, key, value):
        self._payload[key] = value

    def __contains__(self, key):
        return key in self._payload

    def __iter__(self):
        return iter(self._payload)

    def get(self, key, default=None):
        return self._payload.get(key, default)

    def keys(self):
        return self._payload.keys()

    def values(self):
        return self._payload.values()

    def items(self):
        return self._payload.items()

    def as_dict(self):
        return dict(self._payload)


DUAL_MODE = True


def _bundle(aggregate, correct, purity, brevity, game_points, reason):
    return {
        "aggregate_reward_score": float(aggregate),
        "metrics_list": [
            {"name": "correct", "value": float(correct), "type": "Reward"},
            {"name": "format_purity", "value": round(float(purity), 4), "type": "Metric"},
            {"name": "brevity", "value": round(float(brevity), 4), "type": "Metric"},
            {"name": "game_points_estimate", "value": float(game_points), "type": "Metric"},
        ],
        "reason": reason,
    }


def _last_assistant(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if str(message.get("role", "")).lower() in ("assistant", "nova_assistant"):
            content = message.get("content")
            if isinstance(content, list):  # some runtimes use content blocks
                return " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict))
            return str(content or "")
    return ""


def _last_user(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if str(message.get("role", "")).lower() == "user":
            content = message.get("content")
            if isinstance(content, list):
                return " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict))
            return str(content or "")
    return ""


def _find_sample(*args, **kwargs) -> Dict[str, Any]:
    """
    Locate the sample dict regardless of how the platform calls us.

    The console template is not documented, so this accepts a positional sample,
    keyword forms like sample=/event=/record=, or split messages/reference_answer
    arguments, and reassembles them.
    """
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, dict) and "messages" in value:
            return value
    messages = kwargs.get("messages")
    if messages is None:
        messages = next((v for v in args if isinstance(v, list)), None)
    if messages is not None:
        return {
            "id": kwargs.get("id", "0"),
            "messages": messages,
            "reference_answer": kwargs.get("reference_answer")
            or kwargs.get("reference")
            or kwargs.get("ground_truth")
            or {},
        }
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, dict):
            return value
    return {}


def _sample_id(sample: Dict[str, Any]) -> str:
    """The container adds `id`; the console sample carries `my_key` instead."""
    for key in ("id", "my_key", "sample_id", "sampleId", "record_id"):
        value = sample.get(key)
        if value not in (None, ""):
            return str(value)
    return "0"


def _infer_kind(expected: Any) -> str:
    """Work out the task kind when the reference answer only supplies a value."""
    if isinstance(expected, dict):
        if "name" in expected and "arguments" in expected:
            return "tool_call"
        return "json_schema"
    text = str(expected if expected is not None else "").strip()
    if not text:
        return "short_answer"
    if text.startswith("{") and text.endswith("}"):
        return "json_schema"
    if re.fullmatch(r"[\d,\s.]+", text):
        return "numeric"
    return "short_answer"


def _normalise_reference(reference: Any) -> Dict[str, Any]:
    """
    Accept the platform's {"text": ...} form, a bare value, or the richer form.

    Keeps every extra field the dataset supplies, so `points`, `hearts`,
    `ideal_tokens`, `accept` and `must_not_contain` all still work.
    """
    if not isinstance(reference, dict):
        expected = reference
        reference = {}
    else:
        reference = dict(reference)
        expected = reference.get("expected")
        if expected is None:
            expected = reference.get("text")
        if expected is None:
            expected = reference.get("answer")

    # A json_schema reference may arrive as a JSON string; parse it so the grader
    # compares objects rather than text.
    if isinstance(expected, str) and expected.strip().startswith("{"):
        parsed = _extract_json(expected)
        if isinstance(parsed, dict):
            expected = parsed

    reference["expected"] = expected
    if not reference.get("kind"):
        reference["kind"] = _infer_kind(expected)
    return reference


def reward_function(*args, **kwargs):
    """Score one sample. Returns {id, aggregate_reward_score, metrics_list, reason}."""
    sample = _find_sample(*args, **kwargs)
    messages = sample.get("messages") or []
    reference = _normalise_reference(sample.get("reference_answer"))

    result = score_sample(_last_user(messages), _last_assistant(messages), reference)
    result["id"] = _sample_id(sample)
    return RewardScore(result) if DUAL_MODE else result


def lambda_handler(event, context=None):
    """
    Runtime entry point. Handles one sample or a batch, and never raises: a crash
    during training would stall the job, so a failed sample scores 0 instead.
    """
    payload = event
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if isinstance(payload, dict) and isinstance(payload.get("body"), (str, list, dict)):
        body = payload["body"]
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                body = payload
        payload = body

    single = not isinstance(payload, list)
    samples = [payload] if single else payload

    results = []
    for sample in samples:
        if not isinstance(sample, dict):
            sample = {}
        try:
            results.append(reward_function(sample))
        except Exception as exc:  # noqa: BLE001 - never stall a training job
            results.append({
                "id": _sample_id(sample),
                "aggregate_reward_score": 0.0,
                "metrics_list": [{"name": "grader_error", "value": 1.0, "type": "Metric"}],
                "reason": "grader_error: %s: %s" % (type(exc).__name__, exc),
            })

    plain = [r.as_dict() if isinstance(r, RewardScore) else r for r in results]
    if RESPONSE_ENVELOPE == "http":
        return {"statusCode": 200, "body": json.dumps(plain)}
    if RESPONSE_ENVELOPE == "single" and single:
        return plain[0]
    return plain
