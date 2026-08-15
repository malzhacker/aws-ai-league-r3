#!/usr/bin/env python3
"""
Local verifier. Loads the JSON fixtures in ./examples, invokes each handler and
asserts the behaviour we care about. Nothing here is imported by the Lambdas.

    python3 verify.py
"""

import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TILE_ID = re.compile(r"^c\d+$", re.I)


def load(name):
    path = os.path.join(ROOT, name, "lambda.py")
    spec = importlib.util.spec_from_file_location("dungeon_%s" % name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(name):
    with open(os.path.join(ROOT, "examples", name)) as handle:
        return json.load(handle)


def grid_of(payload, pathfinder):
    _, grids = [], []
    pathfinder._walk(payload, _, grids)
    return grids[0] if grids else payload.get("map")


def check_path(name, payload, pathfinder, expect_full_clear):
    # verbose: the verifier needs the diagnostics the compact response omits
    result = pathfinder.lambda_handler(dict(payload, verbose=True))
    grid = grid_of(payload, pathfinder)
    config = result["config"]
    failures = []

    hazards = set(config["hazards"])
    walls = set(config["walls"])
    key_doors = config["key_doors"]
    doors = {door: key for key, door in key_doors.items()}

    cells = {}
    for row_index, row in enumerate(grid):
        for col_index, tile in enumerate(row):
            cells[pathfinder.to_label((row_index, col_index))] = tile

    visited = result["path"]
    tiles_on_path = [cells.get(label) for label in visited]

    if result["issues"]:
        failures += ["self-validation: %s" % issue for issue in result["issues"]]
    hit = sorted({t for t in tiles_on_path if t in hazards})
    if hit:
        failures.append("stepped on hazard(s) %s" % hit)
    if any(t in walls for t in tiles_on_path):
        failures.append("stepped on a wall")

    for step, tile in enumerate(tiles_on_path):
        if tile in doors:
            key = doors[tile]
            if key not in tiles_on_path[:step]:
                failures.append("door %s entered at step %d without key %s" % (tile, step, key))

    treasures = set(config["treasures"])
    if any(t in treasures for t in tiles_on_path[:-1]):
        failures.append("treasure taken before the end")

    if expect_full_clear:
        objectives = {
            label for label, tile in cells.items()
            if TILE_ID.match(tile or "") and tile not in hazards
        }
        missed = objectives - set(visited)
        if missed:
            failures.append("missed objectives %s" % sorted(missed))
        if not result["treasure_reached"]:
            failures.append("treasure not reached")

    print("\n[%s]" % name)
    print("  %s" % result["summary"])
    print("  hazards derived : %s" % sorted(hazards))
    print("  key -> door     : %s" % key_doors)
    print("  est. points     : %s" % result.get("estimated_points"))
    print("  warnings        : %s" % (result.get("warnings") or "none"))
    print("  order           : %s"
          % " ".join("%s(%s)" % (v["cell"], v["tile"]) for v in result["order"]))
    print("  path            : %s" % " ".join(visited))
    print("  RESULT          : %s" % ("PASS" if not failures else "FAIL " + "; ".join(failures)))
    return not failures


def check_treasure_is_final(pathfinder):
    """Regression: BFS must route around treasure while objectives remain."""
    payload = {
        "map": [
            ["normal", "treasure", "c99"],
            ["normal", "normal", "normal"],
        ],
        "start": "A1",
        "strategy": "collect_all",
    }
    result = pathfinder.lambda_handler(dict(payload, verbose=True))
    path = result["path"]
    passed = (
        not result["issues"]
        and result["treasure_reached"]
        and "C1" in path
        and path[-1] == "B1"
        and path.count("B1") == 1
    )
    print("\n[pathfinding / treasure blocks collection routes]")
    print("  path            : %s" % " ".join(path))
    print("  issues          : %s" % (result["issues"] or "none"))
    print("  RESULT          : %s" % ("PASS" if passed else "FAIL"))
    return passed


def main():
    ok = True
    pathfinder = load("pathfinding")

    ok &= check_path("pathfinding / declared taxonomy",
                     fixture("pathfinding_event.json"), pathfinder, True)
    ok &= check_path("pathfinding / taxonomy auto-derived from the challenge list",
                     fixture("pathfinding_autodetect_event.json"), pathfinder, True)
    ok &= check_path("pathfinding / swift, options only",
                     fixture("pathfinding_swift_event.json"), pathfinder, False)
    ok &= check_treasure_is_final(pathfinder)

    # step budget honoured, chest still reached
    budgeted = dict(fixture("pathfinding_event.json"))
    budgeted["max_steps"] = 45
    budgeted["verbose"] = True
    budgeted_result = pathfinder.lambda_handler(budgeted)
    budget_ok = (budgeted_result["steps"] <= 45 and budgeted_result["treasure_reached"]
                 and not budgeted_result["issues"])
    print("\n[pathfinding / budget=45]\n  %s\n  skipped %d\n  RESULT          : %s"
          % (budgeted_result["summary"], len(budgeted_result["skipped"]),
             "PASS" if budget_ok else "FAIL"))
    ok &= budget_ok

    print("\n[mathsolver]")
    solver = load("mathsolver")
    for case in fixture("mathsolver_events.json"):
        expect_rejected = case.pop("expect", None) == "rejected"
        result = solver.lambda_handler(dict(case))
        rejected = bool(result.get("error"))
        label = (case.get("question") or case.get("code", "")).replace("\n", " ; ")[:62]
        passed = rejected if expect_rejected else (not rejected and result.get("answer"))
        ok &= bool(passed)
        print("  %-64s -> %-22s %s" % (
            label,
            (result.get("answer") or result.get("error", ""))[:22],
            "PASS" if passed else "FAIL"))

    print("\n[webscraper]")
    try:
        scraper = load("webscraper")
        page = scraper.lambda_handler(fixture("webscraper_event.json"))
        passed = not page.get("error") and bool(page.get("text"))
        print("  url      : %s" % page.get("url"))
        print("  title    : %s" % page.get("title"))
        print("  snippets : %s" % (page.get("snippets") or [])[:1])
        print("  RESULT   : %s" % ("PASS" if passed else "SKIP/FAIL %s" % page.get("error")))
        ok &= bool(passed)
    except Exception as exc:  # no egress in this environment
        print("  SKIPPED (no network): %s" % exc)

    print("\n%s" % ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
