#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import dodari_engine as eng
import dodari_txt as txt

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PREFLIGHT = 2
EXIT_RATE_LIMIT = 3
EXIT_REJECTED = 4

DODARI_HOME = eng.DODARI_HOME
JOBS_DIR = os.path.join(DODARI_HOME, 'jobs')
VENV_DIR = os.path.join(DODARI_HOME, 'venv')
NLTK_DIR = os.path.join(DODARI_HOME, 'nltk_data')
REQUIREMENTS = os.path.join(SCRIPT_DIR, 'requirements.txt')
if NLTK_DIR not in os.environ.get('NLTK_DATA', '').split(os.pathsep):
    os.environ['NLTK_DATA'] = os.pathsep.join(p for p in (NLTK_DIR, os.environ.get('NLTK_DATA', '')) if p)
MIN_PYTHON = (3, 10)
HEARTBEAT_SEC = 20
HEARTBEAT_STALE_SEC = 120
NCX_KEY_PREFIX = 'ncx:'
IS_WINDOWS = os.name == 'nt'
STATUS_LOCK = threading.Lock()
SUPPORTED_FORMATS = {'.epub': 'epub', '.txt': 'txt'}
UNSUPPORTED_MESSAGE = 'This version supports EPUB and TXT only.'
DIRECT_WARN_BATCHES = 40
PIP_NETWORK_ARGS = ['--timeout', '20', '--retries', '1']

LUNA_PER_MILLION = {'five_hour_pct': 6.7, 'weekly_pct_points': 1.0}
LUNA_BATCH_ROUND_SEC = 144


def say(*lines):
    for line in lines:
        print(line, flush=True)


def script_cmd(*args):
    return ' '.join([f'python3 "{os.path.abspath(__file__)}"'] + [str(a) for a in args])


def venv_python():
    return os.path.join(VENV_DIR, 'Scripts', 'python.exe') if IS_WINDOWS else os.path.join(VENV_DIR, 'bin', 'python')


def deps_status():
    required = []
    optional = []
    try:
        import bs4
    except Exception:
        required.append('beautifulsoup4')
    try:
        import nltk
        if NLTK_DIR not in nltk.data.path:
            nltk.data.path.insert(0, NLTK_DIR)
        try:
            nltk.data.find('tokenizers/punkt_tab')
        except LookupError:
            optional.append('nltk punkt_tab data')
    except Exception:
        optional.append('nltk')
    return required, optional


def reexec_in_venv_if_needed():
    required, optional = deps_status()
    vpy = venv_python()
    if (not required and not optional) or not os.path.exists(vpy) \
            or os.path.abspath(sys.executable) == os.path.abspath(vpy):
        return
    if os.environ.get('DODARI_REEXEC') == '1':
        return
    env = dict(os.environ, DODARI_REEXEC='1')
    args = [vpy, os.path.abspath(__file__)] + sys.argv[1:]
    if IS_WINDOWS:
        sys.exit(subprocess.call(args, env=env))
    os.execve(vpy, args, env)


def requirement_line(name):
    try:
        with open(REQUIREMENTS, 'r', encoding='utf-8') as fp:
            for line in fp:
                if line.strip().lower().startswith(name):
                    return line.strip()
    except OSError:
        pass
    return name


def pip_install(python, args):
    say(f'[install] {python} -m pip install {" ".join(args)}')
    subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-q'] + PIP_NETWORK_ARGS + args,
                   check=True)


def install_deps():
    errors = []
    vpy = venv_python()
    created = False
    targets = []
    if os.path.exists(vpy):
        targets.append((vpy, []))
    else:
        say(f'[install] creating virtual environment: {VENV_DIR}')
        try:
            subprocess.run([sys.executable, '-m', 'venv', VENV_DIR], check=True)
            created = True
            targets.append((vpy, []))
        except (subprocess.CalledProcessError, OSError) as err:
            errors.append(f'venv: {err}')
            shutil.rmtree(VENV_DIR, ignore_errors=True)
    targets.append((sys.executable, ['--user']))
    for python, extra in targets:
        for args in (['-r', REQUIREMENTS], [requirement_line('beautifulsoup4')]):
            try:
                pip_install(python, extra + args)
            except (subprocess.CalledProcessError, OSError) as err:
                errors.append(f'pip install {" ".join(extra + args)}: {err}')
                continue
            if args[0] == '-r':
                say(f'[install] nltk punkt_tab → {NLTK_DIR}')
                proc = subprocess.run([python, '-c', 'import sys, nltk; '
                                       f'sys.exit(0 if nltk.download("punkt_tab", download_dir={NLTK_DIR!r}, quiet=True) else 1)'],
                                      env=dict(os.environ, NLTK_DATA=NLTK_DIR))
                if proc.returncode != 0:
                    errors.append('nltk punkt_tab data could not be downloaded (needs raw.githubusercontent.com) '
                                  '— the built-in regex sentence splitter is used')
            else:
                errors.append('nltk was not installed — the built-in regex sentence splitter is used')
            return python, errors
        if python == vpy and created:
            shutil.rmtree(VENV_DIR, ignore_errors=True)
    return None, errors


def need_epub_modules():
    required, _optional = deps_status()
    if required:
        say(f'ERROR: missing dependencies: {", ".join(required)} (needed for EPUB)',
            f'Fix: {script_cmd("doctor", "--install")}   (installs into {VENV_DIR}, or pip --user)')
        sys.exit(EXIT_PREFLIGHT)
    import dodari_epub
    return dodari_epub


def write_json_atomic(path, data):
    tmp = f'{path}.tmp{os.getpid()}_{threading.get_ident()}'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            return json.load(fp)
    except Exception:
        return default


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def fmt_duration(sec):
    if sec is None:
        return 'unknown'
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{h}h {m:02d}m' if h else (f'{m}m {s:02d}s' if m else f'{s}s')


def pid_alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f'{stem}_{i:03d}{ext}'):
        i += 1
    return f'{stem}_{i:03d}{ext}'


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fp:
        for block in iter(lambda: fp.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def slug(text, limit=40):
    out = ''.join(ch if (ch.isascii() and ch.isalnum()) else '-' for ch in text)
    out = '-'.join(p for p in out.split('-') if p)
    return (out[:limit] or 'book').lower()


class Job:

    def __init__(self, job_id):
        self.id = job_id
        self.dir = os.path.join(JOBS_DIR, job_id)
        self.job_path = os.path.join(self.dir, 'job.json')
        self.status_path = os.path.join(self.dir, 'status.json')
        self.progress_path = os.path.join(self.dir, 'progress.json')
        self.log_path = os.path.join(self.dir, 'log.txt')
        self.failures_path = os.path.join(self.dir, 'cli_failures.jsonl')
        self.chunks_dir = os.path.join(self.dir, 'chunks')
        self.work_bi = os.path.join(self.dir, 'work_bilingual')
        self.work_mono = os.path.join(self.dir, 'work_single')
        self.txt_source = os.path.join(self.dir, 'source.txt')
        self.plan_path = os.path.join(self.dir, 'direct_plan.json')
        self.direct_dir = os.path.join(self.dir, 'direct')

    def exists(self):
        return os.path.exists(self.job_path)

    def settings(self):
        return read_json(self.job_path, {})

    def status(self):
        return read_json(self.status_path, {})

    def update_status(self, **fields):
        with STATUS_LOCK:
            data = self.status()
            data.update(fields)
            data['updated_at'] = now_iso()
            write_json_atomic(self.status_path, data)
            return data

    def progress(self):
        data = read_json(self.progress_path, None) or {}
        data.setdefault('done', [])
        data.setdefault('counts', {})
        return data

    def save_progress(self, data):
        write_json_atomic(self.progress_path, data)

    def chunk_path(self, key, index):
        safe = ''.join(ch if ch.isalnum() else '_' for ch in key)
        return os.path.join(self.chunks_dir, f'{safe}_{index}.json')

    def direct_batch_path(self, no):
        return os.path.join(self.direct_dir, f'batch_{int(no):05d}.json')

    def alive(self):
        st = self.status()
        if not pid_alive(st.get('pid')):
            return False
        beat = st.get('heartbeat_epoch') or 0
        return time.time() - beat < HEARTBEAT_STALE_SEC


def resolve_engine_settings(args, cfg):
    engine = getattr(args, 'engine', None) or eng.default_engine(cfg)
    if engine == eng.ENGINE_DIRECT:
        return engine, None, None, None, None
    model, effort, timeout = eng.engine_settings(engine, cfg, getattr(args, 'model', None),
                                                 getattr(args, 'effort', None), getattr(args, 'timeout', None))
    note = None
    if engine == eng.ENGINE_CODEX_CLI and not getattr(args, 'model', None):
        listed = eng.codex_models(eng.codex_home(cfg))
        if listed and model not in listed:
            note = f'codex model {model} is not in this account model list; using {listed[0]}'
            model = listed[0]
    return engine, model, effort, timeout, note


def batch_rounds(chapter_sentences):
    chunk = eng.CLI_BATCH_SIZE * eng.CLI_WORKERS
    rounds = 0
    for n in chapter_sentences:
        for c in range(0, n, chunk):
            rounds += -(-(-(-min(chunk, n - c) // eng.CLI_BATCH_SIZE)) // eng.CLI_WORKERS)
    return rounds


def estimate_lines(chars, sentences, engine, model, effort, chapter_sentences=(), batches=None, rounds=None,
                   unit='sentences', batch_desc=None):
    lines = []
    if batches is None:
        batches = sum(-(-n // eng.CLI_BATCH_SIZE) for n in chapter_sentences if n) if chapter_sentences else \
            (-(-sentences // eng.CLI_BATCH_SIZE) if sentences else 0)
    direct = engine == eng.ENGINE_DIRECT
    if sentences is not None:
        if direct:
            desc = 'direct mode: one next/submit round per batch'
        else:
            desc = batch_desc or f'batch {eng.CLI_BATCH_SIZE}, {eng.CLI_WORKERS} in parallel'
        lines.append(f'{unit}_to_translate: {sentences}  batches: {batches} ({desc})')
    if direct:
        lines.append(f'estimated_rounds: {batches} next/submit rounds in this conversation '
                     '(time and usage depend on your plan and model)')
        if batches > DIRECT_WARN_BATCHES:
            lines.append(f'NOTE direct: {batches} batches is more than {DIRECT_WARN_BATCHES} — this takes many turns. '
                         'Claude Code with the claude CLI translates whole books in the background.')
    elif engine == eng.ENGINE_CODEX_CLI and model == 'gpt-6-luna' and effort == 'max':
        m = chars / 1_000_000
        rounds = batch_rounds(chapter_sentences) if rounds is None else rounds
        lines.append(f'estimated_time: {fmt_duration(rounds * LUNA_BATCH_ROUND_SEC)}  ({rounds} batch rounds)')
        lines.append(f'estimated_usage: 5-hour window ~{m * LUNA_PER_MILLION["five_hour_pct"]:.1f}%, '
                     f'weekly ~{m * LUNA_PER_MILLION["weekly_pct_points"]:.1f}%p')
        lines.append('estimate_basis: one book measurement (ChatGPT Plus, gpt-6-luna/max, 3 parallel batches: '
                     f'~{LUNA_BATCH_ROUND_SEC}s per batch round, usage per million chars); varies with plan, model and text')
    else:
        lines.append(f'estimated_time/usage: not measured for {engine} {model or "(default)"}/{effort or "(default)"} '
                     '— only letters and batches are shown')
    return lines


def txt_estimate_lines(st, engine, model, effort):
    texts = st['texts']
    batches = len(eng.plan_batches(texts, eng.CLI_BATCH_SIZE, txt.TXT_BATCH_CHARS))
    rounds = len(eng.chunk_ranges(texts, txt.TXT_BATCH_CHARS))
    desc = (f'batch up to {eng.CLI_BATCH_SIZE} paragraphs or {txt.TXT_BATCH_CHARS} chars, '
            f'{eng.CLI_WORKERS} in parallel')
    return estimate_lines(st['chars'], len(texts), engine, model, effort, batches=batches, rounds=rounds,
                          unit='paragraphs', batch_desc=desc)


def sandbox_guidance(engine, folders):
    add_dirs = ' '.join(f'--add-dir "{f}"' for f in folders)
    return [
        'FIX (Codex session sandbox): the background job inherits the session sandbox, so it needs network access and',
        f'  write access to: {", ".join(folders)}',
        '  Option 1: run the dodari start command with escalated (outside-sandbox) permissions and approve it once.',
        '  Option 2: start Codex with:',
        f'    codex --sandbox workspace-write -c sandbox_workspace_write.network_access=true {add_dirs}',
        '    or put in ~/.codex/config.toml: sandbox_mode = "workspace-write" and',
        '    [sandbox_workspace_write] network_access = true, writable_roots = [' + ', '.join(f'"{f}"' for f in folders) + ']',
        '  Option 3: run the printed start command yourself in a normal terminal.',
        'FIX (Claude Code sandbox, if enabled): allow network to the CLI host and write access to the same folders.',
    ]


def cli_status(engine, model, cfg):
    home = eng.codex_home(cfg, create=False)
    found, raw = eng.cli_version(engine, home)
    info = {'binary': eng.CLI_BINARIES[engine], 'found': found, 'raw': raw,
            'need': eng.required_version(engine, model), 'logged': None, 'login_msg': ''}
    if found is not None:
        if engine == eng.ENGINE_CODEX_CLI:
            eng.codex_home(cfg, create=True)
        info['logged'], info['login_msg'] = eng.check_login(engine, home)
    return info


def cli_lines(engine, cli):
    binary = cli['binary']
    if cli['found'] is None:
        return False, [f'FAIL cli: {binary} not found or unreadable version ({cli["raw"][:120]}). '
                       f'Install: {eng.INSTALL_HINTS[engine]}']
    lines = []
    ok = True
    have = '.'.join(map(str, cli['found']))
    need = '.'.join(map(str, cli['need']))
    if not eng._dodari_cli_version_at_least(cli['found'], cli['need']):
        ok = False
        update = eng.CODEX_UPDATE_CMD if engine == eng.ENGINE_CODEX_CLI else eng.CLAUDE_UPDATE_CMD
        lines.append(f'FAIL cli: {binary} {have} < {need} required. Update: {update}  (or doctor --update)')
    else:
        lines.append(f'OK   cli: {binary} {have} (>= {need})')
    if cli['logged'] is False:
        ok = False
        lines.append(f'FAIL login: {cli["login_msg"]}')
    else:
        lines.append(f'{"OK  " if cli["logged"] else "WARN"} login: {cli["login_msg"]}')
    return ok, lines


def direct_reason_lines(engine, cli):
    binary = cli['binary']
    if cli['found'] is None:
        return [f'INFO cli: {binary} CLI not found → direct mode (you translate the batches in this conversation)',
                f'     For background translation instead: install it ({eng.INSTALL_HINTS[engine]}), log in, '
                'then run doctor again.']
    return [f'INFO login: {binary} CLI is not logged in → direct mode (you translate the batches in this conversation)',
            *[f'     {line}' for line in cli['login_msg'].splitlines()],
            '     For background translation instead: log in, then run doctor again.']


def write_checks(output_dir):
    failed = []
    lines = []
    for folder in [JOBS_DIR] + ([output_dir] if output_dir else []):
        w_ok, w_msg = eng.probe_write(folder)
        lines.append(f'{"OK  " if w_ok else "FAIL"} write: {w_msg}')
        if not w_ok:
            failed.append(folder)
    return failed, lines


def preflight(engine, model, cfg, output_dir=None, cli=None):
    if engine == eng.ENGINE_DIRECT:
        failed, lines = write_checks(output_dir)
        if failed:
            lines.append(f'FIX: allow write access to {", ".join(failed)}; for the output files you can pass '
                         '--output-dir <a writable folder>, for the job folder set DODARI_HOME to a writable folder')
        return not failed, lines
    ok, lines = cli_lines(engine, cli or cli_status(engine, model, cfg))
    net_ok, net_msg = eng.probe_network(eng.NETWORK_HOSTS[engine])
    lines.append(f'{"OK  " if net_ok else "FAIL"} network: {net_msg}')
    failed, w_lines = write_checks(output_dir)
    lines.extend(w_lines)
    if not net_ok or failed:
        ok = False
        lines.extend(sandbox_guidance(engine, [DODARI_HOME] + ([output_dir] if output_dir else [])))
    return ok, lines


def dependency_lines(fmt):
    required, optional = deps_status()
    if not required and not optional:
        return True, ['OK   dependencies: beautifulsoup4, nltk, punkt_tab']
    ok = True
    lines = []
    if required and fmt != 'txt':
        ok = False
        lines.append(f'FAIL dependencies: missing {", ".join(required)} (needed for EPUB)')
    elif required:
        lines.append(f'WARN dependencies: missing {", ".join(required)} (needed only for EPUB, not for TXT)')
    if optional:
        lines.append(f'WARN dependencies: missing {", ".join(optional)} '
                     '— sentence splitting falls back to the built-in regex splitter')
    lines.append(f'     Fix: {script_cmd("doctor", "--install")}   (installs into {VENV_DIR}, '
                 'or pip --user if a venv cannot be created)')
    return ok, lines


def cmd_doctor(args):
    cfg = eng.load_config()
    engine = args.engine or eng.default_engine(cfg)
    fmt = check_input(args.file)[1] if args.file else None
    say(f'engine: {engine}   (platform: {"claude-code" if os.environ.get("CLAUDECODE") == "1" else "other/codex"})',
        f'script: {os.path.abspath(__file__)}',
        f'dodari_home: {DODARI_HOME}')
    ok = True
    py_ok = sys.version_info[:2] >= MIN_PYTHON
    say(f'{"OK  " if py_ok else "FAIL"} python: {platform.python_version()} ({sys.executable}), need >= {".".join(map(str, MIN_PYTHON))}')
    ok = ok and py_ok
    if args.install:
        python, errors = install_deps()
        for err in errors:
            say(f'WARN install: {err}')
        if python is None:
            say(f'FAIL install: beautifulsoup4 could not be installed (tried {VENV_DIR} and pip install --user).',
                '     EPUB translation needs beautifulsoup4; TXT works without it.',
                '     pip needs network access to pypi.org and files.pythonhosted.org.')
            return EXIT_PREFLIGHT
        cmd = [python, os.path.abspath(__file__), 'doctor', '--engine', engine]
        cmd += (['--file', args.file] if args.file else []) + (['--output-dir', args.output_dir] if args.output_dir else [])
        proc = subprocess.run(cmd, env=dict(os.environ, DODARI_REEXEC='1'))
        return proc.returncode
    if args.update and engine in eng.CLI_ENGINES:
        found, _raw = eng.cli_version(engine, eng.codex_home(cfg))
        need = eng.required_version(engine, eng.engine_settings(engine, cfg)[0])
        if found is None or not eng._dodari_cli_version_at_least(found, need):
            update = eng.CODEX_UPDATE_CMD if engine == eng.ENGINE_CODEX_CLI else eng.CLAUDE_UPDATE_CMD
            say(f'[update] {update}')
            subprocess.run(update, shell=True)
    model, effort, timeout, note = resolve_engine_settings(args, cfg)[1:]
    cli = None
    if engine == eng.ENGINE_DIRECT:
        mode = 'direct'
    else:
        if note:
            say(f'NOTE model: {note}')
        say(f'settings: model={model or "(CLI default)"} effort={effort or "(CLI default)"} batch_timeout={timeout}s  config={eng.CONFIG_PATH}')
        cli = cli_status(engine, model, cfg)
        mode = 'cli' if cli['found'] is not None and cli['logged'] is not False else 'direct'
    d_ok, d_lines = dependency_lines(fmt)
    say(*d_lines)
    ok = ok and d_ok
    if args.output_dir:
        out_dir = os.path.abspath(args.output_dir)
    else:
        out_dir = os.path.dirname(os.path.abspath(args.file)) if args.file else None
    if mode == 'cli':
        p_ok, lines = preflight(engine, model, cfg, out_dir, cli)
    else:
        p_ok, lines = preflight(eng.ENGINE_DIRECT, None, cfg, out_dir)
        if cli is not None:
            lines = direct_reason_lines(engine, cli) + lines
    say(*lines)
    ok = ok and p_ok
    if mode == 'cli':
        say(f'mode: cli   (use --engine {engine} for inspect and start; the CLI translates in the background)')
    else:
        say(f'mode: direct   (use --engine {eng.ENGINE_DIRECT} for inspect and start; '
            'you translate each batch yourself with next/submit)')
    say(f'RESULT: {"ready" if ok else "not ready"}')
    return EXIT_OK if ok else EXIT_PREFLIGHT


def engine_line(engine, model, effort):
    if engine == eng.ENGINE_DIRECT:
        return 'engine: direct  (no CLI — you translate each batch in this conversation)'
    return f'engine: {engine}  model: {model or "(CLI default)"}  effort: {effort or "(CLI default)"}'


def input_format(path):
    return SUPPORTED_FORMATS.get(os.path.splitext(path or '')[1].lower())


def check_input(path):
    fmt = input_format(path)
    if path and fmt is None:
        say(UNSUPPORTED_MESSAGE, f'file: {path}')
        sys.exit(EXIT_INPUT)
    if not path or not os.path.isfile(path):
        say(f'ERROR: file not found: {path}')
        sys.exit(EXIT_INPUT)
    return os.path.abspath(path), fmt


def read_txt_stats(path):
    try:
        return txt.stats(path)
    except (OSError, ValueError) as err:
        say(f'ERROR: cannot read TXT ({err})')
        return None


def cmd_inspect(args):
    path, fmt = check_input(args.file)
    cfg = eng.load_config()
    engine, model, effort, _timeout, note = resolve_engine_settings(args, cfg)
    if fmt == 'txt':
        return inspect_txt(path, engine, model, effort, note)
    epub = need_epub_modules()
    try:
        st = epub.stats(path, with_units=True)
    except Exception as err:
        say(f'ERROR: cannot read EPUB ({type(err).__name__}: {err})')
        return EXIT_INPUT
    title, lang = epub.zip_metadata(path)
    say(f'file: {path}',
        'format: EPUB',
        f'title: {title or "(unknown)"}',
        f'source_language: {lang or "(not in metadata; assumed en)"}',
        f'size_bytes: {os.path.getsize(path)}',
        f'documents: {st["chapters"]}  with_text: {st["chapters_to_translate"]}',
        f'letters: {st["letters"]}  chars: {st["chars"]} (markup removed, as in the usage measurement)',
        f'images: {st["images"]}',
        f'image_only: {"yes — cannot translate (no text layer)" if st["image_only"] else "no"}',
        engine_line(engine, model, effort))
    if note:
        say(f'NOTE model: {note}')
    say(*estimate_lines(st['chars'], st['sentences'], engine, model, effort, st['chapter_sentences']))
    return EXIT_INPUT if st['image_only'] else EXIT_OK


def inspect_txt(path, engine, model, effort, note):
    st = read_txt_stats(path)
    if st is None:
        return EXIT_INPUT
    info = st['info']
    lang = st['source_lang']
    say(f'file: {path}',
        'format: TXT',
        f'encoding: {info["encoding"]}{" with BOM" if info["bom"] else ""}  newlines: {txt.NEWLINE_NAMES[info["newline"]]}',
        f'title: {os.path.splitext(os.path.basename(path))[0]}',
        f'source_language: {lang + " (guessed from the script)" if lang else "(not detected; assumed en)"}'
        '  — --source-lang overrides',
        f'size_bytes: {os.path.getsize(path)}',
        f'blocks: {st["paragraphs"]} (blank-line separated)  with_text: {st["paragraphs_with_text"]}',
        f'letters: {st["letters"]}  chars: {st["chars"]}',
        engine_line(engine, model, effort))
    if note:
        say(f'NOTE model: {note}')
    say(*txt_estimate_lines(st, engine, model, effort))
    if not st['texts']:
        say('ERROR: this TXT has no text to translate.')
        return EXIT_INPUT
    return EXIT_OK


def cmd_sample(args):
    path, fmt = check_input(args.file)
    if fmt == 'txt':
        try:
            text = txt.sample(path, args.chars)
        except (OSError, ValueError) as err:
            say(f'ERROR: cannot read TXT ({err})')
            return EXIT_INPUT
    else:
        text = need_epub_modules().sample(path, args.chars)
    say(f'--- sample ({len(text)} chars) ---', text, '--- end of sample ---')
    return EXIT_OK


def build_settings(args, cfg, path, fmt):
    engine, model, effort, timeout, note = resolve_engine_settings(args, cfg)
    if args.target_lang not in eng.LANGUAGES:
        say(f'ERROR: unsupported --target-lang {args.target_lang}. Supported: {", ".join(eng.LANGUAGES)}')
        sys.exit(EXIT_INPUT)
    out_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(path)
    settings = {
        'input': path,
        'format': fmt,
        'input_sha1': file_sha1(path),
        'target_lang': args.target_lang,
        'source_lang': args.source_lang,
        'genre': eng.GENRES[args.genre],
        'tone': eng.TONES[args.tone],
        'bilingual_order': eng.BILINGUAL_ORDERS[args.bilingual_order],
        'engine': engine,
        'model': model,
        'effort': effort,
        'timeout_sec': timeout,
        'codex_home': eng.codex_home(cfg) if engine == eng.ENGINE_CODEX_CLI else None,
        'output_dir': out_dir,
    }
    return settings, note


def job_id_for(settings, unit_format):
    key = json.dumps({k: settings[k] for k in (
        'input', 'input_sha1', 'output_dir', 'target_lang', 'source_lang', 'genre', 'tone', 'bilingual_order',
        'engine', 'model', 'effort')}, sort_keys=True) + unit_format
    stem = os.path.splitext(os.path.basename(settings['input']))[0]
    return f'{slug(stem)}-{hashlib.sha1(key.encode()).hexdigest()[:10]}'


def reap_old_group(job):
    old = job.status().get('pid')
    if not old or IS_WINDOWS or pid_alive(old):
        return
    try:
        os.killpg(int(old), 0)
    except OSError:
        return
    try:
        os.killpg(int(old), signal.SIGTERM)
        say(f'cleaned up leftover CLI processes of the previous worker (group {old})')
    except OSError:
        pass


def launch_worker(job):
    os.makedirs(job.dir, exist_ok=True)
    reap_old_group(job)
    log = open(job.log_path, 'ab')
    cmd = [sys.executable, os.path.abspath(__file__), '_worker', job.id]
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, cwd=job.dir, env=env)
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    log.close()
    job.update_status(state='starting', pid=proc.pid, heartbeat_epoch=time.time(), message='worker launched')
    return proc.pid


def print_job_started(job, pid, extra=()):
    say(f'job_id: {job.id}',
        f'pid: {pid}',
        f'job_dir: {job.dir}',
        f'log: {job.log_path}',
        f'check progress: {script_cmd("status", job.id)}',
        *extra)


def cmd_start(args):
    path, fmt = check_input(args.file)
    cfg = eng.load_config()
    settings, note = build_settings(args, cfg, path, fmt)
    engine, model, effort = settings['engine'], settings['model'], settings['effort']
    if fmt == 'epub':
        epub = need_epub_modules()
        st = epub.stats(path, with_units=True)
        if st['image_only']:
            say('ERROR: this EPUB has no translatable text (image-only). OCR is not supported in this version.')
            return EXIT_INPUT
        unit_format = epub.EPUB_UNIT_FORMAT
        estimate = estimate_lines(st['chars'], st['sentences'], engine, model, effort, st['chapter_sentences'])
    else:
        st = read_txt_stats(path)
        if st is None:
            return EXIT_INPUT
        if not st['texts']:
            say('ERROR: this TXT has no text to translate.')
            return EXIT_INPUT
        unit_format = txt.TXT_UNIT_FORMAT
        estimate = txt_estimate_lines(st, engine, model, effort)
    ok, lines = preflight(engine, model, cfg, settings['output_dir'])
    if not ok:
        say(*lines, 'RESULT: not started (preflight failed)')
        return EXIT_PREFLIGHT
    direct = engine == eng.ENGINE_DIRECT
    job = Job(job_id_for(settings, unit_format))
    if job.exists():
        state = job.status().get('state')
        if job.alive():
            say(f'already running: {job.id}')
            print_job_started(job, job.status().get('pid'))
            return EXIT_OK
        if state == 'done':
            say(f'already done: {job.id}', *[f'output: {p}' for p in job.status().get('outputs', [])])
            return EXIT_OK
        if direct:
            say(f'existing direct job ({state}): {job.id}')
            return continue_direct(job)
        say(f'resuming existing job ({state}): {job.id}')
    else:
        os.makedirs(job.dir, exist_ok=True)
        settings['created_at'] = now_iso()
        write_json_atomic(job.job_path, settings)
        job.update_status(state='created', job_id=job.id, engine=engine, model=model,
                          effort=effort, input=path, elapsed_before=0)
    extra = [engine_line(engine, model, effort)]
    if note:
        extra.append(f'NOTE model: {note}')
    extra += estimate
    if direct:
        return start_direct(job, extra)
    pid = launch_worker(job)
    print_job_started(job, pid, extra)
    return EXIT_OK


def cmd_resume(args):
    job = Job(args.job_id)
    if not job.exists():
        say(f'ERROR: no such job: {args.job_id}  (see: status)')
        return EXIT_INPUT
    if job.alive():
        say(f'already running: {job.id}')
        return EXIT_OK
    st = job.status()
    if st.get('state') == 'done':
        say(f'already done: {job.id}', *[f'output: {p}' for p in st.get('outputs', [])])
        return EXIT_OK
    settings = job.settings()
    if settings.get('engine') == eng.ENGINE_DIRECT:
        return continue_direct(job)
    cfg = eng.load_config()
    if settings.get('format', 'epub') == 'epub':
        need_epub_modules()
    ok, lines = preflight(settings['engine'], settings['model'], cfg, settings['output_dir'])
    if not ok:
        say(*lines, 'RESULT: not resumed (preflight failed)')
        return EXIT_PREFLIGHT
    pid = launch_worker(job)
    print_job_started(job, pid, [f'resumed from state: {st.get("state")}'])
    return EXIT_OK


def cmd_cancel(args):
    job = Job(args.job_id)
    if not job.exists():
        say(f'ERROR: no such job: {args.job_id}')
        return EXIT_INPUT
    pid = job.status().get('pid')
    if pid_alive(pid):
        if IS_WINDOWS:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
        else:
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                os.kill(int(pid), signal.SIGTERM)
        for _ in range(50):
            if not pid_alive(pid):
                break
            time.sleep(0.2)
    job.update_status(state='cancelled', message='cancelled by user')
    say(f'cancelled: {job.id} (pid {pid}, alive now: {pid_alive(pid)})',
        f'resume later with: {script_cmd("resume", job.id)}')
    return EXIT_OK


def status_lines(job):
    st = job.status()
    settings = job.settings()
    direct = settings.get('engine') == eng.ENGINE_DIRECT
    alive = job.alive()
    state = st.get('state', 'unknown')
    if state in ('running', 'starting') and not alive:
        state = 'stopped (worker not running — use resume)'
    lines = [f'job_id: {job.id}', f'state: {state}']
    if direct:
        rejected = f'  rejected: {st["direct_rejected"]}' if st.get('direct_rejected') else ''
        lines.append(f'mode: direct  batches: {st.get("batches_done", 0)}/{st.get("batches_total", "?")} submitted{rejected}')
    done = st.get('sentences_done')
    total = st.get('sentences_total')
    unit = 'paragraphs' if settings.get('format') == 'txt' else 'sentences'
    if total:
        lines.append(f'progress: {done or 0}/{total} {unit} ({100 * (done or 0) / total:.1f}%)  '
                     f'documents: {st.get("chapters_done", 0)}/{st.get("chapters_total", "?")}')
    if not direct or st.get('elapsed_sec'):
        lines.append(f'elapsed: {fmt_duration(st.get("elapsed_sec"))}  eta: {fmt_duration(st.get("eta_sec")) if alive else "-"}')
    if st.get('stats') and not direct:
        s = st['stats']
        lines.append(f'cli: calls={s.get("calls", 0)} retries={s.get("batch_retry", 0)} splits={s.get("split", 0)} '
                     f'kept_source={s.get("source_kept", 0)}')
    if st.get('message'):
        lines.append(f'message: {st["message"]}')
    for p in st.get('outputs', []) or []:
        lines.append(f'output: {p}')
    if direct and st.get('state') != 'done' and not alive:
        lines.append(f'next: {script_cmd("next", job.id)}')
    lines.append(f'log: {job.log_path}')
    return lines


def cmd_status(args):
    if args.job_id:
        job = Job(args.job_id)
        if not job.exists():
            say(f'ERROR: no such job: {args.job_id}')
            return EXIT_INPUT
        say(*status_lines(job))
        return EXIT_OK
    if not os.path.isdir(JOBS_DIR):
        say('no jobs')
        return EXIT_OK
    jobs = sorted((Job(n) for n in os.listdir(JOBS_DIR) if Job(n).exists()),
                  key=lambda j: j.status().get('updated_at', ''), reverse=True)
    if not jobs:
        say('no jobs')
    for job in jobs[:5]:
        say(*status_lines(job), '')
    return EXIT_OK


def load_plan(job):
    return read_json(job.plan_path)


def make_plan(job):
    worker = Worker(job, job.log_path)
    batches = []
    for key_base, texts in worker.plan_texts():
        for c_no, (c_start, c_end) in enumerate(eng.chunk_ranges(texts, worker.max_chars)):
            if f'chunk:{key_base}:{c_no}' in worker.progress['done']:
                continue
            chunk = texts[c_start:c_end]
            for b_start, b_end in eng.plan_batches(chunk, eng.CLI_BATCH_SIZE, worker.max_chars):
                batches.append({'no': len(batches) + 1, 'key': key_base, 'chunk': c_no, 'offset': b_start,
                                'chunk_size': len(chunk), 'texts': chunk[b_start:b_end]})
    plan = {'created_at': now_iso(), 'batches': batches}
    write_json_atomic(job.plan_path, plan)
    job.update_status(state='direct', phase='translate', batches_total=len(batches), batches_done=0,
                      sentences_total=worker.total, sentences_done=worker.done_before_run,
                      chapters_total=worker.chapters_total, chapters_done=0, message='waiting for next/submit')
    return plan


def direct_progress(job, plan):
    batches = plan.get('batches', [])
    done = [b['no'] for b in batches if os.path.exists(job.direct_batch_path(b['no']))]
    done_set = set(done)
    return batches, done, [b for b in batches if b['no'] not in done_set]


def start_direct(job, extra=()):
    plan = load_plan(job)
    if plan is None:
        try:
            plan = make_plan(job)
        except Exception as err:
            job.update_status(state='failed', message=f'{type(err).__name__}: {str(err)[:300]}')
            say(f'ERROR: could not prepare the batches ({type(err).__name__}: {err})')
            return EXIT_INPUT
    batches, done, pending = direct_progress(job, plan)
    say(f'job_id: {job.id}',
        'mode: direct (no background process — translate each batch in this conversation with next/submit)',
        f'job_dir: {job.dir}',
        f'batches_total: {len(batches)}  submitted: {len(done)}  items: {sum(len(b["texts"]) for b in batches)}',
        *extra)
    if not pending:
        say('no batch left to translate — building the output files')
        return run_build(job)
    say(f'next: {script_cmd("next", job.id)}')
    return EXIT_OK


def continue_direct(job):
    plan = load_plan(job)
    if plan is None:
        return start_direct(job)
    batches, done, pending = direct_progress(job, plan)
    if not pending:
        return run_build(job)
    job.update_status(state='direct', message=f'waiting for batch {pending[0]["no"]}')
    say(f'job_id: {job.id}',
        f'mode: direct  batches: {len(done)}/{len(batches)} submitted',
        f'next: {script_cmd("next", job.id)}')
    return EXIT_OK


def open_direct_job(job_id):
    job = Job(job_id)
    if not job.exists():
        say(f'ERROR: no such job: {job_id}  (see: status)')
        return None
    if job.settings().get('engine') != eng.ENGINE_DIRECT:
        say(f'ERROR: {job_id} is a background CLI job — use status/resume/cancel, not next/submit/build')
        return None
    return job


def direct_prompt(settings, texts):
    prompt = eng.build_system_prompt(settings['genre'], settings['tone'], settings['target_lang'], isolation=False)
    if settings.get('format', 'epub') == 'epub' and any('⟦' in t for t in texts):
        prompt += need_epub_modules().EPUB_TOKEN_INSTRUCTION
    return prompt.strip()


def parse_submission(raw, expected):
    text = eng._dodari_cli_strip_fence(raw or '')
    if not text:
        raise eng.DodariCliError('submission is empty')
    try:
        payload = json.loads(text)
    except Exception as err:
        raise eng.DodariCliError(f'submission is not valid JSON ({err})')
    items = eng._dodari_cli_extract_array(payload, 'submission')
    eng._dodari_cli_normalize(items, expected, 'submission')
    wrong = [i + 1 for i, x in enumerate(items) if not isinstance(x, str)]
    if wrong:
        raise eng.DodariCliError(f'submission: items {wrong} are not strings — every item must be one translated string')
    cleaned = [eng._dodari_strip_translator_notes(x) for x in items]
    empty = [i + 1 for i, x in enumerate(cleaned) if not x.strip()]
    if empty:
        raise eng.DodariCliError(f'submission: items {empty} are empty or only a translator note — '
                                 'put the translation in every item')
    return cleaned


def read_submission(path):
    if path == '-':
        return sys.stdin.buffer.read().decode('utf-8-sig')
    with open(path, 'r', encoding='utf-8-sig') as fp:
        return fp.read()


def complete_chunk(job, batches, batch):
    members = sorted((b for b in batches if b['key'] == batch['key'] and b['chunk'] == batch['chunk']),
                     key=lambda b: b['offset'])
    out = []
    for b in members:
        items = read_json(job.direct_batch_path(b['no']))
        if not isinstance(items, list) or len(items) != len(b['texts']):
            return False
        out.extend(items)
    if len(out) != batch['chunk_size']:
        return False
    os.makedirs(job.chunks_dir, exist_ok=True)
    write_json_atomic(job.chunk_path(batch['key'], batch['chunk']), out)
    progress = job.progress()
    key = f'chunk:{batch["key"]}:{batch["chunk"]}'
    if key not in progress['done']:
        progress['done'].append(key)
        job.save_progress(progress)
    return True


def cmd_next(args):
    job = open_direct_job(args.job_id)
    if job is None:
        return EXIT_INPUT
    st = job.status()
    if st.get('state') == 'done':
        say('state: done', *[f'output: {p}' for p in st.get('outputs', [])])
        return EXIT_OK
    plan = load_plan(job)
    if plan is None:
        say('ERROR: batch plan missing — run the same start command again')
        return EXIT_INPUT
    batches, done, pending = direct_progress(job, plan)
    if not pending:
        say(f'all {len(batches)} batches submitted — build the output files with:', script_cmd('build', job.id))
        return EXIT_OK
    batch = pending[0]
    texts = batch['texts']
    n = len(texts)
    say(f'job_id: {job.id}',
        f'batch: {batch["no"]} of {len(batches)}   items: {n}   submitted so far: {len(done)}/{len(batches)}',
        '--- instructions ---',
        direct_prompt(job.settings(), texts),
        '--- items (JSON) ---',
        '[',
        ',\n'.join(json.dumps({'n': i + 1, 'text': t}, ensure_ascii=False) for i, t in enumerate(texts)),
        ']',
        '--- submit ---',
        f'Submit {{"translations": [...]}} with exactly {n} strings in item order (item 1 first, item {n} last):',
        f"{script_cmd('submit', job.id, batch['no'], '-')} <<'DODARI_JSON'",
        '{"translations": ["...", "..."]}',
        'DODARI_JSON',
        '(or save the JSON to a file and give its path instead of -)')
    return EXIT_OK


def cmd_submit(args):
    job = open_direct_job(args.job_id)
    if job is None:
        return EXIT_INPUT
    st = job.status()
    if st.get('state') == 'done':
        say(f'already done: {job.id}', *[f'output: {p}' for p in st.get('outputs', [])])
        return EXIT_OK
    plan = load_plan(job)
    if plan is None:
        say('ERROR: batch plan missing — run the same start command again')
        return EXIT_INPUT
    batches = plan['batches']
    if not 1 <= args.batch <= len(batches):
        say(f'ERROR: batch {args.batch} does not exist (1-{len(batches)})')
        return EXIT_INPUT
    batch = batches[args.batch - 1]
    n = len(batch['texts'])
    try:
        items = parse_submission(read_submission(args.file), n)
    except (OSError, UnicodeDecodeError) as err:
        say(f'ERROR: cannot read {args.file} ({err})')
        return EXIT_INPUT
    except eng.DodariCliError as err:
        job.update_status(direct_rejected=(st.get('direct_rejected') or 0) + 1)
        say(f'REJECTED batch {batch["no"]}: {err}',
            f'Nothing was saved. Translate batch {batch["no"]} again — exactly {n} strings in item order — and submit it again.',
            f'Show the batch again with: {script_cmd("next", job.id)}')
        return EXIT_REJECTED
    replaced = os.path.exists(job.direct_batch_path(batch['no']))
    os.makedirs(job.direct_dir, exist_ok=True)
    write_json_atomic(job.direct_batch_path(batch['no']), items)
    complete_chunk(job, batches, batch)
    batches, done, pending = direct_progress(job, plan)
    done_set = set(done)
    job.update_status(state='direct', batches_done=len(done),
                      sentences_done=sum(len(b['texts']) for b in batches if b['no'] in done_set),
                      message=f'waiting for batch {pending[0]["no"]}' if pending else 'all batches submitted')
    say(f'ACCEPTED batch {batch["no"]} ({n} items){" — replaced the earlier submission" if replaced else ""}',
        f'progress: {len(done)}/{len(batches)} batches submitted')
    if pending:
        say(f'next: {script_cmd("next", job.id)}')
        return EXIT_OK
    say('all batches submitted — building the output files')
    return run_build(job)


def cmd_build(args):
    job = open_direct_job(args.job_id)
    if job is None:
        return EXIT_INPUT
    st = job.status()
    if st.get('state') == 'done':
        say(f'already done: {job.id}', *[f'output: {p}' for p in st.get('outputs', [])])
        return EXIT_OK
    if job.alive():
        say(f'already building: {job.id}')
        return EXIT_OK
    plan = load_plan(job)
    if plan is None:
        say('ERROR: batch plan missing — run the same start command again')
        return EXIT_INPUT
    batches, _done, pending = direct_progress(job, plan)
    if pending:
        say(f'ERROR: {len(pending)} of {len(batches)} batches are not submitted yet (first missing: batch {pending[0]["no"]})',
            f'continue with: {script_cmd("next", job.id)}')
        return EXIT_INPUT
    return run_build(job)


def run_build(job):
    say(f'building output files for {job.id} (details: {job.log_path})')
    code = run_worker(job, log_to_file=True)
    say(*status_lines(job))
    return code


class DirectCacheOnly:

    def __init__(self):
        self.stats = {}

    def translate(self, texts):
        raise RuntimeError(f'{len(texts)} items have no submitted translation — run next/submit for the missing batches')


class Worker:

    def __init__(self, job, log_path=None):
        self.job = job
        self.s = job.settings()
        self.fmt = self.s.get('format', 'epub')
        self.direct = self.s.get('engine') == eng.ENGINE_DIRECT
        self.epub = need_epub_modules() if self.fmt == 'epub' else None
        self.max_chars = txt.TXT_BATCH_CHARS if self.fmt == 'txt' else None
        self.progress = job.progress()
        self.run_start = time.time()
        st = job.status()
        self.elapsed_before = max(st.get('elapsed_before') or 0, st.get('elapsed_sec') or 0)
        self.run_done = 0
        self.done_before_run = 0
        self.total = 0
        self.chapters_total = 0
        self.lock = threading.Lock()
        self.translator = None
        self.stop_beat = threading.Event()
        self.log_path = log_path

    def log(self, msg):
        line = f'{time.strftime("%H:%M:%S")} {msg}'
        if self.log_path:
            with open(self.log_path, 'a', encoding='utf-8') as fp:
                fp.write(line + '\n')
        else:
            print(line, flush=True)

    def heartbeat(self):
        while not self.stop_beat.wait(HEARTBEAT_SEC):
            try:
                self.job.update_status(heartbeat_epoch=time.time(), elapsed_sec=self.elapsed())
            except Exception:
                pass

    def elapsed(self):
        return self.elapsed_before + (time.time() - self.run_start)

    def mark(self, key):
        if key not in self.progress['done']:
            self.progress['done'].append(key)
        self.job.save_progress(self.progress)

    def on_batch(self, n):
        with self.lock:
            self.run_done += n
            done = self.done_before_run + self.run_done
            rate = self.run_done / max(time.time() - self.run_start, 1)
            eta = (self.total - done) / rate if rate > 0 else None
            self.job.update_status(sentences_done=done, eta_sec=eta, elapsed_sec=self.elapsed(),
                                   heartbeat_epoch=time.time(), stats=dict(self.translator.stats))

    def use_job_splitter(self):
        recorded = self.progress.get('splitter')
        current = eng.splitter_name(force=recorded)
        if recorded == 'nltk' and current != 'nltk':
            raise RuntimeError('this job was split with nltk punkt_tab, which cannot be loaded now — '
                               f'run: {script_cmd("doctor", "--install")}')
        return current

    def prepare(self):
        ep = self.epub
        if not self.progress.get('extracted'):
            for folder in (self.job.work_bi, self.job.work_mono):
                shutil.rmtree(folder, ignore_errors=True)
                ep.extract(self.s['input'], folder)
            shutil.rmtree(self.job.chunks_dir, ignore_errors=True)
            source = self.s.get('source_lang')
            opf = ep.find_opf(self.job.work_bi)
            if not opf:
                raise RuntimeError('content.opf not found in EPUB')
            source = source or ep.opf_language(opf) or 'en'
            for folder in (self.job.work_bi, self.job.work_mono):
                ep._dodari_epub_set_opf_language(ep.find_opf(folder), self.s['target_lang'])
            self.progress = {'extracted': True, 'source_lang': source, 'splitter': eng.splitter_name(),
                             'done': [], 'counts': {}}
            self.job.save_progress(self.progress)
            self.log(f'extracted twice, OPF language → {self.s["target_lang"]} (source {source})')
        self.use_job_splitter()
        os.makedirs(self.job.chunks_dir, exist_ok=True)

    def prepare_txt(self):
        if not self.progress.get('extracted'):
            text, info = txt.read_txt(self.s['input'])
            txt.write_source(self.job.txt_source, text)
            shutil.rmtree(self.job.chunks_dir, ignore_errors=True)
            source = self.s.get('source_lang') or txt.guess_language(text) or 'en'
            self.progress = {'extracted': True, 'source_lang': source, 'splitter': eng.splitter_name(),
                             'txt': info, 'done': [], 'counts': {}}
            self.job.save_progress(self.progress)
            self.log(f'TXT copied ({info["encoding"]}{" BOM" if info["bom"] else ""}, '
                     f'{txt.NEWLINE_NAMES[info["newline"]]}), source {source}')
        self.use_job_splitter()
        os.makedirs(self.job.chunks_dir, exist_ok=True)
        return txt.split_parts(txt.read_source(self.job.txt_source))

    def count_units(self):
        ep = self.epub
        job = self.job
        files = ep.html_files(job.work_bi)
        code_classes = ep._dodari_epub_folder_code_classes(job.work_bi)
        for rel in files:
            if rel not in self.progress['counts']:
                soup = ep.read_soup(os.path.join(job.work_bi, rel))
                n = 0 if ep.is_skip_page(soup) else len(ep.chapter_units(soup, code_classes)[1])
                self.progress['counts'][rel] = n
        ncx = ep.ncx_files(job.work_bi)
        for rel in ncx:
            key = NCX_KEY_PREFIX + rel
            if key not in self.progress['counts']:
                try:
                    self.progress['counts'][key] = len(ep.ncx_units(ep.read_text(os.path.join(job.work_bi, rel)))[3])
                except Exception:
                    self.progress['counts'][key] = 0
        job.save_progress(self.progress)
        self.total = sum(self.progress['counts'].values())
        self.chapters_total = len(files)
        done_ch = [r for r in files if r in self.progress['done']]
        self.done_before_run = sum(self.progress['counts'][r] for r in done_ch) + sum(
            self.progress['counts'][NCX_KEY_PREFIX + r] for r in ncx if NCX_KEY_PREFIX + r in self.progress['done'])
        return files, ncx, code_classes, done_ch

    def count_txt(self, texts):
        self.progress['counts'][txt.TXT_KEY] = len(texts)
        self.job.save_progress(self.progress)
        self.total = len(texts)
        self.chapters_total = 1
        done = set(self.progress['done'])
        self.done_before_run = sum(end - start for c_no, (start, end) in
                                   enumerate(eng.chunk_ranges(texts, self.max_chars))
                                   if f'chunk:{txt.TXT_KEY}:{c_no}' in done)

    def pending_texts(self, files, ncx, code_classes):
        ep = self.epub
        out = []
        for rel in files:
            if rel in self.progress['done']:
                continue
            soup = ep.read_soup(os.path.join(self.job.work_bi, rel))
            if ep.is_skip_page(soup):
                continue
            texts = ep.chapter_units(soup, code_classes)[1]
            if texts:
                out.append((rel, texts))
        for rel in ncx:
            key = NCX_KEY_PREFIX + rel
            if key in self.progress['done']:
                continue
            try:
                _labels, _soup, units, texts, _whole = ep.ncx_units(ep.read_text(os.path.join(self.job.work_bi, rel)))
            except Exception:
                continue
            if units and texts:
                out.append((key, texts))
        return out

    def plan_texts(self):
        if self.fmt == 'txt':
            texts = txt.unit_texts(self.prepare_txt())
            self.count_txt(texts)
            return [(txt.TXT_KEY, texts)] if texts else []
        self.prepare()
        files, ncx, code_classes, _done_ch = self.count_units()
        return self.pending_texts(files, ncx, code_classes)

    def make_translator(self):
        if self.direct:
            return DirectCacheOnly()
        s = self.s
        system_prompt = eng.build_system_prompt(s['genre'], s['tone'], s['target_lang'])
        return eng.Translator(
            s['engine'], s['model'], s['effort'], s['timeout_sec'], system_prompt,
            token_instruction=self.epub.EPUB_TOKEN_INSTRUCTION if self.epub else '', home=s.get('codex_home'),
            failure_path=self.job.failures_path, on_batch=self.on_batch, log=self.log, max_chars=self.max_chars)

    def translate_cached(self, key_base, only_texts):
        translated = []
        for c_no, (start, end) in enumerate(eng.chunk_ranges(only_texts, self.max_chars)):
            chunk = only_texts[start:end]
            key = f'chunk:{key_base}:{c_no}'
            cpath = self.job.chunk_path(key_base, c_no)
            cached = read_json(cpath) if key in self.progress['done'] else None
            if isinstance(cached, list) and len(cached) == len(chunk):
                self.log(f'[resume] {key_base} chunk {c_no} from cache')
                translated.extend(cached)
                continue
            self.log(f'[chunk] {key_base} chunk {c_no}: {len(chunk)} items')
            out = self.translator.translate(chunk)
            if len(out) != len(chunk):
                out = (list(out) + list(chunk))[:len(chunk)]
            write_json_atomic(cpath, out)
            self.mark(key)
            translated.extend(out)
        return translated

    def translate_ncx(self, rel):
        ep = self.epub
        key = NCX_KEY_PREFIX + rel
        path_bi = os.path.join(self.job.work_bi, rel)
        path_mono = os.path.join(self.job.work_mono, rel)
        try:
            xml_bi = ep.read_text(path_bi)
            xml_mono = ep.read_text(path_mono)
            labels_bi, soup_bi, units_bi, only_texts, whole_particle = ep.ncx_units(xml_bi)
            labels_mono, soup_mono, units_mono, _texts, _whole = ep.ncx_units(xml_mono)
            if len(units_bi) != len(units_mono):
                raise ValueError(f'NCX unit count mismatch ({len(units_bi)} != {len(units_mono)})')
        except Exception as err:
            self.log(f'[ncx] parsing error, keeping source table of contents: {rel}: {err}')
            return
        self.log(f'[ncx] {rel} ({len(labels_bi)} labels, {len(units_bi)} to translate)')
        if units_bi:
            translated = self.translate_cached(key, only_texts)
            try:
                groups = ep.group_translations(whole_particle, translated)
                if len(groups) != len(units_bi):
                    raise ValueError(f'NCX translation groups mismatch ({len(groups)} != {len(units_bi)})')
                bilingual_order = self.s['bilingual_order']
                ep._dodari_epub_apply_units(soup_bi, units_bi, groups, True, bilingual_order)
                ep._dodari_epub_apply_units(soup_mono, units_mono, groups, False, bilingual_order)
                new_bi = ep._dodari_epub_ncx_write(xml_bi, labels_bi, soup_bi)
                new_mono = ep._dodari_epub_ncx_write(xml_mono, labels_mono, soup_mono)
                ep.write_text(path_bi, new_bi)
                ep.write_text(path_mono, new_mono)
            except Exception as err:
                self.log(f'[ncx] reassembly error, keeping source table of contents: {rel}: {err}')
                return
        self.mark(key)

    def translate_chapter(self, rel, code_classes):
        ep = self.epub
        path_bi = os.path.join(self.job.work_bi, rel)
        path_mono = os.path.join(self.job.work_mono, rel)
        soup_bi = ep.read_soup(path_bi)
        soup_mono = ep.read_soup(path_mono)
        if ep.is_skip_page(soup_bi):
            self.mark(rel)
            return
        units_bi, only_texts, whole_particle = ep.chapter_units(soup_bi, code_classes)
        units_mono = ep._dodari_epub_collect_units(soup_mono, code_classes=code_classes)
        if len(units_bi) != len(units_mono):
            raise RuntimeError(f'EPUB unit count mismatch in {rel} ({len(units_bi)} != {len(units_mono)})')
        translated = self.translate_cached(rel, only_texts)
        groups = ep.group_translations(whole_particle, translated)
        if len(groups) != len(units_bi):
            raise RuntimeError(f'EPUB translation groups mismatch in {rel} ({len(groups)} != {len(units_bi)})')
        bilingual_order = self.s['bilingual_order']
        ep._dodari_epub_apply_units(soup_bi, units_bi, groups, True, bilingual_order)
        ep._dodari_epub_apply_units(soup_mono, units_mono, groups, False, bilingual_order)
        ep.write_soup(path_bi, soup_bi)
        ep.write_soup(path_mono, soup_mono)
        self.mark(rel)

    def output_paths(self, ext):
        name = os.path.splitext(os.path.basename(self.s['input']))[0]
        t = self.s['target_lang']
        o = self.progress.get('source_lang') or 'en'
        out_dir = self.s['output_dir']
        if self.s['bilingual_order'] == eng.BILINGUAL_ORDERS['source-first']:
            bi = os.path.join(out_dir, f'{name}_{o}({t}){ext}')
        else:
            bi = os.path.join(out_dir, f'{name}_{t}({o}){ext}')
        mono = os.path.join(out_dir, f'{name}_{t}{ext}')
        return unique_path(mono), unique_path(bi)

    def run_epub(self):
        job = self.job
        ep = self.epub
        self.prepare()
        files, ncx, code_classes, done_ch = self.count_units()
        job.update_status(phase='translate', sentences_total=self.total, sentences_done=self.done_before_run,
                          chapters_total=len(files), chapters_done=len(done_ch))
        self.translator = self.make_translator()
        for rel in files:
            if rel in self.progress['done']:
                continue
            self.log(f'[chapter] {rel} ({self.progress["counts"][rel]} sentences)')
            self.translate_chapter(rel, code_classes)
            done_ch = [r for r in files if r in self.progress['done']]
            with self.lock:
                job.update_status(chapters_done=len(done_ch), stats=dict(self.translator.stats))
        for rel in ncx:
            if NCX_KEY_PREFIX + rel not in self.progress['done']:
                self.translate_ncx(rel)
        job.update_status(phase='package')
        mono, bi = self.output_paths('.epub')
        os.makedirs(self.s['output_dir'], exist_ok=True)
        tmp_bi = os.path.join(job.dir, 'out_bilingual.epub')
        tmp_mono = os.path.join(job.dir, 'out_single.epub')
        ep.repack(job.work_bi, tmp_bi)
        ep.repack(job.work_mono, tmp_mono)
        shutil.move(tmp_mono, mono)
        shutil.move(tmp_bi, bi)
        return [mono, bi]

    def run_txt(self):
        job = self.job
        parts = self.prepare_txt()
        texts = txt.unit_texts(parts)
        self.count_txt(texts)
        job.update_status(phase='translate', sentences_total=self.total, sentences_done=self.done_before_run,
                          chapters_total=1, chapters_done=0)
        self.translator = self.make_translator()
        self.log(f'[txt] {len(texts)} paragraphs')
        translated = self.translate_cached(txt.TXT_KEY, texts)
        job.update_status(phase='package', chapters_done=1, stats=dict(self.translator.stats))
        source_first = self.s['bilingual_order'] == eng.BILINGUAL_ORDERS['source-first']
        mono_text, bi_text = txt.render(parts, translated, source_first)
        mono, bi = self.output_paths('.txt')
        os.makedirs(self.s['output_dir'], exist_ok=True)
        info = self.progress.get('txt') or {}
        tmp_mono = os.path.join(job.dir, 'out_single.txt')
        tmp_bi = os.path.join(job.dir, 'out_bilingual.txt')
        txt.write_output(tmp_mono, mono_text, info)
        txt.write_output(tmp_bi, bi_text, info)
        shutil.move(tmp_mono, mono)
        shutil.move(tmp_bi, bi)
        return [mono, bi]

    def run(self):
        job = self.job
        s = self.s
        job.update_status(state='running', pid=os.getpid(), phase='prepare', heartbeat_epoch=time.time(),
                          run_started_at=now_iso(), message='')
        beat = threading.Thread(target=self.heartbeat, daemon=True)
        beat.start()
        self.log(f'worker start pid={os.getpid()} format={self.fmt} engine={s["engine"]} model={s["model"]} effort={s["effort"]}')
        outputs = self.run_txt() if self.fmt == 'txt' else self.run_epub()
        for folder in (job.work_bi, job.work_mono, job.chunks_dir, job.direct_dir):
            shutil.rmtree(folder, ignore_errors=True)
        for path in (job.txt_source, job.plan_path):
            try:
                os.remove(path)
            except OSError:
                pass
        self.stop_beat.set()
        job.update_status(state='done', phase='done', outputs=outputs, eta_sec=0, elapsed_sec=self.elapsed(),
                          elapsed_before=self.elapsed(), sentences_done=self.total, heartbeat_epoch=time.time(),
                          stats=dict(self.translator.stats), finished_at=now_iso(), message='completed')
        self.log(f'done: {" | ".join(outputs)}')


def run_worker(job, log_to_file=False):
    worker = Worker(job, job.log_path if log_to_file else None)

    def on_term(_signum, _frame):
        job.update_status(state='cancelled', message='terminated by signal', elapsed_before=worker.elapsed())
        os._exit(130)
    signal.signal(signal.SIGTERM, on_term)
    try:
        worker.run()
        return EXIT_OK
    except eng.DodariCliRateLimitError as err:
        job.update_status(state='paused_rate_limit', message=f'subscription limit reached: {str(err)[:300]} — resume after it resets',
                          elapsed_before=worker.elapsed())
        worker.log(f'rate limit: {err}')
        return EXIT_RATE_LIMIT
    except eng.DodariModelRejected as err:
        update = eng.CODEX_UPDATE_CMD if job.settings().get('engine') == eng.ENGINE_CODEX_CLI else eng.CLAUDE_UPDATE_CMD
        job.update_status(state='failed', message=f'model rejected: {str(err)[:300]} — update the CLI ({update}) or choose another model, then resume',
                          elapsed_before=worker.elapsed())
        worker.log(f'model rejected: {err}')
        return EXIT_PREFLIGHT
    except Exception as err:
        job.update_status(state='failed', message=f'{type(err).__name__}: {str(err)[:300]} (progress kept; resume retries)',
                          elapsed_before=worker.elapsed())
        worker.log(traceback.format_exc())
        return EXIT_INPUT
    finally:
        worker.stop_beat.set()


def cmd_worker(args):
    return run_worker(Job(args.job_id))


def build_parser():
    p = argparse.ArgumentParser(prog='dodari_book.py', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    def engine_args(sp):
        sp.add_argument('--engine', choices=eng.ENGINES)
        sp.add_argument('--model')
        sp.add_argument('--effort')
        sp.add_argument('--timeout', type=int, help='seconds per batch call')

    d = sub.add_parser('doctor')
    engine_args(d)
    d.add_argument('--file')
    d.add_argument('--output-dir', help='check write access to this output folder')
    d.add_argument('--install', action='store_true', help=f'install dependencies into {VENV_DIR} (or pip --user)')
    d.add_argument('--update', action='store_true', help='update the engine CLI if it is too old')

    i = sub.add_parser('inspect')
    i.add_argument('file')
    engine_args(i)

    s = sub.add_parser('sample')
    s.add_argument('file')
    s.add_argument('--chars', type=int, default=3000)

    st = sub.add_parser('start')
    st.add_argument('file')
    st.add_argument('--target-lang', required=True, help='target language code: ' + ', '.join(eng.LANGUAGES))
    st.add_argument('--genre', required=True, choices=list(eng.GENRES))
    st.add_argument('--tone', required=True, choices=list(eng.TONES))
    st.add_argument('--source-lang', help='source language code (default: EPUB metadata or TXT script, else en)')
    st.add_argument('--output-dir')
    st.add_argument('--bilingual-order', choices=list(eng.BILINGUAL_ORDERS), default='translation-first')
    engine_args(st)

    s2 = sub.add_parser('status')
    s2.add_argument('job_id', nargs='?')
    r = sub.add_parser('resume')
    r.add_argument('job_id')
    c = sub.add_parser('cancel')
    c.add_argument('job_id')
    n = sub.add_parser('next')
    n.add_argument('job_id')
    sm = sub.add_parser('submit')
    sm.add_argument('job_id')
    sm.add_argument('batch', type=int)
    sm.add_argument('file', help='JSON file with {"translations": [...]}, or - to read it from standard input')
    b = sub.add_parser('build')
    b.add_argument('job_id')
    w = sub.add_parser('_worker')
    w.add_argument('job_id')
    return p


def main(argv=None):
    if IS_WINDOWS:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    if args.command in ('inspect', 'sample', 'start', 'resume', '_worker', 'next', 'submit', 'build') \
            or (args.command == 'doctor' and not args.install):
        reexec_in_venv_if_needed()
    handlers = {
        'doctor': cmd_doctor, 'inspect': cmd_inspect, 'sample': cmd_sample, 'start': cmd_start,
        'status': cmd_status, 'resume': cmd_resume, 'cancel': cmd_cancel, 'next': cmd_next,
        'submit': cmd_submit, 'build': cmd_build, '_worker': cmd_worker,
    }
    return handlers[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
