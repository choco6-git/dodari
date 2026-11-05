---
name: translate
description: Translate a whole EPUB or TXT book or document into another language and save a translated file plus a bilingual (translation + original) file. Use when the user asks to translate an .epub or .txt file, an ebook, a novel or a long text document (e.g. "translate this epub to Korean", "translate this book into English", "이 epub 한국어로 번역해", "이 책 번역해줘", "このepubを日本語に翻訳して", "この本を翻訳して", "把这本书翻译成中文", "traduis ce livre en français", "traduci questo libro in italiano", "vertaal dit boek naar het Nederlands", "ترجم هذا الكتاب إلى العربية"). The bundled script splits, checks and rebuilds the file; the translation runs in the background through the user's Claude Code or Codex CLI, or batch by batch in this conversation when no CLI is available.
---

# Dodari Translator

## Script

- SCRIPT = `${CLAUDE_SKILL_DIR}/scripts/dodari_book.py`
- `${CLAUDE_SKILL_DIR}` not replaced by a path → SCRIPT = absolute path of `scripts/dodari_book.py` in the folder of this SKILL.md.
- Run: `python3 "SCRIPT" <command> ...` (Windows: `py -3 "SCRIPT" <command> ...`).

## Mandatory rules

- Run every step below in the shell and read its real output. Never skip a step. Never guess or invent output. Never answer before the command has finished.
- Read the book only through `sample` (and, in direct mode, `next`). Do not edit or review the output files.
- CLI mode: do not translate anything yourself and do not delegate translation to subagents. The script translates in the background.
- Direct mode: translate only the items printed by `next`, and only through `submit`. Write the translation only inside the submit command; do not repeat it in your reply.
- Input other than `.epub` or `.txt` → tell the user: "This version supports EPUB and TXT only."

## Steps

1. Doctor: `python3 "SCRIPT" doctor --engine <engine> --file "<file>"`
   - `<engine>`: Claude Code → `claude-cli`. Codex → `codex-cli`. claude.ai chat or anywhere without these CLIs → `direct`. User asks for another engine → use that one.
   - `FAIL dependencies` or `WARN dependencies` → ask first, install only after the user agrees. Tell the user: "This will create ~/.dodari/venv and install beautifulsoup4 and nltk from PyPI (or use `pip install --user` if a venv cannot be created), and download nltk sentence data." Wait for the go-ahead.
     - Agreed → run `python3 "SCRIPT" doctor --install --engine <engine> --file "<file>"` once. A `WARN` about nltk that remains after this is fine (a built-in sentence splitter is used). `FAIL install` → show the printed lines; TXT still works, EPUB does not.
     - Declined → TXT still works (built-in sentence splitter); EPUB needs beautifulsoup4, so stop for an EPUB that reports `FAIL dependencies`.
   - `FAIL cli` (too old) → ask first, update only after the user agrees. Show the exact command it will run: `claude update` (claude-cli) or `npm install -g @openai/codex@latest` (codex-cli). Wait for the go-ahead.
     - Agreed → run `python3 "SCRIPT" doctor --update --engine <engine>`.
     - Declined → continue in direct mode (`--engine direct` from here on).
   - `FAIL network` or `FAIL write` in CLI mode → go to steps 2 and 3, then run step 4 with escalated (outside-sandbox) permissions: network access + write access to `~/.dodari` and the output folder. Ask the user to approve it once. Escalation unavailable → show the user the printed `FIX` lines and the exact step 4 command for their own terminal.
   - `FAIL write` for the book folder in direct mode → add `--output-dir <a writable folder>` in step 4 (for example the folder where you save files for the user).
   - Read the `mode:` line. `mode: cli` → the CLI translates in the background. `mode: direct` → you translate batch by batch. If `mode: direct` comes with `INFO cli` or `INFO login`, tell the user once that installing or logging in to that CLI enables background translation; continue in direct mode unless the user wants to fix it first.
   - From here on use the engine named in the `mode:` line (`--engine direct` in direct mode).
2. Inspect: `python3 "SCRIPT" inspect "<file>" --engine <engine>`
   - Report `letters`, the `..._to_translate` count, `batches` and the `estimated_...` lines exactly as printed.
   - `image_only: yes` → tell the user image-only EPUBs are not supported. Stop.
   - `NOTE direct:` (direct mode, more than 40 batches) → before starting, tell the user it will take many turns in this conversation and that Claude Code with the claude CLI translates whole books in the background. Continue if the user wants.
3. Sample: `python3 "SCRIPT" sample "<file>"`
   - Read only this sample. Choose `--genre` and `--tone` from the tables below.
4. Start: `python3 "SCRIPT" start "<file>" --target-lang <code> --genre <genre> --tone <tone> --engine <engine>`
   - `--target-lang`: ko, en, ja, zh, fr, it, nl, da, sv, no, ar, fa (the language the user asked for).
   - Options: `--bilingual-order source-first` (original first), `--output-dir <folder>`, `--source-lang <code>`, and in CLI mode `--model <id>`, `--effort <level>`.
   - Report `job_id` and the estimate lines.
5. Translate — the one branch, by mode:
   - **CLI mode** (`mode: cli`)
     - Tell the user: "Translation runs in the background. Closing this session does not stop it."
     - Progress (when the user asks): `python3 "SCRIPT" status <job_id>` → report the printed lines.
       - `state: done` → report the two `output:` paths.
       - `state: paused_rate_limit` → tell the user to run resume after the subscription limit resets.
       - `state: stopped ...` or `state: failed` → run `python3 "SCRIPT" resume <job_id>`.
     - Stop (when the user asks): `python3 "SCRIPT" cancel <job_id>`. Continue later: `resume <job_id>` or the same `start` command.
   - **Direct mode** (`mode: direct`) — repeat until the script prints `state: done`:
     1. `python3 "SCRIPT" next <job_id>` → prints the instructions and the batch items as numbered JSON.
     2. Translate every item yourself, following the printed instructions: faithful and complete, the same number of items in the same order, one string per item, placeholder tokens such as `⟦C1⟧` and `⟦F1⟧ … ⟦/F1⟧` copied exactly, no notes, no commentary.
     3. Submit with the command `next` printed (valid JSON; escape `"` and `\` inside strings):
        ```
        python3 "SCRIPT" submit <job_id> <batch> - <<'DODARI_JSON'
        {"translations": ["...", "..."]}
        DODARI_JSON
        ```
     4. `REJECTED` (exit code 4) → nothing was saved; fix what the message says and submit the same batch again. `ACCEPTED` → run `next` again.
     - After the last batch the script builds the files itself and prints two `output:` lines → report both paths. If it says the build did not finish, run `python3 "SCRIPT" build <job_id>`.
     - Interrupted or a new conversation → `python3 "SCRIPT" status <job_id>` shows the submitted batches; continue with `next <job_id>` (or the same `start` command).

## Genre (`--genre`)

| Book | `--genre` |
| --- | --- |
| Code, technical, engineering, API, developer books | `it` |
| Novels, essays, memoirs, community writing, emotional prose | `literature` |
| Papers, psychology, history, social analysis, statistics | `humanities` |
| Business, market analysis, contracts, finance | `business` |
| Dialogue, subtitles, screenplays, interview transcripts | `subtitles` |
| None of the above | `general` |

- Mixed genres → the genre of most of the sample.

## Tone (`--tone`)

| Source style | `--tone` |
| --- | --- |
| Novels, essays, community writing, scripts | `plain` |
| Official documents, guides, business documents | `formal` |

## Output

| File | Name |
| --- | --- |
| Translation only | `<name>_<target>.epub` / `.txt` |
| Bilingual (default) | `<name>_<target>(<source>).epub` / `.txt` |
| Bilingual, `source-first` | `<name>_<source>(<target>).epub` / `.txt` |
| Name conflict | `_001`, `_002` suffix |

- Folder: the input file folder, or `--output-dir`.
- EPUB bilingual: each sentence as "translation (original)". TXT bilingual: each paragraph's translation with the original on the next line.
- TXT: UTF-8 (a BOM is detected and kept). A translation unit is a paragraph separated by blank lines; blank lines and line endings are kept.
- Batches: up to 45 sentences (EPUB) or 45 paragraphs / 8,000 characters (TXT).

## Settings

- File: `~/.dodari/config.json`, e.g. `{"engine": "claude-cli", "claude": {"model": "sonnet", "effort": null}, "codex": {"model": "gpt-6-luna", "effort": "max"}}` (`"engine": "direct"` is also accepted).
- Priority: command-line option > config.json > built-in default.
- Codex login folder: `~/.dodari/codex-home` (one-time login command: printed by doctor).
- Jobs: `~/.dodari/jobs/<job_id>/` (`status.json`, `log.txt`). The `DODARI_HOME` environment variable moves `~/.dodari`.
