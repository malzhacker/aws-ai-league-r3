"""
AWS AI League - math / code interpreter tool (AWS Lambda handler).

Built for the `c2 Code Challenge` tile ("Blue Brain"), e.g.
    "Tell me the 3000th number in the fibonacci sequence. Give me only the
     last 10 digits."

Two ways to use it:

  1. Give it `code` (or `expression`) -> it is validated with `ast`, then run in
     a sandbox with no imports, no attribute access, no file/network access and
     a wall-clock timeout. Whatever you `print()` or leave in `result` comes back.
  2. Give it only the `question` in plain English -> a pattern solver handles the
     usual dungeon questions (fibonacci, factorial, primes, digit sums, base
     conversion, plain arithmetic) without the LLM having to write code at all.

The response always contains a bare `answer` string, so the agent can echo it
directly with zero extra prose (important: the challenges award points for short,
exact answers).
"""

import ast
import json
import os
import re
import signal
import sys


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


# Resource ceilings. They exist to stop a runaway expression from burning the
# Lambda's whole timeout, and every one of them is tunable without a code change.
MAX_POW_EXPONENT = _env_int("MAX_POW_EXPONENT", 5_000_000)
MAX_POW_BITS = _env_int("MAX_POW_BITS", 40_000_000)
MAX_FACT = _env_int("MAX_FACTORIAL", 200_000)
DEFAULT_TIMEOUT = _env_int("EXEC_TIMEOUT_SECONDS", 8)
MAX_TIMEOUT = _env_int("MAX_EXEC_TIMEOUT_SECONDS", 25)
INT_STR_DIGITS = _env_int("MAX_INT_STR_DIGITS", 2_000_000)

try:  # allow str() of very large integers (Python 3.11+ caps this at 4300 digits)
    sys.set_int_max_str_digits(INT_STR_DIGITS)
except AttributeError:
    pass


# --------------------------------------------------------------------------- #
# Math helpers exposed to the sandbox
# --------------------------------------------------------------------------- #

def safe_pow(base, exponent, modulus=None):
    if modulus is not None:
        return pow(base, exponent, modulus)
    if isinstance(exponent, int) and abs(exponent) > MAX_POW_EXPONENT:
        raise ValueError("exponent too large")
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 0:
        if base.bit_length() * exponent > MAX_POW_BITS:
            raise ValueError("result too large")
    return pow(base, exponent)


def fib(n):
    """n-th Fibonacci number, fast doubling. fib(1)=1, fib(2)=1."""
    n = int(n)
    if n < 0:
        sign = -1 if n % 2 == 0 else 1
        return sign * fib(-n)

    def _fd(k):
        if k == 0:
            return (0, 1)
        a, b = _fd(k >> 1)
        c = a * (2 * b - a)
        d = a * a + b * b
        return (d, c + d) if k & 1 else (c, d)

    return _fd(n)[0]


def lucas(n):
    return 2 * fib(n + 1) - fib(n)


def fact(n):
    n = int(n)
    if n < 0 or n > MAX_FACT:
        raise ValueError("factorial out of range")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def is_prime(n):
    n = int(n)
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def primes_up_to(limit):
    limit = int(limit)
    if limit < 2:
        return []
    sieve = bytearray([1]) * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
    return [i for i, flag in enumerate(sieve) if flag]


def nth_prime(n):
    n = int(n)
    if n < 1:
        raise ValueError("n must be >= 1")
    count, candidate = 0, 1
    while count < n:
        candidate += 1
        if is_prime(candidate):
            count += 1
    return candidate


def digits(n):
    return [int(ch) for ch in str(abs(int(n)))]


def digit_sum(n):
    return sum(digits(n))


def last_digits(n, count):
    text = str(abs(int(n)))
    return text[-int(count):]


def first_digits(n, count):
    return str(abs(int(n)))[:int(count)]


WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3,
    "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10, "last": 1, "final": 1,
}


def _count(token):
    token = str(token).strip().lower()
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


EDGES_RE = re.compile(
    r"(?:first|leading|initial)\s+(\w+)\s+char\w*.{0,40}?"
    r"(?:last|final|trailing)\s+(\w+)\s+char\w*", re.I | re.S)
POSITIONS_RE = re.compile(
    r"(\d+|\w+)\s*(?:st|nd|rd|th)?\s+and\s+(?:the\s+)?(\d+|\w+)\s*(?:st|nd|rd|th)?\s+char\w*",
    re.I)


def rule_from_text(text):
    """
    Read a door's transformation out of its own wording.

    "combining the first two characters and the last two characters" -> edges 2,2
    "giving the 5th and 7th character"                               -> chars 5,7

    This is the generic path: when the challenge description travels with the
    request, no configuration is needed at all.
    """
    if not text:
        return None
    match = EDGES_RE.search(text)
    if match:
        head, tail = _count(match.group(1)), _count(match.group(2))
        if head and tail:
            return ("edges", [head, tail])
    match = POSITIONS_RE.search(text)
    if match:
        a, b = _count(match.group(1)), _count(match.group(2))
        if a and b:
            return ("chars", [a, b])
    return None


def parse_door_rules(raw):
    """
    Parse a door-to-rule table supplied as configuration, never hardcoded.

        "c32=edges:2,2; c33=chars:5,7"

    Accepts a dict or a JSON object too.
    """
    rules = {}
    if not raw:
        return rules
    if isinstance(raw, dict):
        items = raw.items()
    else:
        text = str(raw).strip()
        if text[:1] == "{":
            try:
                return parse_door_rules(json.loads(text))
            except ValueError:
                pass
        items = []
        for part in re.split(r"[;\n]", text):
            piece = part.split("=", 1)
            if len(piece) == 2:
                items.append((piece[0], piece[1]))
    for door, spec in items:
        match = re.match(r"\s*(edges|chars)\s*[:=]?\s*([\d,\s]+)", str(spec), re.I)
        if match:
            numbers = [int(n) for n in re.findall(r"\d+", match.group(2))]
            if numbers:
                rules[str(door).strip().lower()] = (match.group(1).lower(), numbers)
    return rules


def apply_rule(rule, key):
    kind, numbers = rule
    if kind == "edges":
        head = numbers[0]
        tail = numbers[1] if len(numbers) > 1 else numbers[0]
        return edges(key, head, tail)
    return chars(key, *numbers)


def chars(text, *positions):
    """
    Concatenate characters at 1-based positions: chars("abcdef", 2, 4) -> "bd".

    Exists so an "Nth and Mth character" instruction never has to be converted to
    zero-based indices by a language model, which is where off-by-one errors come
    from. Spaces and symbols count as characters, and positions may be negative to
    count from the end.
    """
    text = str(text)
    out = []
    for position in positions:
        index = int(position)
        if index == 0:
            raise ValueError("positions are 1-based; 0 is not a position")
        if index > 0:
            index -= 1
        if index >= len(text) or index < -len(text):
            raise ValueError("position %s is outside a string of length %d"
                             % (position, len(text)))
        out.append(text[index])
    return "".join(out)


def edges(text, head=1, tail=1):
    """First `head` characters followed by the last `tail`: edges("abcdef",2,2)->"abef"."""
    text = str(text)
    head, tail = int(head), int(tail)
    if head < 0 or tail < 0:
        raise ValueError("head and tail must not be negative")
    if head + tail > len(text):
        raise ValueError("string of length %d is too short for %d+%d characters"
                         % (len(text), head, tail))
    return text[:head] + (text[len(text) - tail:] if tail else "")


def divisors(n):
    n = abs(int(n))
    out = set()
    i = 1
    while i * i <= n:
        if n % i == 0:
            out.add(i)
            out.add(n // i)
        i += 1
    return sorted(out)


def prime_factors(n):
    n = abs(int(n))
    out = []
    d = 2
    while d * d <= n:
        while n % d == 0:
            out.append(d)
            n //= d
        d += 1 if d == 2 else 2
    if n > 1:
        out.append(n)
    return out


def collatz_length(n):
    n, steps = int(n), 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps


def to_base(n, base):
    n, base = int(n), int(base)
    if not 2 <= base <= 36:
        raise ValueError("base must be 2..36")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n == 0:
        return "0"
    out = ""
    while n:
        n, rem = divmod(n, base)
        out = alphabet[rem] + out
    return sign + out


def gcd(a, b):
    a, b = abs(int(a)), abs(int(b))
    while b:
        a, b = b, a % b
    return a


def lcm(a, b):
    if a == 0 or b == 0:
        return 0
    return abs(int(a) * int(b)) // gcd(a, b)


def comb(n, k):
    n, k = int(n), int(k)
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    num, den = 1, 1
    for i in range(k):
        num *= n - i
        den *= i + 1
    return num // den


def perm(n, k=None):
    n = int(n)
    k = n if k is None else int(k)
    result = 1
    for i in range(n, n - k, -1):
        result *= i
    return result


def _isqrt(n):
    """Integer square root, exact for arbitrarily large ints."""
    n = int(n)
    if n < 0:
        raise ValueError("isqrt of negative number")
    if n < 2:
        return n
    x = 1 << ((n.bit_length() + 1) // 2)
    while True:
        y = (x + n // x) // 2
        if y >= x:
            return x
        x = y


# --------------------------------------------------------------------------- #
# AST sandbox
# --------------------------------------------------------------------------- #

ALLOWED_NODES = (
    ast.Module, ast.Interactive, ast.Expression, ast.Expr,
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr,
    ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass,
    ast.FunctionDef, ast.Return, ast.Lambda, ast.arguments, ast.arg,
    ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Del,
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Subscript, ast.Slice, ast.Index,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.JoinedStr, ast.FormattedValue, ast.Starred,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
)

BANNED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "globals",
    "locals", "vars", "dir", "getattr", "setattr", "delattr", "help", "exit",
    "quit", "breakpoint", "memoryview", "object", "type", "super", "id",
}


class PowRewriter(ast.NodeTransformer):
    """Route every `a ** b` through safe_pow so nobody can hang the runtime."""

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="safe_pow", ctx=ast.Load()),
                    args=[node.left, node.right],
                    keywords=[],
                ),
                node,
            )
        return node


def validate_ast(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError("disallowed syntax: %s" % type(node).__name__)
        if isinstance(node, ast.Attribute):
            raise ValueError("attribute access is not allowed")
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise ValueError("disallowed name: %s" % node.id)
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("dunder access is not allowed")


VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_variables(raw):
    """
    Validate caller-supplied values before injecting them into the sandbox.

    Accepts a dict or a JSON object string. Only plain scalars and flat
    lists/tuples of scalars are allowed, so nothing callable or introspectable can
    be smuggled in through a variable binding.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("variables must be a JSON object")
    if not isinstance(raw, dict):
        raise ValueError("variables must be a JSON object")

    def _scalar(value):
        return value is None or isinstance(value, (str, int, float, bool))

    clean = {}
    for name, value in raw.items():
        name = str(name)
        if not VARIABLE_NAME_RE.match(name) or name.startswith("__"):
            raise ValueError("invalid variable name: %s" % name)
        if name in BANNED_NAMES:
            raise ValueError("disallowed variable name: %s" % name)
        if isinstance(value, (list, tuple)):
            if not all(_scalar(item) for item in value):
                raise ValueError("variable %s may only contain scalars" % name)
            clean[name] = list(value)
        elif _scalar(value):
            clean[name] = value
        else:
            raise ValueError("variable %s has an unsupported type" % name)
    return clean


def build_env(printed, variables=None):
    def _print(*args, **_kwargs):
        printed.append(" ".join(str(a) for a in args))

    env = {
        "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
        "chr": chr, "divmod": divmod, "enumerate": enumerate, "filter": filter,
        "float": float, "hex": hex, "int": int, "len": len, "list": list,
        "map": map, "max": max, "min": min, "oct": oct, "ord": ord,
        "print": _print, "range": range, "reversed": reversed, "round": round,
        "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
        "dict": dict, "zip": zip, "pow": safe_pow, "safe_pow": safe_pow,
        # math / number theory toolbox
        "fib": fib, "fibonacci": fib, "lucas": lucas, "fact": fact,
        "factorial": fact, "is_prime": is_prime, "primes_up_to": primes_up_to,
        "nth_prime": nth_prime, "digits": digits, "digit_sum": digit_sum,
        "last_digits": last_digits, "first_digits": first_digits,
        "divisors": divisors, "prime_factors": prime_factors,
        "collatz_length": collatz_length, "to_base": to_base,
        "gcd": gcd, "lcm": lcm, "comb": comb, "perm": perm, "isqrt": _isqrt,
        "sqrt": lambda x: x ** 0.5, "pi": 3.141592653589793,
        "e": 2.718281828459045, "inf": float("inf"),
        # string position helpers, so 1-based instructions never need hand-counting
        "chars": chars, "edges": edges,
    }
    env.update(variables or {})
    return {"__builtins__": {}, **env}


class Timeout(Exception):
    pass


def run_code(code, timeout=DEFAULT_TIMEOUT, variables=None):
    """Validate + execute untrusted python. Returns (result, stdout_lines)."""
    tree = ast.parse(code, mode="exec")
    validate_ast(tree)
    tree = ast.fix_missing_locations(PowRewriter().visit(tree))

    printed = []
    env = build_env(printed, variables)

    # If the last statement is a bare expression, capture its value as `result`.
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        tree.body.append(
            ast.fix_missing_locations(
                ast.Assign(
                    targets=[ast.Name(id="result", ctx=ast.Store())],
                    value=last.value,
                )
            )
        )

    def _alarm(_signum, _frame):
        raise Timeout("execution exceeded %ss" % timeout)

    installed = False
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        installed = True
    except (AttributeError, ValueError):
        pass

    try:
        exec(compile(tree, "<challenge>", "exec"), env)  # noqa: S102 - sandboxed
    finally:
        if installed:
            signal.setitimer(signal.ITIMER_REAL, 0)

    result = env.get("result", env.get("answer"))
    if result is None and printed:
        result = printed[-1]
    return result, printed


# --------------------------------------------------------------------------- #
# Plain-English fallback solver
# --------------------------------------------------------------------------- #

ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def _int(text):
    return int(re.sub(r"[,_\s]", "", text))


def _trim(value, question):
    """Apply 'give me only the last/first N digits' style post-processing."""
    match = re.search(r"last\s+(\d+)\s+digits?", question, re.I)
    if match and isinstance(value, int):
        return last_digits(value, match.group(1))
    match = re.search(r"first\s+(\d+)\s+digits?", question, re.I)
    if match and isinstance(value, int):
        return first_digits(value, match.group(1))
    return value


def solve_question(question):
    q = question.strip()
    low = q.lower()

    def ordinal(pattern):
        match = re.search(pattern, low)
        if not match:
            return None
        token = match.group(1)
        return ORDINAL_WORDS.get(token, None) if not token.isdigit() else _int(token)

    # fibonacci
    if "fibonacci" in low or "fibbonacci" in low:
        n = ordinal(r"(\d[\d,_ ]*|" + "|".join(ORDINAL_WORDS) + r")\s*(?:st|nd|rd|th)?\s*(?:number|term|element|value|digit\s+number)?[^.]{0,40}?fibonacci")
        if n is None:
            n = ordinal(r"fibonacci[^.]{0,40}?(\d[\d,_ ]*|" + "|".join(ORDINAL_WORDS) + r")")
        if n is None:
            match = re.search(r"(\d[\d,_ ]*)", low)
            n = _int(match.group(1)) if match else None
        if n is not None:
            return _trim(fib(n), q)

    # factorial
    match = re.search(r"factorial\s+of\s+(\d[\d,_ ]*)|(\d[\d,_ ]*)\s*!", low)
    if match:
        return _trim(fact(_int(match.group(1) or match.group(2))), q)

    # nth prime
    match = re.search(r"(\d[\d,_ ]*)\s*(?:st|nd|rd|th)\s+prime", low)
    if match:
        return _trim(nth_prime(_int(match.group(1))), q)

    # is prime?
    match = re.search(r"is\s+(\d[\d,_ ]*)\s+(?:a\s+)?prime", low)
    if match:
        return "yes" if is_prime(_int(match.group(1))) else "no"

    # sum of digits
    match = re.search(r"sum\s+of\s+(?:the\s+)?digits[^\d]{0,20}(\d[\d,_ ]*)", low)
    if match:
        return digit_sum(_int(match.group(1)))

    # 2**n style powers
    match = re.search(r"(\d[\d,_ ]*)\s*(?:\^|\*\*|to the power of)\s*(\d[\d,_ ]*)", low)
    if match:
        return _trim(safe_pow(_int(match.group(1)), _int(match.group(2))), q)

    # bare arithmetic expression
    match = re.search(r"[-+]?[\d.,_ ]+(?:[-+*/%()]+[\d.,_ ]+)+", q)
    if match and re.search(r"[-+*/%]", match.group(0)):
        expression = match.group(0).replace(",", "").strip(" .")
        try:
            value, _ = run_code(expression)
            return _trim(value, q)
        except Exception:
            pass

    return None


# --------------------------------------------------------------------------- #
# Input plumbing
# --------------------------------------------------------------------------- #

def _harvest(event):
    """Pull code / question / timeout out of any reasonable event shape."""
    fields = {}
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except ValueError:
            return {"question": event}
    if not isinstance(event, dict):
        return {}

    for param in event.get("parameters") or []:
        if isinstance(param, dict) and "name" in param:
            fields[str(param["name"]).lower()] = param.get("value")

    request_body = (event.get("requestBody") or {}).get("content") or {}
    for payload in request_body.values():
        for prop in payload.get("properties") or []:
            if isinstance(prop, dict) and "name" in prop:
                fields[str(prop["name"]).lower()] = prop.get("value")

    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {"question": body}
    if isinstance(body, dict):
        for key, value in body.items():
            fields.setdefault(str(key).lower(), value)

    # Variable bindings are the one field that is legitimately an object, so it is
    # picked up explicitly before the scalar-only sweep below discards containers.
    for name in ("variables", "vars"):
        if isinstance(event.get(name), (dict, str)):
            fields.setdefault(name, event[name])

    for key, value in event.items():
        if not isinstance(value, (dict, list)):
            fields.setdefault(str(key).lower(), value)

    return fields


def solve(event):
    fields = _harvest(event)
    code = fields.get("code") or fields.get("python") or fields.get("script") or fields.get("expression")
    question = (
        fields.get("question") or fields.get("prompt") or fields.get("inputtext")
        or fields.get("input") or fields.get("text") or fields.get("query") or ""
    )
    try:
        timeout = float(fields.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, float(MAX_TIMEOUT)))
    variables = sanitize_variables(fields.get("variables") or fields.get("vars"))

    # ---- door codes: {"door": "<id>", "key": "<stored value>"} ------------- #
    # The caller supplies only which door and the secret. The transformation is
    # resolved from configuration or from the door's own wording, never from a
    # table baked into this file.
    door = fields.get("door") or fields.get("door_id") or fields.get("doorid")
    key_value = fields.get("key") or fields.get("secret") or fields.get("value")
    if door and key_value:
        door = str(door).strip().lower()
        key_value = str(key_value).strip()

        # The challenge's own wording wins over any configured table.
        #
        # This order matters on the evaluation map. That board is the same shape as the
        # practice board but its questions differ, so a rule remembered from practice
        # can be wrong there. A rule stated in the question is ground truth for the
        # challenge in front of you; a configured rule is only a memory of a different
        # one. Getting this backwards returns a confidently wrong code, and a wrong door
        # code costs five hearts.
        blob = " ".join(str(v) for k, v in fields.items()
                        if isinstance(v, str) and k not in ("key", "secret", "value"))
        rule = rule_from_text(blob)
        if rule is None:
            rules = parse_door_rules(fields.get("door_rules")
                                     or os.environ.get("DOOR_RULES"))
            rule = rules.get(door)
        if rule is None:
            return {
                "answer": "",
                "error": "No transformation known for door %r. Supply it as "
                         "door_rules (\"%s=chars:5,7\") or set the DOOR_RULES "
                         "environment variable, or include the door's wording "
                         "in the request." % (door, door),
                "mode": "door_unresolved",
            }

        if " " in key_value or ":" in key_value:
            return {"answer": "", "mode": "door_bad_key",
                    "error": "The key looks like a sentence, not a value: %r. Pass "
                             "only the stored secret." % key_value[:48]}

        code = apply_rule(rule, key_value)
        return {"answer": str(code), "result": str(code), "stdout": [],
                "mode": "door", "rule": "%s%s" % (rule[0], tuple(rule[1]))}

    if code:
        result, printed = run_code(str(code), timeout, variables)
        if question:
            result = _trim(result, str(question))
        return {
            "answer": "" if result is None else str(result),
            "result": result if isinstance(result, (int, float, str, bool)) else str(result),
            "stdout": printed,
            "mode": "code",
        }

    if question:
        result = solve_question(str(question))
        if result is not None:
            return {
                "answer": str(result),
                "result": result,
                "stdout": [],
                "mode": "pattern",
            }
        return {
            "answer": "",
            "error": "Could not derive the answer from the question alone. "
                     "Call this tool again with a `code` field containing python "
                     "that assigns the answer to `result`.",
            "mode": "needs_code",
        }

    return {"answer": "", "error": "Provide `code` or `question`.", "mode": "empty"}


def lambda_handler(event, context=None):
    try:
        result = solve(event or {})
    except Timeout as exc:
        result = {"answer": "", "error": "timeout: %s" % exc, "mode": "timeout"}
    except Exception as exc:
        result = {"answer": "", "error": "%s: %s" % (type(exc).__name__, exc), "mode": "error"}

    body = json.dumps(result, default=str)

    if isinstance(event, dict) and event.get("messageVersion") and event.get("actionGroup"):
        return {
            "messageVersion": event.get("messageVersion", "1.0"),
            "response": {
                "actionGroup": event.get("actionGroup"),
                "apiPath": event.get("apiPath"),
                "httpMethod": event.get("httpMethod"),
                "httpStatusCode": 200,
                "responseBody": {"application/json": {"body": body}},
            },
        }

    if isinstance(event, dict) and ("requestContext" in event or "rawPath" in event):
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": body}

    return result


if __name__ == "__main__":
    # Reads one event object, or a list of them, from a file argument or stdin:
    #   python3 lambda.py ../examples/mathsolver_events.json
    #   echo '{"question": "the 30th fibonacci number"}' | python3 lambda.py
    source = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin
    with source:
        payload = json.load(source)
    for case in payload if isinstance(payload, list) else [payload]:
        case.pop("expect", None)
        print(json.dumps(lambda_handler(case), default=str))
