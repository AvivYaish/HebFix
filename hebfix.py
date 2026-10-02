"""Write visual RTL Hebrew from files/stdin to stdout even when RTL isn't supported (e.g., Windows Terminal).
Default: --rtl --width 80.
Use --width auto for terminal width, --width N for N columns, --width 0 for unlimited lines, and --reflow to resize formatted text.
For [glow](https://github.com/charmbracelet/glow) to work, use: `glow -w 0`.
Install before using: `python -m pip install "python-bidi>=0.6.11,<0.7" "wcwidth>=0.9.1,<1" "markdown-it-py>=4.2,<5"`
"""
import argparse
import re
import shutil
import sys
from itertools import count, repeat
from os import get_terminal_size
from pathlib import Path
from unicodedata import category

from bidi import algorithm as bidi
from markdown_it import MarkdownIt
from wcwidth import iter_graphemes, wcswidth
from wcwidth.textwrap import SequenceTextWrapper

MD = MarkdownIt("commonmark").enable("strikethrough")
MIRROR = str.maketrans(bidi.MIRRORED)
HEBREW = re.compile(r"[\u0590-\u05ff]")
HEADER = re.compile(r"\A\ufeff?\+{3}\n.*?\n\+{3}(?:\n|\Z)", re.S)
URL = re.compile(r"https?://[^\s<>]+")
ATOMS = re.compile(r"(?P<ticks>`+)[^\n]*?(?P=ticks)|https?://[^\s<>]+|\\[^\w\s]|(?P<style>\*\*|__|~~|\*|_)(?=\S)(?P<inner>.+?)(?P=style)")
LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%:/?&=_+()\-]*(?: [A-Za-z0-9][A-Za-z0-9.,%:/?&=_+()\-]*)+")


def visible_width(text):
    return wcswidth("".join(token.content for token in MD.parseInline(text)[0].children or [] if token.type in ("text", "code_inline")))


def display(text, base_dir=None):
    """Apply bidi to whole graphemes, preserving niqqud and joined emoji."""
    clusters = list(iter_graphemes(text))
    bases = "".join(next((c for c in cluster if category(c)[0] != "M" and category(c) != "Cf"), cluster[0]) for cluster in clusters)
    storage = bidi.get_empty_storage()
    storage["base_level"] = bidi.get_base_level(text) if base_dir is None else int(base_dir == "R")
    bidi.get_embedding_levels(bases, storage)
    for char, cluster in zip(storage["chars"], clusters):
        char["ch"] = cluster
    for step in (bidi.explicit_embed_and_overrides, bidi.resolve_weak_types, bidi.resolve_neutral_types, bidi.resolve_implicit_levels, bidi.reorder_resolved_levels):
        step(storage, debug=False)
    return "".join(char["ch"].translate(MIRROR) if char["level"] % 2 else char["ch"] for char in storage["chars"])


def terminal_columns():
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            return get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            pass
    return shutil.get_terminal_size().columns


def width_arg(value):
    if value == "auto" or value.isdecimal():
        return value if value == "auto" else int(value)
    raise argparse.ArgumentTypeError("width must be auto or a nonnegative integer")


def options(parser):
    parser.add_argument("--width", type=width_arg, default=80, help="columns (default: 80), auto, or 0 for no wrapping")
    directions = parser.add_mutually_exclusive_group()
    directions.add_argument("--rtl", dest="rtl", action="store_true", default=True)
    directions.add_argument("--ltr", dest="rtl", action="store_false")


def document(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    header = HEADER.match(text)
    return (header[0], text[header.end():]) if header else ("", text)


def fix(text, width=80, rtl=True, measure=wcswidth, reflow=False):
    """Format Markdown or resize visual text when reflow=True."""
    prefix, body = document(text)
    if not reflow and not HEBREW.search(body):
        return prefix + body
    width = terminal_columns() if width in (None, "auto") else width
    space = "\u00a0"
    direction = "R" if rtl else "L"
    reverse = lambda row: "".join(reversed(list(iter_graphemes(row)))) if reflow and rtl else row
    output, refs = [], {}
    def mask(source):
        protected = {}
        def replace(match):
            value = match[0]
            style = match.groupdict().get("style")
            if style and not reflow:
                inner, nested = mask(match["inner"])
                value = style + display(inner, direction).translate(nested) + style
            token = 0xF0000 + len(protected)
            while chr(token) in source or token in protected:
                token += 1
            protected[token] = value
            return chr(token)
        masked = ATOMS.sub(replace, source)
        return (LATIN.sub(replace, masked) if reflow else masked), protected
    def paragraph(source, heading="", gutter=""):
        masked, protected = mask(source)
        wrapper = SequenceTextWrapper(width=max(1, width - measure(gutter)), break_on_hyphens=False)
        wrapper._width = lambda row: measure(reverse(row).translate(protected))
        rows = [row for line in map(reverse, masked.split("\n")) for row in (wrapper.wrap(line) if width and not heading else [line])]
        prefix = heading + " " if heading else ""
        for row in rows or [""]:
            row = (reverse(row) if reflow else display(row, direction)).translate(protected)
            row = row + gutter if rtl else gutter + row
            gutter = space * wcswidth(gutter)
            output.append(prefix + space * max(0, width - measure(row) - len(prefix)) + row if rtl and width else prefix + row)
    def ref(url):
        return f"[{refs.setdefault(url, len(refs) + 1)}]"
    def bare_url(match):
        url = match[0]
        while url and (url[-1] in ".,;:!?" or any(url.endswith(closing) and url.count(closing) > url.count(opening) for opening, closing in (("(", ")"), ("[", "]")))):
            url = url[:-1]
        return ref(url) + match[0][len(url):]
    def inline(tokens):
        result, link = [], None
        for token in tokens:
            if token.type == "link_open":
                link = token.attrGet("href")
            elif token.type == "link_close":
                result.append(" " + ref(link))
                link = None
            elif token.type == "image":
                result.append(token.content + " " + ref(token.attrGet("src")))
            elif token.type == "code_inline":
                padding = " " if token.content.startswith("`") or token.content.endswith("`") else ""
                result.append(token.markup + padding + token.content + padding + token.markup)
            elif token.nesting or token.type in ("softbreak", "hardbreak"):
                result.append(token.markup if token.nesting else "\n")
            elif token.content != link:
                result.append(re.sub(r"([\\`*_<>#])", r"\\\1", URL.sub(bare_url, token.content)))
        return "".join(result)
    if not reflow:
        lists, quotes, heading = [], 0, ""
        for token in MD.parse(body):
            kind = token.type
            quotes += {"blockquote_open": 1, "blockquote_close": -1}.get(kind, 0)
            if kind in ("bullet_list_open", "ordered_list_open"):
                markers = repeat("•") if kind == "bullet_list_open" else map(("{}" + token.markup).format, count(token.attrs.get("start", 1)))
                lists.append([markers, ""])
            elif kind.endswith("list_close"):
                lists.pop()
            elif kind == "list_item_open":
                lists[-1][1] = next(lists[-1][0])
            elif kind in ("heading_open", "heading_close"):
                heading = "#" * int(token.tag[1:]) if token.nesting == 1 else ""
            elif kind in ("inline", "fence", "code_block", "hr", "html_block"):
                if output:
                    output.append("")
                if kind == "inline":
                    marker = (">" * quotes + " " + (lists[-1][1] if lists else "")).strip()
                    indent = space * max(0, len(lists) - 1) * 2
                    gutter = (" " + display(marker, "R").replace("<", ">") if marker else "") + indent if rtl else indent + (marker + " " if marker else "")
                    paragraph(inline(token.children), heading, gutter)
                    if lists:
                        lists[-1][1] = ""
                elif kind == "fence":
                    output.extend([token.markup + token.info, *token.content.splitlines(), token.markup])
                elif kind == "code_block":
                    output.extend("    " + line for line in token.content.splitlines())
                else:
                    output.extend((token.markup if kind == "hr" else token.content).splitlines())
        output.extend("\n" + ref(url) + " " + url for url in refs)
    else:
        untouched = {line for token in MD.parse(body)
                     if token.type in ("fence", "code_block", "html_block")
                     or any(child.type == "link_open" for child in token.children or [])
                     for line in range(*token.map)}
        for number, line in enumerate(body.splitlines()):
            if (number in untouched or re.match(r"^(?:\[\d+\] https?://|\[[^]]+\]:)", line) or not (HEBREW.search(line) or line.startswith("\u00a0"))):
                output.append(line)
                continue
            heading = re.match(r"^(#{1,6}) ", line)
            line = line[heading.end() if heading else 0:].lstrip(" \u00a0")
            gutter = re.search(r"(?: (?:•|[.(]\d+|>+)|\u00a0)+$", line) if rtl else None
            gutter = gutter[0] if gutter else ""
            paragraph(line[:-len(gutter)] if gutter else line, heading[1] if heading else "", gutter)
    return prefix + "\n".join(output) + ("\n" if body.endswith("\n") else "")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="-", type=lambda name: Path(name).expanduser(), help="UTF-8 file, or - for stdin")
    options(parser)
    parser.add_argument("--reflow", action="store_true", help="Resize already formatted text without reversing Hebrew again")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="")
    if args.path != Path("-") and not args.path.is_file():
        parser.error("path must be a file")
    text = sys.stdin.read() if args.path == Path("-") else args.path.read_text(encoding="utf-8")
    sys.stdout.write(fix(text, args.width, args.rtl, reflow=args.reflow))
    return 0


if __name__ == "__main__":
    sys.exit(main())
