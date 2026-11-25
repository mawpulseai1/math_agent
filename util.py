import re


def extract_block(txt: str, langs: tuple[str, ...] = ("lean", "lean4")) -> str:
    blocks = re.findall(r"```([\w+-]*)[ \t]*\n(.*?)```", txt, re.S)
    for lang, body in blocks:
        if lang.lower() in langs:
            return body.strip()
    return blocks[0][1].strip() if blocks else txt.strip()


def strip_imports(code: str) -> str:
    return "\n".join(line for line in code.splitlines() if not line.strip().startswith("import ")).strip()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def header_of(code: str) -> str:
    m = re.search(r"theorem\s+main_thm.*?:=", code, re.S)
    return norm(m.group(0)) if m else ""
