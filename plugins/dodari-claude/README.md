# Dodari for Claude

도다리: EPUB·TXT 문서 전체를 12개 언어로 번역하는 Claude 플러그인.

Dodari translates a whole EPUB or TXT document into another language, not just a few pages. For every job you get two files next to your input file: a translated file and a bilingual file (original and translation together). Jobs save their progress, so an interrupted translation resumes where it stopped.

Dodari is also an open-source app. See [Dodari](https://github.com/vEduardovich/dodari) on GitHub.

## Install

- **Claude Code:**
  ```
  /plugin marketplace add vEduardovich/dodari
  /plugin install dodari@himion
  ```
  On Claude Code 2.1.275 or later, the one-step form also works: `/plugin install dodari --marketplace vEduardovich/dodari`.
- **claude.ai and Cowork:** open Customize > Plugins and add Dodari, once it is listed in Anthropic's plugin directory.

## What it does

- Input: EPUB or TXT.
- Output: a translated file named `<name>_<target>.epub` or `.txt`, and a bilingual file (original and translation) named `<name>_<target>(<source>).epub` or `.txt`. Both are written next to the input file, or into `--output-dir` if you give one. If a name already exists, `_001`, `_002` and so on are appended.
- Target languages: Korean, English, Japanese, Chinese, French, Italian, Dutch, Danish, Swedish, Norwegian, Arabic, Persian.
- Genre (`it`, `literature`, `humanities`, `business`, `subtitles`, `general`) and tone (`plain` or `formal`) are chosen automatically from a sample of the text.
- Not supported in this version: PDF and image-only EPUBs.

## How to use

Ask in plain language, or call the skill directly:

```
Translate this EPUB into Korean
/dodari:translate book.epub to Japanese
Translate notes.txt into French
```

Claude asks before it installs Python packages or updates your CLI. It does not ask before ordinary steps. It then runs the job and reports the output file paths.

## Modes

The mode is chosen automatically.

- **CLI mode** (Claude Code, or anywhere the `claude` CLI is installed and logged in; the OpenAI `codex` CLI is used only if you ask for it). Bundled Python scripts split the document and run `claude -p` (or `codex exec`) in the background on your machine, using your own subscription. Text is sent to Anthropic (or OpenAI) under your own account. Progress is saved, so an interrupted job resumes, and closing the session does not stop the job.
- **Direct mode** (claude.ai chat, Cowork, or no CLI available). The scripts split the document into numbered batches, Claude translates each batch in the conversation, and the scripts rebuild the file. A long book takes many turns, so Claude Code is recommended for whole books.

## Requirements

- Python 3.
- `beautifulsoup4==4.14.3` and `nltk==3.10.2`, installed from PyPI into `~/.dodari/venv` after you approve the setup step.
- NLTK sentence data is downloaded during setup. If it cannot be downloaded, a built-in sentence splitter is used.
- For CLI mode: a logged-in `claude` CLI (or `codex` CLI if you choose it).

## What this plugin runs and fetches

- `python3` scripts in the skill folder.
- CLI checks: `claude --version`, `claude auth status`, `codex --version`, `codex login status`.
- Translation calls (CLI mode): `claude -p` with tools, slash commands, MCP, settings and session persistence disabled, run in an empty temporary folder; or `codex exec` in a read-only sandbox with `CODEX_HOME=~/.dodari/codex-home` (a separate Codex login used only by Dodari).
- Optional, only after you agree: `claude update`, or `npm install -g @openai/codex@latest`.
- A connectivity check in CLI mode only: a TCP connect to `api.anthropic.com:443` or `chatgpt.com:443`.
- Setup, only after you agree: `python -m venv ~/.dodari/venv` and `pip install` from pypi.org and files.pythonhosted.org (it falls back to `pip install --user` if a venv cannot be created); NLTK sentence data from raw.githubusercontent.com into `~/.dodari/nltk_data`.
- A detached background worker process (`python dodari_book.py _worker <job_id>`) in CLI mode. Cancelling the job stops it.
- Direct mode starts no processes and opens no connections.

The scripts open no other connections and send nothing to Himion.

## Privacy Policy

**Data collection.** Himion Inc. collects no data through this plugin. There is no telemetry, no analytics, no account, and nothing is sent to Himion or any other third party operated by us.

**Usage and storage.** Your documents are processed locally. Each job has a folder at `~/.dodari/jobs/<job_id>/` holding extracted book files, a copy of the source text, translated chunks, direct-mode batches and logs. Output files are written next to your input file (or into `--output-dir`). The `DODARI_HOME` environment variable moves the `~/.dodari/` folder, and settings are read from `~/.dodari/config.json` if it exists.

**Third-party sharing.** Document text is sent only to the AI provider used by your own session: Anthropic when you use Claude (in the conversation or through the `claude` CLI), or OpenAI if you choose the `codex` CLI. This happens under your own account and your own subscription.
- In both modes, the `sample` command prints up to 3,000 characters of the document, and `inspect` prints the title and metadata, into the conversation, so that text reaches Anthropic as part of the conversation.
- In direct mode, each batch's text and Claude's translation are part of the conversation.
- In CLI mode, code, math and formatting inside EPUB sentences are replaced by placeholder tokens before sending.
- The plugin also contacts PyPI and its file host to install two packages, and raw.githubusercontent.com for NLTK sentence data; none of these receives your document text.

**Data retention.** When a job finishes, the translated chunks, extracted book files, the source text copy and direct-mode batches are deleted. What is kept is `job.json` (input file path, file hash, settings), `status.json`, `progress.json`, `log.txt` and, if a CLI batch failed, `cli_failures.jsonl` with up to 2,000 characters of raw model output. A cancelled or unfinished job keeps everything so it can resume. Deleting `~/.dodari/` removes all of it, including the venv, NLTK data and Dodari's Codex login. Output files are yours and stay where they were written. How long Anthropic or OpenAI retain data is governed by your account with that provider.

**Contact.** Himion Inc., [himion.com](https://himion.com), with@himion.com.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
