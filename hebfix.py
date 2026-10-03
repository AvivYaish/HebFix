"""
Write visual RTL Hebrew from files/stdin to stdout even when RTL isn't supported (e.g., Windows Terminal).
Install prerequisites using: `python -m pip install pyicu-wheels==2.15.2 wcwidth==0.9.1 markdown-it-py==4.2.0 mdit-py-plugins==0.6.1`.
"""
import argparse
import re
import shutil
import sys
from itertools import count, takewhile
from unicodedata import bidirectional

from icu import Bidi, UnicodeString
from markdown_it import MarkdownIt
from markdown_it.rules_block import reference
from mdit_py_plugins.footnote import footnote_plugin
from wcwidth import iter_graphemes, wcswidth
from wcwidth.textwrap import SequenceTextWrapper

MD = MarkdownIt('commonmark', {'inline_definitions': True}).enable('strikethrough')
MD.use(footnote_plugin, inline=False, move_to_end=False, always_match_refs=True)
MD.block.ruler.at('reference', reference, {'alt': ['paragraph', 'reference']})
URL = re.compile(r'https?://[^\s<>]+')


def numeric_note(state, silent):
    if not (match := re.match(r'\[\s*([0-9]+)\s*\](?![\[(])', state.src[state.pos:state.posMax])):
        return False
    if not silent:
        state.push('note_ref', '', 0).content = f'[{match[1]}]'
    state.pos += len(match[0])
    return True


MD.inline.ruler.before('link', 'numeric_note', numeric_note)


def atoms(source):
    """Keep citations, code, and joined emoji indivisible (for use during wrapping and bidi)."""
    saved, codes = {}, (code for code in count(0xf0000) if chr(code) not in source)
    def keep(value):
        saved[code := next(codes)] = value
        return chr(code)
    return keep, saved


def display(text, right=False, protect_urls=False):
    """Use ICU for mirroring and combining marks while protecting compound emoji."""
    keep, saved = atoms(text)
    if protect_urls:
        text = URL.sub(lambda match: keep(match[0]), text)
    text = ''.join(keep(cluster) if '\u200d' in cluster or len(cluster) > 1 and not cluster[0].isalpha() else cluster for cluster in iter_graphemes(text))
    if not text:
        return ''
    bidi = Bidi()
    bidi.setPara(UnicodeString(text), int(right))
    return str(bidi.writeReordered(Bidi.DO_MIRRORING | Bidi.KEEP_BASE_COMBINING | Bidi.REMOVE_BIDI_CONTROLS)).translate(saved)


def wrap(text, width, saved):
    """Wrap atoms with wcwidth 0.9.1's private width hook."""
    wrapper = SequenceTextWrapper(width=max(1, width), break_on_hyphens=False)
    wrapper._width = lambda row: wcswidth(row.translate(saved))
    return [row for line in text.split('\n') for row in (wrapper.wrap(line) if width else [line]) if wcswidth(row.translate(saved)) > 0]


def inline(tokens, ref, source, width=0, link=None):
    """Wrap emphasized spans and replace links with notes."""
    keep, saved = atoms(source)
    output, autolink, existing, tokens = [], False, False, iter(tokens)
    def url_note(match):
        url = match[0].rstrip('.,;:!?')
        while any(url.endswith(b) and url.count(b) > url.count(a) for a, b in (('(', ')'), ('[', ']'))):
            url = url[:-1]
        return keep(url if existing else ref(url)) + match[0][len(url):]
    for token in tokens:
        kind, value = token.type, token.content
        if kind == 'link_open':
            link, autolink = token.attrGet('href'), token.info == 'auto'
        elif kind == 'link_close':
            output.append(('' if autolink else ' ') + keep(ref(link)))
            link, autolink = None, False
        elif kind == 'image':
            output.append(value + ' ' + keep(ref(token.attrGet('src'))))
        elif kind in ('note_ref', 'footnote_ref'):
            existing = not output or output[-1] == '\n'
            output.append(keep(value if kind == 'note_ref' else f"[^{display(token.meta['label'])}]"))
        elif kind == 'code_inline':
            output.append(keep('`' + display(value, protect_urls=True) + '`'))
        elif kind in ('softbreak', 'hardbreak'):
            output.append('\n')
            existing = False
        elif token.nesting > 0:
            available = max(1, width - 2 * len(token.markup)) if width else 0
            inner, protected = inline(takewhile(lambda child: child.level > token.level, tokens), ref, source, available, link)
            for index, piece in enumerate(wrap(inner, available, protected)):
                if index:
                    output.append('\n')
                right = next((bidirectional(char) in ('R', 'AL') for char in piece.translate(protected) if char.isalpha()), False)
                value = display(piece, right).translate(protected)
                output.append(('\u2067' if right else '\u2066') + keep(token.markup + value + token.markup) + '\u2069')
        elif kind == 'text' and not autolink:
            output.append(value if link else URL.sub(url_note, value))
    return ''.join(output), saved


def fix(text, width=80):
    """Produce readable terminal text with automatic per-row direction."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    width = shutil.get_terminal_size().columns if width == 'auto' else width
    refs, output, end, source_lines = {}, [], 0, text.splitlines()
    previous, prefix, quotes, depth, notes = False, '', 0, 0, 0
    used = {int(number) for number in re.findall(r'\[\s*\^?(\d+)\s*\]', text)}
    numbers = (number for number in count(1) if number not in used)
    def ref(url):
        refs[url] = refs.get(url) or next(numbers)
        return f'[{refs[url]}]'
    def emit(value):
        nonlocal end
        start, stop = token.map
        output.append(''.join(line + '\n' for line in source_lines[end:start] if not line.strip('> \t')) + value)
        end = max(end, stop if token.nesting == 0 else start + 1)

    for token in MD.parse(text):
        kind = token.type
        quotes += token.nesting if token.tag == 'blockquote' else 0
        depth += token.nesting if token.tag in ('ul', 'ol') else 0
        notes += token.nesting if kind.startswith('footnote_reference_') else 0
        container = '> ' * quotes + '  ' * depth
        if kind == 'heading_open':
            prefix = '#' * int(token.tag[1:]) + ' '
        elif kind == 'list_item_open':
            prefix = (token.info + token.markup).strip() + ' '
        elif kind == 'footnote_reference_open':
            emit(container + f"[^{display(token.meta['label'])}]:")
        elif kind == 'definition':
            meta = token.meta
            emit(container + f"[{display(meta['label'])}]: {meta['url']}" + (' "' + display(meta['title'], protect_urls=True) + '"' if meta['title'] else ''))
        elif kind in ('fence', 'code_block', 'html_block'):
            block = source_lines[slice(*token.map)] if kind == 'fence' else [container + line for line in token.content.splitlines()]
            emit('\n'.join(display(line, protect_urls=True) for line in block))
            previous, prefix = False, ''
        elif kind == 'hr':
            emit(container + token.markup)
        elif kind == 'inline' and notes and re.fullmatch(r'\S+(?:[/\\]\S+|\.[A-Za-z0-9]+)(?:[?#]\S*)?', token.content) and all(child.type == 'text' for child in token.children):
            emit(container + token.content)
        elif kind == 'inline':
            lead, marker = re.match(r'([ \t]*(?:>[ \t]*)*)((?:#{1,6}|[-+*]|\d+[.)])[ \t]+)?', source_lines[token.map[0]]).groups()
            quote = lead if lead.count('>') == quotes else container[:-2] if depth and prefix else container
            if marker and prefix and marker.strip() == prefix.strip():
                prefix = marker
            available, lines = max(1, width - wcswidth(quote + prefix)) if width else 0, []
            content, saved = inline(token.children, ref, text, available)
            for row in wrap(content, available, saved):
                visible = re.sub(r'\[\^[^]]+\]', '', row.translate(saved))
                previous = next((bidirectional(char) in ('R', 'AL') for char in visible if char.isalpha()), previous)
                row = display(row, previous).translate(saved)
                padding = '\u00a0' * max(0, width - wcswidth(quote + prefix + row)) if width and previous else ''
                lines.append(quote + prefix + padding + row)
                prefix = ' ' * len(prefix)
            emit('\n'.join(lines))
            prefix = ''
    output.extend(line for line in source_lines[end:] if not line.strip('> \t'))
    if refs:
        output.append(('\n' if output and output[-1].strip() else '') + '\n'.join(f'[{number}] {url}' for url, number in refs.items()))
    return '\n'.join(output) + ('\n' if text.endswith('\n') else '')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--width', default='80', help='columns, auto, or 0 for unlimited lines (invalid values use 80)')
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding='utf-8')
    sys.stdout.write(fix(sys.stdin.read(), args.width if args.width == 'auto' else int(args.width) if args.width.isdecimal() else 80))
