# occtl — Voice-first AI Command Center for tmux + OpenCode

occtl is a small CLI that:

- runs OpenCode inside tmux sessions on your Mac
- lets you control/continue sessions remotely (iPhone + Vision Pro)
- supports voice-first control via Siri Shortcuts
- sends alerts when a session appears idle/blocked

## Decisions (what we chose and why)

### Remote access: Tailscale + Termius Starter
- No new subscriptions required.
- Works on iPhone + Vision Pro.
- Use Mosh where possible for smoother reconnects; SSH as fallback.

### Session substrate: tmux
- Sessions persist across disconnects.
- Multiple work streams stay isolated (infra vs finance vs zoom-rag, etc).
- Attach from anywhere.

### Directory mapping: explicit mapping file
- Session names do NOT imply directory names.
- You maintain a mapping: session_name -> absolute path.

### Voice control: Apple Shortcuts (Run Script over SSH)
- One Shortcut “AI Command”.
- Dictate text -> `oc voice "<phrase>"`.
- Deterministic intent parsing (no LLM required; predictable).

### Alerts: launchd watcher
- Runs every minute.
- Checks focused session.
- If idle past threshold: notify (macOS + optional Discord webhook).

## Install (macOS)

Prereqs:
- `tmux` installed
- OpenCode installed and runnable as `opencode`
- Python 3.10+

Install in editable mode (recommended):
```bash
./run install
```

### Setup mappings

```bash
oc map infra ~/src/homelab
oc map finance ~/src/cashclaw
```

List mappings:

```bash
oc maps
```

### Create/ensure sessions

Create and start OpenCode:

```bash
oc new infra
```

Ensure (create if missing, then focus):

```bash
oc ensure infra
```

### Day-to-day commands

```bash
oc ls                    # list sessions
oc focus infra           # focus a session
oc say "run tests and summarize failures"  # send text to focused session
oc enter                 # send Enter to focused session
oc status                # focus + mapped dir + idle seconds
oc attach infra          # interactive attach (best from Termius)
```

## Voice-first workflow (recommended)

- Voice commands are non-interactive (Shortcuts runs an SSH command, not a live terminal).
- Therefore voice commands treat “attach/open/go to” as focus-only.
- Use voice for: focus, new/ensure, list, status, say, continue.
- Use Termius only when you want to view/drive the live terminal.

Typical flow:

1. "Hey Siri, AI Command" -> "focus infra"
2. "Hey Siri, AI Command" -> "run tests and summarize failures"
3. "Hey Siri, AI Command" -> "continue"
4. Open Termius only if you want to see the screen.

## Siri Shortcuts setup (ONE shortcut)

Create a Shortcut named: AI Command

Actions:
1. Dictate Text
2. Run Script Over SSH:
   ```bash
   oc voice "$ProvidedInput"
   ```

## Alerts (automatic “waiting?” notifications)

We include a launchd job that runs every 60 seconds:

```bash
oc watch --idle-seconds 90
```

It checks the focused session.

Install the launchd job:

```bash
./scripts/install_launchd.sh
```

Logs:
- `/tmp/occtl-watch.out`
- `/tmp/occtl-watch.err`

## Optional: Discord webhook notifications

If you want bulletproof delivery to iPhone/Watch/Vision Pro:

```bash
oc set-webhook "https://discord.com/api/webhooks/..."
```

This sends alerts to both:
- macOS notification center
- Discord push notifications

## Config files

- Mappings: `~/.config/occtl/mappings.toml`
- State: `~/.config/occtl/state.json`

Example mappings.toml:

```toml
[map]
infra = "/Users/jason/src/homelab"
finance = "/Users/jason/src/cashclaw"
```

## Scripts
- `scripts/install.sh`: install package
- `scripts/install_launchd.sh`: install launchd watcher

### Development
- `./run lint` runs lint checks.
- `./run lint fix` auto-fixes lint issues.
- `./run test` runs tests.
- `./run format` formats code.
- `./run quality` runs lint + test together.
- `./run quality fix` runs lint with auto-fixes, then tests.
- `./run venv-status` shows local tool environment state.

## Fast checklist

1. Install
   ```bash
   ./scripts/install.sh
   ```
2. Add mappings
   ```bash
   oc map infra ~/src/homelab
   oc map finance ~/src/cashclaw
   ```
3. Create a session
   ```bash
   oc new infra
   ```
4. Install watcher
   ```bash
   ./scripts/install_launchd.sh
   ```
5. Optional webhook
    ```bash
    oc set-webhook "https://discord.com/api/webhooks/..."
    ```

After this, most voice flow is usually:
- "switch to infra"
- "run tests"
- "continue"
- "status"

## Development quickstart

This repo uses a local virtual environment at `./.venv` for all `./run` tooling commands.

- `./run` commands will create `./.venv` automatically if it does not exist.
- Dependency installs for tooling are written into that environment.

Try this first to get started quickly:

```bash
./run install
./run venv-status  # optional: confirm ruff/pytest are installed
./run quality
```

Run occtl from this repo:

```bash
./oc status
```

Tip:

```bash
oc                  # no args = status
```

If you want `oc` on PATH, install the local launcher into `~/bin`:

```bash
./run install --bin
export PATH="${HOME}/bin:$PATH"
oc status
```

Inspect tooling environment:

```bash
./run venv-status
```
