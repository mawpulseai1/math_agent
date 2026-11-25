import hashlib
import heapq
import itertools
import time
from dataclasses import dataclass

# Cheap automation tried at every node. Lean failures are fast.
CLOSERS = [
    "norm_num",
    "ring",
    "linarith",
    "nlinarith",
    "positivity",
    "omega",
    "field_simp",
    "decide",
    "simp_all",
    "aesop",
]


@dataclass
class Node:
    state: int      # REPL proofState id
    goals: list     # goal strings from the REPL
    path: list      # tactics applied from the root
    depth: int


def _is_error(r: dict) -> bool:
    if "goals" not in r:
        return True
    return any(m.get("severity") == "error" for m in r.get("messages", []))


def _key(goals: list[str]) -> str:
    return hashlib.sha1("\x01".join(goals).encode()).hexdigest()


def search(
    repl,
    root_state: int,
    goal: str,
    propose,
    *,
    budget: int = 200,
    seconds: int = 600,
    max_depth: int = 14,
    emit=print,
    tag: str = "",
):
    """
    Best-first search. Priority = depth + 0.5 * number of open goals, so shallow,
    simple states are expanded first. Dead branches are simply never expanded,
    which is the backtracking. Returns a list of tactic strings or None.
    """
    t0 = time.time()
    tick = itertools.count()
    heap = [(0.0, next(tick), Node(root_state, [goal], [], 0))]
    seen = {_key([goal])}
    calls = 0

    while heap and calls < budget and time.time() - t0 < seconds:
        _, _, node = heapq.heappop(heap)
        if node.depth >= max_depth:
            continue
        try:
            suggested = propose(node)
        except Exception as e:
            emit("search", f"{tag}: proposal failed ({e})")
            suggested = []
        for tac in dict.fromkeys(suggested + CLOSERS):
            if calls >= budget or time.time() - t0 >= seconds:
                break
            calls += 1
            if calls % 25 == 0:
                emit("search", f"{tag}: {calls} tactic calls, frontier {len(heap)}")
            r = repl.tactic(tac, node.state)
            if _is_error(r):
                continue
            goals = r["goals"]
            if not goals:
                emit("search", f"{tag}: solved after {calls} calls (depth {node.depth + 1})")
                return node.path + [tac]
            k = _key(goals)
            if k in seen:
                continue
            seen.add(k)
            child = Node(r["proofState"], goals, node.path + [tac], node.depth + 1)
            heapq.heappush(heap, (child.depth + 0.5 * len(goals), next(tick), child))

    emit("search", f"{tag}: no proof within budget ({calls} calls)")
    return None
