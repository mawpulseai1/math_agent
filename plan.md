# Desktop Math Research Agent (research → solve → Lean-verify)

Below is a complete, working starter project in Python. It follows your flow: **deconstruct → research papers → solve informally with that research → formalize and prove in a sandboxed Lean 4 + Mathlib → report**.

## 1. Architecture

```
Problem
  │
  ▼
[1 Deconstruct]  LLM → goal, givens, domain, sub-problems, search queries
  │
  ▼
[2 Research loop] (parallel, up to N rounds)
  │   arXiv · Semantic Scholar · OpenAlex · Crossref · Math.SE · MathOverflow · web (DuckDuckGo) · OEIS
  │   → dedupe → LLM rerank → download PDFs → pick relevant chunks → extract theorems/techniques
  │   → "is this enough?" gap check → new queries
  ▼
[3 Informal solve] uses the extracted insights, cites [n], then a critic loop checks it
  │
  ▼
[4 Formalize]  Lean statement (compiled with `sorry`, then back-translated to check fidelity)
  │
  ▼
[5 Prove loop]  LLM writes proof → Docker sandbox compiles it → errors fed back
  │             Loogle searches Mathlib lemma names → axioms audit (`#print axioms`)
  ▼
[6 Report]  Markdown: solution, sources, Lean code, and status (VERIFIED / UNVERIFIED)
```

Project layout:

```
mathagent/
├── main.py
├── config.py
├── llm.py
├── sources.py
├── lean_sandbox.py
├── agent.py
├── gui.py
├── requirements.txt
├── .env.example
└── docker/Dockerfile.lean
```

## 2. Setup files

**requirements.txt**
```
openai>=1.40
httpx>=0.27
PySide6>=6.7
pymupdf>=1.24
ddgs>=9.0
python-dotenv>=1.0
```

**.env.example**
```
# Any OpenAI-compatible endpoint. Free/cheap options:
#  Ollama (local):  http://localhost:11434/v1   key: ollama
#  Gemini free tier: https://generativelanguage.googleapis.com/v1beta/openai/
#  Groq:            https://api.groq.com/openai/v1
#  OpenRouter:      https://openrouter.ai/api/v1  (":free" models)
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:32b-instruct
# Optional: a separate model for writing Lean (e.g. a DeepSeek-Prover / Goedel-Prover / Kimina build)
PROVER_MODEL=

CONTACT_EMAIL=you@example.com      # polite-pool for OpenAlex/Crossref
S2_API_KEY=                        # optional, free, avoids Semantic Scholar 429s

LEAN_MODE=docker                   # docker | local
LEAN_IMAGE=mathagent-lean
LEAN_PROJECT_DIR=                  # only for LEAN_MODE=local
```

**docker/Dockerfile.lean** (Lean 4 and prebuilt Mathlib cache, about 5–6 GB)
```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y curl git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd -m lean
USER lean
WORKDIR /home/lean
ENV PATH="/home/lean/.elan/bin:${PATH}"
RUN curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
RUN git clone --depth 1 https://github.com/leanprover-community/mathlib4.git proj
WORKDIR /home/lean/proj
RUN elan toolchain install $(cat lean-toolchain) && lake exe cache get
# sanity check at build time
RUN echo 'import Mathlib\nexample : 2 + 2 = 4 := by norm_num' > /tmp/t.lean && lake env lean /tmp/t.lean
```
Build it with `docker build -t mathagent-lean -f docker/Dockerfile.lean .`

## 3. Code

**config.py**
```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    base_url: str = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    api_key: str = os.getenv("LLM_API_KEY", "ollama")
    model: str = os.getenv("LLM_MODEL", "qwen2.5:32b-instruct")
    prover_model: str = os.getenv("PROVER_MODEL") or os.getenv("LLM_MODEL", "qwen2.5:32b-instruct")
    email: str = os.getenv("CONTACT_EMAIL", "anon@example.com")
    s2_key: str = os.getenv("S2_API_KEY", "")
    lean_mode: str = os.getenv("LEAN_MODE", "docker")
    lean_image: str = os.getenv("LEAN_IMAGE", "mathagent-lean")
    lean_project: str = os.getenv("LEAN_PROJECT_DIR", "")
    lean_timeout: int = 180
    lean_mem: str = "4g"
    lean_cpus: str = "2"
    research_rounds: int = 2
    papers_to_read: int = 5
    informal_revisions: int = 2
    lean_attempts: int = 6
```

**llm.py**
```python
import json, re
from openai import OpenAI

class LLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key)

    def chat(self, system: str, user: str, model: str | None = None, temperature: float = 0.2) -> str:
        for attempt in range(3):
            try:
                r = self.client.chat.completions.create(
                    model=model or self.cfg.model,
                    temperature=temperature,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                )
                txt = r.choices[0].message.content or ""
                return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            except Exception as e:
                if attempt == 2:
                    raise
        return ""

    def json(self, system: str, user: str, **kw) -> dict:
        txt = self.chat(system + "\nReply with ONE valid JSON object and nothing else.", user, **kw)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
```

**sources.py** (all free sources, unified `Doc` format)
```python
import re, html, xml.etree.ElementTree as ET
from dataclasses import dataclass, field
import httpx
from config import Config

CFG = Config()
HTTP = httpx.Client(timeout=30, follow_redirects=True,
                    headers={"User-Agent": f"MathAgent/0.1 (mailto:{CFG.email})"})

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

    def key(self):
        return re.sub(r"\W+", "", self.title.lower())[:80]

def _strip(s): return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

# ---------- academic ----------
def arxiv(q, n=6):
    terms = " AND ".join(f"all:{w}" for w in re.findall(r"[\w\-]+", q)[:8])
    r = HTTP.get("https://export.arxiv.org/api/query",
                 params={"search_query": terms, "max_results": n, "sortBy": "relevance"})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        link = e.findtext("a:id", "", ns)
        out.append(Doc(
            title=" ".join(e.findtext("a:title", "", ns).split()),
            url=link, pdf_url=link.replace("/abs/", "/pdf/"), source="arXiv",
            authors=", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns))[:120],
            year=e.findtext("a:published", "", ns)[:4],
            abstract=" ".join(e.findtext("a:summary", "", ns).split())))
    return out

def semanticscholar(q, n=6):
    h = {"x-api-key": CFG.s2_key} if CFG.s2_key else {}
    r = HTTP.get("https://api.semanticscholar.org/graph/v1/paper/search", headers=h,
                 params={"query": q, "limit": n,
                         "fields": "title,abstract,year,authors,url,openAccessPdf,citationCount"})
    r.raise_for_status()
    return [Doc(title=p["title"], url=p["url"], source="SemanticScholar",
                authors=", ".join(a["name"] for a in p.get("authors", []))[:120],
                year=str(p.get("year") or ""), abstract=p.get("abstract") or "",
                pdf_url=(p.get("openAccessPdf") or {}).get("url", ""),
                extra={"citations": p.get("citationCount")})
            for p in r.json().get("data", [])]

def openalex(q, n=6):
    r = HTTP.get("https://api.openalex.org/works",
                 params={"search": q, "per-page": n, "mailto": CFG.email})
    out = []
    for w in r.json().get("results", []):
        idx = w.get("abstract_inverted_index") or {}
        pos = {p: word for word, ps in idx.items() for p in ps}
        oa = w.get("best_oa_location") or {}
        out.append(Doc(
            title=w.get("title") or "", url=w.get("doi") or w.get("id"), source="OpenAlex",
            authors=", ".join(a["author"]["display_name"] for a in w.get("authorships", [])[:6]),
            year=str(w.get("publication_year") or ""),
            abstract=" ".join(pos[i] for i in sorted(pos)),
            pdf_url=oa.get("pdf_url") or ""))
    return [d for d in out if d.title]

def crossref(q, n=5):
    r = HTTP.get("https://api.crossref.org/works",
                 params={"query": q, "rows": n, "mailto": CFG.email})
    out = []
    for it in r.json()["message"]["items"]:
        if not it.get("title"): continue
        out.append(Doc(title=it["title"][0], url=it.get("URL", ""), source="Crossref",
                       authors=", ".join(f'{a.get("given","")} {a.get("family","")}' for a in it.get("author", [])[:6]),
                       year=str((it.get("issued", {}).get("date-parts") or [[""]])[0][0]),
                       abstract=_strip(it.get("abstract", ""))))
    return out

# ---------- Q&A / web ----------
def stackexchange(q, n=4):
    out = []
    for site in ("math.stackexchange", "mathoverflow.net"):
        r = HTTP.get("https://api.stackexchange.com/2.3/search/advanced",
                     params={"q": q, "site": site, "pagesize": n, "order": "desc",
                             "sort": "relevance", "filter": "withbody"})
        items = r.json().get("items", [])
        if not items: continue
        ids = ";".join(str(i["question_id"]) for i in items)
        a = HTTP.get(f"https://api.stackexchange.com/2.3/questions/{ids}/answers",
                     params={"site": site, "order": "desc", "sort": "votes", "filter": "withbody"})
        best = {}
        for ans in a.json().get("items", []):
            best.setdefault(ans["question_id"], ans)
        for it in items:
            ans = best.get(it["question_id"])
            out.append(Doc(
                title=html.unescape(it["title"]), url=it["link"], source=site,
                abstract=_strip(it.get("body", ""))[:700] + ("\n--- TOP ANSWER ---\n" + _strip(ans["body"])[:2500] if ans else ""),
                extra={"score": it.get("score")}))
    return out

def web(q, n=6):
    from ddgs import DDGS
    sites = "(site:proofwiki.org OR site:ncatlab.org OR site:en.wikipedia.org OR site:planetmath.org OR site:mathworld.wolfram.com)"
    out = []
    with DDGS() as d:
        for query in (q + " mathematics", f"{q} {sites}"):
            for x in d.text(query, max_results=n):
                out.append(Doc(title=x["title"], url=x["href"], source="web", abstract=x.get("body", "")))
    return out

def oeis(q, n=4):
    r = HTTP.get("https://oeis.org/search", params={"q": q, "fmt": "json"})
    data = r.json()
    data = data if isinstance(data, list) else (data.get("results") or [])
    return [Doc(title=f'A{d["number"]:06d}: {d["name"]}', url=f'https://oeis.org/A{d["number"]:06d}',
                source="OEIS", abstract=d["name"] + "\n" + "; ".join(d.get("comment", [])[:4]))
            for d in data[:n]]

# ---------- Lean / Mathlib lemma search ----------
def loogle(q, n=8):
    r = HTTP.get("https://loogle.lean-lang.org/json", params={"q": q})
    return [Doc(title=h["name"], url="", source="loogle",
                abstract=f'{h.get("type","")}  [{h.get("module","")}]')
            for h in r.json().get("hits", [])[:n]]

SOURCES = {"arxiv": arxiv, "semanticscholar": semanticscholar, "openalex": openalex,
           "crossref": crossref, "stackexchange": stackexchange, "web": web, "oeis": oeis}
TARGETS = {"papers": ["arxiv", "semanticscholar", "openalex", "crossref"],
           "qa": ["stackexchange"], "web": ["web"], "oeis": ["oeis"]}

def safe(fn, q):
    try: return fn(q)
    except Exception: return []

# ---------- full text ----------
def fulltext(doc: Doc, max_pages=25) -> str:
    if not doc.pdf_url:
        return doc.abstract
    try:
        r = HTTP.get(doc.pdf_url)
        if "pdf" in r.headers.get("content-type", "") or doc.pdf_url.endswith(".pdf"):
            import fitz
            with fitz.open(stream=r.content, filetype="pdf") as pdf:
                return "\n".join(p.get_text() for p in list(pdf)[:max_pages])
    except Exception:
        pass
    return doc.abstract

def best_chunks(text: str, keywords: list[str], k=4, size=5000) -> str:
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    kws = [w.lower() for w in keywords]
    scored = sorted(range(len(chunks)),
                    key=lambda i: -sum(chunks[i].lower().count(w) for w in kws))[:k]
    return "\n...\n".join(chunks[i] for i in sorted(scored))
```

**lean_sandbox.py**
```python
import os, re, subprocess, tempfile
from dataclasses import dataclass
from pathlib import Path

SAFE_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}

@dataclass
class LeanResult:
    compiled: bool        # no errors
    complete: bool        # compiled, no sorry, only standard axioms
    output: str

class LeanSandbox:
    def __init__(self, cfg):
        self.cfg = cfg

    def check(self, code: str, theorem: str | None = None) -> LeanResult:
        if theorem:
            code += f"\n\n#print axioms {theorem}\n"
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "Main.lean"
            f.write_text(code, encoding="utf-8")
            os.chmod(f, 0o644)
            c = self.cfg
            if c.lean_mode == "docker":
                cmd = ["docker", "run", "--rm", "--network", "none",
                       "--memory", c.lean_mem, "--cpus", c.lean_cpus, "--pids-limit", "256",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "-v", f"{d}:/work:ro", c.lean_image,
                       "timeout", str(c.lean_timeout), "lake", "env", "lean", "/work/Main.lean"]
                cwd = None
            else:  # local (NOT sandboxed - use only for trusted code)
                cmd = ["lake", "env", "lean", str(f)]
                cwd = c.lean_project
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=c.lean_timeout + 30, cwd=cwd)
                out, rc = (p.stdout + p.stderr).strip(), p.returncode
            except subprocess.TimeoutExpired:
                return LeanResult(False, False, "TIMEOUT")
            except FileNotFoundError as e:
                return LeanResult(False, False, f"Sandbox not available: {e}")
        if rc == 124:
            return LeanResult(False, False, "TIMEOUT (lean exceeded limit)\n" + out)
        has_error = bool(re.search(r":\s*error", out)) or rc not in (0,)
        has_sorry = "sorry" in out
        bad_axiom = False
        m = re.search(r"depends on axioms: \[(.*?)\]", out, re.S)
        if m:
            used = {a.strip() for a in m.group(1).split(",") if a.strip()}
            bad_axiom = not used <= SAFE_AXIOMS
        elif theorem and not has_error:
            bad_axiom = False  # theorem with no axioms at all
        return LeanResult(not has_error, (not has_error) and not has_sorry and not bad_axiom, out)
```

**agent.py**
```python
import re, json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from config import Config
from llm import LLM
import sources as S
from lean_sandbox import LeanSandbox

MATH = ("You are a rigorous research mathematician. Be precise, never invent theorem statements "
        "or citations; if unsure, say so.")

@dataclass
class Report:
    problem: str = ""
    decomposition: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    insights: list = field(default_factory=list)
    informal: str = ""
    lean_statement: str = ""
    lean_code: str = ""
    lean_verified: bool = False
    markdown: str = ""

def lean_block(txt: str) -> str:
    m = re.search(r"```(?:lean4?|Lean)?\s*\n(.*?)```", txt, re.S)
    return (m.group(1) if m else txt).strip()

def norm(s): return re.sub(r"\s+", " ", s).strip()

def header(code: str) -> str:
    m = re.search(r"theorem\s+main_thm.*?:=", code, re.S)
    return norm(m.group(0)) if m else ""

class MathAgent:
    def __init__(self, cfg: Config | None = None, emit=print):
        self.cfg = cfg or Config()
        self.llm = LLM(self.cfg)
        self.lean = LeanSandbox(self.cfg)
        self.emit = emit
        self.sources: list[S.Doc] = []

    # ------------------------------------------------------------------ 1
    def deconstruct(self, problem):
        self.emit("deconstruct", "Analyzing the problem...")
        d = self.llm.json(MATH, f"""Deconstruct this problem.
PROBLEM: {problem}

JSON keys:
 "restatement": precise restatement,
 "type": "prove" | "compute" | "find" | "decide",
 "domain": area(s) of mathematics, "msc": likely MSC codes,
 "givens": [..], "goal": "...",
 "key_concepts": [..],
 "subproblems": [..],
 "likely_techniques": [..],
 "search_queries": [ {{"q": "short keyword query", "targets": ["papers"|"qa"|"web"|"oeis"]}} ]  (6-10 diverse queries:
     the exact statement, generalizations, special cases, named theorems/techniques, terminology synonyms)""")
        d.setdefault("search_queries", [{"q": problem[:200], "targets": ["papers", "qa", "web"]}])
        self.emit("deconstruct", json.dumps(d, indent=2))
        return d

    # ------------------------------------------------------------------ 2
    def _search(self, queries):
        jobs = []
        for q in queries:
            if isinstance(q, str): q = {"q": q, "targets": ["papers", "qa", "web"]}
            for t in q.get("targets", ["papers"]):
                for s in S.TARGETS.get(t, []):
                    jobs.append((s, q["q"]))
        self.emit("research", f"Running {len(jobs)} searches across free sources...")
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda j: S.safe(S.SOURCES[j[0]], j[1]), jobs))
        return [d for r in res for d in r]

    def _rank(self, problem, docs):
        docs = docs[:45]
        if not docs: return []
        listing = "\n".join(f"[{i}] ({d.source}, {d.year}) {d.title}: {d.abstract[:300]}" for i, d in enumerate(docs))
        r = self.llm.json(MATH, f"PROBLEM: {problem}\n\nCANDIDATES:\n{listing}\n\n"
                          f'Pick up to {self.cfg.papers_to_read} items most likely to contain methods, lemmas, '
                          f'or results useful to solve the problem. JSON: {{"top":[indices]}}')
        idx = [i for i in r.get("top", []) if isinstance(i, int) and 0 <= i < len(docs)]
        return [docs[i] for i in idx] or docs[:self.cfg.papers_to_read]

    def _read(self, docs, d):
        kws = d.get("key_concepts", [])[:8]
        def work(doc):
            text = S.best_chunks(S.fulltext(doc), kws)
            out = self.llm.chat(MATH, f"""PROBLEM: {d.get('restatement')}
SOURCE: {doc.title} ({doc.source}, {doc.year})
EXCERPTS:
{text[:18000]}

Extract ONLY what helps solve the problem: exact theorem/lemma statements, key techniques,
and how they could apply. Quote precisely, note hypotheses. If nothing useful, reply NONE.""")
            return doc, out
        with ThreadPoolExecutor(3) as ex:
            results = list(ex.map(work, docs))
        ins = []
        for doc, out in results:
            if out.strip().upper().startswith("NONE"): continue
            self.sources.append(doc)
            ins.append({"src": len(self.sources), "text": out})
            self.emit("research", f"[{len(self.sources)}] {doc.title} — {doc.url}")
        return ins

    def research(self, problem, d):
        seen, insights, queries = set(), [], d["search_queries"]
        for rnd in range(self.cfg.research_rounds):
            self.emit("research", f"--- round {rnd + 1} ---")
            new = []
            for doc in self._search(queries):
                if doc.key() not in seen:
                    seen.add(doc.key()); new.append(doc)
            insights += self._read(self._rank(problem, new), d)
            digest = "\n".join(f"[{i['src']}] {i['text'][:500]}" for i in insights) or "(nothing yet)"
            gap = self.llm.json(MATH, f"PROBLEM: {d.get('restatement')}\nINSIGHTS SO FAR:\n{digest}\n\n"
                                'Is this enough to attempt a solution? JSON: {"sufficient":bool,'
                                '"new_queries":[{"q":"...","targets":["papers"]}]}')
            if gap.get("sufficient") or not gap.get("new_queries"): break
            queries = gap["new_queries"]
        return insights

    # ------------------------------------------------------------------ 3
    def solve_informal(self, d, insights):
        ctx = "\n\n".join(f"[{i['src']}] {self.sources[i['src']-1].title}\n{i['text']}" for i in insights) or "(no sources found)"
        self.emit("solve", "Drafting solution using research...")
        sol = self.llm.chat(MATH, f"""PROBLEM ANALYSIS:\n{json.dumps(d, indent=1)}\n\nRESEARCH NOTES:\n{ctx}\n
Solve the problem. First state a strategy (which cited results/techniques you use, as [n]).
Then give a complete, step-by-step rigorous solution/proof. State the final answer clearly.
Mark any step you are not fully sure of with (UNSURE).""", temperature=0.3)
        for _ in range(self.cfg.informal_revisions):
            crit = self.llm.json(MATH, f"PROBLEM: {d.get('restatement')}\n\nSOLUTION:\n{sol}\n\n"
                                 'Act as a skeptical referee. Check every step, edge cases, misuse of theorems. '
                                 'JSON: {"valid":bool,"issues":["..."]}')
            self.emit("solve", f"Referee: {crit}")
            if crit.get("valid") or not crit.get("issues"): break
            sol = self.llm.chat(MATH, f"PROBLEM: {d.get('restatement')}\nRESEARCH:\n{ctx}\n\nSOLUTION:\n{sol}\n\n"
                                f"REFEREE ISSUES:\n{crit['issues']}\n\nRewrite the complete corrected solution.")
        return sol

    # ------------------------------------------------------------------ 4/5
    def _loogle(self, queries):
        hits = []
        for q in queries[:5]:
            for doc in S.safe(S.loogle, q):
                hits.append(f"{doc.title} : {doc.abstract}")
        return "\n".join(dict.fromkeys(hits))

    def formalize(self, d):
        self.emit("lean", "Formalizing statement in Lean 4 / Mathlib...")
        feedback = ""
        for attempt in range(4):
            code = lean_block(self.llm.chat(MATH, f"""Write a Lean 4 + Mathlib formal STATEMENT for this problem.
PROBLEM: {d.get('restatement')}
TYPE: {d.get('type')}   GOAL: {d.get('goal')}   GIVENS: {d.get('givens')}
Rules: start with `import Mathlib`; name it exactly `theorem main_thm`; end with `:= by sorry`.
For 'compute/find' problems, state that the answer equals the specific value (or set equality).
Be FAITHFUL: no extra hypotheses, no weakening. Only output a ```lean block.
{feedback}""", model=self.cfg.prover_model))
            res = self.lean.check(code)
            if not res.compiled:
                feedback = f"Previous attempt:\n{code}\nLean errors:\n{res.output[:2500]}\nFix them."
                continue
            back = self.llm.json(MATH, f"ORIGINAL: {d.get('restatement')}\n\nLEAN STATEMENT:\n{code}\n\n"
                                 'Translate the Lean statement back into plain math and compare with the original. '
                                 'JSON: {"faithful":bool,"explanation":"..."}')
            self.emit("lean", f"Fidelity check: {back}")
            if back.get("faithful"):
                return code
            feedback = f"Previous attempt:\n{code}\nUnfaithful because: {back.get('explanation')}"
        return ""

    def prove(self, d, statement, informal):
        locked = header(statement)
        hints, prev, errs = "", "", ""
        for i in range(self.cfg.lean_attempts):
            self.emit("lean", f"Proof attempt {i + 1}/{self.cfg.lean_attempts}")
            code = lean_block(self.llm.chat(MATH, f"""Prove this in Lean 4 + Mathlib. Do NOT change the statement of `main_thm`.
No `sorry`, no new axioms, no `native_decide`. Prefer robust tactics (nlinarith, positivity, omega, simp, induction, rcases).
Break into `have` steps following the informal proof.

STATEMENT:\n{statement}\n\nINFORMAL PROOF:\n{informal}\n
RELEVANT MATHLIB NAMES (from Loogle):\n{hints or '(none yet)'}
{('PREVIOUS ATTEMPT:\n' + prev + '\nLEAN ERRORS:\n' + errs) if prev else ''}
Output the full file in a single ```lean block.""", model=self.cfg.prover_model, temperature=0.4 + 0.1 * (i > 2)))
            if locked and header(code) != locked:
                errs, prev = "You changed the theorem statement. Keep it EXACTLY as given.", code
                continue
            res = self.lean.check(code, theorem="main_thm")
            self.emit("lean", res.output[:1500] or "(no output)")
            if res.complete:
                return code, True
            prev, errs = code, res.output[:3500]
            q = self.llm.json(MATH, f"Lean errors:\n{errs}\n\nCode:\n{code}\n\nGive up to 5 Loogle queries "
                              '(names, type patterns like `_ + _ ≤ _`, or substrings) to find missing Mathlib lemmas. '
                              'JSON: {"queries":["..."]}')
            hints += "\n" + self._loogle(q.get("queries", []))
        return prev, False

    # ------------------------------------------------------------------ run
    def run(self, problem) -> Report:
        r = Report(problem=problem)
        r.decomposition = self.deconstruct(problem)
        r.insights = self.research(problem, r.decomposition)
        r.sources = self.sources
        r.informal = self.solve_informal(r.decomposition, r.insights)
        r.lean_statement = self.formalize(r.decomposition)
        if r.lean_statement:
            r.lean_code, r.lean_verified = self.prove(r.decomposition, r.lean_statement, r.informal)
        else:
            self.emit("lean", "Could not produce a faithful, compiling Lean statement.")
        r.markdown = self._markdown(r)
        self.emit("done", "Finished.")
        return r

    def _markdown(self, r: Report) -> str:
        status = ("✅ **VERIFIED by Lean 4 kernel** (statement fidelity was checked by an LLM, so read the "
                  "statement yourself)") if r.lean_verified else \
                 "⚠️ **NOT formally verified** — informal solution only; treat as unverified."
        src = "\n".join(f"{i+1}. {s.title} ({s.source}, {s.year}) {s.url}" for i, s in enumerate(r.sources))
        lean = f"```lean\n{r.lean_code or r.lean_statement}\n```" if (r.lean_code or r.lean_statement) else "_none_"
        return (f"# Result\n\n{status}\n\n## Problem\n{r.problem}\n\n## Solution\n{r.informal}\n\n"
                f"## Lean 4\n{lean}\n\n## Sources\n{src or '_none_'}\n")
```

**gui.py**
```python
import sys
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QPlainTextEdit,
                               QPushButton, QTabWidget, QTextBrowser, QLabel)
from agent import MathAgent

class Worker(QThread):
    log = Signal(str)
    done = Signal(object)
    def __init__(self, problem):
        super().__init__(); self.problem = problem
    def run(self):
        try:
            agent = MathAgent(emit=lambda stage, text: self.log.emit(f"[{stage}] {text}\n"))
            self.done.emit(agent.run(self.problem))
        except Exception as e:
            self.log.emit(f"[error] {e}\n"); self.done.emit(None)

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Math Research Agent"); self.resize(1100, 800)
        w = QWidget(); lay = QVBoxLayout(w); self.setCentralWidget(w)
        lay.addWidget(QLabel("Enter a math problem:"))
        self.inp = QPlainTextEdit(); self.inp.setMaximumHeight(120); lay.addWidget(self.inp)
        self.btn = QPushButton("Research & Solve"); self.btn.clicked.connect(self.start); lay.addWidget(self.btn)
        self.tabs = QTabWidget(); lay.addWidget(self.tabs)
        self.log = QPlainTextEdit(readOnly=True); self.report = QTextBrowser(); self.lean = QPlainTextEdit(readOnly=True)
        for wd, name in ((self.log, "Live log"), (self.report, "Report"), (self.lean, "Lean code")):
            self.tabs.addTab(wd, name)

    def start(self):
        p = self.inp.toPlainText().strip()
        if not p: return
        self.btn.setEnabled(False); self.log.clear()
        self.worker = Worker(p)
        self.worker.log.connect(lambda t: (self.log.insertPlainText(t), self.log.ensureCursorVisible()))
        self.worker.done.connect(self.finish); self.worker.start()

    def finish(self, r):
        self.btn.setEnabled(True)
        if r:
            self.report.setMarkdown(r.markdown); self.lean.setPlainText(r.lean_code or r.lean_statement)
            self.tabs.setCurrentWidget(self.report)

def run_gui():
    app = QApplication(sys.argv); w = Window(); w.show(); sys.exit(app.exec())
```

**main.py**
```python
import sys
if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--cli":
        from agent import MathAgent
        r = MathAgent(emit=lambda s, t: print(f"[{s}] {t}")).run(" ".join(sys.argv[2:]))
        print(r.markdown)
    else:
        from gui import run_gui
        run_gui()
```

## 4. Running it

```bash
pip install -r requirements.txt
docker build -t mathagent-lean -f docker/Dockerfile.lean .
cp .env.example .env          # set your LLM endpoint/model
python main.py                # desktop GUI
python main.py --cli "Prove that for all positive reals a,b,c: a^2+b^2+c^2 >= ab+bc+ca"
```

Try an easy inequality first to confirm the Lean loop works before moving to harder problems.

## 5. What to know

- **Lean verification is the only real proof certificate.** The agent labels anything else "NOT formally verified". For research-level problems, a general LLM will often fail to get a complete Lean proof, and that is normal. The pipeline still gives you the literature survey and the informal attempt.
- **Statement fidelity is the weak point of any auto-formalizer.** A proof of the wrong statement is worthless. I mitigate this by compiling the statement first, back-translating it, locking the theorem header during proving, and auditing axioms. You should still read the Lean statement yourself.
- **Model quality matters most.** For the main model, use the strongest reasoning model you can reach (Gemini free tier, DeepSeek-R1 via OpenRouter or Ollama, Qwen). For `PROVER_MODEL`, a Lean-specialized model (DeepSeek-Prover-V2, Goedel-Prover, Kimina-Prover) helps a lot.
- **Sandbox:** Docker runs with `--network none`, a memory limit, a CPU limit, a pids limit, all capabilities dropped, and a read-only mount. For stronger isolation, run Docker with gVisor or Firecracker. `LEAN_MODE=local` is not sandboxed.
- **API drift:** I wrote these endpoints from memory (arXiv, OpenAlex, Crossref, Semantic Scholar, Stack Exchange, Loogle, OEIS). Failures are swallowed by `safe()`, so if a source silently returns nothing, test it on its own.



=================



# Math Research Agent v2: all seven upgrades

This version adds every upgrade you listed. The main additions are a persistent Lean REPL with best-first tactic search and backtracking, parallel proof-skeleton holes, a SymPy/Python numerical tester, a SQLite cache with a vector store for your own papers, and LaTeX/BibTeX export.

I could not run this against live services, so the code is written against the public APIs as I know them. A `smoke_test.py` file is included to find any endpoint whose response shape has changed. Verification notes are at the end.

## What's new

| Upgrade | Where it lives |
|---|---|
| zbMATH Open, CORE, HAL, Zenodo | `sources.py` |
| Lean REPL client, tactic-level search with backtracking | `lean_repl.py`, `tactic_search.py` |
| Proof skeletons, holes proven in parallel | `prover.py` |
| SymPy/Python counterexample testing | `pytool.py`, `agent.explore()` |
| LeanSearch and Moogle alongside Loogle | `premise.py` |
| SQLite cache, PDF store, vector store for your papers | `cache.py`, `vectorstore.py` |
| LaTeX + BibTeX export | `export.py` |

**On LeanDojo:** I used the Lean REPL protocol directly instead of LeanDojo. LeanDojo's `Dojo` is built on the same tactic-state interface, but it requires tracing a repository into its dataset format first, which is heavy for an interactive desktop tool. If you want LeanDojo later, wrap it in a class with the same `tactic(state, text)` method as `LeanRepl`.

## Updated architecture

```
Problem
  │
  ▼
[1 Deconstruct]     LLM → goal, givens, domain, sub-problems, search queries
  │
  ▼
[2 Numerical test]  LLM writes a Python tester → sandbox runs it
  │                 → counterexample? (flags the claim as likely false)
  │                 → pattern found? (feeds the solver)
  ▼
[3 Research]        arXiv · S2 · OpenAlex · Crossref · zbMATH · CORE · HAL · Zenodo
  │                 · StackExchange · web · OEIS · your own library (vector store)
  │                 all results cached in SQLite; PDFs cached on disk
  ▼
[4 Informal solve]  uses research + numerical evidence, then referee loop
  ▼
[5 Formalize]       Lean statement → compile → back-translate → fidelity check
  ▼
[6 Skeleton]        LLM writes `have … := by sorry` scaffold → compile/repair
  ▼
[7 Parallel holes]  N Lean REPL workers (persistent processes)
  │                 each hole: best-first tactic search over proof states
  │                 premises from LeanSearch/Moogle; closers tried at every node
  │                 backtracks automatically when a branch dies
  │                 holes assembled back into the file → full kernel check
  │                 fallback: whole-theorem search if skeleton fails
  ▼
[8 Export]          report.md · report.tex · refs.bib · proof.lean · sources.json
                    numerical_tests.py · report.pdf (if latexmk is installed)
```

## Project layout

```
mathagent/
├── main.py
├── gui.py
├── agent.py
├── config.py
├── llm.py
├── util.py
├── cache.py
├── vectorstore.py
├── sources.py
├── premise.py
├── lean_sandbox.py
├── lean_repl.py
├── tactic_search.py
├── prover.py
├── pytool.py
├── export.py
├── smoke_test.py
├── requirements.txt
├── .env.example
└── docker/
    ├── Dockerfile.lean
    └── Dockerfile.py
```

## 1. Setup files

**requirements.txt**
```
openai>=1.40
httpx>=0.27
PySide6>=6.7
pymupdf>=1.24
ddgs>=9.0
python-dotenv>=1.0
numpy>=1.26
sympy>=1.13
```

**.env.example**
```
# ---- LLM (any OpenAI-compatible endpoint) ----
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:32b-instruct
PROVER_MODEL=                       # optional Lean-specialized model
# Embeddings for your own library. Ollama example: nomic-embed-text.
# Empty = offline hashed bag-of-words (works, lower quality).
EMBED_MODEL=

# ---- Academic sources ----
CONTACT_EMAIL=you@example.com       # polite pool for OpenAlex / Crossref
S2_API_KEY=                         # optional, avoids Semantic Scholar 429s
CORE_API_KEY=                       # free registration at core.ac.uk/services/api

# ---- Storage ----
DATA_DIR=./data                     # SQLite cache, vector store, PDFs
RUNS_DIR=./runs                     # exported reports

# ---- Lean ----
LEAN_MODE=docker                    # docker | local
LEAN_IMAGE=mathagent-lean
LEAN_WORKERS=2                      # parallel REPL processes (each ~4-8 GB RAM)
LEAN_MEM=4g
REPL_MEM=8g
LEAN_CPUS=2
LEAN_TIMEOUT=120
REPL_BIN=/home/lean/repl/.lake/build/bin/repl
LEAN_PROJECT_DIR=                   # only for LEAN_MODE=local

# ---- Tactic search ----
SEARCH_BUDGET=200                   # tactic calls per hole
SEARCH_SECONDS=600
TACTICS_PER_NODE=5

# ---- Python / SymPy sandbox ----
PY_MODE=docker                      # docker | local (local = rlimits only, not sandboxed)
PY_IMAGE=mathagent-py
```

**docker/Dockerfile.lean** (Lean 4, Mathlib, and a REPL built against the same toolchain)
```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y curl git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd -m lean
USER lean
WORKDIR /home/lean
ENV PATH="/home/lean/.elan/bin:${PATH}"

# Optional: pin Mathlib to a release tag, e.g. --build-arg MATHLIB_TAG=v4.x.0
ARG MATHLIB_TAG=""
RUN curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none

RUN if [ -n "$MATHLIB_TAG" ]; then B="--branch $MATHLIB_TAG"; else B=""; fi && \
    git clone --depth 1 $B https://github.com/leanprover-community/mathlib4.git proj

WORKDIR /home/lean/proj
RUN elan toolchain install "$(cat lean-toolchain)" && lake exe cache get

# The REPL must be built with the SAME Lean version as Mathlib.
# REPL tags match Lean release tags. If this checkout fails, pick the REPL tag
# that matches lean-toolchain by hand.
RUN git clone https://github.com/leanprover-community/repl.git /home/lean/repl && \
    cd /home/lean/repl && \
    git checkout "$(sed 's/.*://' /home/lean/proj/lean-toolchain)" && \
    lake build

# Sanity checks: one-shot compile and REPL binary present
RUN printf 'import Mathlib\nexample : 2 + 2 = 4 := by norm_num\n' > /tmp/t.lean && \
    lake env lean /tmp/t.lean && \
    test -x /home/lean/repl/.lake/build/bin/repl
```
Build: `docker build -t mathagent-lean -f docker/Dockerfile.lean .` (about 6 GB, 30 to 60 minutes the first time).

**docker/Dockerfile.py** (tiny sandbox for SymPy/NumPy tests)
```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir sympy==1.13.3 numpy mpmath
RUN useradd -m runner
USER runner
WORKDIR /work
```
Build: `docker build -t mathagent-py -f docker/Dockerfile.py .`

## 2. Code

**config.py**
```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def env(key, default=""):
    return os.getenv(key) or default

@dataclass
class Config:
    # LLM
    base_url: str = env("LLM_BASE_URL", "http://localhost:11434/v1")
    api_key: str = env("LLM_API_KEY", "ollama")
    model: str = env("LLM_MODEL", "qwen2.5:32b-instruct")
    prover_model: str = env("PROVER_MODEL") or env("LLM_MODEL", "qwen2.5:32b-instruct")
    embed_model: str = env("EMBED_MODEL")

    # Source keys
    email: str = env("CONTACT_EMAIL", "anon@example.com")
    s2_key: str = env("S2_API_KEY")
    core_key: str = env("CORE_API_KEY")

    # Storage
    data_dir: str = env("DATA_DIR", "./data")
    runs_dir: str = env("RUNS_DIR", "./runs")

    # Lean
    lean_mode: str = env("LEAN_MODE", "docker")
    lean_image: str = env("LEAN_IMAGE", "mathagent-lean")
    lean_project: str = env("LEAN_PROJECT_DIR")
    repl_bin: str = env("REPL_BIN", "/home/lean/repl/.lake/build/bin/repl")
    lean_mem: str = env("LEAN_MEM", "4g")
    repl_mem: str = env("REPL_MEM", "8g")
    lean_cpus: str = env("LEAN_CPUS", "2")
    lean_timeout: int = int(env("LEAN_TIMEOUT", "120"))
    lean_workers: int = int(env("LEAN_WORKERS", "2"))
    heartbeats: int = int(env("HEARTBEATS", "400000"))

    # Tactic search
    search_budget: int = int(env("SEARCH_BUDGET", "200"))
    search_seconds: int = int(env("SEARCH_SECONDS", "600"))
    tactics_per_node: int = int(env("TACTICS_PER_NODE", "5"))

    # Python tool
    py_mode: str = env("PY_MODE", "docker")
    py_image: str = env("PY_IMAGE", "mathagent-py")

    # Agent behaviour
    research_rounds: int = 2
    papers_to_read: int = 6
    informal_revisions: int = 2
    skeleton_repairs: int = 3

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "agent.sqlite3")

    @property
    def pdf_dir(self):
        return os.path.join(self.data_dir, "pdfs")
```

**util.py**
```python
import re

def extract_block(txt, langs=("lean", "lean4")):
    blocks = re.findall(r"```([\w+-]*)[ \t]*\n(.*?)```", txt, re.S)
    for lang, body in blocks:
        if lang.lower() in langs:
            return body.strip()
    return blocks[0][1].strip() if blocks else txt.strip()

def strip_imports(code):
    return "\n".join(l for l in code.splitlines() if not l.strip().startswith("import ")).strip()

def norm(s):
    return re.sub(r"\s+", " ", s).strip()

def header_of(code):
    m = re.search(r"theorem\s+main_thm.*?:=", code, re.S)
    return norm(m.group(0)) if m else ""
```

**llm.py**
```python
import json, re
from openai import OpenAI

class LLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=300)

    def chat(self, system, user, model=None, temperature=0.2):
        last = None
        for _ in range(3):
            try:
                r = self.client.chat.completions.create(
                    model=model or self.cfg.model,
                    temperature=temperature,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                txt = r.choices[0].message.content or ""
                return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            except Exception as e:
                last = e
        raise RuntimeError(f"LLM failed: {last}")

    def json(self, system, user, **kw):
        txt = self.chat(system + "\nReply with ONE valid JSON object and nothing else.", user, **kw)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
```

**cache.py** (SQLite: search results and PDF store)
```python
import hashlib, json, os, sqlite3, threading, time
from pathlib import Path

class Cache:
    def __init__(self, db_path, pdf_dir):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pdf_dir).mkdir(parents=True, exist_ok=True)
        self.pdf_dir = pdf_dir
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS search(key TEXT PRIMARY KEY, payload TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS pdfs(url TEXT PRIMARY KEY, path TEXT, ts REAL);
            """)
            self.db.commit()

    def get(self, key, ttl):
        with self.lock:
            row = self.db.execute("SELECT payload, ts FROM search WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[1] < ttl:
            return json.loads(row[0])
        return None

    def put(self, key, payload):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO search VALUES(?,?,?)",
                            (key, json.dumps(payload, ensure_ascii=False), time.time()))
            self.db.commit()

    def save_pdf(self, url, data):
        h = hashlib.sha256(url.encode()).hexdigest()
        path = os.path.join(self.pdf_dir, h + ".pdf")
        Path(path).write_bytes(data)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pdfs VALUES(?,?,?)", (url, path, time.time()))
            self.db.commit()
        return path

    def load_pdf(self, url):
        with self.lock:
            row = self.db.execute("SELECT path FROM pdfs WHERE url=?", (url,)).fetchone()
        if row and os.path.exists(row[0]):
            return Path(row[0]).read_bytes()
        return None
```

**vectorstore.py** (your own papers: chunking, embeddings, retrieval)
```python
import hashlib, re, sqlite3, threading, time, zlib
from pathlib import Path
import numpy as np

HASH_DIM = 2048

def read_text(path):
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(str(p)) as pdf:
            return "\n".join(pg.get_text() for pg in pdf)
    return p.read_text(encoding="utf-8", errors="ignore")

def chunk(text, size=1200, overlap=200):
    text = re.sub(r"[ \t]+", " ", text)
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return [c for c in out if c.strip()]

class Embedder:
    def __init__(self, llm, model=""):
        self.llm, self.model = llm, model
        self.id = f"api:{model}" if model else f"hash{HASH_DIM}"

    def __call__(self, texts):
        if self.model:
            vecs = []
            for i in range(0, len(texts), 32):
                r = self.llm.client.embeddings.create(model=self.model, input=texts[i:i + 32])
                vecs += [d.embedding for d in r.data]
            v = np.asarray(vecs, dtype=np.float32)
        else:
            v = np.zeros((len(texts), HASH_DIM), dtype=np.float32)
            for i, t in enumerate(texts):
                for tok in re.findall(r"\w+", t.lower()):
                    v[i, zlib.crc32(tok.encode()) % HASH_DIM] += 1.0
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1
        return v / n

class VectorStore:
    def __init__(self, db_path, embedder):
        self.emb = embedder
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    doc_id TEXT PRIMARY KEY, title TEXT, authors TEXT, year TEXT, path TEXT, added REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, pos INTEGER,
                    text TEXT, emb_id TEXT, vec BLOB);
                CREATE INDEX IF NOT EXISTS ix_chunks_emb ON chunks(emb_id);
            """)
            self.db.commit()

    def ingest(self, path, title="", authors="", year=""):
        p = Path(path).resolve()
        doc_id = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        with self.lock:
            if self.db.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone():
                return 0
        pieces = chunk(read_text(p))
        if not pieces:
            return 0
        vecs = self.emb(pieces)
        with self.lock:
            self.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                            (doc_id, title or p.stem, authors, year, str(p), time.time()))
            self.db.executemany(
                "INSERT INTO chunks(doc_id,pos,text,emb_id,vec) VALUES(?,?,?,?,?)",
                [(doc_id, i, t, self.emb.id, v.tobytes()) for i, (t, v) in enumerate(zip(pieces, vecs))])
            self.db.commit()
        return len(pieces)

    def search(self, q, k=8):
        qv = self.emb([q])[0]
        with self.lock:
            rows = self.db.execute(
                "SELECT c.doc_id, c.text, c.vec, d.title, d.authors, d.year, d.path "
                "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE c.emb_id = ?",
                (self.emb.id,)).fetchall()
        if not rows:
            return []
        M = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
        scores = M @ qv
        seen, out = set(), []
        for i in np.argsort(-scores):
            doc = rows[i][0]
            if doc in seen:
                continue
            seen.add(doc)
            out.append((float(scores[i]), rows[i]))
            if len(out) >= k:
                break
        return out
```

**sources.py** (all free academic and web sources)
```python
import hashlib, html, logging, re, threading, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
import httpx
from config import Config

log = logging.getLogger("sources")
CFG = Config()
HTTP = httpx.Client(timeout=30, follow_redirects=True,
                    headers={"User-Agent": f"MathAgent/0.2 (mailto:{CFG.email})"})
CACHE = None   # set by MathAgent
VSTORE = None  # set by MathAgent

# Minimum seconds between calls per source (arXiv asks for 3 s; S2 is strict)
_MIN_GAP = {"arxiv": 3.0, "semanticscholar": 1.1, "web": 1.5, "oeis": 1.0,
            "leansearch": 1.0, "moogle": 1.0}
_last = {}
_locks = {k: threading.Lock() for k in _MIN_GAP}

def throttle(name):
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

    def key(self):
        return re.sub(r"\W+", "", self.title.lower())[:80]

def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

def _first(x):
    return (x[0] if isinstance(x, list) and x else x) or ""

# ---------------- academic ----------------

def arxiv(q, n=6):
    terms = " AND ".join(f"all:{w}" for w in re.findall(r"[\w\-]+", q)[:8]) or "all:math"
    r = HTTP.get("https://export.arxiv.org/api/query",
                 params={"search_query": terms, "max_results": n, "sortBy": "relevance"})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        link = e.findtext("a:id", "", ns)
        out.append(Doc(
            title=" ".join(e.findtext("a:title", "", ns).split()), url=link,
            pdf_url=link.replace("/abs/", "/pdf/"), source="arXiv",
            authors=", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns))[:120],
            year=e.findtext("a:published", "", ns)[:4],
            abstract=" ".join(e.findtext("a:summary", "", ns).split())))
    return out

def semanticscholar(q, n=6):
    h = {"x-api-key": CFG.s2_key} if CFG.s2_key else {}
    r = HTTP.get("https://api.semanticscholar.org/graph/v1/paper/search", headers=h,
                 params={"query": q, "limit": n,
                         "fields": "title,abstract,year,authors,url,openAccessPdf,citationCount"})
    r.raise_for_status()
    return [Doc(title=p.get("title") or "", url=p.get("url") or "", source="SemanticScholar",
                authors=", ".join(a.get("name", "") for a in p.get("authors", []))[:120],
                year=str(p.get("year") or ""), abstract=p.get("abstract") or "",
                pdf_url=(p.get("openAccessPdf") or {}).get("url") or "",
                extra={"citations": p.get("citationCount")})
            for p in r.json().get("data", [])]

def openalex(q, n=6):
    r = HTTP.get("https://api.openalex.org/works",
                 params={"search": q, "per-page": n, "mailto": CFG.email})
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        idx = w.get("abstract_inverted_index") or {}
        pos = {p: word for word, ps in idx.items() for p in ps}
        oa = w.get("best_oa_location") or {}
        out.append(Doc(
            title=w.get("title") or "", url=w.get("doi") or w.get("id") or "", source="OpenAlex",
            authors=", ".join(a["author"]["display_name"] for a in w.get("authorships", [])[:6]),
            year=str(w.get("publication_year") or ""),
            abstract=" ".join(pos[i] for i in sorted(pos)),
            pdf_url=oa.get("pdf_url") or ""))
    return [d for d in out if d.title]

def crossref(q, n=5):
    r = HTTP.get("https://api.crossref.org/works", params={"query": q, "rows": n, "mailto": CFG.email})
    r.raise_for_status()
    out = []
    for it in r.json()["message"]["items"]:
        if not it.get("title"):
            continue
        out.append(Doc(title=it["title"][0], url=it.get("URL", ""), source="Crossref",
                       authors=", ".join(f'{a.get("given", "")} {a.get("family", "")}'
                                         for a in it.get("author", [])[:6]),
                       year=str((it.get("issued", {}).get("date-parts") or [[""]])[0][0]),
                       abstract=_strip(it.get("abstract", ""))))
    return out

def zbmath(q, n=8):
    """zbMATH Open: the free, open metadata counterpart to MathSciNet."""
    r = HTTP.get("https://api.zbmath.org/v1/document/_search",
                 params={"search_string": q, "page": 0, "results_per_page": n})
    r.raise_for_status()
    out = []
    for it in r.json().get("result", []) or []:
        title = it.get("title", "")
        if isinstance(title, dict):
            title = title.get("title", "")
        contrib = it.get("contributors") if isinstance(it.get("contributors"), dict) else {}
        authors_raw = contrib.get("authors") or it.get("authors") or []
        authors = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a)
                            for a in authors_raw)[:120]
        ident = it.get("id") or it.get("identifier") or ""
        ab = it.get("abstract") if isinstance(it.get("abstract"), str) else ""
        out.append(Doc(title=_strip(str(title)),
                       url=f"https://zbmath.org/?q=an:{ident}" if ident else "",
                       source="zbMATH", authors=authors, year=str(it.get("year") or ""),
                       abstract=_strip(ab)))
    return [d for d in out if d.title]

def core(q, n=8):
    if not CFG.core_key:
        return []
    r = HTTP.get("https://api.core.ac.uk/v3/search/works", params={"q": q, "limit": n},
                 headers={"Authorization": f"Bearer {CFG.core_key}"})
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        doi = w.get("doi")
        links = w.get("links") or [{}]
        out.append(Doc(title=w.get("title") or "",
                       url=f"https://doi.org/{doi}" if doi else links[0].get("url", ""),
                       source="CORE",
                       authors=", ".join(a.get("name", "") for a in w.get("authors", [])
                                         if isinstance(a, dict))[:120],
                       year=str(w.get("yearPublished") or ""),
                       abstract=w.get("abstract") or "",
                       pdf_url=w.get("downloadUrl") or ""))
    return [d for d in out if d.title]

def hal(q, n=8):
    r = HTTP.get("https://api.archives-ouvertes.fr/search/",
                 params={"q": q, "rows": n, "wt": "json",
                         "fl": "title_s,authFullName_s,producedDateY_i,abstract_s,uri_s,fileMain_s"})
    r.raise_for_status()
    out = []
    for d in r.json().get("response", {}).get("docs", []):
        out.append(Doc(title=_strip(_first(d.get("title_s"))), url=d.get("uri_s", ""), source="HAL",
                       authors=", ".join(d.get("authFullName_s", []))[:120],
                       year=str(d.get("producedDateY_i", "")),
                       abstract=_strip(_first(d.get("abstract_s"))),
                       pdf_url=d.get("fileMain_s", "")))
    return [d for d in out if d.title]

def zenodo(q, n=8):
    r = HTTP.get("https://zenodo.org/api/records", params={"q": q, "size": n, "sort": "bestmatch"})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", {}).get("hits", []):
        m = h.get("metadata", {})
        links = h.get("links", {})
        pdf = next((f.get("links", {}).get("self", "") for f in h.get("files", [])
                    if f.get("key", "").lower().endswith(".pdf")), "")
        out.append(Doc(title=m.get("title", ""),
                       url=links.get("html") or links.get("self", ""), source="Zenodo",
                       authors=", ".join(c.get("name", "") for c in m.get("creators", []))[:120],
                       year=(m.get("publication_date") or "")[:4],
                       abstract=_strip(m.get("description", "")), pdf_url=pdf))
    return [d for d in out if d.title]

# ---------------- Q&A / web ----------------

def stackexchange(q, n=4):
    out = []
    for site in ("math.stackexchange", "mathoverflow.net"):
        r = HTTP.get("https://api.stackexchange.com/2.3/search/advanced",
                     params={"q": q, "site": site, "pagesize": n, "order": "desc",
                             "sort": "relevance", "filter": "withbody"})
        items = r.json().get("items", [])
        if not items:
            continue
        ids = ";".join(str(i["question_id"]) for i in items)
        a = HTTP.get(f"https://api.stackexchange.com/2.3/questions/{ids}/answers",
                     params={"site": site, "order": "desc", "sort": "votes", "filter": "withbody"})
        best = {}
        for ans in a.json().get("items", []):
            best.setdefault(ans["question_id"], ans)
        for it in items:
            ans = best.get(it["question_id"])
            out.append(Doc(
                title=html.unescape(it["title"]), url=it["link"], source=site,
                abstract=_strip(it.get("body", ""))[:700] +
                         ("\n--- TOP ANSWER ---\n" + _strip(ans["body"])[:2500] if ans else ""),
                extra={"score": it.get("score")}))
    return out

def web(q, n=6):
    from ddgs import DDGS
    sites = "(site:proofwiki.org OR site:ncatlab.org OR site:en.wikipedia.org OR site:planetmath.org OR site:mathworld.wolfram.com)"
    out = []
    with DDGS() as d:
        for query in (q + " mathematics", f"{q} {sites}"):
            for x in d.text(query, max_results=n):
                out.append(Doc(title=x["title"], url=x["href"], source="web", abstract=x.get("body", "")))
    return out

def oeis(q, n=4):
    r = HTTP.get("https://oeis.org/search", params={"q": q, "fmt": "json"})
    data = r.json()
    data = data if isinstance(data, list) else (data.get("results") or [])
    return [Doc(title=f'A{d["number"]:06d}: {d["name"]}', url=f'https://oeis.org/A{d["number"]:06d}',
                source="OEIS", abstract=d["name"] + "\n" + "; ".join(d.get("comment", [])[:4]))
            for d in data[:n]]

# ---------------- your own library ----------------

def local(q, n=8):
    if VSTORE is None:
        return []
    out = []
    for score, row in VSTORE.search(q, n):
        doc_id, text, _vec, title, authors, year, path = row
        out.append(Doc(title=title, url="file://" + path, source="local", authors=authors,
                       year=year, abstract=text[:800],
                       extra={"path": path, "score": round(score, 3)}))
    return out

SOURCES = {"arxiv": arxiv, "semanticscholar": semanticscholar, "openalex": openalex,
           "crossref": crossref, "zbmath": zbmath, "core": core, "hal": hal, "zenodo": zenodo,
           "stackexchange": stackexchange, "web": web, "oeis": oeis, "local": local}

TARGETS = {
    "papers": ["arxiv", "semanticscholar", "openalex", "crossref", "zbmath", "hal", "zenodo", "core"],
    "qa": ["stackexchange"],
    "web": ["web"],
    "oeis": ["oeis"],
    "mine": ["local"],
}

def run_cached(name, fn, q, ttl=7 * 86400):
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

def safe(name, q):
    try:
        if name == "local":
            return local(q)
        return run_cached(name, SOURCES[name], q)
    except Exception as e:
        log.warning("%s failed for %r: %s", name, q, e)
        return []

# ---------------- full text ----------------

def fulltext(doc, max_pages=30):
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

def best_chunks(text, keywords, k=4, size=5000):
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    kws = [w.lower() for w in keywords]
    scored = sorted(range(len(chunks)),
                    key=lambda i: -sum(chunks[i].lower().count(w) for w in kws))[:k]
    return "\n...\n".join(chunks[i] for i in sorted(scored))
```

**premise.py** (Mathlib premise retrieval: Loogle, LeanSearch, Moogle)
```python
import hashlib, logging
import sources as S

log = logging.getLogger("premise")

def _names(data):
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

def _post(url, bodies):
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

def loogle(q, n=8):
    """Pattern/name search over Mathlib: e.g. 'Nat.succ_le' or '_ * _ ≤ _'."""
    try:
        r = S.HTTP.get("https://loogle.lean-lang.org/json", params={"q": q}, timeout=25)
        return [f"{h.get('name', '')} : {h.get('type', '')}  [{h.get('module', '')}]"
                for h in r.json().get("hits", [])[:n]]
    except Exception:
        return []

def leansearch(q, n=8):
    """Natural-language and semantic search over Mathlib (leansearch.net)."""
    return _post("https://leansearch.net/api/search",
                 [[[q], n], {"query": [q], "num_results": n}])[:n]

def moogle(q, n=8):
    """Semantic Mathlib search (moogle.ai)."""
    return _post("https://www.moogle.ai/api/search", [{"query": q}])[:n]

def _cached(name, q, fn, n=8):
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

def search_names(queries, n=6):
    """LLM- or error-derived queries → candidate Mathlib names (used by the skeleton repair loop)."""
    out = []
    for q in queries[:5]:
        out += _cached("loogle", q, loogle, n)
        out += _cached("leansearch", q, leansearch, n)
    return list(dict.fromkeys(out))

def for_goal(goal, n=8):
    """Hints for a proof state: search with the target type of the goal."""
    target = goal.split("⊢")[-1].strip()[:300]
    if not target:
        return []
    names = _cached("leansearch", target, leansearch, n) + _cached("moogle", target, moogle, n)
    return list(dict.fromkeys(names))[:n]
```

**lean_sandbox.py** (one-shot compile and axiom audit)
```python
import os, re, subprocess, tempfile
from dataclasses import dataclass
from pathlib import Path

SAFE_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}

@dataclass
class LeanResult:
    compiled: bool   # no errors
    complete: bool   # compiled, no sorry, only standard axioms
    output: str

class LeanSandbox:
    def __init__(self, cfg):
        self.cfg = cfg

    def check(self, code, theorem=None):
        if theorem:
            code += f"\n\n#print axioms {theorem}\n"
        c = self.cfg
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "Main.lean"
            f.write_text(code, encoding="utf-8")
            os.chmod(f, 0o644)
            if c.lean_mode == "docker":
                cmd = ["docker", "run", "--rm", "--network", "none",
                       "--memory", c.lean_mem, "--cpus", c.lean_cpus, "--pids-limit", "256",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "-v", f"{d}:/work:ro", c.lean_image,
                       "timeout", str(c.lean_timeout), "lake", "env", "lean", "/work/Main.lean"]
                cwd = None
            else:  # NOT sandboxed
                cmd, cwd = ["lake", "env", "lean", str(f)], c.lean_project
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=c.lean_timeout + 60, cwd=cwd)
            except subprocess.TimeoutExpired:
                return LeanResult(False, False, "TIMEOUT")
            except FileNotFoundError as e:
                return LeanResult(False, False, f"sandbox unavailable: {e}")
        out = (p.stdout + p.stderr).strip()
        if p.returncode == 124:
            return LeanResult(False, False, "TIMEOUT\n" + out)
        has_error = p.returncode != 0 or bool(re.search(r":\d+:\d+: error", out))
        has_sorry = "sorry" in out
        bad_axiom = False
        if theorem:
            m = re.search(r"depends on axioms: \[(.*?)\]", out, re.S)
            if m:
                used = {a.strip() for a in m.group(1).split(",") if a.strip()}
                bad_axiom = not used <= SAFE_AXIOMS
            elif "does not depend on any axioms" in out:
                bad_axiom = False
            else:
                bad_axiom = True  # cannot certify
        return LeanResult(not has_error, (not has_error) and not has_sorry and not bad_axiom, out)
```

**lean_repl.py** (persistent Lean REPL client)
```python
import json, queue, subprocess, threading

class ReplError(RuntimeError):
    pass

class LeanRepl:
    """
    Talks to leanprover-community/repl over stdin/stdout.
    Each request is a JSON object followed by a blank line; each response is a
    JSON object followed by a blank line.
    """

    def __init__(self, cmd, cwd=None, timeout=120):
        self.timeout = timeout
        self.p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8")
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        buf = []
        for line in self.p.stdout:
            if line.strip():
                buf.append(line)
                continue
            if buf:
                text = "".join(buf)
                buf = []
                try:
                    self.q.put(json.loads(text))
                except json.JSONDecodeError:
                    pass  # stray non-JSON output (warnings etc.)
        self.q.put(None)

    def _send(self, obj, timeout=None):
        if self.p.poll() is not None:
            raise ReplError("REPL process exited")
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n\n")
        self.p.stdin.flush()
        try:
            r = self.q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise TimeoutError("REPL response timeout")
        if r is None:
            raise ReplError("REPL closed")
        return r

    def cmd(self, text, env=None, timeout=None):
        obj = {"cmd": text}
        if env is not None:
            obj["env"] = env
        return self._send(obj, timeout)

    def tactic(self, tac, state):
        return self._send({"tactic": tac, "proofState": state})

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
```

**tactic_search.py** (best-first search with backtracking over Lean proof states)
```python
import hashlib, heapq, itertools, time
from dataclasses import dataclass

# Cheap automation tried at every node. Lean failures are fast.
CLOSERS = ["norm_num", "linarith", "nlinarith", "positivity", "omega", "simp_all", "aesop"]

@dataclass
class Node:
    state: int      # REPL proofState id
    goals: list     # goal strings from the REPL
    path: list      # tactics applied from the root
    depth: int

def _is_error(r):
    if "goals" not in r:
        return True
    return any(m.get("severity") == "error" for m in r.get("messages", []))

def _key(goals):
    return hashlib.sha1("\x01".join(goals).encode()).hexdigest()

def search(repl, root_state, goal, propose, *, budget=200, seconds=600, max_depth=14,
           emit=print, tag=""):
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
```

**prover.py** (proof skeletons, parallel hole proving, assembly)
```python
import os, queue, re, threading
from concurrent.futures import ThreadPoolExecutor

from lean_repl import LeanRepl, ReplError
from tactic_search import search
from util import extract_block, header_of, strip_imports

SYS = "You are an expert Lean 4 / Mathlib proof engineer. Output exactly what is asked."

class ProofError(RuntimeError):
    pass

def _errors(resp):
    return [m.get("data", "") for m in resp.get("messages", []) if m.get("severity") == "error"]

def _strip_sorry_tail(stmt):
    return re.sub(r":=\s*by\s*sorry\s*$", ":= by", strip_imports(stmt).strip())

def assemble(body, holes, scripts):
    """Replace each solved `sorry` with its tactic script. Edits go bottom-up so positions stay valid."""
    lines = body.split("\n")
    edits = [(h["pos"]["line"], h["pos"]["column"], h["end"]["column"], s)
             for h, s in zip(holes, scripts) if s]
    for line_no, c1, c2, script in sorted(edits, reverse=True):
        line = lines[line_no - 1]
        ind = len(line) - len(line.lstrip(" "))
        prefix, suffix = line[:c1], line[c2:]
        if prefix.strip() == "":
            # `sorry` is a tactic on its own line
            block = [prefix + script[0]] + [" " * c1 + t for t in script[1:]]
        else:
            # `sorry` follows `by` on the same line
            block = [prefix.rstrip()] + [" " * (ind + 2) + t for t in script]
        block[-1] += suffix
        lines[line_no - 1:line_no] = block
    return "\n".join(lines)

def make_skeleton(cfg, llm, statement, informal, check, emit, search_names):
    want = header_of(statement)
    feedback = ""
    for attempt in range(cfg.skeleton_repairs + 1):
        txt = llm.chat(SYS, f"""Write a Lean 4 + Mathlib PROOF SKELETON for the theorem below.

STATEMENT (copy the header EXACTLY):
{_strip_sorry_tail(statement)}

INFORMAL PROOF (research-backed):
{informal[:6000]}

Rules:
- Keep the theorem header identical. The proof starts after `:= by`.
- Apart from `intro`/`obtain` for naming hypotheses from the statement, use only
  `have name : <precise Lean proposition> := by sorry` lines mirroring the informal proof.
  Each proposition must typecheck with the hypotheses in scope at that point.
- Finish with one line `sorry`, which stands for closing the main goal from the haves.
- Every `sorry` becomes a hole that is filled automatically. Do not write other tactics.
- Output only one ```lean block.{feedback}""", model=cfg.prover_model, temperature=0.3)
        body = strip_imports(extract_block(txt, ("lean", "lean4")))
        if header_of(body) != want:
            feedback = "\n\nThe theorem header changed. Copy the statement EXACTLY as given."
            emit("prove", f"skeleton attempt {attempt + 1}: header changed, retrying")
            continue
        res = check("import Mathlib\n\n" + body)
        emit("prove", f"skeleton attempt {attempt + 1}: {'compiles' if res.compiled else 'has errors'}")
        if res.compiled:
            return body
        unknown = re.findall(r"unknown (?:identifier|constant) '([^']+)'", res.output)
        hints = search_names(unknown) if unknown else []
        feedback = (f"\n\nYour previous skeleton:\n{body}\n\nLean errors:\n{res.output[:2500]}\n"
                    + ("Possibly relevant Mathlib names:\n" + "\n".join(hints) + "\n" if hints else "")
                    + "Fix all errors.")
    return None

class ProofRunner:
    def __init__(self, cfg, llm, check, hints, emit):
        self.cfg, self.llm, self.check, self.hints, self.emit = cfg, llm, check, hints, emit
        self._live = []
        self._lock = threading.Lock()

    # ---- REPL lifecycle ----
    def _cmd(self):
        c = self.cfg
        if c.lean_mode == "docker":
            return ["docker", "run", "-i", "--rm", "--network", "none",
                    "--memory", c.repl_mem, "--cpus", c.lean_cpus,
                    "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    c.lean_image, "bash", "-c",
                    f"cd /home/lean/proj && exec lake env {c.repl_bin}"], None
        return ["lake", "env", c.repl_bin], c.lean_project

    def _start(self):
        cmd, cwd = self._cmd()
        r = LeanRepl(cmd, cwd=cwd, timeout=self.cfg.lean_timeout)
        with self._lock:
            self._live.append(r)
        errs = _errors(r.cmd("import Mathlib", timeout=900))
        if errs:
            raise ProofError("import Mathlib failed: " + "\n".join(errs)[:500])
        return r

    def _load(self, repl, body):
        """Elaborate the body in env 0 and return its holes (sorries) sorted by position."""
        r = repl.cmd(body, env=0)
        errs = _errors(r)
        if errs:
            raise ProofError("\n".join(errs)[:3000])
        ss = sorted(r.get("sorries", []), key=lambda s: (s["pos"]["line"], s["pos"]["column"]))
        return [{"state": s["proofState"], "goal": s["goal"], "pos": s["pos"], "end": s["endPos"]}
                for s in ss]

    # ---- hole solving ----
    def _propose(self, goal, path, hints):
        hint_txt = "\n".join(hints) or "(none)"
        try:
            r = self.llm.json(SYS, f"""Goal state (hypotheses above ⊢, target below):
{goal[:3000]}

Tactics already applied on this branch: {path[-6:] or '(none)'}
Possibly useful Mathlib names:
{hint_txt}

Propose {self.cfg.tactics_per_node} DIFFERENT candidate tactics that make progress on the first goal.
Each must be ONE line of Lean 4. No sorry, admit, native_decide, or multi-line blocks.
A one-line `have h : P := by nlinarith [...]` is allowed.
Reply JSON: {{"tactics": ["...", "..."]}}""", model=self.cfg.prover_model, temperature=0.7)
        except Exception:
            return []
        out = []
        for t in r.get("tactics", []):
            if isinstance(t, str):
                t = t.strip()
                if t and "\n" not in t and not re.search(r"\b(sorry|admit|native_decide)\b", t):
                    out.append(t)
        return out[: self.cfg.tactics_per_node]

    def _solve_hole(self, repl, h, i):
        hints = self.hints(h["goal"])
        return search(repl, h["state"], h["goal"],
                      lambda node: self._propose(node.goals[0], node.path, hints),
                      budget=self.cfg.search_budget, seconds=self.cfg.search_seconds,
                      emit=self.emit, tag=f"hole {i}")

    def _worker(self, repl, pre, body, q, scripts):
        try:
            mine = pre if pre is not None else self._load(repl, body)
        except Exception as e:
            self.emit("prove", f"worker failed to load: {e}")
            return
        while True:
            try:
                i = q.get_nowait()
            except queue.Empty:
                return
            try:
                scripts[i] = self._solve_hole(repl, mine[i], i)
            except Exception as e:
                self.emit("prove", f"hole {i}: {type(e).__name__}: {e}; restarting REPL")
                repl.close()
                try:
                    repl = self._start()
                    mine = self._load(repl, body)
                except Exception as e2:
                    self.emit("prove", f"worker stopped: {e2}")
                    return

    # ---- public ----
    def prove(self, body):
        """Returns (final_code, holes_solved, holes_total, LeanResult)."""
        body = f"set_option maxHeartbeats {self.cfg.heartbeats} in\n{body}"
        n = max(1, self.cfg.lean_workers)
        try:
            with ThreadPoolExecutor(n) as ex:
                repls = list(ex.map(lambda _: self._start(), range(n)))
            holes = self._load(repls[0], body)
            self.emit("prove", f"{len(holes)} hole(s), {n} worker(s)")
            scripts = [None] * len(holes)
            q = queue.Queue()
            for i in range(len(holes)):
                q.put(i)
            ts = [threading.Thread(target=self._worker,
                                   args=(repls[k], holes if k == 0 else None, body, q, scripts),
                                   daemon=True) for k in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            for r in list(self._live):
                r.close()
            self._live.clear()

        final = assemble(body, holes, scripts)
        solved = sum(1 for s in scripts if s)
        res = self.check(f"import Mathlib\n\n{final}", theorem="main_thm")
        self.emit("prove", f"holes closed {solved}/{len(holes)}; kernel check complete={res.complete}")
        return final, solved, len(holes), res
```

**pytool.py** (SymPy/Python sandbox for numerical tests)
```python
import os, subprocess, sys, tempfile
from pathlib import Path

def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    except Exception:
        pass

class PyRunner:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, code, timeout=60):
        """Returns (ok, stdout, stderr)."""
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "test.py"
            f.write_text(code, encoding="utf-8")
            os.chmod(f, 0o644)
            if self.cfg.py_mode == "docker":
                cmd = ["docker", "run", "--rm", "-i", "--network", "none",
                       "--memory", "1g", "--cpus", "1", "--pids-limit", "64",
                       "--read-only", "--tmpfs", "/tmp:size=64m",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "-v", f"{d}:/work:ro", self.cfg.py_image,
                       "timeout", str(timeout), "python", "/work/test.py"]
                pre = None
            else:  # resource limits only, NOT sandboxed
                cmd = [sys.executable, str(f)]
                pre = _limits if os.name == "posix" else None
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20,
                                   preexec_fn=pre)
            except subprocess.TimeoutExpired:
                return False, "", "TIMEOUT"
            except FileNotFoundError as e:
                return False, "", str(e)
            return p.returncode == 0, p.stdout, p.stderr
```

**export.py** (LaTeX and BibTeX)
```python
import datetime, json, re, shutil, subprocess
from dataclasses import asdict
from pathlib import Path
from util import extract_block

SYS = "You are a LaTeX typesetter for mathematics. Output only LaTeX."

TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage{fontspec}
\usepackage{unicode-math}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amsthm,amssymb}
\usepackage[colorlinks=true]{hyperref}
\usepackage{listings}
\lstset{basicstyle=\ttfamily\small, breaklines=true, columns=flexible}
\newtheorem{theorem}{Theorem}
\newtheorem{lemma}[theorem]{Lemma}
\title{Mathematical Research Report}
\date{\today}
\begin{document}
\maketitle
\noindent\textbf{Status:} @@STATUS@@

@@BODY@@

\section*{Formal verification (Lean 4)}
\begin{lstlisting}
@@LEAN@@
\end{lstlisting}

\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

def _esc(s):
    return re.sub(r"([&%$#_])", r"\\\1", s or "")

def bibtex(sources):
    entries, keys, used = [], {}, set()
    for i, d in enumerate(sources, 1):
        parts = d.authors.split(",")[0].split()
        last = parts[-1] if parts else "anon"
        word = (re.findall(r"[A-Za-z]+", d.title) or ["ref"])[0].lower()
        base = re.sub(r"\W", "", f"{last}{d.year}{word}") or f"ref{i}"
        key, c = base, 2
        while key in used:
            key, c = f"{base}{c}", c + 1
        used.add(key)
        keys[i] = key
        authors = " and ".join(a.strip() for a in d.authors.split(",") if a.strip())
        fields = [("title", _esc(d.title)), ("author", _esc(authors)), ("year", d.year),
                  ("url", d.url), ("note", f"Source: {d.source}")]
        body = "\n".join(f"  {name} = {{{val}}}," for name, val in fields if val)
        entries.append(f"@misc{{{key},\n{body}\n}}")
    return "\n\n".join(entries), keys

def latex_body(llm, markdown, keys):
    md = re.sub(r"```lean.*?```", "", markdown, flags=re.S)
    txt = llm.chat(SYS,
        "Convert this Markdown report into the BODY of a LaTeX article.\n"
        "- No \\documentclass, no preamble, no \\begin{document}.\n"
        "- Use \\section / \\subsection, displayed math \\[ ... \\], and amsthm environments "
        "(theorem, lemma, proof) where natural.\n"
        "- Keep citation markers such as [3] unchanged.\n"
        "- Escape % & # _ wherever they are outside math mode.\n"
        "- Omit the Lean block and the sources list.\n"
        "- Output only LaTeX, no commentary.\n\nMARKDOWN:\n" + md,
        temperature=0.1)
    body = extract_block(txt, ("latex", "tex", ""))
    body = re.sub(r"\[(\d+)\]",
                  lambda m: f"\\cite{{{keys[int(m.group(1))]}}}" if int(m.group(1)) in keys else m.group(0),
                  body)
    return body

def compile_pdf(out):
    if not shutil.which("latexmk"):
        return False
    try:
        subprocess.run(["latexmk", "-xelatex", "-interaction=nonstopmode", "-file-line-error", "report.tex"],
                       cwd=out, capture_output=True, text=True, timeout=240)
    except Exception:
        return False
    return (out / "report.pdf").exists()

def write_run(agent, report):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(agent.cfg.runs_dir) / stamp
    out.mkdir(parents=True, exist_ok=True)

    bib, keys = bibtex(report.sources)
    (out / "refs.bib").write_text(bib, encoding="utf-8")
    (out / "report.md").write_text(report.markdown, encoding="utf-8")
    (out / "sources.json").write_text(
        json.dumps([asdict(s) for s in report.sources], indent=2, ensure_ascii=False), encoding="utf-8")
    if report.lean_code or report.statement:
        (out / "proof.lean").write_text(report.lean_code or report.statement, encoding="utf-8")
    if report.experiment_code:
        (out / "numerical_tests.py").write_text(report.experiment_code, encoding="utf-8")

    body = latex_body(agent.llm, report.markdown, keys)
    status = report.status.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")
    tex = (TEMPLATE.replace("@@STATUS@@", status)
                   .replace("@@BODY@@", body)
                   .replace("@@LEAN@@", report.lean_code or report.statement or "(none)"))
    (out / "report.tex").write_text(tex, encoding="utf-8")
    return out, compile_pdf(out)
```

**agent.py** (orchestration)
```python
import json, re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import premise
import sources as S
from cache import Cache
from config import Config
from export import write_run
from lean_sandbox import LeanSandbox
from llm import LLM
from prover import ProofRunner, make_skeleton
from pytool import PyRunner
from util import extract_block, strip_imports
from vectorstore import Embedder, VectorStore

MATH = ("You are a rigorous research mathematician. Be precise, never invent theorem statements "
        "or citations; if unsure, say so.")

@dataclass
class Report:
    problem: str = ""
    decomposition: dict = field(default_factory=dict)
    experiments: dict = field(default_factory=dict)
    experiment_code: str = ""
    sources: list = field(default_factory=list)
    insights: list = field(default_factory=list)
    informal: str = ""
    statement: str = ""
    lean_code: str = ""
    lean_verified: bool = False
    prover_stats: dict = field(default_factory=dict)
    status: str = "UNVERIFIED"
    markdown: str = ""

class MathAgent:
    def __init__(self, cfg=None, emit=None):
        self.cfg = cfg or Config()
        self.emit = emit or (lambda stage, text: print(f"[{stage}] {text}"))
        self.cache = Cache(self.cfg.db_path, self.cfg.pdf_dir)
        self.llm = LLM(self.cfg)
        self.vs = VectorStore(self.cfg.db_path, Embedder(self.llm, self.cfg.embed_model))
        S.CACHE, S.VSTORE = self.cache, self.vs
        self.lean = LeanSandbox(self.cfg)
        self.py = PyRunner(self.cfg)
        self.sources = []

    # ------------------------------------------------------------ library
    def ingest(self, paths):
        return {p: self.vs.ingest(p) for p in paths}

    # ------------------------------------------------------------ 1
    def deconstruct(self, problem):
        self.emit("deconstruct", "Analyzing the problem...")
        d = self.llm.json(MATH, f"""Deconstruct this problem.
PROBLEM: {problem}

JSON keys:
 "restatement", "type" (prove | compute | find | decide), "domain", "msc",
 "givens": [..], "goal": "...", "key_concepts": [..], "subproblems": [..], "likely_techniques": [..],
 "search_queries": [{{"q": "short keyword query", "targets": [...]}}]
   6-10 diverse queries covering the exact statement, generalizations, special cases, named theorems,
   and terminology synonyms. targets are chosen from: "papers", "qa", "web", "oeis", "mine"
   ("mine" = the user's own paper library).""")
        d.setdefault("restatement", problem)
        d.setdefault("type", "prove")
        d.setdefault("goal", problem)
        d.setdefault("givens", [])
        d.setdefault("key_concepts", [])
        if not d.get("search_queries"):
            d["search_queries"] = [{"q": problem[:200], "targets": ["papers", "qa", "web", "mine"]}]
        self.emit("deconstruct", json.dumps(d, indent=2, ensure_ascii=False))
        return d

    # ------------------------------------------------------------ 2
    def explore(self, problem, d):
        self.emit("explore", "Writing and running numerical tests...")
        fb = ""
        code = ""
        for _ in range(3):
            code = extract_block(self.llm.chat(MATH, f"""PROBLEM: {d.get('restatement', problem)}
TYPE: {d.get('type')}

Write a self-contained Python 3 script that tests this claim numerically.
- Allowed modules: sympy, numpy, mpmath, fractions, itertools, random, math. No file, network, or subprocess use.
- prove/decide: look for counterexamples over small exact inputs (fractions.Fraction) and random samples
  (random.seed(0)), including edge cases. Check identities symbolically with sympy where possible.
- find/compute: enumerate candidate values and print the pattern you observe.
- Total runtime under 15 seconds.
- The last printed line must be exactly:
  RESULT: {{"verdict": ..., "evidence": ..., "counterexample": ..., "candidate_answer": ...}}
  verdict is one of: no_counterexample_found, counterexample_found, pattern_found, inconclusive.
  evidence: short string including the number of tests run. counterexample and candidate_answer: null or a string.
Reply with one ```python block only.{fb}""", temperature=0.2), ("python", "py", ""))
            ok, out, err = self.py.run(code)
            m = re.search(r"^RESULT:\s*(\{.*\})\s*$", out, re.M)
            if ok and m:
                try:
                    res = json.loads(m.group(1))
                    self.emit("explore", f"{res.get('verdict')}: {res.get('evidence')}")
                    return res, code
                except json.JSONDecodeError:
                    pass
            fb = f"\n\nYour previous script failed.\nSTDERR:\n{err[-1500:]}\nSTDOUT tail:\n{out[-500:]}\nFix it."
        return {"verdict": "inconclusive", "evidence": "numerical script failed"}, code

    # ------------------------------------------------------------ 3
    def _search(self, queries):
        jobs = []
        for q in queries:
            if isinstance(q, str):
                q = {"q": q, "targets": ["papers", "qa", "web"]}
            for t in q.get("targets", ["papers"]):
                for s in S.TARGETS.get(t, []):
                    jobs.append((s, q["q"]))
        jobs = list(dict.fromkeys(jobs))
        self.emit("research", f"{len(jobs)} searches across sources")
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda j: S.safe(*j), jobs))
        return [d for r in res for d in r]

    def _rank(self, problem, docs):
        docs = docs[:50]
        if not docs:
            return []
        listing = "\n".join(f"[{i}] ({d.source}, {d.year}) {d.title}: {d.abstract[:300]}"
                            for i, d in enumerate(docs))
        r = self.llm.json(MATH, f"PROBLEM: {problem}\n\nCANDIDATES:\n{listing}\n\n"
                          f'Pick up to {self.cfg.papers_to_read} items most likely to contain methods, '
                          f'lemmas, or results useful for the problem. JSON: {{"top": [indices]}}')
        idx = [i for i in r.get("top", []) if isinstance(i, int) and 0 <= i < len(docs)]
        return [docs[i] for i in idx] or docs[: self.cfg.papers_to_read]

    def _read(self, docs, d):
        kws = d.get("key_concepts", [])[:8]

        def work(doc):
            try:
                text = S.best_chunks(S.fulltext(doc), kws)
                out = self.llm.chat(MATH, f"""PROBLEM: {d.get('restatement')}
SOURCE: {doc.title} ({doc.source}, {doc.year})
EXCERPTS:
{text[:18000]}

Extract ONLY what helps solve the problem: exact theorem/lemma statements, key techniques,
and how they could apply. Quote precisely and note hypotheses. If nothing useful, reply NONE.""")
            except Exception as e:
                out = f"NONE ({e})"
            return doc, out

        with ThreadPoolExecutor(3) as ex:
            results = list(ex.map(work, docs))
        ins = []
        for doc, out in results:
            if out.strip().upper().startswith("NONE"):
                continue
            self.sources.append(doc)
            ins.append({"src": len(self.sources), "text": out})
            self.emit("research", f"[{len(self.sources)}] {doc.title} ({doc.source}) {doc.url}")
        return ins

    def research(self, problem, d):
        seen, insights, queries = set(), [], d["search_queries"]
        for rnd in range(self.cfg.research_rounds):
            self.emit("research", f"--- round {rnd + 1} ---")
            new = []
            for doc in self._search(queries):
                if doc.key() not in seen:
                    seen.add(doc.key())
                    new.append(doc)
            self.emit("research", f"{len(new)} new candidates")
            insights += self._read(self._rank(problem, new), d)
            digest = "\n".join(f"[{i['src']}] {i['text'][:500]}" for i in insights) or "(nothing yet)"
            gap = self.llm.json(MATH, f"PROBLEM: {d.get('restatement')}\nINSIGHTS SO FAR:\n{digest}\n\n"
                                'Is this enough to attempt a solution? JSON: {"sufficient": bool, '
                                '"new_queries": [{"q": "...", "targets": ["papers"]}]}')
            if gap.get("sufficient") or not gap.get("new_queries"):
                break
            queries = gap["new_queries"]
        return insights

    # ------------------------------------------------------------ 4
    def solve_informal(self, d, insights, experiments):
        ctx = "\n\n".join(f"[{i['src']}] {self.sources[i['src'] - 1].title}\n{i['text']}"
                          for i in insights) or "(no sources found)"
        self.emit("solve", "Drafting solution using research and numerical evidence...")
        sol = self.llm.chat(MATH, f"""PROBLEM ANALYSIS:\n{json.dumps(d, indent=1, ensure_ascii=False)}

NUMERICAL EVIDENCE (Python tests):\n{json.dumps(experiments, indent=1, ensure_ascii=False)}

RESEARCH NOTES:\n{ctx}

Solve the problem. First state the strategy, citing the results you use as [n].
Then give a complete, rigorous, step-by-step solution. State the final answer clearly.
If numerical evidence contradicts the claim, say so. Mark any step you are not sure of with (UNSURE).""",
            temperature=0.3)
        for _ in range(self.cfg.informal_revisions):
            crit = self.llm.json(MATH, f"PROBLEM: {d.get('restatement')}\n\nSOLUTION:\n{sol}\n\n"
                                 'Act as a skeptical referee. Check every step, edge cases, and misuse of theorems. '
                                 'JSON: {"valid": bool, "issues": ["..."]}')
            self.emit("solve", f"Referee: {crit}")
            if crit.get("valid") or not crit.get("issues"):
                break
            sol = self.llm.chat(MATH, f"PROBLEM: {d.get('restatement')}\nRESEARCH:\n{ctx}\n\nSOLUTION:\n{sol}\n\n"
                                f"REFEREE ISSUES:\n{crit['issues']}\n\nRewrite the complete corrected solution.")
        return sol

    # ------------------------------------------------------------ 5
    def formalize(self, d):
        self.emit("lean", "Formalizing the statement in Lean 4 / Mathlib...")
        feedback = ""
        for _ in range(4):
            code = extract_block(self.llm.chat(MATH, f"""Write a Lean 4 + Mathlib formal STATEMENT for this problem.
PROBLEM: {d.get('restatement')}
TYPE: {d.get('type')}   GOAL: {d.get('goal')}   GIVENS: {d.get('givens')}
Rules:
- Start with `import Mathlib`. Name the theorem exactly `main_thm`. End the statement with `:= by sorry`.
- For compute/find problems, state that the answer equals the specific value (or a set equality).
- Be FAITHFUL: no extra hypotheses, no weakening.
- Output only one ```lean block.{feedback}""", model=self.cfg.prover_model), ("lean", "lean4"))
            code = "import Mathlib\n\n" + strip_imports(code)
            res = self.lean.check(code)
            if not res.compiled:
                feedback = f"\n\nPrevious attempt:\n{code}\nLean errors:\n{res.output[:2500]}\nFix them."
                continue
            back = self.llm.json(MATH, f"ORIGINAL: {d.get('restatement')}\n\nLEAN STATEMENT:\n{code}\n\n"
                                 'Translate the Lean statement back into plain math and compare it with the original. '
                                 'JSON: {"faithful": bool, "explanation": "..."}')
            self.emit("lean", f"Fidelity check: {back}")
            if back.get("faithful"):
                return code
            feedback = f"\n\nPrevious attempt:\n{code}\nUnfaithful because: {back.get('explanation')}"
        return ""

    # ------------------------------------------------------------ 6/7
    def prove(self, r, d):
        body = strip_imports(r.statement)
        runner = ProofRunner(self.cfg, self.llm, self.lean.check, premise.for_goal, self.emit)
        skel = make_skeleton(self.cfg, self.llm, r.statement, r.informal,
                             self.lean.check, self.emit, premise.search_names)
        plans = ([("skeleton", skel)] if skel else []) + [("whole", body)]
        best = None
        for name, cand in plans:
            self.emit("prove", f"proof search mode: {name}")
            try:
                final, solved, total, res = runner.prove(cand)
            except Exception as e:
                self.emit("prove", f"{name} mode failed: {e}")
                continue
            best = (name, final, solved, total)
            if res.complete:
                r.lean_code = final
                r.lean_verified = True
                r.prover_stats = {"mode": name, "holes_solved": solved, "holes_total": total}
                r.status = "VERIFIED by Lean 4 kernel"
                return
        if best:
            name, final, solved, total = best
            r.lean_code = final
            r.prover_stats = {"mode": name, "holes_solved": solved, "holes_total": total}
            r.status = f"UNVERIFIED: {solved}/{total} subgoals closed, Lean proof incomplete"
        else:
            r.status = "UNVERIFIED: Lean proof search failed"

    # ------------------------------------------------------------ run
    def run(self, problem):
        r = Report(problem=problem)
        d = self.deconstruct(problem)
        r.decomposition = d
        r.experiments, r.experiment_code = self.explore(problem, d)
        refuted = r.experiments.get("verdict") == "counterexample_found"
        r.insights = self.research(problem, d)
        r.sources = list(self.sources)
        r.informal = self.solve_informal(d, r.insights, r.experiments)
        r.statement = self.formalize(d)
        if refuted:
            r.status = "LIKELY FALSE: numerical counterexample found (verify it before trusting this)"
        elif r.statement:
            self.prove(r, d)
        else:
            r.status = "UNVERIFIED: no faithful Lean statement produced"
        r.markdown = self._markdown(r)
        self.emit("done", r.status)
        return r

    def export(self, report):
        return write_run(self, report)

    def _markdown(self, r):
        src = "\n".join(f"{i + 1}. {s.title} ({s.source}, {s.year}) {s.url}" for i, s in enumerate(r.sources))
        exp = r.experiments
        exp_txt = (f"Verdict: **{exp.get('verdict')}**\n\n{exp.get('evidence', '')}"
                   if exp else "_not run_")
        ps = r.prover_stats
        ps_txt = (f"Mode: {ps.get('mode')}, subgoals closed {ps.get('holes_solved')}/{ps.get('holes_total')}"
                  if ps else "")
        lean_src = r.lean_code or r.statement
        lean = f"```lean\n{lean_src}\n```" if lean_src else "_none_"
        return (f"# Result\n\n**Status:** {r.status}\n\n## Problem\n{r.problem}\n\n"
                f"## Numerical exploration\n{exp_txt}\n\n## Solution\n{r.informal}\n\n"
                f"## Lean 4\n{ps_txt}\n\n{lean}\n\n## Sources\n{src or '_none_'}\n")
```

Note: `agent.py` uses a `Report` class defined above `MathAgent`. The `deconstruct` method returns a dict, and `run()` stores it in `r.decomposition`.

**gui.py**
```python
import json, sys, traceback
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QPushButton, QTabWidget, QTextBrowser, QLabel,
                               QFileDialog)
from agent import MathAgent

class RunWorker(QThread):
    log = Signal(str)
    done = Signal(object, str)
    failed = Signal(str)

    def __init__(self, problem):
        super().__init__()
        self.problem = problem

    def run(self):
        try:
            agent = MathAgent(emit=lambda s, t: self.log.emit(f"[{s}] {t}\n"))
            rep = agent.run(self.problem)
            path, pdf = agent.export(rep)
            where = f"{path}" + ("  (PDF built)" if pdf else "  (LaTeX saved; install latexmk to build PDF)")
            self.done.emit(rep, where)
        except Exception:
            self.failed.emit(traceback.format_exc())

class IngestWorker(QThread):
    log = Signal(str)

    def __init__(self, files):
        super().__init__()
        self.files = files

    def run(self):
        agent = MathAgent(emit=lambda s, t: self.log.emit(f"[{s}] {t}\n"))
        for f in self.files:
            try:
                n = agent.vs.ingest(f)
                self.log.emit(f"ingested {f}: {n} chunks\n")
            except Exception as e:
                self.log.emit(f"failed {f}: {e}\n")

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Math Research Agent")
        self.resize(1200, 850)
        w = QWidget()
        self.setCentralWidget(w)
        lay = QVBoxLayout(w)

        lay.addWidget(QLabel("Enter a math problem:"))
        self.inp = QPlainTextEdit()
        self.inp.setMaximumHeight(110)
        lay.addWidget(self.inp)

        row = QHBoxLayout()
        self.btn = QPushButton("Research & Solve")
        self.btn.clicked.connect(self.start)
        self.add = QPushButton("Add papers to library…")
        self.add.clicked.connect(self.add_papers)
        row.addWidget(self.btn)
        row.addWidget(self.add)
        lay.addLayout(row)

        self.status = QLabel("")
        lay.addWidget(self.status)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.report = QTextBrowser()
        self.lean = QPlainTextEdit()
        self.lean.setReadOnly(True)
        self.numerics = QPlainTextEdit()
        self.numerics.setReadOnly(True)
        for widget, name in ((self.log, "Live log"), (self.report, "Report"),
                             (self.lean, "Lean proof"), (self.numerics, "Numerics")):
            self.tabs.addTab(widget, name)

    def start(self):
        p = self.inp.toPlainText().strip()
        if not p:
            return
        self.btn.setEnabled(False)
        self.log.clear()
        self.status.setText("Running…")
        self.worker = RunWorker(p)
        self.worker.log.connect(self._append)
        self.worker.done.connect(self._finish)
        self.worker.failed.connect(self._fail)
        self.worker.start()

    def _append(self, text):
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _finish(self, rep, where):
        self.btn.setEnabled(True)
        self.status.setText(f"Saved to {where}")
        self.report.setMarkdown(rep.markdown)
        self.lean.setPlainText(rep.lean_code or rep.statement)
        self.numerics.setPlainText(rep.experiment_code + "\n\n" +
                                   json.dumps(rep.experiments, indent=2, ensure_ascii=False))
        self.tabs.setCurrentWidget(self.report)

    def _fail(self, tb):
        self.btn.setEnabled(True)
        self.status.setText("Error (see live log)")
        self._append(tb)

    def add_papers(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add papers", "",
                                                "Papers (*.pdf *.txt *.md *.tex)")
        if not files:
            return
        self.add.setEnabled(False)
        self.ing = IngestWorker(files)
        self.ing.log.connect(self._append)
        self.ing.finished.connect(lambda: self.add.setEnabled(True))
        self.ing.start()

def run_gui():
    app = QApplication(sys.argv)
    w = Window()
    w.show()
    sys.exit(app.exec())
```

**main.py**
```python
import argparse, logging

def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Math research agent")
    ap.add_argument("--cli", nargs="+", metavar="PROBLEM", help="solve a problem from the terminal")
    ap.add_argument("--ingest", nargs="+", metavar="FILE", help="add PDFs/text to your library")
    args = ap.parse_args()

    if args.ingest:
        from agent import MathAgent
        agent = MathAgent()
        for path, n in agent.ingest(args.ingest).items():
            print(f"{path}: {n} chunks")
    elif args.cli:
        from agent import MathAgent
        agent = MathAgent()
        rep = agent.run(" ".join(args.cli))
        out, pdf = agent.export(rep)
        print(rep.markdown)
        print(f"\nSaved to {out} (PDF: {'yes' if pdf else 'no'})")
    else:
        from gui import run_gui
        run_gui()

if __name__ == "__main__":
    main()
```

**smoke_test.py** (run this first; it shows which external APIs match what the code expects)
```python
"""Checks each component separately. Run: python smoke_test.py"""
import logging
from config import Config
import sources as S
from cache import Cache
from pytool import PyRunner
from lean_sandbox import LeanSandbox
from prover import ProofRunner
from llm import LLM
import premise

logging.basicConfig(level=logging.WARNING)
cfg = Config()
S.CACHE = Cache(cfg.db_path, cfg.pdf_dir)

def check(name, fn):
    try:
        print(f"[ok]   {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")

for name in ["arxiv", "semanticscholar", "openalex", "crossref", "zbmath",
             "core", "hal", "zenodo", "stackexchange", "web", "oeis"]:
    check(f"source {name}", lambda n=name: f"{len(S.SOURCES[n]('Ramsey theory'))} results")

check("loogle", lambda: f"{len(premise.loogle('Nat.succ_le'))} hits")
check("leansearch", lambda: f"{len(premise.leansearch('sum of two even numbers is even'))} hits")
check("moogle", lambda: f"{len(premise.moogle('prime numbers are infinite'))} hits")
check("python sandbox", lambda: PyRunner(cfg).run("print(2 + 2)")[1].strip())

def lean_test():
    pr = ProofRunner(cfg, LLM(cfg), LeanSandbox(cfg).check, lambda g: [], print)
    final, solved, total, res = pr.prove(
        "theorem main_thm (a b : ℕ) : a + b = b + a := by sorry")
    return f"holes {solved}/{total}, kernel-complete={res.complete}"

check("lean REPL + tactic search", lean_test)
```

## 3. Running it

```bash
pip install -r requirements.txt
docker build -t mathagent-lean -f docker/Dockerfile.lean .
docker build -t mathagent-py   -f docker/Dockerfile.py .
cp .env.example .env                 # set your LLM endpoint and model

python smoke_test.py                 # check each component first
python main.py --ingest ~/papers/*.pdf                # add your own library
python main.py --cli "Prove that for all positive reals a,b,c: a^2+b^2+c^2 >= ab+bc+ca"
python main.py                       # desktop GUI
```

Start with an easy inequality or a small number-theory identity. The first run takes longest because of the Mathlib import in each REPL worker and the Docker builds. After that, the search cache makes repeat research fast.

## 4. Verification notes

- **API shapes.** zbMATH, CORE, LeanSearch, and Moogle are the most likely to differ from what I wrote. zbMATH field names are read defensively. LeanSearch and Moogle try two request formats each. `smoke_test.py` prints the failures. Fix a parser in `sources.py` or `premise.py` by comparing with one real response. The rest of the pipeline is unaffected because `safe()` returns an empty list on failure.
- **Lean REPL.** Each REPL command is a JSON object followed by a blank line, and the response uses the same framing. Check the `repl` version you build against. If the `cmd`/`tactic`/`proofState` names differ, `lean_repl.py` and `prover.py` are the only files to change.
- **Hole positions.** Holes are located with the REPL's `pos`/`endPos` data (line and codepoint column). Assembly handles `sorry` as a tactic on its own line or after `by`. Proof validity never depends on this step, because the final `lake env lean` check with `#print axioms main_thm` is authoritative.
- **Numerical tests are evidence, not proof.** A `counterexample_found` verdict flags the claim as likely false, and the report says to verify it. Verify it yourself in exact arithmetic before trusting it. The same applies to statement fidelity, which is still checked by an LLM back-translation.
- **Resources.** Each REPL worker imports Mathlib, so budget about 8 GB RAM per worker. `LEAN_WORKERS=2` is a safe default on a 32 GB machine.
- **Sandboxing.** The Lean and Python containers run with no network, dropped capabilities, and memory limits. `LEAN_MODE=local` and `PY_MODE=local` are not sandboxed.
- **PDF output.** `report.tex` is compiled with `latexmk -xelatex`, which needs a TeX distribution and fonts that cover Lean's Unicode symbols (ℝ, ∀, ⊢). If `latexmk` is missing, the `.tex` file and BibTeX entries are still written.