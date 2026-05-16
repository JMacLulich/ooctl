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
- OpenCode installed and runnable as `opencode`
- Python 3.10+

`./run install` will install `tmux` automatically via Homebrew if it is missing.

Install in editable mode (recommended):
```bash
./run install
```

### Setup mappings

```bash
oc map infra ~/src/homelab
oc map finance ~/src/cashclaw
oc map "gig guide" ~/src/gig-folder
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
oc kill infra            # terminate a session (or: oc kill for focused)
```

### Link cross-rig mailbox to tmux

For projects using the `.rig-mailbox` protocol, use the normal session TUI:

```bash
oc attach
```

The TUI shows mailbox roles beside linked sessions, e.g. `[Rig A]` and
`[Rig B]`. Press `m` to enter mailbox mode, then select two running sessions
from the same workspace. If that workspace does not have `.rig-mailbox` yet,
`oc attach` creates it first, then links the sessions.

If a mapped project has exactly two running tmux sessions, `oc attach`
auto-creates `.rig-mailbox` when missing and auto-links them as Rig A/Rig B
when the TUI refreshes. This covers the common case where you create two
`zoom-mvps` or `cash-claw` sessions and want them tied together without
remembering any setup commands.

The standalone wizard also exists:

```bash
oc mailbox
```

For scripted setup, `oc` can create the project mailbox if needed and write the
stable tmux targets into `.rig-mailbox/rigs.toml`:

```bash
oc mailbox link cash-claw-rig-a --rig "Rig A" --runtime claude-code --workspace ~/dev/cash-claw
oc mailbox link cash-claw-rig-b --rig "Rig B" --runtime codex --workspace ~/dev/cash-claw
```

This configures the mailbox `tmux` notifier as `<session>:main`, matching the
session/window shape created by `oc new`. If the session already has an agent
running in another window, `oc` auto-targets the agent window instead, for
example `cash claw:shell` when Claude is running in the `shell` window. Mailbox
wakes then use `tmux send-keys ... Enter`, so they do not steal macOS focus and
do not depend on changing iTerm tab titles.

Mailbox linking also writes `RIG_NAME` and `RIG_WORKSPACE` into the tmux
session environment. Newly launched shells and agents in that session inherit
their identity automatically. Already-running agent processes cannot have their
Unix environment changed from outside; restart the agent inside the linked tmux
session if it was started before the mailbox link existed.

When a tmux session contains multiple windows, expand the session in `oc attach`
to see individual windows. Press `Enter` on a window row to attach directly to
that window. Press `x` on a window row to kill that tmux window.

### Clipboard over SSH (tmux + OSC52)

`oc attach` auto-installs and reloads the managed tmux clipboard include when
needed. It keeps tmux mouse reporting on so Mosh can preserve trackpad and
scroll-wheel scrolling, and binds tmux mouse drag-release to copy through OSC52
or the host clipboard.
The manual commands are available for inspection or repair:

```bash
oc clipboard setup --mode auto --reload
oc clipboard status
oc clipboard verify
```

Notes:
- Run setup on the same host where tmux runs.
- `oc attach` and default setup use tmux mouse mode (`set -g mouse on`) so Mosh scrolling keeps working.
- Drag selection inside tmux copies on mouse release; copy-mode `y` is still bound as a keyboard fallback.
- If you need direct local iTerm2 selection instead of tmux selection, use Option-drag.
- If you want terminal selection behavior and can live without tmux/Mosh mouse scrolling, run `oc clipboard setup --mouse-mode terminal --reload`.
- If your terminal blocks OSC52, `verify` may show emission success but clipboard failure.
- Uninstall managed config:

```bash
oc clipboard uninstall --remove-helper
```

## Voice-first workflow (recommended)

- Voice commands are non-interactive (Shortcuts runs an SSH command, not a live terminal).
- Therefore voice commands treat “attach/open/go to” as focus-only.
- Use voice for: focus, new/ensure, list, status, say, continue.
- Use Termius only when you want to view/drive the live terminal.

Typical flow:

1. "Hey Siri, AI Command" -> "focus infra"
2. "Hey Siri, AI Command" -> "open gig guide"
3. "Hey Siri, AI Command" -> "run tests and summarize failures"
4. "Hey Siri, AI Command" -> "continue"
5. Open Termius only if you want to see the screen.
6. "Hey Siri, AI Command" -> "start cash claw"

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
    ./run install
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

`./run install` also creates a local launcher at `~/bin/oc`.

```bash
./run install
export PATH="${HOME}/bin:$PATH"
oc status
```

Inspect tooling environment:

```bash
./run venv-status
```

## Shell autocomplete

Completions are now installed automatically by `./run install`.

```bash
oc   # tab-complete works after restarting your shell
```

Tip:

- In bash/zsh/fish, type `oc` and `<TAB><TAB>` to complete `map|maps|new|...`
- Session names with spaces need shell quoting, for example:

  ```bash
  oc map "gig guide" ~/src/gig-folder
  oc new "gig guide"
  ```
