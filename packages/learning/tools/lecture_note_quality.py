#!/usr/bin/env python3
"""Mechanical layout and title heuristics for legacy lecture-note reviews.

This does not judge pedagogy. It also flags title and text-content patterns, so
it is not the acceptance gate for the 2026-09-24 pi-authored, Codex-layout-only
workflow described in the study-system skill.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def run(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout


def balanced_argument(text: str, start: int) -> tuple[str, int] | None:
    """Return a braced TeX argument beginning at start, including nested braces."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return None


def displayed_titles(tex: str) -> list[str]:
    """Collect section-level and titled-box headings in source order."""
    starts: list[tuple[int, int]] = []
    section_pattern = re.compile(
        r"\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\s*\{"
    )
    box_pattern = re.compile(r"\\begin\{[A-Za-z]*box\}\s*\{")
    for pattern in (section_pattern, box_pattern):
        for match in pattern.finditer(tex):
            starts.append((match.start(), match.end() - 1))
    titles: list[str] = []
    for _, brace_start in sorted(starts):
        parsed = balanced_argument(tex, brace_start)
        if parsed:
            titles.append(parsed[0].strip())
    return titles


def heading_tree(tex: str) -> list[dict[str, object]]:
    """Return section headings with nested subsection headings in source order."""
    pattern = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{")
    tree: list[dict[str, object]] = []
    current_section: dict[str, object] | None = None
    current_subsection: dict[str, object] | None = None
    for match in pattern.finditer(tex):
        parsed = balanced_argument(tex, match.end() - 1)
        if not parsed:
            continue
        title, _ = parsed
        level = match.group(1)
        if level == "section":
            current_section = {"title": title.strip(), "subsections": []}
            tree.append(current_section)
            current_subsection = None
        elif level == "subsection" and current_section is not None:
            current_subsection = {"title": title.strip(), "subsubsections": []}
            current_section["subsections"].append(current_subsection)
        elif level == "subsubsection" and current_subsection is not None:
            current_subsection["subsubsections"].append(title.strip())
    return tree


def plain_title(title: str) -> str:
    """Produce a conservative plain-text approximation for title linting."""
    value = re.sub(r"\$([^$]*)\$", r"\1", title)
    value = re.sub(r"\\(?:textbf|textit|emph|mathrm|operatorname)\s*\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\[A-Za-z]+\*?", " ", value)
    value = value.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", value).strip()


def non_noun_title_reason(title: str) -> str | None:
    """Flag only obvious question, instruction, or full-clause title forms."""
    value = plain_title(title)
    if re.search(r"[?？!]", value):
        return "question or exclamation punctuation"
    if re.match(r"^(?:为什么|为何|如何|怎么|是否|能否|请|不要|不能|必须|先|再|把|从)", value):
        return "question, instruction, or verbal transition"
    if re.search(r"(?:为什么|如何|怎么|是否|能否|不等于|不是一回事|无需每次|必须分|就是功|是一种|是一次)", value):
        return "question or full-sentence predicate"
    if re.search(r"\b(?:why|how|should|must)\b", value, re.IGNORECASE):
        return "English question or instruction"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tex", required=True, type=Path)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--min-pages", type=int)
    parser.add_argument("--min-sections", type=int)
    parser.add_argument("--min-text-chars", type=int)
    parser.add_argument("--min-page-chars", type=int, default=180)
    parser.add_argument("--length-review", help="Specific source-based justification for resolved length warnings")
    parser.add_argument("--out", type=Path, help="Save the generated mechanical audit as JSON")
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []
    if not args.tex.is_file():
        failures.append(f"missing TeX source: {args.tex}")
    if not args.pdf.is_file():
        failures.append(f"missing PDF: {args.pdf}")
    if failures:
        print(json.dumps({"passed": False, "failures": failures}, ensure_ascii=False, indent=2))
        return 1

    tex = args.tex.read_text(encoding="utf-8")
    info = run("pdfinfo", str(args.pdf))
    match = re.search(r"^Pages:\s+(\d+)$", info, re.MULTILINE)
    pages = int(match.group(1)) if match else 0
    sections = len(re.findall(r"\\section\s*\{", tex))
    extracted = run("pdftotext", "-layout", str(args.pdf), "-")
    page_text = [re.sub(r"\s+", "", page) for page in extracted.split("\f") if page.strip()]
    text_chars = sum(len(page) for page in page_text)
    sparse_pages = [i + 1 for i, page in enumerate(page_text) if len(page) < args.min_page_chars]
    placeholders = sorted(set(re.findall(r"\b(?:TODO|TBD|FIXME)\b|待补|占位", tex, re.IGNORECASE)))
    titles = displayed_titles(tex)
    outline = heading_tree(tex)
    title_failures = [
        {"title": title, "reason": reason}
        for title in titles
        if (reason := non_noun_title_reason(title))
    ]

    if args.min_pages is not None and pages < args.min_pages:
        warnings.append(f"page count {pages} < warning threshold {args.min_pages}")
    if args.min_sections is not None and sections < args.min_sections:
        warnings.append(f"section count {sections} < warning threshold {args.min_sections}")
    if args.min_text_chars is not None and text_chars < args.min_text_chars:
        warnings.append(f"extracted text characters {text_chars} < warning threshold {args.min_text_chars}")
    if sparse_pages:
        failures.append(f"suspiciously sparse pages: {sparse_pages}")
    if placeholders:
        failures.append(f"placeholder text remains: {placeholders}")
    if title_failures:
        failures.append("obvious non-noun title forms remain")
    if warnings and not args.length_review:
        failures.append("unresolved length warnings: recheck source coverage and depth")

    report = {
        "passed": not failures,
        "pdf": str(args.pdf),
        "pages": pages,
        "sections": sections,
        "text_chars": text_chars,
        "page_text_chars": [len(page) for page in page_text],
        "explicit_page_breaks": len(re.findall(r"\\(?:newpage|clearpage)\b", tex)),
        "titles": titles,
        "heading_tree": outline,
        "title_failures": title_failures,
        "warnings": warnings,
        "length_review": args.length_review,
        "scope": "mechanical checks only; semantic and visual review required separately",
        "thresholds": {
            "min_pages": args.min_pages,
            "min_sections": args.min_sections,
            "min_text_chars": args.min_text_chars,
            "min_page_chars": args.min_page_chars,
        },
        "failures": failures,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
