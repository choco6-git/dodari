import codecs
import os
import re

from dodari_engine import sent_tokenize

TXT_UNIT_FORMAT = 'txt-paragraphs-v1'
TXT_KEY = 'txt'
TXT_MAX_UNIT_CHARS = 2000
TXT_BATCH_CHARS = 8000
TXT_WRAPPED_LINE_MAX = 100
BLANK_SEP_RE = re.compile(r'\n[ \t　]*\n(?:[ \t　]*\n)*')
NEWLINE_NAMES = {'\n': 'LF', '\r\n': 'CRLF', '\r': 'CR'}


def decode(raw):
    if raw.startswith(codecs.BOM_UTF8):
        encoding, bom, body = 'utf-8', True, raw[len(codecs.BOM_UTF8):]
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding, bom, body = 'utf-16', True, raw
    else:
        encoding, bom, body = 'utf-8', False, raw
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as err:
        raise ValueError(f'not {encoding.upper()} text (invalid byte at offset {err.start}). '
                         'Save the file as UTF-8 and try again.')
    crlf = text.count('\r\n')
    lf = text.count('\n') - crlf
    cr = text.count('\r') - crlf
    newline = '\r\n' if crlf and crlf >= max(lf, cr) else ('\r' if cr > lf else '\n')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return text, {'encoding': encoding, 'bom': bom, 'newline': newline}


def read_txt(path):
    with open(path, 'rb') as fp:
        return decode(fp.read())


def write_output(path, text, info):
    if info.get('encoding') == 'utf-16':
        encoding = 'utf-16'
    else:
        encoding = 'utf-8-sig' if info.get('bom') else 'utf-8'
    with open(path, 'w', encoding=encoding, newline='') as fp:
        fp.write(text.replace('\n', info.get('newline') or '\n'))
        fp.flush()
        os.fsync(fp.fileno())


def write_source(path, text):
    with open(path, 'w', encoding='utf-8', newline='') as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())


def read_source(path):
    with open(path, 'r', encoding='utf-8', newline='') as fp:
        return fp.read()


def is_translatable(text):
    return any(ch.isalpha() for ch in text)


def pack(pieces, limit):
    out = []
    current = ''
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            out.append(current)
            current = ''
        current += piece
    if current:
        out.append(current)
    return out


def sentence_pieces(text):
    pieces = []
    pos = 0
    for sentence in sent_tokenize(text):
        idx = text.find(sentence, pos)
        if idx < 0:
            continue
        end = idx + len(sentence)
        pieces.append(text[pos:end])
        pos = end
    if pos < len(text):
        if pieces:
            pieces[-1] += text[pos:]
        else:
            pieces.append(text[pos:])
    return pieces


def split_long(text):
    if len(text) <= TXT_MAX_UNIT_CHARS:
        return [text]
    lines = re.findall(r'[^\n]*\n|[^\n]+$', text)
    if len(lines) > 1:
        pieces = []
        for line in lines:
            pieces.extend(split_long(line))
    else:
        pieces = sentence_pieces(text)
    return pack(pieces, TXT_MAX_UNIT_CHARS)


def unit_parts(text, mid_line=False):
    if not text:
        return []
    if not is_translatable(text):
        return [text]
    core = text.strip()
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return [{'lead': lead, 'src': core, 'trail': trail, 'mid_line': mid_line}]


def block_parts(block):
    if not block:
        return []
    lines = block.split('\n')
    if len(lines) > 1 and max(len(line) for line in lines) > TXT_WRAPPED_LINE_MAX:
        texts = [line + '\n' for line in lines[:-1]] + [lines[-1]]
    else:
        texts = [block]
    parts = []
    for text in texts:
        pieces = split_long(text)
        for i, piece in enumerate(pieces):
            mid_line = not piece.endswith('\n') and i < len(pieces) - 1
            parts.extend(unit_parts(piece, mid_line))
    return parts


def split_parts(text):
    parts = []
    pos = 0
    for m in BLANK_SEP_RE.finditer(text):
        parts.extend(block_parts(text[pos:m.start()]))
        parts.append(m.group())
        pos = m.end()
    parts.extend(block_parts(text[pos:]))
    return parts


def unit_texts(parts):
    return [p['src'] for p in parts if isinstance(p, dict)]


def render(parts, translations, source_first=False):
    mono = []
    bilingual = []
    t_idx = 0
    for part in parts:
        if not isinstance(part, dict):
            mono.append(part)
            bilingual.append(part)
            continue
        src = part['src']
        trans = translations[t_idx] if t_idx < len(translations) else None
        t_idx += 1
        trans = str(trans).replace('\r\n', '\n').replace('\r', '\n').strip() if trans is not None else ''
        if not trans or trans == src.strip():
            mono.append(part['lead'] + src + part['trail'])
            bilingual.append(part['lead'] + src + part['trail'])
            continue
        indent = part['lead'].rsplit('\n', 1)[-1]
        first, second = (src, trans) if source_first else (trans, src)
        trail_bi = '\n' if part.get('mid_line') else part['trail']
        mono.append(part['lead'] + trans + part['trail'])
        bilingual.append(part['lead'] + first + '\n' + indent + second + trail_bi)
    return ''.join(mono), ''.join(bilingual)


def guess_language(text):
    sample = text[:20000]
    counts = {'hangul': 0, 'kana': 0, 'han': 0, 'arabic': 0, 'persian': 0, 'letters': 0}
    for ch in sample:
        if not ch.isalpha():
            continue
        counts['letters'] += 1
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            counts['hangul'] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts['kana'] += 1
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            counts['han'] += 1
        elif 0x0600 <= code <= 0x06FF:
            counts['arabic'] += 1
            if ch in 'پچژگکی':
                counts['persian'] += 1
    letters = counts['letters'] or 1
    if counts['hangul'] / letters > 0.3:
        return 'ko'
    if counts['kana'] / letters > 0.1:
        return 'ja'
    if counts['han'] / letters > 0.3:
        return 'zh'
    if counts['arabic'] / letters > 0.3:
        return 'fa' if counts['persian'] / max(counts['arabic'], 1) > 0.02 else 'ar'
    return None


def stats(path):
    text, info = read_txt(path)
    parts = split_parts(text)
    texts = unit_texts(parts)
    blocks = [b for b in BLANK_SEP_RE.split(text) if b.strip()]
    return {
        'chars': len(text),
        'letters': sum(1 for ch in text if ch.isalpha()),
        'paragraphs': len(blocks),
        'paragraphs_with_text': sum(1 for b in blocks if is_translatable(b)),
        'texts': texts,
        'info': info,
        'source_lang': guess_language(text),
    }


def sample(path, chars=3000):
    text, _info = read_txt(path)
    return text.strip()[:chars]
