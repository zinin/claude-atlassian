# Changelog

All notable changes to claude-atlassian will be documented here.

## [Unreleased]

## [0.5.0] - 2026-09-16

### Added
- `investigate-feature` skill — the non-bug counterpart to `investigate-bug`: takes a
  summarized feature, task, improvement or tech-debt ticket and grounds it in code.
  Parallel scouts cover where the change attaches and what of it already exists, the
  precedents and conventions it should follow, neighbouring repositories, git history and
  related tickets; then come acceptance criteria drafted with their sources, the open
  decisions with the evidence that makes them decisions, and one to three challengers,
  each attacking the insertion point, the constraints or the criteria. By default it hands
  the brief to `superpowers:brainstorming` rather than designing anything itself; a fatal
  blocker, a needed split, too-thin requirements or a single obvious path ends the run
  before that. Read-only on the same terms as `investigate-bug`.

### Changed
- `investigate-bug` now offers `investigate-feature` for a non-bug ticket where it used to
  offer `superpowers:brainstorming`.
- `investigate-bug` tightens its read-only contract: git is used only to read — never to
  change the repository or its working tree, `bisect` and `fetch` included — and the
  report's directory is created only after you confirm the save. Its scouts receive the
  ticket evidence between `<evidence>` tags, apart from their instructions.
- `investigate-bug` names tests by path once the ticket's attachments are on disk,
  instead of a tree-wide runner (`pytest`, `npm test`, `go test ./...`) that would
  pick up an attachment as a test.
- `investigate-bug` offers to save the report before it takes the outcome's action,
  so brainstorming or a fix does not take over the session first.
- `investigate-bug` asks before overwriting `docs/investigations/{KEY}.md` if that
  file already exists.
- `analyze-jira-ticket` ends its summary with a one-line pointer to the next step:
  `investigate-bug` for a bug, `investigate-feature` for anything else.

## [0.4.0] - 2026-08-26

### Changed
- Test fixtures and one README example now use invented identifiers throughout —
  hostnames, project and space keys, page and attachment ids, and filenames. No
  behaviour changed: none of those values was ever what a test checked.

## [0.3.0] - 2026-08-26

### Added
- `scripts/atlassian-attachments.py` — attachment downloader speaking the Atlassian
  REST API directly (Python 3, standard library only), with a `unittest` suite under
  `tests/`.
- `analyze-wiki` gained an attachment step: page attachments now land on disk in
  `docs/wiki-attachments/<page-id>-<title>/`.
- `investigate-bug` now treats ticket attachments as evidence — logs, stack traces and
  screenshots — and fetches them itself when `analyze-jira-ticket` has not already.

### Changed
- Attachments are downloaded directly to disk via REST instead of through MCP base64
  responses, so file contents no longer pass through the model context.

### Fixed
- `analyze-jira-ticket` documented a `/tmp` download path that the MCP attachment tool
  never supported — it has no path argument, so nothing was ever written to disk.
- Attachments the server rebuilds while serving them — every JPEG on Confluence
  Server — were downloaded correctly and then discarded, because their size cannot
  match the one in the page metadata. The size check now applies only to bodies the
  server delivers with a declared length; a chunked body is guarded by its own
  framing. Such rows are marked `(пересобран сервером)`.
- Two attachments could land on top of each other where the filesystem considers their
  names one entry: a case variant on NTFS or APFS, a name differing only in Unicode
  normalisation on macOS, or a trailing dot on Windows. Reservation keys now follow the
  rules of the filesystem in use, so the second attachment gets its `-<id>` suffix.
- On Windows the downloader no longer fails on names the platform cannot create —
  reserved device names, `< > : " | ? *`, trailing dots and spaces — in attachment names
  and page-folder names alike. Names are untouched on Linux and macOS.
- `os.O_NOFOLLOW` does not exist on Windows, and reaching for it aborted the whole run
  on the first attachment.

## [0.2.0] - 2026-07-22

### Added
- `investigate-bug` skill — after `analyze-jira-ticket`, traces a bug from the ticket
  summary into the code: parallel scout subagents over the current and neighbouring
  repositories, git history and related tickets, then hypothesis synthesis with a
  falsification round and a report carrying the root cause, what was ruled out, and a
  fix plan. Read-only: writes no code, runs tests and builds only if you confirm, and
  saves the report only on confirmation.

## [0.1.0] - 2026-07-16

### Added
- Initial release: tools ported from the author's internal ai-tools monorepo (@ ad23588).
- `analyze-jira-ticket` skill — one subagent reads a Jira ticket, comments, linked Confluence pages, attachments, and linked issues; returns a compact structured summary.
- `analyze-wiki` skill — one subagent reads a Confluence page (by id, URL, or `SPACE:Title`), child/linked pages and comments; optionally answers a question.
