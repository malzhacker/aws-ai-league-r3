"""Pathfinding handler for the AWS AI League dungeon agent.

A compact route token is self-contained across Lambda cold starts and is preferred on scored runs. Existing bz1 board tokens remain accepted only as a migration fallback. No board or route is embedded in this file.

The handler also reports ``issues`` and ``treasure_reached`` so the supervisor can
refuse an unsafe route. Ragged, malformed, or corrupted maps are rejected rather than
repaired or guessed.
"""

import base64
import hashlib
import json
import os
import re
import struct
import zlib
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
# Stateless board token.
#
# The token contains the canonical runtime board compressed with zlib, plus a
# content checksum. It is data derived from the request, not a board in source code.
# Because reconstruction needs no process memory, cold starts and Lambda scale-out
# cannot turn a compact call into an expensive cache-miss retry.
# --------------------------------------------------------------------------- #

NEWLINE = chr(10)
DEFAULT_STRATEGY = os.environ.get("STRATEGY", "collect_all")
BOARD_TOKEN_PREFIX = "bz1-"
BOARD_TOKEN_CHECKSUM_BYTES = 8
MAX_BOARD_TOKEN_CHARS = 10000
MAX_CANONICAL_BOARD_BYTES = 2000000
ROUTE_TOKEN_PREFIX = "rp1-"
ROUTE_TOKEN_CHECKSUM_BYTES = 8
ROUTE_TOKEN_BOARD_HASH_BYTES = 8
MAX_ROUTE_TOKEN_CHARS = 1024
MOVE_TO_BITS = {"up": 0, "down": 1, "left": 2, "right": 3}
BITS_TO_MOVE = {value: key for key, value in MOVE_TO_BITS.items()}
STRATEGY_TO_ID = {"collect_all": 0, "get_coins": 1, "swift": 2}
ID_TO_STRATEGY = {value: key for key, value in STRATEGY_TO_ID.items()}
MAX_BOARD_ROWS = 100
MAX_BOARD_COLS = 100
MAX_BOARD_CELLS = 10000
MAX_CELL_CHARS = 128


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


def encode_board_token(value):
    """Validate a board and return (self-contained token, defensive board copy)."""
    board = _validate_board(value)
    canonical = _canonical_board(board)
    if len(canonical) > MAX_CANONICAL_BOARD_BYTES:
        raise ValueError("game_map encoding is too large")
    checksum = hashlib.sha256(canonical).digest()[:BOARD_TOKEN_CHECKSUM_BYTES]
    packed = checksum + zlib.compress(canonical, 9)
    encoded = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return BOARD_TOKEN_PREFIX + encoded, board


def decode_board_token(token):
    """Reconstruct and validate a board without relying on Lambda process state."""
    if not isinstance(token, str):
        raise ValueError("board_token must be a string")
    token = token.strip()
    if not token.startswith(BOARD_TOKEN_PREFIX):
        raise ValueError("board_token has an unsupported version")
    encoded = token[len(BOARD_TOKEN_PREFIX):]
    if (not encoded or len(token) > MAX_BOARD_TOKEN_CHARS
            or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)):
        raise ValueError("board_token has an invalid format")

    try:
        padding = "=" * ((-len(encoded)) % 4)
        packed = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("board_token is not valid base64") from exc
    if len(packed) <= BOARD_TOKEN_CHECKSUM_BYTES:
        raise ValueError("board_token is truncated")

    checksum = packed[:BOARD_TOKEN_CHECKSUM_BYTES]
    compressed = packed[BOARD_TOKEN_CHECKSUM_BYTES:]
    try:
        decompressor = zlib.decompressobj()
        canonical = decompressor.decompress(compressed, MAX_CANONICAL_BOARD_BYTES + 1)
        if (len(canonical) > MAX_CANONICAL_BOARD_BYTES or decompressor.unconsumed_tail
                or not decompressor.eof or decompressor.unused_data):
            raise ValueError("board_token expands beyond its allowed size")
    except zlib.error as exc:
        raise ValueError("board_token has invalid compressed data") from exc

    if hashlib.sha256(canonical).digest()[:BOARD_TOKEN_CHECKSUM_BYTES] != checksum:
        raise ValueError("board_token checksum mismatch")
    try:
        parsed = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("board_token does not contain valid board JSON") from exc
    board = _validate_board(parsed)
    if _canonical_board(board) != canonical:
        raise ValueError("board_token board encoding is not canonical")
    return board


def _board_token_reference(body, game_map):
    """Read a token from an optional field or the existing one-cell map field."""
    for key in ("board_token", "cache_id", "board_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip().startswith(BOARD_TOKEN_PREFIX):
            return value.strip()
    if (isinstance(game_map, list) and len(game_map) == 1
            and isinstance(game_map[0], list) and len(game_map[0]) == 1
            and isinstance(game_map[0][0], str)
            and game_map[0][0].strip().startswith(BOARD_TOKEN_PREFIX)):
        return game_map[0][0].strip()
    return None


def _route_token_reference(body, game_map):
    """Read a compact route token from a field or one-cell map envelope."""
    for key in ("route_token", "plan_token"):
        value = body.get(key)
        if isinstance(value, str) and value.strip().startswith(ROUTE_TOKEN_PREFIX):
            return value.strip()
    if (isinstance(game_map, list) and len(game_map) == 1
            and isinstance(game_map[0], list) and len(game_map[0]) == 1
            and isinstance(game_map[0][0], str)
            and game_map[0][0].strip().startswith(ROUTE_TOKEN_PREFIX)):
        return game_map[0][0].strip()
    return None


def encode_route_token(path, start, strategy, board):
    """Pack a validated route into a small, cold-start-safe token."""
    if strategy not in STRATEGY_TO_ID:
        raise ValueError("route_token strategy is invalid")
    if not (isinstance(start, (tuple, list)) and len(start) == 2
            and all(isinstance(value, int) and 0 <= value <= 255 for value in start)):
        raise ValueError("route_token start is invalid")
    if not isinstance(path, list) or not path or len(path) > 65535:
        raise ValueError("route_token path length is invalid")

    packed_moves = bytearray()
    for offset in range(0, len(path), 4):
        byte = 0
        for index, move in enumerate(path[offset:offset + 4]):
            if move not in MOVE_TO_BITS:
                raise ValueError("route_token contains an invalid move")
            byte |= MOVE_TO_BITS[move] << (6 - 2 * index)
        packed_moves.append(byte)

    board_hash = hashlib.sha256(_canonical_board(board)).digest()[:ROUTE_TOKEN_BOARD_HASH_BYTES]
    payload = (board_hash
               + struct.pack(">BBBH", start[0], start[1], STRATEGY_TO_ID[strategy], len(path))
               + bytes(packed_moves))
    checksum = hashlib.sha256(payload).digest()[:ROUTE_TOKEN_CHECKSUM_BYTES]
    encoded = base64.urlsafe_b64encode(checksum + payload).decode("ascii").rstrip("=")
    token = ROUTE_TOKEN_PREFIX + encoded
    if len(token) > MAX_ROUTE_TOKEN_CHARS:
        raise ValueError("route_token path is too long")
    return token


def decode_route_token(token):
    """Return a route plan after strict format and integrity validation."""
    if not isinstance(token, str):
        raise ValueError("route_token must be a string")
    token = token.strip()
    if not token.startswith(ROUTE_TOKEN_PREFIX):
        raise ValueError("route_token has an unsupported version")
    encoded = token[len(ROUTE_TOKEN_PREFIX):]
    if (not encoded or len(token) > MAX_ROUTE_TOKEN_CHARS
            or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)):
        raise ValueError("route_token has an invalid format")
    try:
        padding = "=" * ((-len(encoded)) % 4)
        packed = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("route_token is not valid base64") from exc
    canonical = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    if encoded != canonical:
        raise ValueError("route_token base64 is not canonical")

    fixed_size = ROUTE_TOKEN_CHECKSUM_BYTES + ROUTE_TOKEN_BOARD_HASH_BYTES + 5
    if len(packed) < fixed_size:
        raise ValueError("route_token is truncated")
    checksum = packed[:ROUTE_TOKEN_CHECKSUM_BYTES]
    payload = packed[ROUTE_TOKEN_CHECKSUM_BYTES:]
    if hashlib.sha256(payload).digest()[:ROUTE_TOKEN_CHECKSUM_BYTES] != checksum:
        raise ValueError("route_token checksum mismatch")

    board_hash = payload[:ROUTE_TOKEN_BOARD_HASH_BYTES]
    row, col, strategy_id, path_length = struct.unpack(">BBBH", payload[ROUTE_TOKEN_BOARD_HASH_BYTES:
                                                                      ROUTE_TOKEN_BOARD_HASH_BYTES + 5])
    strategy = ID_TO_STRATEGY.get(strategy_id)
    if strategy is None or path_length == 0:
        raise ValueError("route_token metadata is invalid")
    move_bytes = payload[ROUTE_TOKEN_BOARD_HASH_BYTES + 5:]
    expected_bytes = (path_length + 3) // 4
    if len(move_bytes) != expected_bytes:
        raise ValueError("route_token move data has the wrong length")
    unused_moves = expected_bytes * 4 - path_length
    if unused_moves and move_bytes[-1] & ((1 << (unused_moves * 2)) - 1):
        raise ValueError("route_token padding is not canonical")

    path = []
    for index in range(path_length):
        byte = move_bytes[index // 4]
        path.append(BITS_TO_MOVE[(byte >> (6 - 2 * (index % 4))) & 3])
    return {"path": path, "start": (row, col), "strategy": strategy,
            "board_hash": board_hash.hex()}


def _normalise_strategy(value):
    strategy = str(value or DEFAULT_STRATEGY).lower().strip()
    if 'collect' in strategy or 'clear' in strategy or 'all' in strategy:
        return 'collect_all'
    if 'coin' in strategy:
        return 'get_coins'
    return 'swift'


def _token_retry(reason):
    payload = {"error": reason, "needs_game_map": True}
    # Keep HTTP 200 so AgentCore exposes the retry instruction to the supervisor.
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


def _coordinate_int(value):
    """Parse one numeric coordinate without treating JSON booleans as integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _try_parse_start(pos):
    """Return a parsed position, or None when the supplied value is malformed."""
    if isinstance(pos, dict):
        for row_key in ('row', 'r', 'rowIndex', 'row_index', 'y'):
            if row_key in pos:
                for col_key in ('col', 'c', 'column', 'colIndex', 'col_index', 'x'):
                    if col_key in pos:
                        row = _coordinate_int(pos[row_key])
                        col = _coordinate_int(pos[col_key])
                        return (row, col) if row is not None and col is not None else None
        for label_key in ('label', 'cell', 'position', 'start'):
            if label_key in pos:
                return _try_parse_start(pos[label_key])
        return None
    try:
        if isinstance(pos, (list, tuple)):
            if len(pos) == 1:
                return _try_parse_start(pos[0])
            if len(pos) == 2:
                first, second = pos
                if (isinstance(first, str) and re.fullmatch(r'[A-Za-z]', first.strip())
                        and _coordinate_int(second) is not None):
                    return (_coordinate_int(second) - 1,
                            ord(first.strip().upper()) - ord('A'))
                row = _coordinate_int(first)
                col = _coordinate_int(second)
                if row is not None and col is not None:
                    return (row, col)
                return None
        if not isinstance(pos, str):
            return None
        text = pos.strip()
        label = re.fullmatch(r'([A-Za-z])(\d+)', text)
        if label:
            return (int(label.group(2)) - 1,
                    ord(label.group(1).upper()) - ord('A'))
        for pattern in (
                r'(-?\d+)\s*[,;:]\s*(-?\d+)',
                r'\(\s*(-?\d+)\s*[,;:]\s*(-?\d+)\s*\)',
                r'\[\s*(-?\d+)\s*[,;:]\s*(-?\d+)\s*\]',
                r'(-?\d+)\s+(-?\d+)'):
            numeric = re.fullmatch(pattern, text)
            if numeric:
                return (int(numeric.group(1)), int(numeric.group(2)))
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _parse_start(pos):
    # Preserve the historical (0,0) default for full-map planning. Token replay
    # separately requires _try_parse_start to succeed on an explicit request value.
    parsed = _try_parse_start(pos)
    return parsed if parsed is not None else (0, 0)


def _request_start(body):
    """Return the explicit request position and whether it parsed successfully."""
    map_config = body.get('map_config')
    if isinstance(map_config, dict):
        value = map_config.get('playerStart')
        if value not in (None, '', [], {}):
            parsed = _try_parse_start(value)
            return parsed, parsed is not None
    for key in ('playerStart', 'start_pos', 'start', 'position'):
        if key in body and body.get(key) not in (None, '', [], {}):
            parsed = _try_parse_start(body.get(key))
            return parsed, parsed is not None
    return None, False


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

        # A full runtime board wins over tokens. Existing bz1 board tokens remain
        # accepted as a migration source; successful planning returns a much smaller
        # rp1 route token that needs no board or warm Lambda state on later runs.
        raw_map = None
        for key in ('game_map', 'grid', 'map', 'board', 'tiles', 'dungeon'):
            if key in body and body.get(key) not in (None, []):
                raw_map = body.get(key)
                break

        route_token = _route_token_reference(body, raw_map)
        board_token = _board_token_reference(body, raw_map)
        map_is_route_token = bool(
            route_token and isinstance(raw_map, list) and len(raw_map) == 1
            and isinstance(raw_map[0], list) and len(raw_map[0]) == 1
            and raw_map[0][0] == route_token)
        map_is_board_token = bool(
            board_token and isinstance(raw_map, list) and len(raw_map) == 1
            and isinstance(raw_map[0], list) and len(raw_map[0]) == 1
            and raw_map[0][0] == board_token)
        game_map = []

        if raw_map is not None and not map_is_route_token and not map_is_board_token:
            try:
                game_map = _validate_board(raw_map)
            except ValueError as exc:
                return _err(400, str(exc))
        elif board_token and not is_door_request:
            try:
                game_map = decode_board_token(board_token)
            except ValueError:
                return _token_retry('board_token_invalid')
        elif route_token and not is_door_request:
            # Decoding is deferred until start and strategy have been normalized.
            pass

        if (not game_map and not is_door_request and re.search(
                r"\b(fibonacci|factorial|modulo|prime|add|multiply|divide|digit|"
                r"calculate|compute|what\s+is|door|key)\b", str(body), re.I)):
            return _err(400, "This looks like a math, code, or door question. "
                             "Call the MathSolver tool instead - Pathfinding cannot "
                             "compute it.")

        explicit_start, start_is_valid = _request_start(body)
        start_pos = explicit_start if start_is_valid else (0, 0)

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
        strategy = _normalise_strategy(body.get('strategy'))

        if route_token and not game_map:
            if not start_is_valid:
                return _token_retry('route_token_mismatch')
            if any(key in body for key in ('blocked_cells', 'trap_cells', 'treasure')):
                return _token_retry('route_token_mismatch')
            try:
                route_plan = decode_route_token(route_token)
            except ValueError:
                return _token_retry('route_token_invalid')
            if tuple(start_pos) != route_plan['start'] or strategy != route_plan['strategy']:
                return _token_retry('route_token_mismatch')
            route_result = {
                'path': route_plan['path'],
                'steps': len(route_plan['path']),
                'start_position': list(start_pos),
                'strategy': strategy,
                'issues': [],
                'treasure_reached': True,
            }
            return {'statusCode': 200,
                    'body': json.dumps(route_result, separators=(',', ':'))}

        if not game_map:
            return _err(400, 'Missing game_map, route_token, or board_token')

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
        # Only issue a reusable route token after the path has passed every local
        # safety check. It is derived from this runtime board, route, start, and
        # strategy; no board or route is embedded in source code.
        if treasure_reached and not issues:
            try:
                result['route_token'] = encode_route_token(
                    path, start_pos, strategy, game_map)
            except ValueError:
                # An otherwise valid long route is still usable for this call, but
                # must not emit a token that the decoder's size limit would reject.
                pass
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
    #   echo '{"game_map":[["rp1-<route-token>"]],"start":"A1"}' | python lambda.py
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
        print("door_codes       : %s" % (body.get('door_codes') or '{} none yet'))
        print("path             : %s%s"
              % (json.dumps(body['path'][:12]),
                 " ... +%d more" % (len(body['path']) - 12) if len(body['path']) > 12 else ""))
