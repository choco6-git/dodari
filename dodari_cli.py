#!/usr/bin/env python3
import argparse
import contextlib
import html
import json
import os
import re
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

LANGS = ['ko', 'en', 'ja', 'zh', 'fr', 'it', 'nl', 'da', 'sv', 'no', 'ar', 'fa']
GENRES = ['it', 'literature', 'humanities', 'business', 'subtitles', 'general']
TONES = ['narrative', 'polite']
ORDERS = ['translation-first', 'original-first']
FORMATS = ['same', 'txt']
ENGINES = ['claude', 'codex', 'local']
SUPPORTED_EXTENSIONS = ('.txt', '.epub', '.pdf')

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOT_STARTED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


class _NullProgress:
    def __call__(self, *args, **kwargs):
        return None

    def tqdm(self, iterable=None, *args, **kwargs):
        return iterable


class UsageError(Exception):
    pass


def _range_arg(text):
    match = re.fullmatch(r'(\d*)-(\d*)|(\d+)', text.strip())
    if not match or text.strip() in ('', '-'):
        raise argparse.ArgumentTypeError(f"invalid range '{text}' (use 10-30, 10-, -30 or 5)")
    if match.group(3):
        start = end = int(match.group(3))
    else:
        start = int(match.group(1)) if match.group(1) else None
        end = int(match.group(2)) if match.group(2) else None
    if (start is not None and start < 1) or (end is not None and end < 1) or (
            start is not None and end is not None and start > end):
        raise argparse.ArgumentTypeError(f"invalid range '{text}' (numbers start at 1 and start must not exceed end)")
    return start, end


def build_parser():
    parser = argparse.ArgumentParser(
        prog='dodari_cli.py',
        description='Dodari command line: translate EPUB/PDF/TXT files or inspect them. '
                    'Progress logs go to stderr; stdout carries one JSON result.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    def add_common(p):
        p.add_argument('files', nargs='+', metavar='FILE', help='.txt, .epub or .pdf files')
        p.add_argument('--engine', choices=ENGINES, help='default: the engine currently selected in the web UI')
        p.add_argument('--model', help='model for the claude/codex engine')
        p.add_argument('--effort', help='reasoning effort for the claude/codex engine (e.g. low, medium, high, max)')

    tr = sub.add_parser('translate', help='translate files')
    add_common(tr)
    tr.add_argument('--target', choices=LANGS, default='ko', help='target language (default: ko)')
    tr.add_argument('--source-lang', choices=LANGS, help='override the detected source language')
    tr.add_argument('--genre', choices=GENRES, help='default: inferred from the file')
    tr.add_argument('--tone', choices=TONES, default='narrative')
    tr.add_argument('--no-natural-ko', dest='natural_ko', action='store_false',
                    help='turn off the translationese removal that is on by default for Korean output')
    tr.add_argument('--bilingual-order', choices=ORDERS, default='translation-first')
    tr.add_argument('--format', choices=FORMATS, default='txt', help="'txt' turns EPUB/PDF results into plain text")
    tr.add_argument('--range', dest='page_range', type=_range_arg, metavar='A-B',
                    help='PDF: pages, EPUB: chapter numbers (e.g. 10-30, 10-, -30). TXT is always translated in full')
    tr.add_argument('--output-dir', help="default: the 'outputs' folder next to dodari.py")

    ins = sub.add_parser('inspect', help='show language, genre guess and size without translating')
    add_common(ins)
    return parser


def _error_json(message):
    return {'ok': False, 'error': message}


def _check_files(paths):
    resolved = []
    for path in paths:
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            raise UsageError(f'file not found: {path}')
        if os.path.splitext(full)[1].lower() not in SUPPORTED_EXTENSIONS:
            raise UsageError(f'unsupported file type: {path} (use .txt, .epub or .pdf)')
        resolved.append(full)
    return resolved


def _create_app(args, dodari):
    if args.engine:
        engine_id = {'claude': dodari.ENGINE_CLAUDE_CLI, 'codex': dodari.ENGINE_CODEX_CLI, 'local': dodari.ENGINE_LOCAL}[args.engine]
        dodari.load_engine_config = lambda: engine_id
    app = dodari.Dodari()
    if dodari._dodari_cli_is_engine(app.gemma_model):
        engine = app.gemma_model
        if args.model or args.effort:
            current_model, current_effort = app._cli_selection(engine)
            efforts = dodari.DODARI_CONFIG[dodari.DODARI_ENGINE_SECTIONS[engine]].get('efforts') or []
            if args.effort and efforts and args.effort not in efforts:
                raise UsageError(f"unsupported effort '{args.effort}' for {engine} (choose from: {', '.join(efforts)})")
            app._set_cli_selection(engine, args.model or current_model, args.effort or current_effort, save=False)
    elif args.model or args.effort:
        raise UsageError('--model and --effort only apply to the claude and codex engines')
    return app


def _total_info(app, path):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pdf':
        import fitz
        with fitz.open(path) as doc:
            return len(doc), 'pages'
    if ext == '.epub':
        import dodari
        return len(dodari._dodari_epub_spine_docs(path)), 'chapters'
    handle = app.open_text_with_detection(path)
    if handle is None:
        return None, 'lines'
    with handle:
        return handle.read().count('\n') + 1, 'lines'


def _detect(app, path):
    app.selected_files = [{'path': path, 'orig_name': os.path.basename(path)}]
    app.origin_lang = None
    app.genre_inference_failed = False
    final = None
    for final in app.on_file_upload(app.selected_files):
        pass
    genre = final[3] if final and isinstance(final[3], str) else None
    return genre


def _genre_slug(dodari, genre_ko):
    if genre_ko in dodari.GENRE_CHOICES_KO:
        return GENRES[dodari.GENRE_CHOICES_KO.index(genre_ko)]
    return None


def _inspect_file(app, dodari, path):
    entry = {'input': path, 'type': os.path.splitext(path)[1].lower().lstrip('.')}
    genre = _detect(app, path)
    total, unit = _total_info(app, path)
    entry.update({
        'source_language': app.origin_lang,
        'genre_guess': _genre_slug(dodari, genre),
        'total': total,
        'unit': unit,
        'range_supported': entry['type'] in ('pdf', 'epub'),
    })
    return entry


def _translate_file(app, dodari, args, path):
    entry = {'input': path}
    genre_ko = dodari.GENRE_CHOICES_KO[GENRES.index(args.genre)] if args.genre else None
    detected_genre = _detect(app, path)
    if args.source_lang:
        app.origin_lang = args.source_lang
    if genre_ko is None:
        genre_ko = detected_genre if detected_genre in dodari.GENRE_CHOICES_KO else dodari.GENRE_CHOICES_KO[-1]
    target_name = next(name for name, (iso, _prompt) in dodari.SUPPORTED_LANGUAGES.items() if iso == args.target)
    start, end = args.page_range or (None, None)

    app.last_run = None
    outputs, message = app.execute_translation_pipeline(
        genre_ko,
        dodari.TONE_CHOICES_KO[TONES.index(args.tone)],
        target_name,
        dodari.BILINGUAL_CHOICES_KO[ORDERS.index(args.bilingual_order)],
        dodari.OUTPUT_FORMAT_CHOICES_KO[FORMATS.index(args.format)],
        start,
        end,
        args.natural_ko,
        _NullProgress(),
    )
    run = app.last_run
    if outputs is None or run is None:
        text = re.sub(r'<[^>]+>', ' ', html.unescape(message or ''))
        entry.update({'status': 'not_started', 'ok': False, 'outputs': [], 'error': ' '.join(text.split())})
        return entry
    file_result = run['files'][0] if run['files'] else {}
    ok = bool(file_result.get('ok')) and not run['failures']
    entry.update({
        'status': 'done' if ok else 'failed',
        'ok': ok,
        'elapsed': file_result.get('elapsed'),
        'outputs': [os.path.abspath(p) for p in run['outputs']],
    })
    if run['failures']:
        entry['error'] = '; '.join(f['reason'] for f in run['failures'])
    return entry


def run(args):
    try:
        files = _check_files(args.files)
        output_dir = os.path.abspath(args.output_dir) if getattr(args, 'output_dir', None) else None
    except UsageError as err:
        return EXIT_USAGE, _error_json(str(err))

    os.chdir(HERE)
    sys.path.insert(0, HERE)
    import dodari

    try:
        app = _create_app(args, dodari)
    except UsageError as err:
        return EXIT_USAGE, _error_json(str(err))
    if output_dir:
        app.output_folder = output_dir
    else:
        app.output_folder = os.path.join(HERE, 'outputs')

    engine_display = dodari._dodari_engine_display(app.gemma_model, *app._cli_selection(app.gemma_model))
    result = {'ok': False, 'command': args.command, 'engine': engine_display, 'files': []}
    handler = _inspect_file if args.command == 'inspect' else _translate_file
    try:
        for path in files:
            try:
                if args.command == 'inspect':
                    entry = handler(app, dodari, path)
                    entry['ok'] = True
                else:
                    entry = handler(app, dodari, args, path)
            except Exception as err:
                traceback.print_exc()
                entry = {'input': path, 'status': 'error', 'ok': False, 'error': f'{type(err).__name__}: {err}'}
            result['files'].append(entry)
    except KeyboardInterrupt:
        result['interrupted'] = True
        result['ok'] = False
        return EXIT_INTERRUPTED, result

    result['ok'] = all(f.get('ok') for f in result['files'])
    if result['ok']:
        return EXIT_OK, result
    if any(f.get('status') == 'not_started' for f in result['files']):
        return EXIT_NOT_STARTED, result
    return EXIT_FAILED, result


def main(argv=None):
    args = build_parser().parse_args(argv)
    real_stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        code, result = run(args)
    real_stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    real_stdout.flush()
    return code


if __name__ == '__main__':
    sys.exit(main())
