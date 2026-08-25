# Documentation agents guide

Extends the root `AGENTS.md`. (`CLAUDE.md` and `GEMINI.md` in this directory are symlinks to this
file so that Claude Code and Gemini CLI pick it up too.)

## Stack

- **Sphinx** + **MyST** Markdown, built on the Canonical Sphinx starter pack (`canonical_sphinx`
  extension in `conf.py`) and published to Read the Docs
  (`canonical-charmed-valkey.readthedocs-hosted.com`).
- Builder is `dirhtml`. `.sphinx/version` tracks the starter-pack template revision (currently
  `1.3.0`) — update it via `make update`, not by hand.
- reStructuredText (`.rst`) is natively supported too (all existing pages are `.md`, and `conf.py`
  itself appends/prepends reST snippets via `rst_epilog`/`rst_prolog`) — but **use MyST Markdown
  for any new page** unless there's a specific reason to reach for `.rst`.

## Build

From the `docs/` directory (all targets create/reuse `.sphinx/venv` automatically):

```bash
make run        # install deps, build, and serve with live reload at http://127.0.0.1:8000
make html       # build once; --fail-on-warning --keep-going, warnings written to .sphinx/warnings.txt
make linkcheck  # check external links (fails on any "[broken]" entry)
make lint-md    # pymarkdownlnt, config in .sphinx/.pymarkdown.json
make spelling   # Vale spellcheck (accepted terms: .custom_wordlist.txt)
make woke       # Vale inclusive-language check
make clean      # remove .sphinx/venv and node_modules
```

`make html` is strict: any new Sphinx warning (broken cross-reference, page missing from a
toctree, bad anchor) fails the build, not just a lint pass.

## Structure (Diátaxis)

- `index.md` — landing page.
- `tutorial.md` — the Tutorial, a single page (not a directory).
- `how-to/` — task-oriented guides. Each guide needs an entry in the `how-to/index.md` toctree, or
  Sphinx will emit an "not included in any toctree" warning and fail `make html`.
- `reference/` — currently only `contact.md`; there is no auto-generated reference content in this
  charm (no `generate_statuses.py`-style build step) and no `explanation/` section yet.

## File conventions

- Reference labels use MyST anchor syntax `(label-name)=` on the line before a heading (e.g.
  `(tutorial)=`, `(define-roles)=`), linked with `` {ref}`label-name` ``. Cross-doc links use
  `` {doc}`text <path/to/page>` `` (see `index.md`).
- Wrap terms that trip the Vale spellcheck in `` {spellexception}`term` `` (see
  `manage-passwords.md`, `tls.md`) instead of adding one-off entries to `.custom_wordlist.txt`.
- `reuse/links.txt` (RST hyperlink targets) and `reuse/substitutions.txt` (RST `replace::`
  substitutions) are appended to every page via `rst_epilog` in `conf.py`; `reuse/substitutions.yaml`
  feeds MyST `myst_substitutions`. Add repeated links/text there instead of duplicating them.
- Redirects for moved/renamed pages go in the `redirects` dict in `conf.py`
  (`sphinx_reredirects`); link-checker exceptions go in `linkcheck_ignore` in the same file.

## CI

- `.github/workflows/automatic-doc-checks.yml` — on push to `main`/`*/edge` or PRs touching
  `docs/**`, runs the shared `canonical/documentation-workflows` checks (spelling, inclusive
  language, linkcheck).
- `.github/workflows/markdown-style-checks.yml` — `make install && make lint-md` on `docs/**`
  changes.
- `.github/workflows/check-removed-urls.yml` — on PRs, builds docs from both the PR branch and the
  base branch and diffs the generated URL lists to catch pages/anchors removed by the PR.
- Per the root `AGENTS.md`: docs-only changes (`docs/**`) are excluded from release tagging and
  from the integration-test gate.

## Gotchas

- `conf.py`'s `exclude_patterns` MUST keep `.venv`, `.sphinx`, and `_build` excluded — without
  them Sphinx walks installed venv packages and emits dozens of bogus toctree/autodoc warnings.
- A stray, unmanaged `docs/.venv` can coexist with the real `.sphinx/venv` (the one every Makefile
  target actually uses, via `VENVDIR`). `make clean` only removes `.sphinx/venv`; don't rely on
  `.venv` for anything.
- `pymarkdownlnt --exclude` globs must NOT have a leading `./` — it silently matches nothing (see
  the `lint-md` target in `Makefile` for the correct pattern).
- `make pdf` needs system TeX/LaTeX packages; run `make pdf-prep` first (or `sudo make
  pdf-prep-force` to install them) — don't run PDF generation speculatively.
