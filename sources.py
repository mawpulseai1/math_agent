import hashlib
import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
import httpx
from config import Config

log = logging.getLogger("sources")
CFG = Config()
HTTP = httpx.Client(
    timeout=30,
    follow_redirects=True,
    headers={"User-Agent": f"MathAgent/0.2 (mailto:{CFG.email})"},
)
CACHE = None   # set by MathAgent
VSTORE = None  # set by MathAgent

# Minimum seconds between calls per source (arXiv asks for 3 s; S2 is strict)
_MIN_GAP = {
    "arxiv": 3.0,
    "semanticscholar": 1.1,
    "web": 1.5,
    "oeis": 1.0,
    "leansearch": 1.0,
    "moogle": 1.0,
}
_last: dict[str, float] = {}
_locks = {k: threading.Lock() for k in _MIN_GAP}


def throttle(name: str):
    if name not in _MIN_GAP:
        return
    with _locks[name]:
        wait = _MIN_GAP[name] - (time.time() - _last.get(name, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last[name] = time.time()


@dataclass
class Doc:
    title: str
    url: str
    source: str
    authors: str = ""
    year: str = ""
    abstract: str = ""
    pdf_url: str = ""
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        return re.sub(r"\W+", "", self.title.lower())[:80]


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _first(x):
    return (x[0] if isinstance(x, list) and x else x) or ""


# ---------------- academic ----------------

def arxiv(q: str, n: int = 6) -> list[Doc]:
    terms = " AND ".join(f"all:{w}" for w in re.findall(r"[\w\-]+", q)[:8]) or "all:math"
    r = HTTP.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": terms, "max_results": n, "sortBy": "relevance"},
    )
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        link = e.findtext("a:id", "", ns)
        out.append(Doc(
            title=" ".join(e.findtext("a:title", "", ns).split()),
            url=link,
            pdf_url=link.replace("/abs/", "/pdf/"),
            source="arXiv",
            authors=", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns))[:120],
            year=e.findtext("a:published", "", ns)[:4],
            abstract=" ".join(e.findtext("a:summary", "", ns).split()),
        ))
    return out


def semanticscholar(q: str, n: int = 6) -> list[Doc]:
    h = {"x-api-key": CFG.s2_key} if CFG.s2_key else {}
    r = HTTP.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        headers=h,
        params={
            "query": q,
            "limit": n,
            "fields": "title,abstract,year,authors,url,openAccessPdf,citationCount",
        },
    )
    r.raise_for_status()
    return [
        Doc(
            title=p.get("title") or "",
            url=p.get("url") or "",
            source="SemanticScholar",
            authors=", ".join(a.get("name", "") for a in p.get("authors", []))[:120],
            year=str(p.get("year") or ""),
            abstract=p.get("abstract") or "",
            pdf_url=(p.get("openAccessPdf") or {}).get("url") or "",
            extra={"citations": p.get("citationCount")},
        )
        for p in r.json().get("data", [])
    ]


def openalex(q: str, n: int = 6) -> list[Doc]:
    r = HTTP.get(
        "https://api.openalex.org/works",
        params={"search": q, "per-page": n, "mailto": CFG.email},
    )
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        idx = w.get("abstract_inverted_index") or {}
        pos = {p: word for word, ps in idx.items() for p in ps}
        oa = w.get("best_oa_location") or {}
        out.append(Doc(
            title=w.get("title") or "",
            url=w.get("doi") or w.get("id") or "",
            source="OpenAlex",
            authors=", ".join(a["author"]["display_name"] for a in w.get("authorships", [])[:6]),
            year=str(w.get("publication_year") or ""),
            abstract=" ".join(pos[i] for i in sorted(pos)),
            pdf_url=oa.get("pdf_url") or "",
        ))
    return [d for d in out if d.title]


def crossref(q: str, n: int = 5) -> list[Doc]:
    r = HTTP.get(
        "https://api.crossref.org/works",
        params={"query": q, "rows": n, "mailto": CFG.email},
    )
    r.raise_for_status()
    out = []
    for it in r.json()["message"]["items"]:
        if not it.get("title"):
            continue
        out.append(Doc(
            title=it["title"][0],
            url=it.get("URL", ""),
            source="Crossref",
            authors=", ".join(
                f'{a.get("given", "")} {a.get("family", "")}'.strip()
                for a in it.get("author", [])[:6]
            ),
            year=str((it.get("issued", {}).get("date-parts") or [[""]])[0][0]),
            abstract=_strip(it.get("abstract", "")),
        ))
    return out


def zbmath(q: str, n: int = 8) -> list[Doc]:
    """zbMATH Open: the free, open metadata counterpart to MathSciNet."""
    r = HTTP.get(
        "https://api.zbmath.org/v1/document/_search",
        params={"search_string": q, "page": 0, "results_per_page": n},
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("result", []) or []:
        title = it.get("title", "")
        if isinstance(title, dict):
            title = title.get("title", "")
        contrib = it.get("contributors") if isinstance(it.get("contributors"), dict) else {}
        authors_raw = contrib.get("authors") or it.get("authors") or []
        authors = ", ".join(
            a.get("name", "") if isinstance(a, dict) else str(a) for a in authors_raw
        )[:120]
        ident = it.get("id") or it.get("identifier") or ""
        ab = it.get("abstract") if isinstance(it.get("abstract"), str) else ""
        out.append(Doc(
            title=_strip(str(title)),
            url=f"https://zbmath.org/?q=an:{ident}" if ident else "",
            source="zbMATH",
            authors=authors,
            year=str(it.get("year") or ""),
            abstract=_strip(ab),
        ))
    return [d for d in out if d.title]


def core(q: str, n: int = 8) -> list[Doc]:
    if not CFG.core_key:
        return []
    r = HTTP.get(
        "https://api.core.ac.uk/v3/search/works",
        params={"q": q, "limit": n},
        headers={"Authorization": f"Bearer {CFG.core_key}"},
    )
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        doi = w.get("doi")
        links = w.get("links") or [{}]
        out.append(Doc(
            title=w.get("title") or "",
            url=f"https://doi.org/{doi}" if doi else links[0].get("url", ""),
            source="CORE",
            authors=", ".join(
                a.get("name", "") for a in w.get("authors", []) if isinstance(a, dict)
            )[:120],
            year=str(w.get("yearPublished") or ""),
            abstract=w.get("abstract") or "",
            pdf_url=w.get("downloadUrl") or "",
        ))
    return [d for d in out if d.title]


def hal(q: str, n: int = 8) -> list[Doc]:
    r = HTTP.get(
        "https://api.archives-ouvertes.fr/search/",
        params={
            "q": q,
            "rows": n,
            "wt": "json",
            "fl": "title_s,authFullName_s,producedDateY_i,abstract_s,uri_s,fileMain_s",
        },
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("response", {}).get("docs", []):
        out.append(Doc(
            title=_strip(_first(d.get("title_s"))),
            url=d.get("uri_s", ""),
            source="HAL",
            authors=", ".join(d.get("authFullName_s", []))[:120],
            year=str(d.get("producedDateY_i", "")),
            abstract=_strip(_first(d.get("abstract_s"))),
            pdf_url=d.get("fileMain_s", ""),
        ))
    return [d for d in out if d.title]


def zenodo(q: str, n: int = 8) -> list[Doc]:
    r = HTTP.get(
        "https://zenodo.org/api/records",
        params={"q": q, "size": n, "sort": "bestmatch"},
    )
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", {}).get("hits", []):
        m = h.get("metadata", {})
        links = h.get("links", {})
        pdf = next(
            (
                f.get("links", {}).get("self", "")
                for f in h.get("files", [])
                if f.get("key", "").lower().endswith(".pdf")
            ),
            "",
        )
        out.append(Doc(
            title=m.get("title", ""),
            url=links.get("html") or links.get("self", ""),
            source="Zenodo",
            authors=", ".join(c.get("name", "") for c in m.get("creators", []))[:120],
            year=(m.get("publication_date") or "")[:4],
            abstract=_strip(m.get("description", "")),
            pdf_url=pdf,
        ))
    return [d for d in out if d.title]


# ---------------- Q&A / web ----------------

def stackexchange(q: str, n: int = 4) -> list[Doc]:
    out = []
    base_params = {"order": "desc", "filter": "withbody"}
    if CFG.se_key:
        base_params["key"] = CFG.se_key
    for site in ("math.stackexchange", "mathoverflow.net"):
        r = HTTP.get(
            "https://api.stackexchange.com/2.3/search/advanced",
            params={**base_params, "q": q, "site": site, "pagesize": n, "sort": "relevance"},
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            continue
        ids = ";".join(str(i["question_id"]) for i in items)
        a = HTTP.get(
            f"https://api.stackexchange.com/2.3/questions/{ids}/answers",
            params={**base_params, "site": site, "sort": "votes"},
        )
        a.raise_for_status()
        best = {}
        for ans in a.json().get("items", []):
            best.setdefault(ans["question_id"], ans)
        for it in items:
            ans = best.get(it["question_id"])
            out.append(Doc(
                title=html.unescape(it["title"]),
                url=it["link"],
                source=site,
                abstract=_strip(it.get("body", ""))[:700]
                + ("\n--- TOP ANSWER ---\n" + _strip(ans["body"])[:2500] if ans else ""),
                extra={"score": it.get("score")},
            ))
    return out


def web(q: str, n: int = 6) -> list[Doc]:
    from ddgs import DDGS
    sites = "(site:proofwiki.org OR site:ncatlab.org OR site:en.wikipedia.org OR site:planetmath.org OR site:mathworld.wolfram.com)"
    out = []
    with DDGS() as d:
        for query in (q + " mathematics", f"{q} {sites}"):
            try:
                for x in d.text(query, max_results=n):
                    out.append(Doc(
                        title=x["title"],
                        url=x["href"],
                        source="web",
                        abstract=x.get("body", ""),
                    ))
            except Exception as e:
                log.debug("ddgs query failed (%s): %s", query, e)
    return out


def oeis(q: str, n: int = 4) -> list[Doc]:
    r = HTTP.get("https://oeis.org/search", params={"q": q, "fmt": "json"})
    r.raise_for_status()
    data = r.json()
    data = data if isinstance(data, list) else (data.get("results") or [])
    return [
        Doc(
            title=f'A{d["number"]:06d}: {d["name"]}',
            url=f'https://oeis.org/A{d["number"]:06d}',
            source="OEIS",
            abstract=d["name"] + "\n" + "; ".join(d.get("comment", [])[:4]),
        )
        for d in data[:n]
    ]


# ---------------- your own library ----------------

def local(q: str, n: int = 8) -> list[Doc]:
    if VSTORE is None:
        return []
    out = []
    for score, row in VSTORE.search(q, n):
        _doc_id, text, _vec, title, authors, year, path = row
        out.append(Doc(
            title=title,
            url=Path(path).as_uri(),
            source="local",
            authors=authors,
            year=year,
            abstract=text[:800],
            extra={"path": path, "score": round(score, 3)},
        ))
    return out


SOURCES = {
    "arxiv": arxiv,
    "semanticscholar": semanticscholar,
    "openalex": openalex,
    "crossref": crossref,
    "zbmath": zbmath,
    "core": core,
    "hal": hal,
    "zenodo": zenodo,
    "stackexchange": stackexchange,
    "web": web,
    "oeis": oeis,
    "local": local,
}

TARGETS = {
    "papers": ["arxiv", "semanticscholar", "openalex", "crossref", "zbmath", "hal", "zenodo", "core"],
    "qa": ["stackexchange"],
    "web": ["web"],
    "oeis": ["oeis"],
    "mine": ["local"],
}


def run_cached(name: str, fn, q: str, ttl: int = 7 * 86400) -> list[Doc]:
    key = hashlib.sha256(f"{name}\x00{q}".encode()).hexdigest()
    if CACHE is not None:
        hit = CACHE.get(key, ttl)
        if hit is not None:
            return [Doc(**d) for d in hit]
    throttle(name)
    docs = fn(q)
    if CACHE is not None and docs:
        CACHE.put(key, [asdict(d) for d in docs])
    return docs


def safe(name: str, q: str) -> list[Doc]:
    try:
        if name == "local":
            return local(q)
        return run_cached(name, SOURCES[name], q)
    except Exception as e:
        log.warning("%s failed for %r: %s", name, q, e)
        return []


# ---------------- full text ----------------

def fulltext(doc: Doc, max_pages: int = 30) -> str:
    path = doc.extra.get("path")
    if path:
        from vectorstore import read_text
        return read_text(path)
    if not doc.pdf_url:
        return doc.abstract
    data = CACHE.load_pdf(doc.pdf_url) if CACHE is not None else None
    if data is None:
        try:
            r = HTTP.get(doc.pdf_url)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                data = r.content
                if CACHE is not None:
                    CACHE.save_pdf(doc.pdf_url, data)
        except Exception as e:
            log.info("PDF download failed %s: %s", doc.pdf_url, e)
    if data is None:
        return doc.abstract
    import fitz
    with fitz.open(stream=data, filetype="pdf") as pdf:
        return "\n".join(p.get_text() for p in list(pdf)[:max_pages])


def _split_chunks(text: str, size: int = 4500, overlap: int = 400) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind("\n", start + size // 2, end)
            if cut != -1:
                end = cut
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def best_chunks(text: str, keywords: list[str], k: int = 5, size: int = 4500) -> str:
    if not text:
        return ""
    import math
    chunks = _split_chunks(text, size=size)
    if len(chunks) <= k:
        return "\n...\n".join(chunks)

    kws = [w.lower() for w in keywords if w]
    n = len(chunks)
    lows = [c.lower() for c in chunks]
    # IDF weight per keyword so rare domain terms outweigh generic words
    df = {w: sum(1 for c in lows if w in c) for w in kws}
    idf = {w: math.log((n + 1) / (df[w] + 1)) + 1.0 for w in kws}

    def score(idx: int) -> float:
        c = lows[idx]
        s = sum(idf[w] * math.log1p(c.count(w)) for w in kws)
        s += 0.8 * sum(c.count(marker) for marker in ("theorem", "lemma", "proposition", "corollary", "proof"))
        if idx == 0:
            s += 0.5  # bias slightly toward abstract/intro/notation
        return s

    top = sorted(range(n), key=score, reverse=True)[:k]
    return "\n...\n".join(chunks[i] for i in sorted(top))
