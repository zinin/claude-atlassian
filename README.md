# claude-atlassian

Claude Code plugin: Jira ticket analysis, Confluence page reading, and
cross-repository investigation — of a bug's root cause, or of where a new piece of
work lands — via context-protecting subagents: the main context receives only a
compact summary, never raw MCP output.

## Features

(Slash commands are namespaced under `claude-atlassian:` — that is how Claude Code surfaces plugin commands.)

- **`/claude-atlassian:analyze-jira-ticket PROJ-123`** — one subagent reads the ticket, comments, linked Confluence pages, attachments, and linked issues, then returns a structured summary.
- **`/claude-atlassian:analyze-wiki <page> [question]`** — one subagent reads a Confluence page (numeric id, URL, or `SPACE:Page Title`), its children/linked pages and comments; optionally answers a question about the content.
- **`/claude-atlassian:investigate-bug [PROJ-123]`** — runs after `analyze-jira-ticket`:
  traces the bug from the ticket summary into the code, scouting the current repository,
  neighbouring projects, git history and related tickets in parallel subagents, then
  reports a root cause with a fix plan. Bugs only, and strictly read-only — it never
  edits code; the only things it puts on disk are the ticket's attachments and, once
  you confirm, its report.
- **`/claude-atlassian:investigate-feature [PROJ-123]`** — the non-bug counterpart, also
  after `analyze-jira-ticket`: grounds the request in code — what is being asked, draft
  acceptance criteria tagged by source, where the work lands, which precedents it should
  follow, what the neighbours must change, which decisions are still open — then hands the
  brief to `superpowers:brainstorming` instead of designing anything itself. Features,
  tasks and tech debt; read-only on the same terms as `investigate-bug`.

## Attachments

Attachments are downloaded straight to disk over the Atlassian REST API by
`scripts/atlassian-attachments.py`, never through the MCP server. The MCP
attachment tools return file contents over the protocol — they have no path
argument and write nothing to disk — so a single ticket can cost a million tokens:
images arrive as embedded resources a model can look at, everything else as base64
it cannot decode.

The skills call the script themselves, by absolute path inside the installed
plugin; the lines below are what that looks like run by hand:

```
scripts/atlassian-attachments.py jira PROJ-123
scripts/atlassian-attachments.py wiki 123456789
```

Files land unchanged in `docs/jira-attachments/<KEY>/` and
`docs/wiki-attachments/<page-id>-<title>/`, resolved from the root of the git
repository you run in — the current directory outside a repository, and whatever
`--dir` names if you pass it. They stay untracked; the plugin does not touch your
`.gitignore`. Every attachment of the ticket or page is fetched whole and there is
no size limit, so narrow the run with `--only` when one of them is large: it takes
mime prefixes and extensions, as in `--only image/,.log`.

On Windows the downloader adjusts names the platform cannot represent — reserved device
names such as `CON.log`, trailing dots and spaces, and the characters `< > : " | ? *`, in
attachment names and in page-folder names alike. Those either fail outright or, worse,
silently resolve to a different file. Everywhere else names reach the disk exactly as the
server gave them.

A repeat run skips any file already on disk whose size matches the one the server
reports, so re-running costs almost nothing. Size is the whole identity check: an
attachment replaced by one of exactly the same byte count goes unnoticed, and Confluence,
unlike Jira, lets an attachment be replaced in place. Pass `--force` to re-fetch
everything regardless.

Confluence Server rebuilds some attachments while serving them — on the instance
this was tested against, every JPEG — so what arrives is a re-encoded image whose
size never matches the one in the page metadata. Those rows are marked
`(пересобран сервером)` and carry the size that actually landed on disk. Nothing
is lost, but such files are fetched again on every run: the skip-on-matching-size
shortcut cannot fire for them.

Credentials are resolved in order: `--url` with `--token` (they count only as a
pair), then `JIRA_URL` with `JIRA_PERSONAL_TOKEN` (or `JIRA_USERNAME` and
`JIRA_API_TOKEN`) and the matching `CONFLUENCE_*` variables, then the MCP
config — `.mcp.json` upwards from the current directory, `~/.claude.json`,
`~/.mcp.json`, `.claude/settings*.json`. Prefer the environment or the MCP config:
a token passed on the command line is visible to your other processes and stays in
your shell history. Run with `--explain-auth` to see which source matched; the
token itself is never printed.

## Install

```
/plugin marketplace add zinin/claude-plugins
/plugin install claude-atlassian@zinin
```

## Dependencies

- **Required:** an Atlassian MCP server registered under the exact name `mcp-atlassian`
  (e.g. [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian)), configured
  with access to your Jira and Confluence. The skills call
  `mcp__mcp-atlassian__jira_*` / `mcp__mcp-atlassian__confluence_*` tools — a different
  server name breaks these prefixes, and without the MCP the two analysis skills are
  non-functional (`investigate-bug` and `investigate-feature` still work if you supply the
  ticket summary yourself — they skip their Atlassian scout and say so in the report).
- **Required:** Python 3 for the attachment downloader. Standard library only —
  nothing to install.
- **Recommended:** use read-only Jira/Confluence credentials or scopes for the MCP — the
  plugin only reads, and read-only tokens limit the blast radius if malicious ticket/page
  content ever manages to steer the reading subagent. `investigate-bug` keeps the same
  discipline by instruction, not by tooling — its scouts are ordinary subagents, so the
  usual permission prompts remain the last line of defence.
- **Recommended:** [superpowers](https://github.com/obra/superpowers) — `investigate-bug`
  hands off to `superpowers:brainstorming` when the fix has open design questions, and
  `investigate-feature` hands off to it as its default outcome. Without it both skills
  still produce their full report and simply name the next step.

## See also

- [claude-forge](https://github.com/zinin/claude-forge) — build/test/lint delegation and dependency-update plugin by the same author
