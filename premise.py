import hashlib
import logging
import sources as S

log = logging.getLogger("premise")


def _names(data) -> list[str]:
    out = []

    def field(d):
        name = d.get("name", "")
        if isinstance(name, list):
            name = ".".join(map(str, name))
        typ = d.get("signature") or d.get("type") or d.get("formal_type") or ""
        mod = d.get("module_name") or d.get("module") or ""
        if isinstance(mod, list):
            mod = ".".join(map(str, mod))
        if not name:
            return ""
        return f"{name} : {typ}  [{mod}]".rstrip()

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("result"), dict):
                lab = field(x["result"])
                if lab:
                    out.append(lab)
            elif "name" in x:
                lab = field(x)
                if lab:
                    out.append(lab)
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return list(dict.fromkeys(out))


def _post(url: str, bodies: list) -> list[str]:
    # Response shapes differ across versions; try known request formats in turn.
    for body in bodies:
        try:
            r = S.HTTP.post(url, json=body, timeout=25)
            if r.status_code == 200:
                names = _names(r.json())
                if names:
                    return names
        except Exception as e:
            log.debug("%s: %s", url, e)
    return []


def loogle(q: str, n: int = 8) -> list[str]:
    """Pattern/name search over Mathlib: e.g. 'Nat.succ_le' or '_ * _ ≤ _'."""
    try:
        r = S.HTTP.get("https://loogle.lean-lang.org/json", params={"q": q}, timeout=25)
        return [
            f"{h.get('name', '')} : {h.get('type', '')}  [{h.get('module', '')}]"
            for h in r.json().get("hits", [])[:n]
        ]
    except Exception:
        return []


def leansearch(q: str, n: int = 8) -> list[str]:
    """Natural-language and semantic search over Mathlib (leansearch.net)."""
    return _post(
        "https://leansearch.net/search",
        [{"query": [q], "num_results": n}, [[q], n]],
    )[:n]


def moogle(q: str, n: int = 8) -> list[str]:
    """Semantic Mathlib search (moogle.ai)."""
    return _post("https://www.moogle.ai/api/search", [{"query": q}])[:n]


def _cached(name: str, q: str, fn, n: int = 8) -> list[str]:
    key = hashlib.sha256(f"premise:{name}\x00{q}".encode()).hexdigest()
    if S.CACHE is not None:
        hit = S.CACHE.get(key, 7 * 86400)
        if hit is not None:
            return hit
    S.throttle(name)
    try:
        res = fn(q, n)
    except Exception:
        res = []
    if S.CACHE is not None and res:
        S.CACHE.put(key, res)
    return res


def search_names(queries: list[str], n: int = 6) -> list[str]:
    """LLM- or error-derived queries -> candidate Mathlib names (used by the skeleton repair loop)."""
    out = []
    for q in queries[:5]:
        out += _cached("loogle", q, loogle, n)
        out += _cached("leansearch", q, leansearch, n)
    return list(dict.fromkeys(out))


def for_goal(goal: str, n: int = 8) -> list[str]:
    """Hints for a proof state: search with the target type of the goal."""
    target = goal.split("⊢")[-1].strip()[:300]
    if not target:
        return []
    names = _cached("leansearch", target, leansearch, n) + _cached("moogle", target, moogle, n)
    return list(dict.fromkeys(names))[:n]
