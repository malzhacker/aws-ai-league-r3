"""
AWS AI League - Dungeon pathfinding tool (AWS Lambda handler).

Nothing about a specific dungeon lives in this file. There is no map, no tile-ID
table and no reward table baked in. The handler derives everything it needs at
runtime from three channels, in this order of precedence:

  1. the invocation payload  (`avoid`, `key_doors`, `values`, `walls`, ...)
  2. environment variables   (`AVOID_TILES`, `KEY_DOOR_PAIRS`, `TILE_VALUES`, ...)
  3. the prompt text itself  - the map literal, the start position, the
     navigation prompt and any challenge descriptions are mined for the tile
     taxonomy (which id is a trap, which is a key, which door it opens, what
     each tile is worth, what it costs in hearts).

Inference rules used on the text, none of which name a specific tile id:

  * `avoid: <tiles>` / "never step on <tiles>" / "<id> is a spike trap"  -> hazard
  * a tile whose description costs health but awards no points            -> hazard
  * "<id> a <colour> key" / "<id> a <colour> door"                        -> key / door,
    paired by the colour word, or by `a->b`, or by "<a> before <b>"
  * "+<n>" next to a tile id                                             -> its value
  * "-<n> heart|hp|health" next to a tile id                             -> its risk

The routing behaviour itself is the only fixed part: never enter a hazard or a
wall, always walk to the nearest unvisited objective, never enter a door before
its key is held, and take the treasure last.
"""

import json
import os
import re
from collections import deque

# The three structural words below belong to the *map serialisation format* the
# game emits, not to any particular dungeon. They stay overridable anyway.
DEFAULT_WALL_WORDS = "wall,block,blocked,obstacle"
DEFAULT_FLOOR_WORDS = "normal,empty,floor,none,start,player,agent"
DEFAULT_TREASURE_WORDS = "treasure,chest,goal"
DEFAULT_STEP_BUDGET = "400"

TILE_ID_RE = re.compile(r"\bc\d+\b", re.I)
COORD_RE = re.compile(r"\b([A-Z]{1,2})\s*(\d{1,3})\b")
MOVES = ((-1, 0, "up"), (1, 0, "down"), (0, -1, "left"), (0, 1, "right"))

COLOUR_RE = re.compile(
    r"\b(grey|gray|yellow|red|blue|green|purple|violet|silver|gold|golden|black|"
    r"white|orange|pink|brown|cyan|teal|magenta|bronze|copper|iron|crystal|"
    r"rainbow|rusty|ancient)\b",
    re.I,
)
COLOUR_ALIASES = {"gray": "grey", "golden": "gold"}

TRAP_WORDS = re.compile(r"\b(spike|trap|hazard|mine|lava|poison|pit|thorn)\w*\b", re.I)
KEY_WORDS = re.compile(r"\bkeys?\b", re.I)
DOOR_WORDS = re.compile(r"\b(doors?|gates?)\b", re.I)
VALUE_RE = re.compile(r"\+\s*([\d,]+)")
HEART = r"heart|hearts|hp|health|life|lives|\u2764|\u2665|\U0001f494"
HEALTH_RE = re.compile(
    r"(?:[-–−]\s*(\d+)\s*(?:%(h)s)"
    r"|(?:%(h)s)\s*[:\-–−]?\s*[-–−]\s*(\d+)"
    r"|lose\s+(\d+)\s*(?:%(h)s))" % {"h": HEART},
    re.I,
)
AVOID_RE = re.compile(
    r"(?:avoid|never\s+(?:step|walk|move)\s+(?:on|onto|into|through)|"
    r"do\s*not\s+(?:step|walk|enter)|don'?t\s+(?:step|walk|enter)|stay\s+off|"
    r"impassable|blocked\s+tiles?|hazards?|avoid_tiles?)\b([^.\n;]{0,160})",
    re.I,
)
PAIR_ARROW_RE = re.compile(r"\b(c\d+)\s*(?:->|=>|→|:|unlocks?|opens?)\s*(c\d+)\b", re.I)
PAIR_BEFORE_RE = re.compile(r"\b(c\d+)\s+(?:before|then)\s+(c\d+)\b", re.I)


# --------------------------------------------------------------------------- #
# Coordinate helpers  ("A5" <-> (row_index, col_index))
# --------------------------------------------------------------------------- #

def col_to_index(letters):
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def index_to_col(index):
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def to_label(cell):
    row, col = cell
    return "%s%d" % (index_to_col(col), row + 1)


def from_label(label):
    match = COORD_RE.search(str(label).strip().upper())
    if not match:
        return None
    return (int(match.group(2)) - 1, col_to_index(match.group(1)))


# --------------------------------------------------------------------------- #
# Payload mining - grid, start position, free text, named options
# --------------------------------------------------------------------------- #

def _looks_like_grid(value):
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(row, list) and row for row in value)
        and all(all(isinstance(cell, str) for cell in row) for row in value)
    )


# Compact grid encoding. The verbose list-of-lists costs the supervisor 518 output
# tokens to relay; the same board as comma-separated row strings costs 271, and is
# 284 characters to emit instead of 777, so there is less to mistype rather than
# more. The legend is only a default and can be replaced per request, so no board
# vocabulary is fixed in this file.
DEFAULT_LEGEND = {".": "normal", "#": "wall", "T": "treasure"}
ROW_SEPARATORS = "\n;/|"


def parse_legend(raw):
    """Read a legend such as ".=normal, #=wall, T=treasure" or a dict."""
    legend = dict(DEFAULT_LEGEND)
    if not raw:
        return legend
    if isinstance(raw, dict):
        legend.update({str(k): str(v) for k, v in raw.items()})
        return legend
    text = str(raw)
    if text.strip()[:1] == "{":
        try:
            return parse_legend(json.loads(text))
        except ValueError:
            pass
    for pair in re.split(r"[,;\n]", text):
        piece = pair.split("=", 1)
        if len(piece) == 2 and piece[0].strip():
            legend[piece[0].strip()] = piece[1].strip()
    return legend


def expand_compact_grid(value, legend=None):
    """Turn compact row strings into the list-of-lists the solver expects.

    Accepts a list of row strings, or one string whose rows are separated by a
    newline, semicolon, slash or pipe. Returns None when the value is not a
    compact grid, so a caller can fall through to the verbose form untouched.
    """
    legend = legend or dict(DEFAULT_LEGEND)

    if isinstance(value, str):
        rows = [part for part in re.split("[" + re.escape(ROW_SEPARATORS) + "]", value)
                if part.strip()]
    elif isinstance(value, (list, tuple)) and value and all(isinstance(r, str) for r in value):
        rows = [r for r in value if r.strip()]
    else:
        return None

    if len(rows) < 2:
        return None

    grid = []
    for row in rows:
        cells = [cell.strip() for cell in row.split(",")]
        if len(cells) < 2 or any(cell == "" for cell in cells):
            return None
        grid.append([legend.get(cell, cell) for cell in cells])

    width = len(grid[0])
    if any(len(row) != width for row in grid):
        # Ragged input is a transcription error, not a format to guess at. Refusing
        # here surfaces it as a clear failure instead of silently routing off-board.
        raise ValueError(
            "compact grid is ragged: rows have widths %s. Every row must list the "
            "same number of comma-separated cells."
            % ", ".join(str(len(row)) for row in grid))
    return grid


def _grid_from_string(text):
    """Pull a nested list-of-strings literal out of a prompt string."""
    depth, start = 0, None
    for index, char in enumerate(text):
        if char == "[":
            if depth == 0:
                start = index
            depth += 1
        elif char == "]" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blob = text[start:index + 1]
                if '"' not in blob and "'" not in blob:
                    continue
                for candidate in (blob, blob.replace("'", '"')):
                    try:
                        parsed = json.loads(candidate)
                    except (ValueError, TypeError):
                        continue
                    if _looks_like_grid(parsed):
                        return parsed
    return None


def _walk(node, strings, grids, depth=0):
    """Recursively harvest every string and every grid-shaped list."""
    if depth > 12:
        return
    if _looks_like_grid(node):
        grids.append(node)
        return
    if isinstance(node, str):
        strings.append(node)
        if "[" in node and len(node) < 400000:
            found = _grid_from_string(node)
            if found:
                grids.append(found)
        stripped = node.strip()
        if stripped[:1] in "{[":
            try:
                _walk(json.loads(stripped), strings, grids, depth + 1)
            except (ValueError, TypeError):
                pass
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk(value, strings, grids, depth + 1)
        return
    if isinstance(node, (list, tuple)):
        for value in node:
            _walk(value, strings, grids, depth + 1)


def flatten_options(event):
    """Collect scalar options from every calling convention we might see."""
    options = {}
    if not isinstance(event, dict):
        return options

    for param in event.get("parameters") or []:
        if isinstance(param, dict) and "name" in param:
            options[str(param["name"]).lower()] = param.get("value")

    content = (event.get("requestBody") or {}).get("content") or {}
    for payload in content.values():
        if isinstance(payload, dict):
            for prop in payload.get("properties") or []:
                if isinstance(prop, dict) and "name" in prop:
                    options.setdefault(str(prop["name"]).lower(), prop.get("value"))

    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    if isinstance(body, dict):
        for key, value in body.items():
            options.setdefault(str(key).lower(), value)

    query = event.get("queryStringParameters")
    if isinstance(query, dict):
        for key, value in query.items():
            options.setdefault(str(key).lower(), value)

    for key, value in event.items():
        options.setdefault(str(key).lower(), value)
    return options


def parse_event(event):
    """Return (grid, start_cell, text_blob, options)."""
    strings, grids = [], []
    _walk(event, strings, grids)
    options = flatten_options(event)

    grid = None
    legend = parse_legend(options.get("legend") or os.environ.get("GRID_LEGEND"))
    for key in ("map", "grid", "tiles", "board", "dungeon"):
        value = options.get(key)
        if _looks_like_grid(value):
            grid = value
            break
        compact = expand_compact_grid(value, legend)
        if compact:
            grid = compact
            break
    if grid is None and grids:
        grid = grids[0]

    text = DOCUMENT_SEPARATOR.join(strings)

    start = None
    for key in ("start", "start_position", "startposition", "position", "from"):
        value = options.get(key)
        if isinstance(value, str) and from_label(value):
            start = from_label(value)
            break
    if start is None:
        match = re.search(
            r"(?:from|at|current(?:ly)?(?:\s+at)?)\s+position\s+([A-Za-z]{1,2}\s*\d{1,3})",
            text, re.IGNORECASE)
        if match:
            start = from_label(match.group(1))
    return grid, start, text, options


# --------------------------------------------------------------------------- #
# Tile taxonomy, derived at runtime
# --------------------------------------------------------------------------- #

def _token_set(value):
    """Accept a list, a JSON array, or a comma/space separated string."""
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        text = str(value).strip()
        if text[:1] == "[":
            try:
                items = json.loads(text)
            except ValueError:
                items = re.split(r"[,\s;]+", text.strip("[]"))
        else:
            items = re.split(r"[,\s;]+", text)
    return {str(item).strip().strip("\"'").lower() for item in items if str(item).strip()}


def _pair_map(value):
    """Parse 'a->b, c->d' / {'a': 'b'} / [['a','b']] into a dict."""
    pairs = {}
    if not value:
        return pairs
    if isinstance(value, dict):
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs[str(item[0]).strip().lower()] = str(item[1]).strip().lower()
            else:
                pairs.update(_pair_map(item))
        return pairs
    text = str(value)
    if text.strip()[:1] == "{":
        try:
            return _pair_map(json.loads(text))
        except ValueError:
            pass
    for left, right in re.findall(r"([\w]+)\s*(?:->|=>|→|:)\s*([\w]+)", text):
        pairs[left.strip().lower()] = right.strip().lower()
    return pairs


def _number_map(value):
    out = {}
    if not value:
        return out
    if isinstance(value, dict):
        for key, val in value.items():
            try:
                out[str(key).strip().lower()] = int(val)
            except (TypeError, ValueError):
                pass
        return out
    text = str(value)
    if text.strip()[:1] == "{":
        try:
            return _number_map(json.loads(text))
        except ValueError:
            pass
    for key, val in re.findall(r"([\w]+)\s*[:=]\s*([-+]?\d+)", text):
        out[key.strip().lower()] = int(val)
    return out


def _colour(text):
    match = COLOUR_RE.search(text or "")
    if not match:
        return None
    word = match.group(1).lower()
    return COLOUR_ALIASES.get(word, word)


class TileConfig(object):
    """Everything the planner needs to know about tile semantics."""

    def __init__(self):
        self.walls = set()
        self.floors = set()
        self.treasures = set()
        self.hazards = set()
        self.key_doors = {}      # key id -> door id
        self.locked_doors = set()  # doors whose key could not be identified
        self.values = {}         # tile id -> points
        self.risks = {}          # tile id -> hearts lost
        self.classified = {}
        self.sources = []
        self.warnings = []

    @property
    def door_keys(self):
        """door id -> the key that opens it, or None when no key is known."""
        mapping = {door: None for door in self.locked_doors}
        mapping.update({door: key for key, door in self.key_doors.items()})
        return mapping

    def describe(self):
        return {
            "walls": sorted(self.walls),
            "treasures": sorted(self.treasures),
            "hazards": sorted(self.hazards),
            "key_doors": dict(sorted(self.key_doors.items())),
            "locked_doors": sorted(self.locked_doors),
            "values": dict(sorted(self.values.items())),
            "risks": dict(sorted(self.risks.items())),
            "classified": self.classified,
            "derived_from": self.sources,
        }


DOCUMENT_SEPARATOR = "\n\n"


def describe_tiles(text):
    """
    Slice the text into a {tile_id: (name, body)} lookup.

    `body` starts at the tile id and stops at whichever comes first: the next
    tile id, or the end of the current document. That double boundary matters -
    without it an entry absorbs its neighbour's reward badge, or worse, words
    from a completely different string that happened to be concatenated after it.

    `name` is the first line of the body, i.e. the tile's label in a challenge
    list. Classification only ever looks at `name`, so a door whose description
    happens to mention its key is still read as a door.
    """
    text = text or ""
    matches = list(TILE_ID_RE.finditer(text))
    starts = [match.start() for match in matches]
    entries = {}

    for index, match in enumerate(matches):
        tile = match.group(0).lower()
        next_start = starts[index + 1] if index + 1 < len(starts) else len(text)
        body = text[match.end():min(next_start, match.end() + 600)]
        body = body.split(DOCUMENT_SEPARATOR)[0]
        name = body.split("\n")[0][:90]
        score = (
            3 * len(TRAP_WORDS.findall(name))
            + 3 * len(KEY_WORDS.findall(name))
            + 3 * len(DOOR_WORDS.findall(name))
            + len(VALUE_RE.findall(body))
            + len(HEALTH_RE.findall(body))
        )
        current = entries.get(tile)
        if current is None or score > current[0]:
            entries[tile] = (score, name, body)

    return {tile: (name, body) for tile, (_, name, body) in entries.items()}


def classify(name):
    """
    Decide what a tile is from its label, by whichever noun appears FIRST.

    "a Crystal Gate - needs the crystal key" -> door
    "a Yellow Key - find it before the door" -> key
    """
    candidates = []
    for kind, pattern in (("hazard", TRAP_WORDS), ("key", KEY_WORDS), ("door", DOOR_WORDS)):
        match = pattern.search(name or "")
        if match:
            candidates.append((match.start(), kind))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def derive_config(text, options, env=None):
    env = os.environ if env is None else env
    config = TileConfig()

    def pick(*keys):
        for key in keys:
            if key in options and options[key] not in (None, ""):
                return options[key]
        for key in keys:
            variable = key.upper()
            if env.get(variable):
                return env[variable]
        return None

    config.walls = _token_set(pick("walls", "wall_tiles")) or _token_set(
        env.get("WALL_TILES") or DEFAULT_WALL_WORDS)
    config.floors = _token_set(pick("floors", "floor_tiles")) or _token_set(
        env.get("FLOOR_TILES") or DEFAULT_FLOOR_WORDS)
    config.treasures = _token_set(pick("treasures", "treasure_tiles", "goal_tiles")) or _token_set(
        env.get("TREASURE_TILES") or DEFAULT_TREASURE_WORDS)

    # ---- explicit configuration wins -------------------------------------- #
    explicit_hazards = _token_set(pick("avoid", "avoid_tiles", "hazards", "hazard_tiles"))
    if explicit_hazards:
        config.hazards |= {t for t in explicit_hazards if t not in config.walls}
        config.sources.append("hazards: explicit option/env")

    explicit_pairs = _pair_map(pick("key_doors", "key_door_pairs", "keys_to_doors"))
    if explicit_pairs:
        config.key_doors.update(explicit_pairs)
        config.sources.append("key_doors: explicit option/env")

    config.values.update(_number_map(pick("values", "tile_values")))
    config.risks.update(_number_map(pick("risks", "tile_risks", "risky_tiles")))

    # ---- mine the prompt text --------------------------------------------- #
    text = text or ""
    entries = describe_tiles(text)

    for span in AVOID_RE.findall(text):
        found = {tile.lower() for tile in TILE_ID_RE.findall(span)}
        found |= {word for word in re.findall(r"[a-z_]+", span.lower())
                  if word in config.walls or word in config.treasures}
        found -= config.walls | config.treasures
        if found:
            config.hazards |= found
            config.sources.append("hazards: 'avoid' instruction in prompt")

    keys_seen, doors_seen = [], []
    for tile in sorted(entries):
        name, body = entries[tile]

        value_match = VALUE_RE.search(body)
        if value_match and tile not in config.values:
            config.values[tile] = int(value_match.group(1).replace(",", ""))

        health_match = HEALTH_RE.search(body)
        if health_match and tile not in config.risks:
            cost = next(group for group in health_match.groups() if group)
            config.risks[tile] = int(cost)

        kind = classify(name)
        if kind == "hazard":
            config.hazards.add(tile)
            config.sources.append("hazards: trap wording for %s" % tile)
        elif kind == "door":
            doors_seen.append((tile, _colour(name)))
        elif kind == "key":
            keys_seen.append((tile, _colour(name)))

        # a tile that costs health and awards nothing is a trap, whatever it is called
        if (config.risks.get(tile, 0) > 0 and config.values.get(tile, 0) == 0
                and tile not in config.hazards):
            config.hazards.add(tile)
            config.sources.append("hazards: %s costs health for no points" % tile)

    config.classified = {
        "keys": [tile for tile, _ in keys_seen],
        "doors": [tile for tile, _ in doors_seen],
    }

    for left, right in PAIR_ARROW_RE.findall(text) + PAIR_BEFORE_RE.findall(text):
        left, right = left.lower(), right.lower()
        if left != right and left not in config.key_doors:
            config.key_doors[left] = right
            config.sources.append("key_doors: explicit '%s -> %s' in prompt" % (left, right))

    unpaired_keys = [item for item in keys_seen if item[0] not in config.key_doors]
    unpaired_doors = [item for item in doors_seen if item[0] not in config.key_doors.values()]
    for key_tile, key_colour in list(unpaired_keys):
        match = next((d for d in unpaired_doors if key_colour and d[1] == key_colour), None)
        if match:
            config.key_doors[key_tile] = match[0]
            unpaired_doors.remove(match)
            unpaired_keys.remove((key_tile, key_colour))
            config.sources.append("key_doors: %s -> %s matched on colour '%s'"
                                  % (key_tile, match[0], key_colour))
    if len(unpaired_keys) == len(unpaired_doors) and unpaired_keys:
        for (key_tile, _), (door_tile, _) in zip(sorted(unpaired_keys), sorted(unpaired_doors)):
            config.key_doors[key_tile] = door_tile
            config.sources.append("key_doors: %s -> %s matched by order" % (key_tile, door_tile))
    elif unpaired_doors:
        config.locked_doors |= {tile for tile, _ in unpaired_doors}
        config.warnings.append(
            "doors %s have no identifiable key; treating them as impassable"
            % sorted(config.locked_doors))

    if not config.hazards:
        config.warnings.append(
            "no hazard tiles could be identified - declare them in the navigation "
            "prompt (e.g. 'avoid: c8') or via the AVOID_TILES env var")
    return config


def parse_strategy(text, options, env=None):
    env = os.environ if env is None else env
    explicit = None
    for key in ("strategy", "mode", "plan"):
        if options.get(key) and isinstance(options[key], str):
            explicit = options[key].strip().lower()
            break
    if explicit is None and env.get("STRATEGY"):
        explicit = env["STRATEGY"].strip().lower()

    blob = " ".join(
        str(value) for value in list(options.values()) + [text or ""]
        if value is not None and not isinstance(value, (dict, list))
    ).lower()

    known = ("collect_all", "swift", "coins", "safe")
    strategy = explicit if explicit in known else None
    if strategy is None:
        if re.search(r"collect[_ ]?all|all challenges?|complete[_ ]?all|full[_ ]?clear", blob):
            strategy = "collect_all"
        elif re.search(r"\bswift\b|\bdirect\b|straight to the treasure|speed\s*run", blob):
            strategy = "swift"
        elif re.search(r"coins?[_ ]only|only coins?", blob):
            strategy = "coins"
        elif re.search(r"\bsafe\b|no[_ ]risk|preserve (?:hp|health|hearts)", blob):
            strategy = "safe"
        else:
            strategy = "collect_all"

    budget = int(env.get("STEP_BUDGET") or DEFAULT_STEP_BUDGET)
    match = re.search(r"(?:budget|max[_ ]?steps|step[_ ]?limit)\D{0,4}(\d{2,5})", blob)
    if match:
        budget = int(match.group(1))
    for key in ("max_steps", "maxsteps", "budget", "step_budget"):
        try:
            if options.get(key) is not None:
                budget = int(options[key])
                break
        except (TypeError, ValueError):
            pass
    return strategy, max(1, budget)


# --------------------------------------------------------------------------- #
# Grid model
# --------------------------------------------------------------------------- #

class Dungeon(object):
    def __init__(self, grid, config):
        self.config = config
        self.rows = len(grid)
        self.cols = max(len(row) for row in grid)
        filler = sorted(config.walls)[0] if config.walls else "wall"
        self.grid = [
            [str(cell).strip().lower() for cell in row] + [filler] * (self.cols - len(row))
            for row in grid
        ]

    def tile(self, cell):
        if cell is None:
            return None
        row, col = cell
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return self.grid[row][col]
        return sorted(self.config.walls)[0] if self.config.walls else "wall"

    def in_bounds(self, cell):
        row, col = cell
        return 0 <= row < self.rows and 0 <= col < self.cols

    def is_wall(self, cell):
        return self.tile(cell) in self.config.walls

    def is_hazard(self, cell):
        return self.tile(cell) in self.config.hazards

    def is_door(self, cell):
        return self.tile(cell) in self.config.door_keys

    def is_treasure(self, cell):
        return self.tile(cell) in self.config.treasures

    def is_objective(self, cell):
        """Any tile worth standing on: a challenge id that is not a hazard."""
        tile = self.tile(cell)
        if tile in self.config.walls or tile in self.config.floors:
            return False
        if tile in self.config.treasures or tile in self.config.hazards:
            return False
        return bool(TILE_ID_RE.fullmatch(tile or ""))

    def passable(self, cell, keys, allow_treasure=False):
        if not self.in_bounds(cell):
            return False
        if self.is_wall(cell) or self.is_hazard(cell):
            return False
        # Entering treasure ends the run. During challenge collection it must be
        # treated as a wall, not merely excluded from the target list; otherwise
        # BFS may use it as a shortcut and leave it again before the final leg.
        if self.is_treasure(cell) and not allow_treasure:
            return False
        if self.is_door(cell):
            required = self.config.door_keys[self.tile(cell)]
            return required is not None and required in keys
        return True

    def cells(self):
        for row in range(self.rows):
            for col in range(self.cols):
                yield (row, col)


def bfs(dungeon, start, keys, allow_treasure=False):
    """Uniform-cost BFS; treasure is blocked unless this is the final leg."""
    parents = {start: None}
    queue = deque([start])
    while queue:
        row, col = queue.popleft()
        for d_row, d_col, _ in MOVES:
            nxt = (row + d_row, col + d_col)
            if nxt in parents or not dungeon.passable(nxt, keys, allow_treasure):
                continue
            parents[nxt] = (row, col)
            queue.append(nxt)
    return parents


def rebuild(parents, target):
    if target not in parents:
        return None
    path, node = [], target
    while node is not None:
        path.append(node)
        node = parents[node]
    path.reverse()
    return path


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def collect_targets(dungeon, strategy):
    config = dungeon.config
    coin_value = min(config.values.values()) if config.values else None
    targets, treasure = [], None
    for cell in dungeon.cells():
        if dungeon.is_treasure(cell):
            treasure = cell
            continue
        if not dungeon.is_objective(cell):
            continue
        tile = dungeon.tile(cell)
        structural = tile in config.key_doors or tile in config.door_keys
        if strategy == "coins" and not structural:
            # "coins" = the risk-free tiles, i.e. the cheapest tiles that cost no health
            if config.risks.get(tile, 0) > 0:
                continue
            if coin_value is not None and config.values.get(tile, 0) > coin_value:
                continue
        if strategy == "safe" and not structural and config.risks.get(tile, 0) > 0:
            continue
        targets.append(cell)
    return targets, treasure


def rank(dungeon, cell, distance):
    """Nearest first. Ties: keys, then doors, then value, then top-left."""
    tile = dungeon.tile(cell)
    if tile in dungeon.config.key_doors:
        tier = 0
    elif tile in dungeon.config.door_keys:
        tier = 1
    else:
        tier = 2
    return (distance, tier, -dungeon.config.values.get(tile, 0), cell[0], cell[1])


def pairwise(dungeon, nodes, keys):
    """BFS distance between every pair of nodes, with `keys` assumed held."""
    table = {}
    for node in nodes:
        parents = bfs(dungeon, node, keys, allow_treasure=True)
        row = {}
        for other in nodes:
            route = rebuild(parents, other)
            if route is not None:
                row[other] = len(route) - 1
        table[node] = row
    return table


def tour_length(sequence, dist):
    total = 0
    for before, after in zip(sequence, sequence[1:]):
        step = dist.get(before, {}).get(after)
        if step is None:
            return None
        total += step
    return total


def two_opt(sequence, dist, legal):
    """
    Reverse segments while the tour gets shorter and stays legal.

    Endpoints are pinned: sequence[0] is the current position and sequence[-1] is
    the treasure, so only the interior is reordered.
    """
    best = list(sequence)
    best_length = tour_length(best, dist)
    if best_length is None:
        return sequence, None

    improved = True
    while improved:
        improved = False
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best) - 1):
                candidate = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                length = tour_length(candidate, dist)
                if length is None or length >= best_length:
                    continue
                if not legal(candidate):
                    continue
                best, best_length, improved = candidate, length, True
        # or-opt: relocate a single stop somewhere cheaper
        for i in range(1, len(best) - 1):
            stop = best[i]
            without = best[:i] + best[i + 1:]
            for j in range(1, len(without)):
                candidate = without[:j] + [stop] + without[j:]
                length = tour_length(candidate, dist)
                if length is None or length >= best_length:
                    continue
                if not legal(candidate):
                    continue
                best, best_length, improved = candidate, length, True
                break
    return best, best_length


def walk_order(dungeon, start, sequence):
    """
    Replay a visit order under the real rules: doors stay shut until their key is
    picked up. Returns (path, visits) or (None, None) if the order is not walkable.
    """
    keys, position = set(), start
    path, visits = [start], []
    if dungeon.is_objective(start) and dungeon.tile(start) in dungeon.config.key_doors:
        keys.add(dungeon.tile(start))

    for cell in sequence:
        terminal = dungeon.is_treasure(cell)
        leg = rebuild(bfs(dungeon, position, keys, allow_treasure=terminal), cell)
        if leg is None:
            return None, None
        path.extend(leg[1:])
        position = cell
        tile = dungeon.tile(cell)
        if tile in dungeon.config.key_doors:
            keys.add(tile)
        visits.append({"tile": tile, "cell": to_label(cell), "steps": len(leg) - 1})
    return path, visits


def shorten(dungeon, start, greedy_visits, treasure):
    """
    Try to walk the same set of objectives in fewer steps.

    The distance matrix is built with every key assumed held, which is a relaxation,
    so any candidate order is replayed under the strict rules before it is accepted.
    Only a strictly shorter and fully legal route wins.
    """
    stops = [from_label(visit["cell"]) for visit in greedy_visits
             if not dungeon.is_treasure(from_label(visit["cell"]))]
    if len(stops) < 4:
        return None

    all_keys = set(dungeon.config.key_doors)
    nodes = [start] + stops + ([treasure] if treasure is not None else [])
    dist = pairwise(dungeon, nodes, all_keys)

    door_keys = dungeon.config.door_keys

    def legal(candidate):
        index = {cell: position for position, cell in enumerate(candidate)}
        for cell, position in index.items():
            tile = dungeon.tile(cell)
            required = door_keys.get(tile)
            if required is None:
                continue
            holder = next((c for c in candidate if dungeon.tile(c) == required), None)
            if holder is None or index[holder] > position:
                return False
        return True

    sequence = [start] + stops + ([treasure] if treasure is not None else [])
    if not legal(sequence):
        return None

    improved, _ = two_opt(sequence, dist, legal)
    if improved == sequence:
        return None
    return walk_order(dungeon, start, improved[1:])


def plan(dungeon, start, strategy, budget, route="nearest"):
    targets, treasure = collect_targets(dungeon, strategy)
    keys, position = set(), start
    path, visits, skipped = [start], [], []

    if dungeon.is_objective(start):
        tile = dungeon.tile(start)
        if tile in dungeon.config.key_doors:
            keys.add(tile)
        if start in targets:
            targets.remove(start)
            visits.append({"tile": tile, "cell": to_label(start), "steps": 0})

    if strategy == "swift":
        targets = []

    def treasure_steps(from_cell, held):
        if treasure is None:
            return 0
        route = rebuild(bfs(dungeon, from_cell, held, allow_treasure=True), treasure)
        return len(route) - 1 if route else 0

    remaining = list(targets)
    while remaining:
        parents = bfs(dungeon, position, keys)
        reachable = []
        for cell in remaining:
            required = dungeon.config.door_keys.get(dungeon.tile(cell))
            if dungeon.is_door(cell) and (required is None or required not in keys):
                continue  # keys always before doors
            hop = rebuild(parents, cell)
            if hop:
                reachable.append((rank(dungeon, cell, len(hop) - 1), cell, hop))
        if not reachable:
            break

        reachable.sort(key=lambda item: item[0])
        _, chosen, leg = reachable[0]
        tile = dungeon.tile(chosen)
        steps = len(leg) - 1

        projected = (len(path) - 1) + steps + treasure_steps(chosen, keys | {tile})
        if projected > budget:
            break

        path.extend(leg[1:])
        position = chosen
        remaining.remove(chosen)
        if tile in dungeon.config.key_doors:
            keys.add(tile)
        visits.append({"tile": tile, "cell": to_label(chosen), "steps": steps})

    for cell in remaining:
        skipped.append({"tile": dungeon.tile(cell), "cell": to_label(cell)})

    treasure_reached = False
    if treasure is not None:
        leg = rebuild(bfs(dungeon, position, keys, allow_treasure=True), treasure)
        if leg:
            path.extend(leg[1:])
            treasure_reached = True
            visits.append({"tile": dungeon.tile(treasure), "cell": to_label(treasure),
                           "steps": len(leg) - 1})
        else:
            skipped.append({"tile": dungeon.tile(treasure), "cell": to_label(treasure)})

    # Optional second pass: same objectives, fewer steps. Shorter routes mean a
    # shorter directions array, which is what the agent pays tokens for.
    optimised = None
    if route == "short" and strategy != "swift" and len(visits) >= 4:
        attempt = shorten(dungeon, start, visits, treasure if treasure_reached else None)
        if attempt and attempt[0] is not None and len(attempt[0]) < len(path):
            candidate_path, candidate_visits = attempt
            trial = {"path": [to_label(cell) for cell in candidate_path]}
            if not validate(dungeon, trial):
                optimised = (candidate_path, candidate_visits)

    if optimised:
        before = len(path) - 1
        path, visits = optimised
        route_note = "shortened from %d to %d steps" % (before, len(path) - 1)
    else:
        route_note = "greedy nearest-first, %d steps" % (len(path) - 1)

    points = sum(dungeon.config.values.get(visit["tile"], 0) for visit in visits)
    return {
        "strategy": strategy,
        "route": route,
        "route_note": route_note,
        "path": [to_label(cell) for cell in path],
        "directions": directions(path),
        "moves": compact_moves(directions(path)),
        "steps": len(path) - 1,
        "order": visits,
        "skipped": skipped,
        "keys_collected": sorted(keys),
        "treasure_reached": treasure_reached,
        "estimated_points": points,
        "budget": budget,
    }


def directions(path):
    lookup = {(d_row, d_col): name for d_row, d_col, name in MOVES}
    return [
        lookup.get((after[0] - before[0], after[1] - before[1]), "wait")
        for before, after in zip(path, path[1:])
    ]


def compact_moves(direction_list):
    """
    The same route as one uppercase letter per move: RRRUUULLL...

    The agent is billed on output tokens, and the JSON array of 69 words costs
    about 120 of them versus roughly 17 for this string. Doing the conversion here
    rather than in the prompt matters: asking a language model to rewrite 69
    entries by hand risks dropping a move, and a dropped move can walk the agent
    onto a trap.
    """
    return "".join(str(name)[:1].upper() for name in direction_list or [])


def validate(dungeon, result):
    """Independent re-check of the produced path."""
    problems = []
    cells = [from_label(label) for label in result["path"]]
    for label, cell in zip(result["path"], cells):
        if cell is None:
            problems.append("unparseable tile %s" % label)
        elif dungeon.is_wall(cell):
            problems.append("path enters a wall at %s" % label)
        elif dungeon.is_hazard(cell):
            problems.append("path enters hazard %s at %s" % (dungeon.tile(cell), label))
    for before, after in zip(cells, cells[1:]):
        if before and after and abs(before[0] - after[0]) + abs(before[1] - after[1]) != 1:
            problems.append("non-adjacent jump %s -> %s" % (to_label(before), to_label(after)))

    held = set()
    for label, cell in zip(result["path"], cells):
        tile = dungeon.tile(cell)
        if tile in dungeon.config.key_doors:
            held.add(tile)
        if tile in dungeon.config.door_keys:
            required = dungeon.config.door_keys[tile]
            if required is None or required not in held:
                problems.append("entered door %s at %s without its key" % (tile, label))

    treasure_hits = [i for i, cell in enumerate(cells) if cell and dungeon.is_treasure(cell)]
    if treasure_hits and treasure_hits[0] != len(cells) - 1:
        problems.append("treasure is not the final tile")
    return problems


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def solve(event, env=None):
    grid, start, text, options = parse_event(event)
    if not grid:
        return {"error": "No map found in the payload.", "path": []}

    config = derive_config(text, options, env)
    dungeon = Dungeon(grid, config)

    if start is None or not dungeon.in_bounds(start):
        start = next((cell for cell in dungeon.cells() if dungeon.passable(cell, set())), (0, 0))
        config.warnings.append("start position not stated; using %s" % to_label(start))
    if not dungeon.passable(start, set()):
        return {"error": "Start tile %s (%s) is not walkable."
                         % (to_label(start), dungeon.tile(start)), "path": []}

    strategy, budget = parse_strategy(text, options, env)
    blob = " ".join(str(v) for v in list(options.values()) + [text or ""]
                    if v is not None and not isinstance(v, (dict, list))).lower()
    route = "nearest"
    if re.search(r"\bshortest\b|\bshort route\b|minimi[sz]e steps|fewest steps", blob):
        route = "short"
    if str(options.get("route") or env.get("ROUTE") if env else options.get("route") or "").lower() == "short":
        route = "short"
    result = plan(dungeon, start, strategy, budget, route)
    result["start"] = to_label(start)
    result["config"] = config.describe()
    result["warnings"] = config.warnings
    result["issues"] = validate(dungeon, result)
    result["summary"] = "%s: %d steps, %d objectives, keys=%s, treasure=%s" % (
        strategy, result["steps"], len(result["order"]),
        ",".join(result["keys_collected"]) or "none",
        "yes" if result["treasure_reached"] else "no",
    )
    return result


# The only fields the supervisor prompt reads. Everything else is diagnostics.
COMPACT_FIELDS = ("moves", "directions", "steps", "treasure_reached")


def compact(result):
    """
    Strip the response down to what the caller actually acts on.

    Everything returned here is read back by the agent as tool output, and those
    are billed tokens. On the current board the full response is ~2990 characters
    of which the supervisor uses ~570: `order`, `path` and `config` alone account
    for 70% of it and are never referenced. Diagnostics stay available by asking
    for them explicitly with verbose.
    """
    if "error" in result:
        return {"error": result["error"], "directions": []}
    lean = {key: result[key] for key in COMPACT_FIELDS if key in result}
    # keep a diagnosis channel open, but only when something is actually wrong
    for key in ("issues", "warnings", "skipped"):
        if result.get(key):
            lean[key] = result[key]
    return lean


def _wants_verbose(event):
    if isinstance(event, dict):
        for key in ("verbose", "debug", "full"):
            value = event.get(key)
            if isinstance(value, bool):
                if value:
                    return True
            elif isinstance(value, str) and value.strip().lower() in ("1", "true", "yes"):
                return True
    return str(os.environ.get("VERBOSE_RESPONSE", "")).lower() in ("1", "true", "yes")


def lambda_handler(event, context=None):
    try:
        result = solve(event or {})
    except Exception as exc:  # never break the game loop
        result = {"error": "%s: %s" % (type(exc).__name__, exc), "path": []}

    if not _wants_verbose(event):
        result = compact(result)

    body = json.dumps(result)

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
    import sys

    source = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin
    with source:
        payload = json.load(source)
    print(json.dumps(lambda_handler(payload), indent=2))
