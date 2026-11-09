import copy
import html
import os
import re
import zipfile

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from dodari_engine import sent_tokenize


EPUB_SKIP_EPUB_TYPES = {'index', 'toc', 'cover', 'lot', 'loi'}


EPUB_BLOCK_TAGS = {
  'address', 'article', 'aside', 'blockquote', 'body', 'br', 'caption', 'col', 'colgroup', 'dd', 'details',
  'dialog', 'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4',
  'h5', 'h6', 'header', 'hgroup', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'summary', 'table',
  'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul',
}


EPUB_VISUAL_TAGS = {'img', 'svg', 'image', 'picture', 'video', 'audio', 'object', 'embed', 'iframe', 'canvas'}


EPUB_HIDDEN_TAGS = {'script', 'style', 'noscript', 'template'}


EPUB_CODE_TAGS = {'pre', 'code', 'kbd', 'samp', 'tt', 'var', 'math'}


EPUB_CODE_CLASS_RE = re.compile(
  r'programcode|programlisting|sourcecode|source-code|codeblock|code-block|codelisting|fixedline|'
  r'nonproportional|monospace|verbatim|mathml|equation|formula|mathjax|katex',
  re.IGNORECASE,
)


EPUB_CODE_CLASS_TOKEN_RE = re.compile(
  r'(^|[-_])(code|mono|math|tex|latex|listing|console|terminal|notranslate)([-_]|$)',
  re.IGNORECASE,
)


EPUB_TOKEN_RE = re.compile(r'⟦(/?)([CF])(\d+)⟧')


EPUB_UNIT_FORMAT = 'epub-units-v2'


EPUB_TOKEN_INSTRUCTION = (
  'Some sentences contain placeholder tokens such as ⟦C1⟧, ⟦F2⟧ and ⟦/F2⟧. '
  'They stand for program code, math or formatting that must not be translated. '
  'Copy every token exactly as written into the translation: keep each ⟦Fn⟧ ... ⟦/Fn⟧ pair around '
  'the translated words it wraps, and put each ⟦Cn⟧ where that item belongs in the translated sentence. '
  'Never translate, change, drop, duplicate or invent tokens. '
)


MATH_STRONG_CHARS = frozenset(
  '∈∉∋∌⊆⊂⊊⊄⊇⊃⊋∪∩∖∅≤≥≦≧≠≡≢≈≅≃∝∀∃∄⇒⇐⇔⟹⟸⟺→←↔↦×÷√∛∑∏∐∫∬∮∂∇∞±∓∘∙⋅¬∧∨⊕⊗⊖⊥∥∦∠△'
  'ℕℤℚℝℂℙ℘ℵ⌊⌋⌈⌉⟨⟩=<>{}^∣∤⊈⊉⊅≰≱≮≯≁≉ℓ̸'
)


MATH_PUNCT_CHARS = frozenset(".,;:!?'′″()[]+-−–*/\\_|~…·")


MATH_FUNC_WORDS = frozenset({
  'sin', 'cos', 'tan', 'cot', 'sec', 'csc', 'sinh', 'cosh', 'tanh', 'arcsin', 'arccos', 'arctan',
  'log', 'exp', 'lim', 'max', 'min', 'sup', 'inf', 'gcd', 'lcm', 'mod', 'det', 'deg', 'dim', 'ker', 'arg', 'sgn',
})


MATH_BREAK_WORDS = frozenset({
  'is', 'in', 'or', 'of', 'to', 'an', 'as', 'at', 'be', 'by', 'if', 'it', 'no', 'on', 'so', 'up', 'we',
  'am', 'do', 'go', 'he', 'me', 'my', 'us', 'vs',
})


def _dodari_math_piece_ok(piece):
    for ch in piece:
        if ch in MATH_STRONG_CHARS or ch in MATH_PUNCT_CHARS or ch.isdigit():
            continue
        if ('a' <= ch.lower() <= 'z') or ('Ͱ' <= ch <= 'Ͽ'):
            continue
        return False
    for word in re.findall(r'[A-Za-z]+', piece):
        low = word.lower()
        if len(word) >= 3 and low not in MATH_FUNC_WORDS:
            return False
        if low in MATH_BREAK_WORDS:
            return False
    return True


def _dodari_math_trim(text, start, end):
    while True:
        seg = text[start:end]
        if not seg:
            return start, end
        if seg[-1] in '.,;:!?' or (seg[-1] in ')]' and seg.count(seg[-1]) > seg.count('(' if seg[-1] == ')' else '[')):
            end -= 1
        elif seg[0] in '([' and seg.count(seg[0]) > seg.count(')' if seg[0] == '(' else ']'):
            start += 1
        elif re.match(r'(a|I)\s', seg):
            start += 2
        else:
            stripped = seg.strip()
            if stripped != seg:
                start += len(seg) - len(seg.lstrip())
                end -= len(seg) - len(seg.rstrip())
                continue
            return start, end


def _dodari_math_spans(text):
    if not text or not any(ch in MATH_STRONG_CHARS for ch in text):
        return []
    pieces = [(m.start(), m.end()) for m in re.finditer(r'\S+', text)]
    spans = []
    i = 0
    while i < len(pieces):
        if not _dodari_math_piece_ok(text[pieces[i][0]:pieces[i][1]]):
            i += 1
            continue
        j = i
        strong = False
        while j < len(pieces) and _dodari_math_piece_ok(text[pieces[j][0]:pieces[j][1]]):
            strong = strong or any(ch in MATH_STRONG_CHARS for ch in text[pieces[j][0]:pieces[j][1]])
            j += 1
        if strong:
            start, end = _dodari_math_trim(text, pieces[i][0], pieces[j - 1][1])
            if end > start and any(ch in MATH_STRONG_CHARS for ch in text[start:end]):
                spans.append((start, end))
        i = j
    return spans


def _dodari_math_like(text):
    stripped = (text or '').strip()
    if not stripped or not all(_dodari_math_piece_ok(p) for p in stripped.split()):
        return False
    letters = sum(1 for ch in stripped if ch.isalpha())
    return letters <= 2 or any(ch in MATH_STRONG_CHARS for ch in stripped)


EPUB_MONO_FONT_RE = re.compile(
  r'monospace|courier|consolas|menlo|monaco|lucida\s*console|lucida\s*sans\s*typewriter|inconsolata|'
  r'source\s*code|fira\s*(code|mono)|dejavu\s*sans\s*mono|liberation\s*mono|roboto\s*mono|ubuntu\s*mono|'
  r'andale\s*mono|sf\s*mono|jetbrains\s*mono|cascadia|droid\s*sans\s*mono|noto\s*(sans\s*)?mono|'
  r'\bmono\b|typewriter',
  re.IGNORECASE,
)


def _dodari_epub_css_code_classes(css_text):
    classes = set()
    css = re.sub(r'/\*.*?\*/', '', css_text or '', flags=re.DOTALL)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        body = m.group(2)
        family = re.search(r'font(?:-family)?\s*:\s*([^;]+)', body, re.IGNORECASE)
        if not family or not EPUB_MONO_FONT_RE.search(family.group(1)):
            continue
        for selector in m.group(1).split(','):
            parts = [p for p in re.split(r'[\s>+~]+', selector.strip()) if p]
            if parts:
                classes.update(re.findall(r'\.([A-Za-z0-9_-]+)', parts[-1]))
    return classes


def _dodari_epub_folder_code_classes(folder):
    classes = set()
    for root, _dirs, files in os.walk(folder):
        for fname in files:
            if fname.lower().endswith('.css'):
                try:
                    with open(os.path.join(root, fname), 'r', encoding='utf-8', errors='ignore') as fp:
                        classes |= _dodari_epub_css_code_classes(fp.read())
                except OSError:
                    continue
    return classes


def _dodari_epub_is_code(tag, code_classes=()):
    if tag.name in EPUB_CODE_TAGS:
        return True
    if str(tag.get('translate', '')).strip().lower() == 'no':
        return True
    for cls in tag.get('class') or []:
        if cls in code_classes:
            return True
        if EPUB_CODE_CLASS_RE.search(cls) or EPUB_CODE_CLASS_TOKEN_RE.search(cls):
            return True
    style = str(tag.get('style', '')).lower()
    if 'font-family' in style and ('monospace' in style or 'courier' in style):
        return True
    if tag.get('data-code-language'):
        return True
    return False


def _dodari_epub_described_ids(soup):
    ids = set()
    for tag in soup.find_all(attrs={'aria-describedby': True}):
        ids.update(str(tag.get('aria-describedby')).split())
    return ids


def _dodari_epub_is_plain_text(node):
    return type(node) is NavigableString


def _dodari_epub_own_text(tag, ctx):
    parts = []
    for text in tag.find_all(string=True):
        if not _dodari_epub_is_plain_text(text):
            continue
        skip = False
        for parent in text.parents:
            if parent is tag:
                break
            if (parent.name in EPUB_VISUAL_TAGS or parent.name in EPUB_HIDDEN_TAGS
                or _dodari_epub_is_code(parent, ctx['code']) or parent.get('id') in ctx['described']):
                skip = True
                break
        if not skip:
            parts.append(str(text))
    return ''.join(parts)


def _dodari_epub_inline_kind(tag, ctx, math_context=False):
    if (tag.name in EPUB_VISUAL_TAGS or _dodari_epub_is_code(tag, ctx['code'])
        or tag.get('id') in ctx['described']):
        return 'atom'
    if tag.name == 'a' and tag.get('role') == 'doc-backlink':
        return 'atom'
    if tag.name in EPUB_HIDDEN_TAGS:
        return 'hoist'
    own = _dodari_epub_own_text(tag, ctx)
    if not any(ch.isalpha() for ch in own):
        return 'hoist' if not tag.get_text().strip() and not tag.find(EPUB_VISUAL_TAGS) else 'atom'
    if math_context and _dodari_math_like(own):
        return 'atom'
    return 'format'


def _dodari_epub_breaks_flow(tag, ctx):
    if tag.name in EPUB_BLOCK_TAGS:
        return True
    if tag.name in EPUB_VISUAL_TAGS or _dodari_epub_is_code(tag, ctx['code']):
        return False
    return tag.find(EPUB_BLOCK_TAGS) is not None


def _dodari_epub_balance_sentences(sentences):
    balanced = []
    stack = []
    for sent in sentences:
        prefix = ''.join(f'⟦F{n}⟧' for n in stack)
        for m in EPUB_TOKEN_RE.finditer(sent):
            if m.group(2) != 'F':
                continue
            n = int(m.group(3))
            if not m.group(1):
                stack.append(n)
            elif n in stack:
                while stack and stack.pop() != n:
                    pass
        suffix = ''.join(f'⟦/F{n}⟧' for n in reversed(stack))
        balanced.append(prefix + sent + suffix)
    return balanced


def _dodari_epub_translatable(sentence):
    bare = EPUB_TOKEN_RE.sub('', sentence)
    return sum(1 for ch in bare if ch.isalpha()) >= 2


def _dodari_epub_make_unit(run, ctx, depth, tokenize, nested):
    atoms = []
    shells = []
    hoisted = []
    parts = []
    run_text = ''.join(n.get_text() if isinstance(n, Tag) else str(n) for n in run)
    math_context = any(ch in MATH_STRONG_CHARS for ch in run_text)

    def walk(node):
        if isinstance(node, NavigableString):
            if not _dodari_epub_is_plain_text(node):
                hoisted.append(node)
                return
            text = str(node)
            pos = 0
            for start, end in _dodari_math_spans(text):
                parts.append(text[pos:start])
                atoms.append(NavigableString(text[start:end]))
                parts.append(f'⟦C{len(atoms)}⟧')
                pos = end
            parts.append(text[pos:])
            return
        if not isinstance(node, Tag):
            return
        kind = _dodari_epub_inline_kind(node, ctx, math_context)
        if kind == 'hoist':
            hoisted.append(node)
            return
        if kind == 'atom':
            atoms.append(node)
            parts.append(f'⟦C{len(atoms)}⟧')
            for inner in [node] + node.find_all(id=True):
                if inner.get('id') in ctx['described']:
                    nested.append(inner)
            return
        chain = [node]
        inner = node
        while True:
            kids = [k for k in inner.children if not (isinstance(k, NavigableString) and not str(k).strip())]
            if (len(kids) == 1 and isinstance(kids[0], Tag)
                and not _dodari_epub_breaks_flow(kids[0], ctx)
                and _dodari_epub_inline_kind(kids[0], ctx, math_context) == 'format'):
                inner = kids[0]
                chain.append(inner)
            else:
                break
        shells.append(chain)
        n = len(shells)
        parts.append(f'⟦F{n}⟧')
        for child in list(inner.children):
            walk(child)
        parts.append(f'⟦/F{n}⟧')

    for node in run:
        walk(node)
    raw = ''.join(parts)
    text = re.sub(r'\s+', ' ', raw).strip()
    if not text or not re.search(r'[a-zA-Z]', EPUB_TOKEN_RE.sub('', text)):
        return None
    sentences = _dodari_epub_balance_sentences(tokenize(text))
    records = [{'src': s, 'translate': _dodari_epub_translatable(s)} for s in sentences]
    if not any(r['translate'] for r in records):
        return None
    return {
      'nodes': list(run),
      'atoms': atoms,
      'shells': shells,
      'hoisted': hoisted,
      'sentences': records,
      'lead': re.match(r'\s*', raw).group(),
      'trail': raw[len(raw.rstrip()):] if raw.strip() else '',
      'depth': depth,
    }


def _dodari_epub_collect_units(soup, tokenize=None, code_classes=()):
    if tokenize is None:
        tokenize = sent_tokenize
    body = soup.find('body') or soup
    inline_css = ' '.join(style.get_text() for style in soup.find_all('style'))
    ctx = {
      'described': _dodari_epub_described_ids(soup),
      'code': set(code_classes) | _dodari_epub_css_code_classes(inline_css),
    }
    units = []
    visited = set()

    def collect(container, depth):
        if id(container) in visited:
            return
        visited.add(id(container))
        run = []
        nested = []

        def flush():
            if run:
                solid = [n for n in run if not (isinstance(n, NavigableString) and not str(n).strip())]
                if (len(solid) == 1 and isinstance(solid[0], Tag)
                    and _dodari_epub_inline_kind(solid[0], ctx) == 'format'):
                    run.clear()
                    collect(solid[0], depth + 1)
                    return
                unit = _dodari_epub_make_unit(run, ctx, depth, tokenize, nested)
                if unit:
                    units.append(unit)
                run.clear()

        for child in list(container.children):
            if isinstance(child, Tag) and _dodari_epub_breaks_flow(child, ctx):
                flush()
                if (child.name not in EPUB_VISUAL_TAGS and child.name not in EPUB_HIDDEN_TAGS
                    and not _dodari_epub_is_code(child, ctx['code'])):
                    collect(child, depth + 1)
            else:
                run.append(child)
        flush()
        for inner in nested:
            collect(inner, depth + 1)

    collect(body, 0)
    return units


def _dodari_epub_strip_ids(tag):
    if isinstance(tag, Tag):
        tag.attrs.pop('id', None)
        for inner in tag.find_all(id=True):
            inner.attrs.pop('id', None)
    return tag


def _dodari_epub_render(soup, sentence, unit, used, source=None):
    atoms = unit['atoms']
    shells = unit['shells']
    tokens = list(EPUB_TOKEN_RE.finditer(sentence))
    formats_ok = True
    check = []
    for m in tokens:
        if m.group(2) != 'F':
            continue
        n = int(m.group(3))
        if n < 1 or n > len(shells):
            formats_ok = False
            break
        if not m.group(1):
            check.append(n)
        elif not check or check.pop() != n:
            formats_ok = False
            break
    if check:
        formats_ok = False

    out = []
    stack = []

    def add(node):
        if stack:
            stack[-1].append(node)
        else:
            out.append(node)

    def place_atom(n):
        atom = atoms[n - 1]
        if n in used['atoms']:
            add(_dodari_epub_strip_ids(copy.copy(atom)))
        else:
            used['atoms'].add(n)
            add(atom.extract() if atom.parent is not None else atom)

    placed = set()
    pos = 0
    for m in tokens:
        if m.start() > pos:
            add(NavigableString(sentence[pos:m.start()]))
        pos = m.end()
        n = int(m.group(3))
        if m.group(2) == 'C':
            if 1 <= n <= len(atoms):
                place_atom(n)
                placed.add(n)
            continue
        if not formats_ok:
            continue
        if m.group(1):
            stack.pop()
            continue
        outer = None
        for el in shells[n - 1]:
            attrs = dict(el.attrs)
            if n in used['shells']:
                attrs.pop('id', None)
            shell = soup.new_tag(el.name, attrs=attrs)
            if outer is None:
                add(shell)
            else:
                outer.append(shell)
            outer = shell
        used['shells'].add(n)
        stack.append(outer)
    if pos < len(sentence):
        add(NavigableString(sentence[pos:]))
    if source is not None:
        for m in EPUB_TOKEN_RE.finditer(source):
            n = int(m.group(3))
            if m.group(2) == 'C' and n not in placed and 1 <= n <= len(atoms):
                stack.clear()
                out.append(NavigableString(' '))
                place_atom(n)
                placed.add(n)
    return out


def _dodari_epub_unit_nodes(soup, unit, translations, bilingual, bilingual_order):
    used = {'atoms': set(), 'shells': set()}
    nodes = list(unit['hoisted'])
    if unit['lead']:
        nodes.append(NavigableString(unit['lead']))
    t_idx = 0
    first = True
    for record in unit['sentences']:
        src = record['src']
        trans = None
        if record['translate']:
            trans = translations[t_idx] if t_idx < len(translations) else None
            t_idx += 1
        if trans is not None and (not str(trans).strip() or str(trans).strip() == src.strip()):
            trans = None
        if not first:
            nodes.append(NavigableString(' '))
        first = False
        if trans is None:
            nodes.extend(_dodari_epub_render(soup, src, unit, used))
        elif not bilingual:
            nodes.extend(_dodari_epub_render(soup, trans, unit, used, source=src))
        elif bilingual_order == "원문(번역문)":
            nodes.extend(_dodari_epub_render(soup, src, unit, used))
            nodes.append(NavigableString(' ('))
            nodes.extend(_dodari_epub_render(soup, trans, unit, used, source=src))
            nodes.append(NavigableString(')'))
        else:
            nodes.extend(_dodari_epub_render(soup, trans, unit, used, source=src))
            nodes.append(NavigableString(' ('))
            nodes.extend(_dodari_epub_render(soup, src, unit, used))
            nodes.append(NavigableString(')'))
    if unit['trail']:
        nodes.append(NavigableString(unit['trail']))
    return nodes


def _dodari_epub_apply_units(soup, units, translations_per_unit, bilingual, bilingual_order):
    order = sorted(range(len(units)), key=lambda i: -units[i]['depth'])
    for i in order:
        unit = units[i]
        first = unit['nodes'][0]
        parent = first.parent
        if parent is None:
            continue
        index = parent.index(first)
        for node in unit['nodes']:
            node.extract()
        new_nodes = _dodari_epub_unit_nodes(soup, unit, translations_per_unit[i], bilingual, bilingual_order)
        for offset, node in enumerate(new_nodes):
            parent.insert(index + offset, node)


def _dodari_epub_group_translations(particles):
    groups = []
    current = []
    for item in particles:
        if item == 0:
            groups.append(current)
            current = []
        else:
            current.append(item)
    return groups


def _dodari_epub_text_weight(html_text):
    import html as _html
    if not html_text:
        return 0
    text = str(html_text)
    body = re.search(r'<body\b[^>]*>', text, re.IGNORECASE)
    if body:
        types = re.search(r'epub:type\s*=\s*["\']([^"\']*)["\']', body.group(0), re.IGNORECASE)
        if types and EPUB_SKIP_EPUB_TYPES.intersection(types.group(1).split()):
            return 0
    text = re.sub(r'<(head|script|style)\b.*?</\1\s*>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
    text = _html.unescape(re.sub(r'<[^>]+>', ' ', text))
    return sum(1 for ch in text if ch.isalpha())


EPUB_NCX_LABEL_RE = re.compile(
  r'(<(?:[A-Za-z_][\w.-]*:)?(?:navLabel|docTitle)\b[^>]*>\s*<(?:[A-Za-z_][\w.-]*:)?text\b[^>]*>)'
  r'(.*?)(</(?:[A-Za-z_][\w.-]*:)?text\s*>)',
  re.DOTALL,
)


EPUB_NCX_PAGELIST_RE = re.compile(
  r'<(?:[A-Za-z_][\w.-]*:)?pageList\b.*?</(?:[A-Za-z_][\w.-]*:)?pageList\s*>', re.DOTALL,
)


def _dodari_epub_ncx_labels(xml_text):
    import html as _html
    skip = [(m.start(), m.end()) for m in EPUB_NCX_PAGELIST_RE.finditer(xml_text)]
    labels = []
    for m in EPUB_NCX_LABEL_RE.finditer(xml_text):
        if any(s <= m.start() < e for s, e in skip):
            continue
        labels.append(((m.start(2), m.end(2)), _html.unescape(m.group(2))))
    return labels


def _dodari_epub_ncx_soup(texts):
    import html as _html
    body = ''.join(f'<p>{_html.escape(t, quote=False)}</p>' for t in texts)
    return BeautifulSoup(f'<html><body>{body}</body></html>', 'html.parser')


def _dodari_epub_ncx_write(xml_text, labels, soup):
    import html as _html
    paras = soup.find('body').find_all('p', recursive=False)
    if len(paras) != len(labels):
        raise ValueError(f'NCX label count mismatch ({len(paras)} != {len(labels)})')
    out = []
    pos = 0
    for ((start, end), _src), para in zip(labels, paras):
        out.append(xml_text[pos:start])
        out.append(_html.escape(para.get_text(), quote=False))
        pos = end
    out.append(xml_text[pos:])
    return ''.join(out)


HTML_EXTS = ('xhtml', 'html', 'htm')
IMAGE_ONLY_MAX_LETTERS = 500


def extract(epub_path, folder):
    os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(epub_path, 'r') as zf:
        zf.extractall(folder)


def repack(folder, out_path):
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        mimetype = os.path.join(folder, 'mimetype')
        if os.path.exists(mimetype):
            zf.write(mimetype, 'mimetype', compress_type=zipfile.ZIP_STORED)
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, folder).replace(os.sep, '/')
                if rel == 'mimetype':
                    continue
                zf.write(path, rel)


def html_files(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(HTML_EXTS):
                out.append(os.path.relpath(os.path.join(root, name), folder))
    return out


def ncx_files(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith('.ncx'):
                out.append(os.path.relpath(os.path.join(root, name), folder))
    return out


def ncx_units(xml_text):
    labels = _dodari_epub_ncx_labels(xml_text)
    soup = _dodari_epub_ncx_soup([t for _span, t in labels])
    units, only_texts, whole_particle = chapter_units(soup, ())
    return labels, soup, units, only_texts, whole_particle


def read_text(path):
    with open(path, 'r', encoding='utf-8') as fp:
        return fp.read()


def write_text(path, text):
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())


def find_opf(folder):
    container = os.path.join(folder, 'META-INF', 'container.xml')
    try:
        with open(container, 'r', encoding='utf-8', errors='ignore') as fp:
            m = re.search(r'full-path\s*=\s*["\']([^"\']+\.opf)["\']', fp.read(), re.IGNORECASE)
        if m:
            path = os.path.join(folder, *m.group(1).split('/'))
            if os.path.exists(path):
                return path
    except OSError:
        pass
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith('.opf'):
                return os.path.join(root, name)
    return None


LANG_ELEMENT_RE = re.compile(r'(<(?:[A-Za-z_][\w.-]*:)?language\b[^>]*>)(.*?)(</(?:[A-Za-z_][\w.-]*:)?language\s*>)', re.DOTALL)
TITLE_ELEMENT_RE = re.compile(r'<(?:[A-Za-z_][\w.-]*:)?title\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?title\s*>', re.DOTALL)


def opf_language(opf_path):
    try:
        with open(opf_path, 'r', encoding='utf-8', errors='ignore') as fp:
            text = fp.read()
    except OSError:
        return None
    m = LANG_ELEMENT_RE.search(text)
    if not m:
        return None
    value = html.unescape(m.group(2)).strip().lower()
    return re.split(r'[-_]', value)[0] or None


def opf_title(opf_path):
    try:
        with open(opf_path, 'r', encoding='utf-8', errors='ignore') as fp:
            m = TITLE_ELEMENT_RE.search(fp.read())
    except OSError:
        return None
    return html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else None


EPUB_OPF_LANGUAGE_RE = re.compile(
  r'(<(?:[A-Za-z_][\w.-]*:)?language\b[^>]*?)(?:/>|>(.*?)(</(?:[A-Za-z_][\w.-]*:)?language\s*>))',
  re.DOTALL,
)


def _dodari_epub_set_opf_language(opf_path, lang):
    with open(opf_path, 'r', encoding='utf-8', errors='surrogateescape') as fp:
        text = fp.read()

    def repl(m):
        close = m.group(3) or '</' + m.group(1)[1:].split()[0] + '>'
        return f'{m.group(1)}>{lang}{close}'

    new_text, count = EPUB_OPF_LANGUAGE_RE.subn(repl, text)
    if count:
        with open(opf_path, 'w', encoding='utf-8', errors='surrogateescape') as fp:
            fp.write(new_text)
    return count


def read_soup(path):
    with open(path, 'r', encoding='utf-8') as fp:
        return BeautifulSoup(fp.read(), 'html.parser')


def is_skip_page(soup):
    body = soup.find('body')
    return bool(body and EPUB_SKIP_EPUB_TYPES.intersection((body.get('epub:type') or '').split()))


def chapter_units(soup, code_classes):
    units = _dodari_epub_collect_units(soup, code_classes=code_classes)
    only_texts = []
    whole_particle = []
    for unit in units:
        particle = [r['src'] for r in unit['sentences'] if r['translate']]
        only_texts.extend(particle)
        whole_particle.extend(particle)
        whole_particle.append(0)
    return units, only_texts, whole_particle


def group_translations(whole_particle, translated):
    particles = []
    t_idx = 0
    for item in whole_particle:
        if item:
            particles.append(translated[t_idx] if t_idx < len(translated) and translated[t_idx] is not None else item)
            t_idx += 1
        else:
            particles.append(0)
    return _dodari_epub_group_translations(particles)


def write_soup(path, soup):
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(str(soup))
        fp.flush()
        os.fsync(fp.fileno())


def stats(epub_path, with_units=False):
    out = {'chapters': 0, 'chapters_to_translate': 0, 'letters': 0, 'chars': 0, 'images': 0, 'sentences': None,
           'chapter_sentences': []}
    with zipfile.ZipFile(epub_path, 'r') as zf:
        names = zf.namelist()
        css = ''
        for name in names:
            if name.lower().endswith('.css'):
                css += zf.read(name).decode('utf-8', 'ignore') + '\n'
        code_classes = _dodari_epub_css_code_classes(css)
        sentences = 0
        for name in sorted(names):
            low = name.lower()
            if low.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')):
                out['images'] += 1
            if with_units and low.endswith('.ncx'):
                try:
                    n = len(ncx_units(zf.read(name).decode('utf-8', 'ignore'))[3])
                except Exception:
                    n = 0
                if n:
                    sentences += n
                    out['chapter_sentences'].append(n)
                continue
            if not low.endswith(HTML_EXTS):
                continue
            out['chapters'] += 1
            text = zf.read(name).decode('utf-8', 'ignore')
            out['chars'] += len(re.sub(r'<[^>]+>', '', text))
            weight = _dodari_epub_text_weight(text)
            out['letters'] += weight
            if weight:
                out['chapters_to_translate'] += 1
                if with_units:
                    soup = BeautifulSoup(text, 'html.parser')
                    if not is_skip_page(soup):
                        n = len(chapter_units(soup, code_classes)[1])
                        sentences += n
                        out['chapter_sentences'].append(n)
        if with_units:
            out['sentences'] = sentences
    out['image_only'] = out['letters'] < IMAGE_ONLY_MAX_LETTERS and out['images'] > 0
    return out


def spine_order(zf):
    names = zf.namelist()
    docs = sorted(n for n in names if n.lower().endswith(HTML_EXTS))
    try:
        container = zf.read('META-INF/container.xml').decode('utf-8', 'ignore')
        opf_name = re.search(r'full-path\s*=\s*["\']([^"\']+)["\']', container).group(1)
        opf = zf.read(opf_name).decode('utf-8', 'ignore')
    except Exception:
        return docs
    base = opf_name.rsplit('/', 1)[0] + '/' if '/' in opf_name else ''
    hrefs = {}
    for m in re.finditer(r'<(?:\w+:)?item\b[^>]*>', opf):
        tag = m.group(0)
        item_id = re.search(r'\bid\s*=\s*["\']([^"\']+)', tag)
        href = re.search(r'\bhref\s*=\s*["\']([^"\']+)', tag)
        if item_id and href:
            hrefs[item_id.group(1)] = os.path.normpath(base + html.unescape(href.group(1)).split('#')[0]).replace(os.sep, '/')
    order = []
    for m in re.finditer(r'<(?:\w+:)?itemref\b[^>]*\bidref\s*=\s*["\']([^"\']+)', opf):
        path = hrefs.get(m.group(1))
        if path in names and path not in order:
            order.append(path)
    return order + [n for n in docs if n not in order]


def sample(epub_path, chars=3000):
    parts = []
    total = 0
    with zipfile.ZipFile(epub_path, 'r') as zf:
        for name in spine_order(zf):
            if not name.lower().endswith(HTML_EXTS):
                continue
            text = zf.read(name).decode('utf-8', 'ignore')
            if not _dodari_epub_text_weight(text):
                continue
            soup = BeautifulSoup(text, 'html.parser')
            if is_skip_page(soup):
                continue
            body = soup.find('body') or soup
            for tag in body.find_all(['script', 'style']):
                tag.decompose()
            plain = re.sub(r'\s+', ' ', body.get_text(' ')).strip()
            if not plain:
                continue
            parts.append(plain)
            total += len(plain)
            if total >= chars:
                break
    return ' '.join(parts)[:chars]


def zip_metadata(epub_path):
    with zipfile.ZipFile(epub_path, 'r') as zf:
        try:
            container = zf.read('META-INF/container.xml').decode('utf-8', 'ignore')
            opf_name = re.search(r'full-path\s*=\s*["\']([^"\']+)["\']', container).group(1)
            opf = zf.read(opf_name).decode('utf-8', 'ignore')
        except Exception:
            return None, None
    t = TITLE_ELEMENT_RE.search(opf)
    m = LANG_ELEMENT_RE.search(opf)
    title = html.unescape(re.sub(r'<[^>]+>', '', t.group(1))).strip() if t else None
    lang = html.unescape(m.group(2)).strip() if m else None
    return title, lang
