# TokenLens

TokenLens is a local-first CLI for checking usage and remaining quota across developer assistant workflows.

## Install

For local development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tokenlens --help
```

For isolated installation:

```bash
pipx install .
tokenlens status
```

## Commands

```bash
tokenlens status
tokenlens doctor
tokenlens providers
tokenlens config show
```

Legacy direct execution still works:

```bash
python3 src/tokenlens.py status
```

## Output model

Each provider is normalized into:

- `provider`
- `name`
- `status`
- `source_kind`
- `confidence`
- `window`
- `used`
- `remaining`
- `limit`
- `unit`
- `observed_at`
- `reset_kind`
- `reset_at`
- `reset_note`
- `manual_check`
- `details`

Exit codes:

- `0` = ok
- `10` = warn
- `20` = critical
- `30` = unknown or unavailable
- `1` = CLI or config error

## Configuration

Config is stored at:

```text
~/.config/tokenlens/config.json
```

Examples:

```bash
tokenlens config set <provider>.limit 500M
tokenlens config set <provider>.warn_threshold 15%
tokenlens config set <provider>.enabled false
tokenlens config set <provider>.label "Main quota"
```

## Notes

- TokenLens uses local-only inspection and does not upload usage data.
- Some providers are estimated from local state rather than an official quota endpoint.
- Codex reports real quota percentages: TokenLens reads the `rate_limits` block (5h + weekly windows, with reset times) that the Codex CLI records in its local session logs — the same numbers Codex shows on startup. Run Codex at least once so a recent snapshot exists.
- Claude can report real quota percentages too, opt-in. Enable it with `tokenlens config set claude.use_api true`. TokenLens then reads the local Claude Code OAuth token (`~/.claude/.credentials.json`) and calls the official `/api/oauth/usage` endpoint — the same data behind Claude Code's `/usage` (session 5h + weekly all-models + weekly Sonnet/Opus). The token is only used to authenticate your own lookup; it is never logged or stored. With `use_api` off (the default), Claude usage stays a local-log estimate and no network call is made.
- Availability depends on what local data or authenticated tools are present on the machine.
