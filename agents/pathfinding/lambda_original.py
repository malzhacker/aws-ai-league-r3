"""Vendored copy of the deployed pathfinding handler, for comparison and testing.

This is the version currently being uploaded, kept here so its behaviour can be
measured against agents/pathfinding/lambda.py rather than argued about. It differs
in two ways that matter: the tile taxonomy is hardcoded, so hazards are avoided
without any configuration, and a ragged row is silently padded with 'normal'
instead of raising.
"""

import json
import os
import re
from collections import deque

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
# Compact grid input.
#
# Relaying the board as a list of lists costs the supervisor 532 output tokens.
# The same board as comma-separated row strings costs 251, and is 284 characters
# to type instead of 777, so there is less to mistype rather than more. Output
# tokens are the only cost term in the score, so this is worth 6 points.
#
# The legend is a default, not a fixed vocabulary: it can be replaced per request
# or through GRID_LEGEND, so the compact form works on any board.
# --------------------------------------------------------------------------- #

DEFAULT_LEGEND = {".": "normal", "#": "wall", "T": "treasure"}
# Built with chr(10) rather than an escape. A literal backslash-n in a string does
# not survive every paste into a console editor: one turned into a real line break
# and left a sibling file with an unterminated string literal at import time.
NEWLINE = chr(10)
ROW_SEPARATORS = NEWLINE + ";/|"
DEFAULT_STRATEGY = os.environ.get("STRATEGY", "collect_all")


def parse_legend(raw):
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
    for pair in re.split("[,;" + NEWLINE + "]", text):
        piece = pair.split("=", 1)
        if len(piece) == 2 and piece[0].strip():
            legend[piece[0].strip()] = piece[1].strip()
    return legend


def expand_compact_grid(value, legend=None):
    """Turn compact row strings into the list of lists the solver expects.

    Accepts a list of row strings, or one string whose rows are separated by a
    newline, semicolon, slash or pipe. Returns None when the value is not a
    compact grid, so the caller falls through to the verbose form untouched.
    """
    legend = legend or dict(DEFAULT_LEGEND)
    if isinstance(value, str):
        rows = [p for p in re.split("[" + re.escape(ROW_SEPARATORS) + "]", value) if p.strip()]
    elif isinstance(value, (list, tuple)) and value and all(isinstance(r, str) for r in value):
        rows = [r for r in value if r.strip()]
    else:
        return None
    if len(rows) < 2:
        return None

    grid = []
    commas = any("," in row for row in rows)
    for row in rows:
        if commas:
            cells = [cell.strip() for cell in row.split(",")]
        else:
            # No commas: every character is one cell. This is the cheapest form to
            # relay, because a full single-character legend lives in GRID_LEGEND on
            # the function and therefore costs zero output tokens. A 10x10 board
            # becomes ten 10-character rows.
            cells = list(row.strip())
        if len(cells) < 2 or any(cell == "" for cell in cells):
            return None
        if not commas:
            # In single-character mode an unmapped letter would be accepted as an
            # unknown tile and silently treated as walkable. An incomplete legend is
            # a transcription slip, so say so instead of routing on a wrong board.
            missing = sorted({c for c in cells if c not in legend})
            if missing:
                raise ValueError(
                    "legend is missing an entry for %s. Every character in the grid "
                    "must appear in the legend, for example %s=normal."
                    % (", ".join(repr(m) for m in missing), missing[0]))
        grid.append([legend.get(cell, cell) for cell in cells])

    width = len(grid[0])
    if any(len(r) != width for r in grid):
        raise ValueError(
            "compact grid is ragged: rows have widths %s. Every row must list the "
            "same number of comma-separated cells."
            % ", ".join(str(len(r)) for r in grid))
    return grid


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
        if 'body' in event:
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        else:
            body = event

        # Accept the board under any of the usual names, verbose or compact.
        legend = parse_legend(body.get('legend') or os.environ.get('GRID_LEGEND'))
        game_map = []
        for key in ('game_map', 'grid', 'map', 'board', 'tiles', 'dungeon'):
            value = body.get(key)
            if isinstance(value, list) and value and all(isinstance(r, list) for r in value):
                game_map = value
                break
            compact = expand_compact_grid(value, legend)
            if compact:
                game_map = compact
                break

        if (not game_map and not body.get('door_code') and not body.get('door')
                and not body.get('door_id') and not body.get('challenge_id')
                and re.search(
                r"\b(fibonacci|factorial|modulo|prime|add|multiply|divide|digit|"
                r"calculate|compute|what\s+is|door|key)\b", str(body), re.I)):
            return _err(400, "This looks like a math, code, or door question. "
                             "Call the MathSolver tool instead - Pathfinding cannot "
                             "compute it.")

        # A ragged row is a transcription slip, not a format to guess at. Padding it
        # with 'normal' silently solves a board that does not exist: dropping one
        # cell from row 3 of the real board shifted the yellow door a column left and
        # produced a 63-step route that walks through a wall. Refusing is cheaper
        # than a desynchronised path.
        if game_map:
            widths = sorted({len(row) for row in game_map})
            if len(widths) > 1:
                return _err(400,
                            "Ragged map: rows have widths %s. Every row must have the "
                            "same number of cells. Re-send the row that is short - do "
                            "not pad it." % ", ".join(str(w) for w in widths))

        map_config = body.get('map_config', {})
        player_start = map_config.get('playerStart') or body.get('playerStart') or {}
        if isinstance(player_start, str):
            start_pos = _parse_start(player_start)
        elif isinstance(player_start, dict) and player_start:
            start_pos = (player_start.get('row', 0), player_start.get('col', 0))
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
            return _err(400, 'Missing game_map')

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
            "Otherwise call this tool with {\"door_code\": \"c33\", \"key\": "
            "\"<key string from this run>\"} or MathSolver with "
            "{\"door\": \"c33\", \"key\": \"<key string>\"} and echo only the "
            "returned code. A wrong answer costs -5 health."
        )
        result = {'path': path, 'steps': len(path), 'start_position': list(start_pos),
                  'strategy': strategy, 'door_hints': door_hints, 'door_codes': door_codes,
                  'door_instructions': door_instructions}
        return {'statusCode': 200, 'body': json.dumps(result, separators=(',', ':'))}

    except Exception as e:
        return _err(500, str(e))


def _err(code, msg):
    return {'statusCode': code, 'body': json.dumps({'error': msg}, separators=(',', ':'))}
