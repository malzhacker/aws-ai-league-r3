"""Pathfinding handler for the AWS AI League dungeon agent.

The first full runtime map is validated, solved, and registered under a derived
content ID. Later warm invocations can send that ID, the current top/bottom
fingerprint rows, and the start position. No board is embedded in this file, and a
missing or boundary-mismatched cache entry fails explicitly instead of routing on a
guessed default board.

The handler also reports ``issues`` and ``treasure_reached`` so the supervisor can
refuse an unsafe route. Ragged or malformed maps are rejected rather than repaired.
"""

import hashlib
import json
import os
import re
from collections import OrderedDict, deque

CELL_POINTS = {"c7": 250}
COLLECTIBLE_COINS = {"c7"}
KEYS = {"c42", "c43"}
DOORS = {"c32": "c42", "c33": "c43"}
CHALLENGES = {"c1", "c2", "c4", "c5", "c17", "c18"}
DAMAGE_CELLS = {"c8", "trap"}
DIRECTIONS = [(-1, 0, "up"), (1, 0, "down"), (0, -1, "left"), (0, 1, "right")]

DOOR_HINTS = {
    "c33": "unlock code = 5th and 7th characters of the yellow key",
    "c32": "unlock code = first 2 and last 2 characters of the grey key",
}

# --------------------------------------------------------------------------- #
# Runtime board cache.
#
# A full map registers itself automatically. Its ID is derived from canonical map
# content, so a caller cannot choose an ID and poison another entry. The cache is
# deliberately bounded and contains no fallback board. Lambda process memory is
# best-effort: a cold execution environment returns ``cache_miss`` and asks for the
# full map again rather than solving a guessed board.
# --------------------------------------------------------------------------- #

NEWLINE = chr(10)
DEFAULT_STRATEGY = os.environ.get("STRATEGY", "collect_all")
CACHE_ID_PREFIX = "b1-"
MAX_CACHED_BOARDS = max(1, min(64, int(os.environ.get("MAX_CACHED_BOARDS", "8"))))
MAX_BOARD_ROWS = 100
MAX_BOARD_COLS = 100
MAX_BOARD_CELLS = 10000
MAX_CELL_CHARS = 128
BOARD_CACHE = OrderedDict()


def _validate_board(value):
    """Return a defensive board copy or raise ValueError with a useful reason."""
    if not isinstance(value, list) or not value:
        raise ValueError("game_map must be a non-empty array of rows")
    if len(value) > MAX_BOARD_ROWS:
        raise ValueError("game_map has too many rows")

    board = []
    width = None
    for row_index, row in enumerate(value):
        if not isinstance(row, list) or not row:
            raise ValueError("game_map row %d must be a non-empty array" % row_index)
        if width is None:
            width = len(row)
            if width > MAX_BOARD_COLS:
                raise ValueError("game_map has too many columns")
        elif len(row) != width:
            raise ValueError(
                "Ragged map: rows have widths %s. Every row must have the same "
                "number of cells; re-send the short row without padding."
                % ", ".join(str(v) for v in sorted({width, len(row)})))
        clean_row = []
        for col_index, cell in enumerate(row):
            if not isinstance(cell, str):
                raise ValueError(
                    "game_map cell (%d,%d) must be a string" % (row_index, col_index))
            if not cell or len(cell) > MAX_CELL_CHARS:
                raise ValueError(
                    "game_map cell (%d,%d) has an invalid length" % (row_index, col_index))
            clean_row.append(cell)
        board.append(clean_row)

    if len(board) * width > MAX_BOARD_CELLS:
        raise ValueError("game_map has too many cells")
    return board


def _canonical_board(board):
    return json.dumps(board, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _cache_id_for(canonical):
    """Return a globally deterministic 128-bit content address."""
    return CACHE_ID_PREFIX + hashlib.sha256(canonical).hexdigest()[:32]


def register_board(value):
    board = _validate_board(value)
    canonical = _canonical_board(board)
    cache_id = _cache_id_for(canonical)
    existing = BOARD_CACHE.get(cache_id)
    if existing is not None and _canonical_board(existing) != canonical:
        raise ValueError("board cache hash collision")
    BOARD_CACHE[cache_id] = tuple(tuple(row) for row in board)
    BOARD_CACHE.move_to_end(cache_id)
    while len(BOARD_CACHE) > MAX_CACHED_BOARDS:
        BOARD_CACHE.popitem(last=False)
    return cache_id, [row[:] for row in board]


def get_cached_board(cache_id):
    if not isinstance(cache_id, str):
        return None
    cache_id = cache_id.strip().lower()
    if not re.fullmatch(r"b1-[0-9a-f]{32}", cache_id):
        return None
    stored = BOARD_CACHE.get(cache_id)
    if stored is None:
        return None
    BOARD_CACHE.move_to_end(cache_id)
    return [list(row) for row in stored]


def _row_fingerprint(value):
    if isinstance(value, list) and value and all(isinstance(cell, str) for cell in value):
        return list(value)
    if isinstance(value, str) and value.strip():
        parts = [part.strip().strip('"\'') for part in value.split(',')]
        if len(parts) > 1 and all(parts):
            return parts
    return None


def _cache_reference(body, game_map):
    """Return (ID, top row, bottom row) from fields or a map-shaped envelope.

    The envelope keeps the deployed ``game_map`` schema: two rows, each prefixed
    with the same cache ID. The remaining cells are copied top/bottom fingerprints
    from the current challenge, preventing an old remembered ID from silently
    selecting a different cached board whose boundary changed.
    """
    declared = None
    for key in ("cache_id", "board_id", "board_cache_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            declared = value.strip().lower()
            break
    if declared:
        return (declared, _row_fingerprint(body.get("first_row")),
                _row_fingerprint(body.get("last_row")))

    if (isinstance(game_map, list) and len(game_map) == 2
            and all(isinstance(row, list) and len(row) >= 2 for row in game_map)
            and all(isinstance(cell, str) for row in game_map for cell in row)):
        first_id = game_map[0][0].strip().lower()
        last_id = game_map[1][0].strip().lower()
        if first_id == last_id and first_id.startswith(CACHE_ID_PREFIX):
            return first_id, list(game_map[0][1:]), list(game_map[1][1:])
    return None, None, None


def _cache_retry(reason, cache_id):
    payload = {"error": reason, "cache_id": cache_id, "needs_game_map": True}
    # Keep HTTP 200 so AgentCore surfaces the retry payload instead of replacing a
    # non-2xx response with a generic tool failure.
    return {"statusCode": 200,
            "body": json.dumps(payload, separators=(",", ":"))}


def door_unlock_code(challenge_id, key):
    try:
        if challenge_id == "c33":
            return key[4] + key[6]
        if challenge_id == "c32":
            return key[:2] + key[-2:]
    except IndexError:
        pass
    return key


def _collect_key_map(body):
    known = {}
    raw = body.get('keys') or body.get('key_map') or {}
    if isinstance(raw, list):
        flat = {}
        for item in raw:
            if isinstance(item, dict):
                k = item.get('id') or item.get('name') or item.get('type') or ''
                flat[k] = item.get('value') or item.get('key') or ''
        raw = flat
    if isinstance(raw, dict):
        for name, value in raw.items():
            n = re.sub(r'[^a-z0-9]', '', str(name).lower())
            if n in ('c43', 'yellow', 'yellowkey', 'yellowkey1'):
                known['c33'] = str(value)
            elif n in ('c42', 'grey', 'greykey', 'greykey1'):
                known['c32'] = str(value)
    for name in ('yellow_key', 'yellowkey', 'grey_key', 'greykey'):
        if body.get(name):
            n = re.sub(r'[^a-z0-9]', '', name.lower())
            if 'yellow' in n:
                known['c33'] = str(body[name])
            else:
                known['c32'] = str(body[name])
    return known


def _parse_start(pos):
    # A dict is the shape the game itself uses for position, {"row":4,"col":0}, and
    # the gateway schema is likely to declare it that way too. Reaching the string
    # branch with a dict silently produced (0,0), a wrong start with no error.
    if isinstance(pos, dict):
        for row_key in ('row', 'r', 'rowIndex', 'row_index', 'y'):
            if row_key in pos:
                for col_key in ('col', 'c', 'column', 'colIndex', 'col_index', 'x'):
                    if col_key in pos:
                        try:
                            return (int(pos[row_key]), int(pos[col_key]))
                        except (TypeError, ValueError):
                            return (0, 0)
        for label_key in ('label', 'cell', 'position', 'start'):
            if label_key in pos:
                return _parse_start(pos[label_key])
        return (0, 0)
    try:
        if isinstance(pos, (list, tuple)):
            if len(pos) == 1:
                return _parse_start(pos[0])
            if len(pos) >= 2:
                a = re.sub(r'[^A-Za-z0-9]', '', str(pos[0]))
                b = re.sub(r'[^A-Za-z0-9]', '', str(pos[1]))
                if a.isalpha():
                    return (int(b) - 1, ord(a.upper()) - ord('A'))
                return (int(a), int(b))
        s = re.sub(r'[^A-Za-z0-9]', '', str(pos))
        m = re.match(r'([A-Za-z])(\d+)', s)
        if m:
            return (int(m.group(2)) - 1, ord(m.group(1).upper()) - ord('A'))
        nums = re.findall(r'\d+', s)
        if len(nums) >= 2:
            return (int(nums[0]), int(nums[1]))
    except (ValueError, TypeError, IndexError):
        pass
    return (0, 0)


def _walkable(cell, keys):
    if cell in DAMAGE_CELLS or cell == 'wall':
        return False
    if cell in DOORS and DOORS[cell] not in keys:
        return False
    return True


def _bfs(game_map, rows, cols, start, goal, keys, blocked=None):
    blocked = blocked or set()
    queue = deque([(start[0], start[1], [])])
    visited = {(start[0], start[1])}
    while queue:
        r, c, path = queue.popleft()
        if (r, c) == goal:
            return path
        for dr, dc, move in DIRECTIONS:
            nr, nc = r + dr, c + dc
            if (nr, nc) in blocked:
                continue
            if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited \
                    and _walkable(game_map[nr][nc], keys):
                visited.add((nr, nc))
                queue.append((nr, nc, path + [move]))
    return None


def _greedy(game_map, rows, cols, start, treasure, targets, keys, blocked=None):
    blocked = blocked or set()
    board = [row[:] for row in game_map]
    board[treasure[0]][treasure[1]] = 'wall'
    r, c = start
    full_path = []
    for _ in range(200):
        queue = deque([(r, c, [])])
        visited = {(r, c)}
        found = None
        while queue:
            cr, cc, p = queue.popleft()
            cell = board[cr][cc]
            if cell in targets and (cr, cc) != (r, c):
                found = (p, cr, cc)
                break
            for dr, dc, move in DIRECTIONS:
                nr, nc = cr + dr, cc + dc
                if (nr, nc) in blocked:
                    continue
                if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited \
                        and _walkable(board[nr][nc], keys):
                    visited.add((nr, nc))
                    queue.append((nr, nc, p + [move]))
        if found is None:
            break
        p, r, c = found
        cell = board[r][c]
        full_path.extend(p)
        if cell in KEYS:
            keys.add(cell)
        board[r][c] = 'normal'
    return full_path, (r, c), keys


def _greedy_order(game_map, rows, cols, start, treasure, targets, keys, blocked=None):
    blocked = blocked or set()
    board = [row[:] for row in game_map]
    board[treasure[0]][treasure[1]] = 'wall'
    r, c = start
    order = []
    keys = set(keys)
    for _ in range(200):
        queue = deque([(r, c, None)])
        visited = {(r, c)}
        found = None
        while queue:
            cr, cc, prev = queue.popleft()
            cell = board[cr][cc]
            if cell in targets and (cr, cc) != (r, c):
                found = (cr, cc)
                break
            for dr, dc, _move in DIRECTIONS:
                nr, nc = cr + dr, cc + dc
                if (nr, nc) in blocked:
                    continue
                if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited \
                        and _walkable(board[nr][nc], keys):
                    visited.add((nr, nc))
                    queue.append((nr, nc, (cr, cc)))
        if found is None:
            break
        r, c = found
        order.append((r, c))
        if game_map[r][c] in KEYS:
            keys.add(game_map[r][c])
        board[r][c] = 'normal'
    return order


def _order_cost(game_map, rows, cols, start, treasure, order, keys, blocked):
    total = 0
    r, c = start
    k = set(keys)
    for t in order:
        leg = _bfs(game_map, rows, cols, (r, c), t, k, blocked)
        if leg is None:
            return None
        total += len(leg)
        r, c = t
        if game_map[r][c] in KEYS:
            k.add(game_map[r][c])
    leg = _bfs(game_map, rows, cols, (r, c), treasure, k, blocked)
    if leg is None:
        return None
    return total + len(leg)


def _optimize_order(game_map, rows, cols, start, treasure, order, keys, blocked):
    best = order[:]
    best_cost = _order_cost(game_map, rows, cols, start, treasure, best, keys, blocked)
    if best_cost is None:
        return best
    n = len(best)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                cost = _order_cost(game_map, rows, cols, start, treasure, cand, keys, blocked)
                if cost is not None and cost < best_cost:
                    best, best_cost = cand, cost
                    improved = True
                    break
            if improved:
                break
    return best


def _distance_matrix(game_map, rows, cols, targets, keys, blocked):
    blocked = blocked or set()
    all_keys = set(KEYS) | set(keys)
    nodes = [(r, c) for (r, c) in targets if (r, c) not in blocked]
    n = len(nodes)
    mat = [[0] * n for _ in range(n)]
    for i, a in enumerate(nodes):
        for j, b in enumerate(nodes):
            if i < j:
                d = _bfs(game_map, rows, cols, a, b, all_keys, blocked)
                if d is None:
                    return None
                mat[i][j] = mat[j][i] = len(d)
    return mat


def _optimize_order3(game_map, rows, cols, start, treasure, order, keys, blocked):
    blocked = blocked or set()
    targets = set(order)
    nodes = [(r, c) for (r, c) in sorted(targets)]
    if start not in nodes:
        nodes.insert(0, start)
    treasure_idx = len(nodes)
    nodes.append(treasure)
    mat = _distance_matrix(game_map, rows, cols, nodes, keys, blocked)
    if mat is None:
        return _optimize_order(game_map, rows, cols, start, treasure, order, keys, blocked)
    idx = {cell: i for i, cell in enumerate(nodes)}

    def cost(o):
        if not o:
            return 0
        keys_held = set(keys)
        total = mat[idx[start]][idx[o[0]]] if start != o[0] else 0
        prev = o[0]
        for cur in o[1:]:
            cell = game_map[cur[0]][cur[1]]
            if cell in DOORS and DOORS[cell] not in keys_held:
                return None
            if cell in KEYS:
                keys_held.add(cell)
            total += mat[idx[prev]][idx[cur]]
            prev = cur
        total += mat[idx[prev]][treasure_idx]
        return total

    best = order[:]
    best_cost = cost(best)
    if best_cost is None:
        return best
    n = len(best)
    improved = True
    while improved:
        improved = False
        for i in range(n - 2):
            for j in range(i + 1, n - 1):
                for k in range(j + 1, n):
                    for cand in (
                        best[:i] + best[j:k] + best[i:j] + best[k:],
                        best[:i] + best[j:k][::-1] + best[i:j] + best[k:],
                        best[:i] + best[j:k] + best[i:j][::-1] + best[k:],
                    ):
                        c = cost(cand)
                        if c is not None and c < best_cost:
                            best, best_cost = cand, c
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break
    return _optimize_order(game_map, rows, cols, start, treasure, best, keys, blocked)


def _path_from_order(game_map, rows, cols, start, treasure, order, keys, blocked):
    path = []
    r, c = start
    k = set(keys)
    for t in order:
        leg = _bfs(game_map, rows, cols, (r, c), t, k, blocked)
        if leg is None:
            return None
        path.extend(leg)
        r, c = t
        if game_map[r][c] in KEYS:
            k.add(game_map[r][c])
    leg = _bfs(game_map, rows, cols, (r, c), treasure, k, blocked)
    if leg is None:
        return None
    path.extend(leg)
    return path


def swift_path(game_map, rows, cols, start, treasure, keys=None, blocked=None):
    return _bfs(game_map, rows, cols, start, treasure, keys or set(), blocked) or []


def get_coins_path(game_map, rows, cols, start, treasure, keys=None, blocked=None):
    keys = keys or set()
    targets = COLLECTIBLE_COINS | KEYS
    full_path, pos, keys = _greedy(game_map, rows, cols, start, treasure, targets, keys, blocked)
    path_end = _bfs(game_map, rows, cols, pos, treasure, keys, blocked)
    if path_end is not None:
        full_path.extend(path_end)
        return full_path
    return swift_path(game_map, rows, cols, start, treasure, keys, blocked)


def collect_all_path(game_map, rows, cols, start, treasure, keys=None, blocked=None):
    keys = keys or set()
    blocked = blocked or set()
    targets = COLLECTIBLE_COINS | KEYS | CHALLENGES | set(DOORS)
    order = _greedy_order(game_map, rows, cols, start, treasure, targets, keys, blocked)
    order = _optimize_order3(game_map, rows, cols, start, treasure, order, keys, blocked)
    full_path = _path_from_order(game_map, rows, cols, start, treasure, order, keys, blocked)
    if full_path is None:
        full_path, pos, keys = _greedy(game_map, rows, cols, start, treasure, targets, keys, blocked)
        path_end = _bfs(game_map, rows, cols, pos, treasure, keys, blocked)
        if path_end is not None:
            full_path.extend(path_end)
            return full_path
        return swift_path(game_map, rows, cols, start, treasure, keys, blocked)
    return full_path


def lambda_handler(event, context=None):
    try:
        if not isinstance(event, dict):
            return _err(400, "Event must be a JSON object")
        if 'body' in event:
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        else:
            body = event
        if not isinstance(body, dict):
            return _err(400, "Request body must be a JSON object")

        # A door-code request carries no board and needs none.
        is_door_request = bool(body.get('door_code') or body.get('door')
                               or body.get('door_id') or body.get('challenge_id')
                               or body.get('door_type'))

        # A full runtime board always wins and registers itself automatically. A
        # later request may use cache_id plus boundary rows, or a two-row
        # [[ID,...top],[ID,...bottom]] envelope inside game_map when the deployed
        # Gateway schema exposes only the existing map field.
        raw_map = None
        for key in ('game_map', 'grid', 'map', 'board', 'tiles', 'dungeon'):
            if key in body and body.get(key) not in (None, []):
                raw_map = body.get(key)
                break

        cache_id, sent_top, sent_bottom = _cache_reference(body, raw_map)
        map_is_cache_envelope = bool(
            cache_id and isinstance(raw_map, list) and len(raw_map) == 2
            and all(isinstance(row, list) and row for row in raw_map)
            and all(str(row[0]).strip().lower() == cache_id for row in raw_map))
        game_map = []
        cache_status = None

        if raw_map is not None and not map_is_cache_envelope:
            try:
                cache_id, game_map = register_board(raw_map)
            except ValueError as exc:
                return _err(400, str(exc))
            cache_status = 'registered'
        elif cache_id and not is_door_request:
            game_map = get_cached_board(cache_id)
            if game_map is None:
                return _cache_retry('cache_miss', cache_id)
            if sent_top is None or sent_bottom is None:
                return _cache_retry('cache_signature_required', cache_id)
            if sent_top != game_map[0] or sent_bottom != game_map[-1]:
                return _cache_retry('cache_mismatch', cache_id)
            cache_status = 'hit'

        if (not game_map and not is_door_request and re.search(
                r"\b(fibonacci|factorial|modulo|prime|add|multiply|divide|digit|"
                r"calculate|compute|what\s+is|door|key)\b", str(body), re.I)):
            return _err(400, "This looks like a math, code, or door question. "
                             "Call the MathSolver tool instead - Pathfinding cannot "
                             "compute it.")

        map_config = body.get('map_config', {})
        player_start = map_config.get('playerStart') or body.get('playerStart') or {}
        if isinstance(player_start, str):
            start_pos = _parse_start(player_start)
        elif isinstance(player_start, dict) and player_start:
            start_pos = _parse_start(player_start)
        else:
            raw = body.get('start_pos') or body.get('start') or body.get('position') or [0, 0]
            start_pos = _parse_start(raw)

        if game_map and (start_pos[0] < 0 or start_pos[1] < 0
                         or start_pos[0] >= len(game_map) or start_pos[1] >= len(game_map[0])):
            start_pos = (0, 0)

        door_req = (body.get('door_code') or body.get('door') or body.get('door_id')
                    or body.get('challenge_id') or body.get('door_type'))
        key_req = (body.get('key') or body.get('key_string') or body.get('key_value'))
        if door_req:
            door = str(door_req).lower()
            key = str(key_req or '')
            if door in DOOR_HINTS:
                if not key:
                    return _err(400, "Missing key")
                code = door_unlock_code(door, key)
                return {'statusCode': 200, 'body': json.dumps({
                    'door': door, 'key': key, 'code': code, 'answer': code,
                    'hint': DOOR_HINTS[door]}, separators=(',', ':'))}
            return _err(400, "Unknown door: %s" % door_req)

        # Default to collect_all so the payload never has to spend tokens saying so.
        # Override per request, or with the STRATEGY environment variable.
        strategy = str(body.get('strategy') or DEFAULT_STRATEGY).lower().strip()
        if 'collect' in strategy or 'clear' in strategy or 'all' in strategy:
            strategy = 'collect_all'
        elif 'coin' in strategy:
            strategy = 'get_coins'
        else:
            strategy = 'swift'

        if not game_map:
            return _err(400, 'Missing game_map or cache_id')

        rows, cols = len(game_map), len(game_map[0])

        blocked = set()
        for raw in (body.get('trap_cells') or body.get('blocked_cells') or []):
            blocked.add(_parse_start(raw))

        treasure = None
        if body.get('treasure'):
            treasure = _parse_start(body.get('treasure'))
        if not treasure:
            for r in range(rows):
                for c in range(cols):
                    if game_map[r][c] == 'treasure':
                        treasure = (r, c)
                        break
                if treasure:
                    break
        if not treasure:
            return _err(400, 'No treasure found on map')

        if strategy == 'collect_all':
            path = collect_all_path(game_map, rows, cols, start_pos, treasure, blocked=blocked)
        elif strategy == 'get_coins':
            path = get_coins_path(game_map, rows, cols, start_pos, treasure, blocked=blocked)
        else:
            path = swift_path(game_map, rows, cols, start_pos, treasure, blocked=blocked)

        door_hints = {d: h for d, h in DOOR_HINTS.items() if any(d in row for row in game_map)}
        door_codes = {}
        if door_hints:
            for door, key in _collect_key_map(body).items():
                if door in door_hints and key:
                    door_codes[door] = door_unlock_code(door, key)
        door_instructions = (
            "DOOR RULE - READ THIS. When the game asks a door question "
            "(e.g. 'What is yellow key 1?') the expected answer is the UNLOCK "
            "CODE, never the key string itself and never characters counted by "
            "hand. c33 = 5th and 7th characters of the yellow key; c32 = first "
            "2 and last 2 characters of the grey key. If door_codes above is "
            "non-empty, answer with EXACTLY door_codes[door] and nothing else. "
            'Otherwise call this tool with {"door_code": "c33", "key": '
            '"<key string from this run>"} or MathSolver with '
            '{"door": "c33", "key": "<key string>"} and echo only the '
            "returned code. A wrong answer costs -5 health."
        )
        # Walk the route back over the board and report what it would actually do.
        # The supervisor prompt gates on these two fields, so without them it can
        # never confirm a route is safe, and a prompt that says "otherwise output []"
        # would submit an empty move list and end the run.
        issues = []
        rr, cc = start_pos
        for move in path:
            dr, dc = {'up': (-1, 0), 'down': (1, 0),
                      'left': (0, -1), 'right': (0, 1)}[move]
            rr, cc = rr + dr, cc + dc
            if not (0 <= rr < rows and 0 <= cc < cols):
                issues.append('step leaves the board at (%d,%d)' % (rr, cc))
                break
            cell = game_map[rr][cc]
            if cell == 'wall':
                issues.append('walks into a wall at (%d,%d)' % (rr, cc))
            elif cell in DAMAGE_CELLS:
                issues.append('enters %s at (%d,%d)' % (cell, rr, cc))
        treasure_reached = (rr, cc) == tuple(treasure) and not issues
        if path and (rr, cc) != tuple(treasure):
            issues.append('path ends at (%d,%d), not on the treasure at (%d,%d)'
                          % (rr, cc, treasure[0], treasure[1]))
        if not path:
            issues.append('empty path')

        result = {'path': path, 'steps': len(path), 'start_position': list(start_pos),
                  'strategy': strategy, 'issues': issues,
                  'treasure_reached': treasure_reached,
                  'door_hints': door_hints, 'door_codes': door_codes,
                  'door_instructions': door_instructions}
        # The caller needs the ID only after a full-map registration. Cache-hit
        # responses omit it to keep the scored tool output at its minimum.
        if cache_status == 'registered':
            result['cache_id'] = cache_id
        return {'statusCode': 200, 'body': json.dumps(result, separators=(',', ':'))}

    except Exception as e:
        return _err(500, str(e))


def _err(code, msg):
    return {'statusCode': code, 'body': json.dumps({'error': msg}, separators=(',', ':'))}


if __name__ == "__main__":
    # Local test. Reads one event, or a list of them, from a file argument or stdin.
    # Needs no AWS credentials and no network:
    #
    #   echo '{"game_map":[["normal","treasure"]],"start":"A1"}' | python lambda.py
    #   echo '{"game_map":[["b1-<32 hex>","<top cells>"],["b1-<32 hex>","<bottom cells>"]],"start":"A1"}' | python lambda.py
    #
    # Prints a summary rather than the whole body, because the three fields that
    # decide whether a route is safe are steps, issues and treasure_reached.
    import sys

    source = open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin
    with source:
        payload = json.load(source)

    for case in payload if isinstance(payload, list) else [payload]:
        response = lambda_handler(case, None)
        body = json.loads(response['body'])
        if 'error' in body:
            print("FAILED  %s" % body['error'])
            continue
        print("steps            : %d" % body['steps'])
        print("issues           : %s" % (body['issues'] or '[] none'))
        print("treasure_reached : %s" % body['treasure_reached'])
        print("start_position   : %s" % body['start_position'])
        print("door_codes       : %s" % (body['door_codes'] or '{} none yet'))
        print("path             : %s%s"
              % (json.dumps(body['path'][:12]),
                 " ... +%d more" % (len(body['path']) - 12) if len(body['path']) > 12 else ""))
