"""
AWS AI League - web scraper / web search tool (AWS Lambda handler).

Built for the `c4 Web Search Challenge` tile ("Dark Prophet"), e.g.
    "According to https://kiwiconnoisseurs.com what is their favorite pet tree?"

Rules the challenge imposes: no extra dependencies may be installed, so this uses
nothing but the Python standard library (`urllib`, `re`, `html`, `gzip`).

What it does
------------
  * takes a `url` (or finds the first URL inside the `question` text)
  * if no URL is given, falls back to an HTML search engine query
  * downloads with a browser-ish User-Agent, size cap and timeout
  * strips script/style/nav markup, unescapes entities, collapses whitespace
  * returns the readable page text PLUS keyword-scored snippets so the agent can
    answer with an exact quote instead of guessing.

IMPORTANT deployment note: a Lambda in a VPC needs a NAT gateway for egress.
Deploy this function WITHOUT a VPC config (default networking) so it can reach
the public internet.
"""

import gzip
import html as html_module
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


# Every knob below is overridable per deployment, so nothing here has to be
# edited to point the tool at a different search endpoint or raise a limit.
USER_AGENT = os.environ.get("HTTP_USER_AGENT") or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
MAX_BYTES = _env_int("MAX_RESPONSE_BYTES", 3_000_000)
DEFAULT_TIMEOUT = _env_int("FETCH_TIMEOUT_SECONDS", 12)
MAX_TIMEOUT = _env_int("MAX_FETCH_TIMEOUT_SECONDS", 25)
DEFAULT_MAX_CHARS = _env_int("DEFAULT_MAX_CHARS", 12_000)
HARD_MAX_CHARS = _env_int("HARD_MAX_CHARS", 40_000)

# `%s` is replaced with the url-encoded query. Override with a comma separated
# list in SEARCH_ENDPOINTS to use a different engine or your own proxy.
SEARCH_ENDPOINTS = tuple(
    endpoint.strip() for endpoint in (
        os.environ.get("SEARCH_ENDPOINTS")
        or "https://html.duckduckgo.com/html/?q=%s,https://lite.duckduckgo.com/lite/?q=%s"
    ).split(",") if endpoint.strip()
)

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]}]+|(?<![\w@.])(?:www\.)[\w.-]+\.[a-z]{2,}[^\s<>\"'\)\]}]*", re.I)

DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|iframe|form)\b.*?</\1\s*>",
    re.I | re.S,
)
COMMENTS = re.compile(r"<!--.*?-->", re.S)
BLOCK_END = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|nav|table|ul|ol|dl|dd|dt|blockquote|pre)\b[^>]*>",
    re.I,
)
TAG = re.compile(r"<[^>]+>")
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META_DESC = re.compile(
    r"<meta[^>]+(?:name|property)\s*=\s*[\"'](?:description|og:description)[\"'][^>]*>",
    re.I,
)
META_CONTENT = re.compile(r"content\s*=\s*[\"'](.*?)[\"']", re.I | re.S)

STOPWORDS = {
    "the", "a", "an", "of", "is", "are", "was", "were", "to", "in", "on", "for",
    "and", "or", "what", "which", "who", "whom", "whose", "how", "why", "when",
    "where", "their", "them", "they", "this", "that", "these", "those", "it",
    "its", "do", "does", "did", "with", "at", "by", "from", "as", "be", "been",
    "according", "tell", "me", "you", "your", "please", "about", "can", "site",
    "website", "page", "there", "has", "have", "had", "will", "would", "only",
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def normalise_url(url):
    url = str(url).strip().strip("<>\"'")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    return url


def fetch(url, timeout=DEFAULT_TIMEOUT):
    """Download a URL. Returns (final_url, content_type, text)."""
    url = normalise_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, identity",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPCookieProcessor(),
    )
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_BYTES)
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "") or ""
        if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except OSError:
                pass

    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    else:
        head = raw[:4096].decode("latin-1", "ignore")
        match = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
        if match:
            charset = match.group(1)
    try:
        text = raw.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", "replace")
    return final_url, content_type, text


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def html_to_text(document):
    body = DROP_BLOCKS.sub(" ", document)
    body = COMMENTS.sub(" ", body)
    body = BLOCK_END.sub("\n", body)
    body = TAG.sub(" ", body)
    body = html_module.unescape(body)
    body = body.replace("\u00a0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in body.split("\n")]
    return "\n".join(line for line in lines if line)


def page_title(document):
    match = TITLE.search(document)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html_module.unescape(TAG.sub(" ", match.group(1)))).strip()


def page_description(document):
    match = META_DESC.search(document)
    if not match:
        return ""
    content = META_CONTENT.search(match.group(0))
    if not content:
        return ""
    return re.sub(r"\s+", " ", html_module.unescape(content.group(1))).strip()


def keywords(question):
    tokens = re.findall(r"[a-z0-9']{3,}", str(question).lower())
    return [t for t in tokens if t not in STOPWORDS]


def snippets(text, question, limit=8, window=320):
    """Rank sentences/lines by how many question keywords they contain."""
    terms = keywords(question)
    if not terms:
        return []
    chunks = []
    for block in text.split("\n"):
        for piece in re.split(r"(?<=[.!?])\s+", block):
            piece = piece.strip()
            if len(piece) > 2:
                chunks.append(piece[:window])

    scored = []
    for index, chunk in enumerate(chunks):
        low = chunk.lower()
        score = sum(3 if re.search(r"\b%s\b" % re.escape(t), low) else 0 for t in terms)
        score += sum(1 for t in terms if t in low)
        if score:
            scored.append((-score, index, chunk))
    scored.sort()

    out, seen = [], set()
    for _, index, chunk in scored[: limit * 3]:
        key = chunk.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        context = " ".join(chunks[max(0, index - 1): index + 2])[: window * 2]
        out.append(context if len(context) > len(chunk) else chunk)
        if len(out) >= limit:
            break
    return out


def links(document, base_url, limit=40):
    found, seen = [], set()
    for match in re.finditer(r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", document, re.I | re.S):
        href = urllib.parse.urljoin(base_url, html_module.unescape(match.group(1).strip()))
        if not href.lower().startswith("http") or href in seen:
            continue
        seen.add(href)
        label = re.sub(r"\s+", " ", html_module.unescape(TAG.sub(" ", match.group(2)))).strip()
        found.append({"url": href, "text": label[:120]})
        if len(found) >= limit:
            break
    return found


# --------------------------------------------------------------------------- #
# Search fallback (no URL supplied)
# --------------------------------------------------------------------------- #

def web_search(query, timeout=DEFAULT_TIMEOUT, limit=8):
    encoded = urllib.parse.quote_plus(query)
    for template in SEARCH_ENDPOINTS:
        try:
            _, _, document = fetch(template % encoded, timeout)
        except Exception:
            continue
        results, seen = [], set()
        for match in re.finditer(r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", document, re.I | re.S):
            href = html_module.unescape(match.group(1))
            if "uddg=" in href:  # duckduckgo redirect wrapper
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = (parsed.get("uddg") or [href])[0]
            if not href.startswith("http"):
                continue
            if re.search(r"duckduckgo\.com|/y\.js|javascript:", href):
                continue
            if href in seen:
                continue
            seen.add(href)
            label = re.sub(r"\s+", " ", html_module.unescape(TAG.sub(" ", match.group(2)))).strip()
            if not label:
                continue
            results.append({"url": href, "title": label[:160]})
            if len(results) >= limit:
                break
        if results:
            return results
    return []


# --------------------------------------------------------------------------- #
# Input plumbing
# --------------------------------------------------------------------------- #

def _harvest(event):
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

    query = event.get("queryStringParameters") or {}
    if isinstance(query, dict):
        for key, value in query.items():
            fields.setdefault(str(key).lower(), value)

    for key, value in event.items():
        if not isinstance(value, (dict, list)):
            fields.setdefault(str(key).lower(), value)

    return fields


def solve(event):
    fields = _harvest(event)
    question = str(
        fields.get("question") or fields.get("prompt") or fields.get("query")
        or fields.get("inputtext") or fields.get("input") or fields.get("text") or ""
    )
    url = fields.get("url") or fields.get("link") or fields.get("website") or ""

    try:
        timeout = float(fields.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(2.0, min(timeout, float(MAX_TIMEOUT)))

    try:
        max_chars = int(fields.get("max_chars") or fields.get("maxchars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(500, min(max_chars, HARD_MAX_CHARS))

    if not url:
        found = URL_RE.findall(question)
        if found:
            url = found[0]

    if not url:
        if not question:
            return {"error": "Provide a `url` or a `question` containing one."}
        results = web_search(question, timeout)
        if not results:
            return {"error": "No URL in the question and the search fallback returned nothing.",
                    "question": question}
        # auto-follow the top hit so the caller gets real content in one round trip
        top = results[0]["url"]
        page = solve({"url": top, "question": question, "timeout": timeout, "max_chars": max_chars})
        page["search_results"] = results
        page["mode"] = "search+fetch"
        return page

    try:
        final_url, content_type, document = fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        return {"error": "HTTP %s for %s" % (exc.code, url), "url": url}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc), "url": url}

    if "json" in content_type.lower():
        try:
            data = json.loads(document)
            text = json.dumps(data, indent=2)[:max_chars]
        except ValueError:
            text = document[:max_chars]
        return {
            "url": final_url,
            "content_type": content_type,
            "title": "",
            "text": text,
            "snippets": snippets(text, question),
            "mode": "json",
        }

    text = html_to_text(document)
    truncated = len(text) > max_chars
    return {
        "url": final_url,
        "content_type": content_type,
        "title": page_title(document),
        "description": page_description(document),
        "text": text[:max_chars],
        "truncated": truncated,
        "chars": len(text),
        "snippets": snippets(text, question) if question else [],
        "links": links(document, final_url) if bool(fields.get("include_links")) else [],
        "mode": "fetch",
    }


def lambda_handler(event, context=None):
    try:
        result = solve(event or {})
    except Exception as exc:
        result = {"error": "%s: %s" % (type(exc).__name__, exc)}

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
    # python3 lambda.py ../examples/webscraper_event.json
    # echo '{"url": "https://example.com"}' | python3 lambda.py
    source = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin
    with source:
        payload = json.load(source)
    print(json.dumps(lambda_handler(payload), default=str, indent=2))
