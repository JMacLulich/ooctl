from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from . import config, tmux
from .notify import discord_webhook, mac_notify
from .voice import parse_voice

COMMANDS = (
    "map",
    "maps",
    "new",
    "ensure",
    "ls",
    "focus",
    "focused",
    "status",
    "say",
    "enter",
    "attach",
    "watch",
    "set-webhook",
    "voice",
    "completion",
)


def _fmt_cmds_for_shell(commands: Sequence[str]) -> str:
    return " ".join(commands)


def cmd_map(args: argparse.Namespace) -> int:
    config.set_mapping(args.name, args.path)
    print(f"mapped: {args.name} -> {config.get_mapping(args.name)}")
    return 0


def cmd_maps(_: argparse.Namespace) -> int:
    m = config.load_mappings()
    if not m:
        print("(no mappings)")
        return 0
    for k in sorted(m.keys()):
        print(f"{k}\t{m[k]}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name
    if tmux.has_session(name):
        config.set_focus(name)
        print(f"exists+focused: {name}")
        return 0

    workdir = config.get_mapping(name)
    if not workdir:
        print(f"No mapping for '{name}'. Add one:\n  oc map {name} /path/to/project")
        return 1

    if not Path(workdir).exists():
        print(f"Mapped directory does not exist: {workdir}")
        return 1

    tmux.new_session(name, workdir)
    tmux.send_keys(f"{name}:main", ["opencode", "Enter"])
    tmux.new_window(name, "logs", workdir)
    tmux.new_window(name, "shell", workdir)

    config.set_focus(name)
    print(f"created+focused: {name}\tdir={workdir}")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    name = args.name
    if not tmux.has_session(name):
        return cmd_new(argparse.Namespace(name=name))
    config.set_focus(name)
    print(f"focused: {name}")
    return 0


def cmd_ls(_: argparse.Namespace) -> int:
    rows = tmux.list_sessions()
    if not rows:
        print("(no tmux sessions)")
        return 0
    for r in rows:
        print(f"{r['name']}\tattached={int(r['attached'])}\twindows={r['windows']}")
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    config.set_focus(args.name)
    print(f"focused: {args.name}")
    return 0


def cmd_focused(_: argparse.Namespace) -> int:
    print(config.get_focus())
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    focus = config.get_focus()
    webhook = config.get_webhook()
    m = config.load_mappings()

    print(f"focus:\t{focus or '(none)'}")
    if focus and focus in m:
        print(f"dir:\t{m[focus]}")
    else:
        print("dir:\t(n/a)")
    print(f"webhook:\t{'set' if webhook else '(none)'}")

    if focus and tmux.has_session(focus):
        last = tmux.pane_last_activity(focus, "main")
        now = int(time.time())
        delta = now - last if last > 0 else 0
        print(f"idle_seconds:\t{delta}")
    else:
        print("idle_seconds:\t(n/a)")
    return 0


def _resolve_session(explicit: str | None) -> str | None:
    return explicit or (config.get_focus() or None)


def cmd_say(args: argparse.Namespace) -> int:
    session = _resolve_session(args.session)
    if not session:
        print("no focused session; run: oc focus <name>")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1
    tmux.send_keys(f"{session}:main", [args.text, "Enter"])
    print(f"sent: {session}\t{args.text}")
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    session = _resolve_session(args.session)
    if not session:
        print("no focused session; run: oc focus <name>")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1
    tmux.send_keys(f"{session}:main", ["Enter"])
    print(f"enter: {session}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    if not tmux.has_session(args.name):
        print(f"session not found: {args.name}")
        return 1
    config.set_focus(args.name)
    tmux.attach(args.name)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    # Watch focused session by default
    session = args.name or config.get_focus()
    if not session:
        print("no session provided and nothing focused")
        return 1
    if not tmux.has_session(session):
        print(f"session not found: {session}")
        return 1

    last = tmux.pane_last_activity(session, "main")
    now = int(time.time())
    delta = now - last if last > 0 else 0

    if delta >= args.idle_seconds:
        title = "OpenCode waiting?"
        body = f"tmux:{session} idle for {delta}s (might be awaiting input)"
        mac_notify(title, body)
        discord_webhook(config.get_webhook(), f"**{title}**\n{body}")
        print(f"notified: {session}\tidle={delta}s")
    else:
        print(f"ok: {session}\tidle={delta}s")
    return 0


def cmd_set_webhook(args: argparse.Namespace) -> int:
    config.set_webhook(args.url)
    print("webhook set" if args.url else "webhook cleared")
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    intent = parse_voice(args.phrase)

    # Voice-first decision:
    # - "attach/open/go to" => focus-only (Shortcuts SSH is non-interactive).
    # - interactive attach happens in Termius when you want the live screen.
    if intent.action == "status":
        return cmd_status(args)
    if intent.action == "ls":
        return cmd_ls(args)
    if intent.action == "new":
        return cmd_new(argparse.Namespace(name=intent.session))
    if intent.action == "focus":
        return cmd_focus(argparse.Namespace(name=intent.session))
    if intent.action == "attach_or_focus":
        return cmd_focus(argparse.Namespace(name=intent.session))
    if intent.action == "enter":
        return cmd_enter(argparse.Namespace(session=None))
    if intent.action == "say":
        return cmd_say(argparse.Namespace(session=intent.session, text=intent.text or ""))
    print("unhandled intent")
    return 2


def cmd_completion(args: argparse.Namespace) -> int:
    shell = args.shell.lower()
    if shell == "bash":
        print(_bash_completion_script())
        return 0
    if shell == "zsh":
        print(_zsh_completion_script())
        return 0
    if shell == "fish":
        print(_fish_completion_script())
        return 0

    print(f"unsupported shell: {shell}")
    return 2


def _bash_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """# occtl bash completion
_occtl_complete() {
  local cur prev
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"

  if [[ $COMP_CWORD -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{cmds}" -- "$cur") )
    return 0
  fi

  case "$prev" in
    watch)
      COMPREPLY+=( $(compgen -W "--name --idle-seconds" -- "$cur") )
      ;;
    set-webhook)
      return 0
      ;;
    completion)
      COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
      ;;
  esac
}

complete -F _occtl_complete oc
    """
    return template.replace("{cmds}", cmds)


def _zsh_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """#compdef oc

_occtl() {
  local -a commands
  commands=(
    __CMD_LIST__
  )

  _arguments -C \
    '1: :->command' \
    '*: :->args'

  case "$state" in
    command)
      compadd -a commands
      ;;
  esac
}

compdef _occtl oc
    """
    return template.replace("__CMD_LIST__", cmds)


def _fish_completion_script() -> str:
    cmds = _fmt_cmds_for_shell(COMMANDS)
    template = """# occtl fish completion
complete -c oc -f
complete -c oc -n '__fish_use_subcommand' -a "{cmds}"
complete -c oc -n "__fish_seen_subcommand_from watch" -l name -r
complete -c oc -n "__fish_seen_subcommand_from watch" -l idle-seconds -r
complete -c oc -n "__fish_seen_subcommand_from completion" -f -a "bash zsh fish"
    """
    return template.replace("{cmds}", cmds)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oc", description="occtl — tmux + OpenCode command center")
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("map", help="map session name to directory")
    sp.add_argument("name")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_map)

    sp = sub.add_parser("maps", help="list mappings")
    sp.set_defaults(fn=cmd_maps)

    sp = sub.add_parser("new", help="create session and start opencode (focuses)")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("ensure", help="create if missing, then focus")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_ensure)

    sp = sub.add_parser("ls", help="list tmux sessions")
    sp.set_defaults(fn=cmd_ls)

    sp = sub.add_parser("focus", help="set focused session")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_focus)

    sp = sub.add_parser("focused", help="print focused session")
    sp.set_defaults(fn=cmd_focused)

    sp = sub.add_parser("status", help="show focus + mapping + idle seconds")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("say", help="send text to OpenCode (focused session by default)")
    sp.add_argument("text", nargs="+")
    sp.add_argument("--session", default=None)
    sp.set_defaults(
        fn=lambda a: cmd_say(argparse.Namespace(session=a.session, text=" ".join(a.text)))
    )

    sp = sub.add_parser("enter", help="send Enter (focused session by default)")
    sp.add_argument("--session", default=None)
    sp.set_defaults(fn=cmd_enter)

    sp = sub.add_parser("attach", help="attach to a session interactively")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_attach)

    sp = sub.add_parser("watch", help="idle-based waiting alert (focused session by default)")
    sp.add_argument("--name", default=None)
    sp.add_argument("--idle-seconds", type=int, default=90)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("set-webhook", help="set Discord webhook URL for alerts (optional)")
    sp.add_argument("url")
    sp.set_defaults(fn=cmd_set_webhook)

    sp = sub.add_parser("voice", help="parse a voice phrase and execute (Shortcuts)")
    sp.add_argument("phrase", nargs="+")
    sp.set_defaults(fn=lambda a: cmd_voice(argparse.Namespace(phrase=" ".join(a.phrase))))

    sp = sub.add_parser("completion", help="print shell completion script")
    sp.add_argument("shell", choices=("bash", "zsh", "fish"))
    sp.set_defaults(fn=cmd_completion)

    return p


def main() -> None:
    config.ensure_config_dir()
    parser = build_parser()
    parser.set_defaults(fn=cmd_status)
    args = parser.parse_args()
    rc = args.fn(args)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
