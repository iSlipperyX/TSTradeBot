"""Run the order-manager chaos simulation over many seeds and list the ones that fail.

Usage, from the repository root:
    python tools/chaos_seeds.py 0-9 30000-30399 31000-31399

Each argument is a seed or an inclusive range. One line is printed per failing seed with what went
wrong; the total goes to stderr. The simulation is ``chaos()`` in tests/test_stress_faults.py, and
the checks are the ones its test makes.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def seeds(args: list[str]) -> list[int]:
    out: list[int] = []
    for arg in args:
        first, _, last = arg.partition("-")
        out.extend(range(int(first), int(last or first) + 1))
    return out


def problems(seed: int) -> list[str]:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]
    from tests.test_stress_faults import chaos

    logging.disable(logging.CRITICAL)  # the order manager's own messages; only the verdict matters here

    result = asyncio.run(chaos(seed))
    found = []
    if result["max_position"] > 3:
        found.append(f"max_position={result['max_position']}")
    if result["wrong_side"]:
        found.append(f"wrong_side={result['wrong_side']}")
    if result["final_position"]:
        found.append(f"final_position={result['final_position']}")
    if result["working"]:
        found.append(f"working={result['working']}")
    if result["trade"] is not None:
        found.append("trade not None")
    if result["errors"] <= 100 or result["closed"] <= 20:
        found.append(f"too calm (errors {result['errors']}, closed {result['closed']})")
    return found


def main(argv: list[str]) -> int:
    todo = seeds(argv or ["0-9"])
    failing = 0
    with ProcessPoolExecutor() as pool:
        for seed, found in zip(todo, pool.map(problems, todo, chunksize=4), strict=True):
            if found:
                failing += 1
                print(seed, found, flush=True)
    print(f"TOTAL seeds {len(todo)} failing {failing}", file=sys.stderr)
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
