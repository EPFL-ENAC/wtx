"""The wtx command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    __version__,
    context,
    doctor,
    envfile,
    git,
    guard,
    machine,
    monitor,
    notify,
    orchestrate,
    repos,
    tmux,
    wt,
)
from . import (
    config as config_mod,
)
from . import (
    init as init_mod,
)
from . import (
    land as land_mod,
)
from . import (
    setup as setup_mod,
)
from . import (
    teardown as teardown_mod,
)
from .agents import base as agents
from .agents.base import PROMPT_FILE
from .hooks import GO_AGENT_ENV, GO_DRIVING_ENV, GO_LLM_ENV, run_hook
from .proc import CommandError, is_dry_run, say, set_dry_run, warn


class UserError(Exception):
    """Something the user can fix. Printed without a traceback."""


# ---------------------------------------------------------------------------
# helpers


def _main_checkout(cwd: Path | None = None) -> Path:
    here = (cwd or Path.cwd()).resolve()
    main = git.main_checkout(here)
    if main is None:
        raise UserError(f"{here} is not inside a git repository")
    return main


def _load_cfg(main: Path) -> config_mod.WtxConfig:
    """The repo's config, or a message the user can act on."""
    path = main / config_mod.CONFIG_NAME
    if not path.is_file():
        raise UserError(f"no {config_mod.CONFIG_NAME} in {main}. Run `wtx init` there first.")
    try:
        return config_mod.load(path)
    except config_mod.ConfigError as exc:
        raise UserError(str(exc)) from exc


def _load(cwd: Path | None = None) -> context.Ctx:
    try:
        return context.load(cwd=cwd)
    except context.ContextError as exc:
        raise UserError(str(exc)) from exc


def _require_valid(cfg: config_mod.WtxConfig) -> None:
    """Refuse to build a worktree from a config with problems.

    Setting one up wrong is worse than not setting it up: it writes permissions,
    installs a guard and starts servers from values nobody checked.
    """
    problems = config_mod.validate(cfg)
    if problems:
        joined = "\n  - ".join(problems)
        raise UserError(
            f"{config_mod.CONFIG_NAME} has problems, fix them first "
            f"(wtx config validate):\n  - {joined}"
        )


def _write_prompt(path: Path, prompt: str, cfg: config_mod.WtxConfig) -> None:
    """A brief is a file at the checkout root. The agent pane starts on it.

    With orchestration on, the planner is also told to size the plan, so wtx
    knows which model to hand it to.
    """
    candidate = Path(prompt).expanduser()
    text = candidate.read_text() if candidate.is_file() else prompt
    text = text.rstrip() + "\n"
    if cfg.agent.orchestration.enabled:
        text += orchestrate.plan_instruction()
    (path / PROMPT_FILE).write_text(text)


# ---------------------------------------------------------------------------
# commands


def cmd_init(args: argparse.Namespace) -> int:
    main = _main_checkout()
    if args.from_json:
        answers = init_mod.load_answers(Path(args.from_json))
    else:
        answers = init_mod.scan(main)
        if args.lab:
            answers["repo"]["lab"] = args.lab
    if args.scan:
        answers.pop("_notes", None) if args.no_notes else None
        print(json.dumps(answers, indent=2))
        return 0
    answers.pop("_notes", None)
    try:
        written = init_mod.write_all(main, answers, force=args.force)
    except FileExistsError as exc:
        raise UserError(str(exc)) from exc
    for path in written:
        say(f"wrote {path}")
    print()
    init_mod.print_next_steps(answers)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    main = _main_checkout()
    path = main / config_mod.CONFIG_NAME
    cfg = _load_cfg(main)
    problems = config_mod.validate(cfg)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 1 if problems else 0
    if not problems:
        say(f"{path} is valid")
        return 0
    for p in problems:
        warn(p)
    return 1


def cmd_go(args: argparse.Namespace) -> int:
    main = _main_checkout()
    cfg = _load_cfg(main)
    _require_valid(cfg)

    if args.branch == "all":
        return _go_all(main, cfg)

    branch = args.branch
    if branch in cfg.repo.protected_branches:
        raise UserError(
            f"{branch} is protected. It stays in the main checkout, "
            "worktrees are for feature branches."
        )
    if not wt.available():
        raise UserError("wt is not installed. See `wtx doctor`.")

    base = args.base or f"origin/{cfg.repo.base_branch}"
    if base.startswith("origin/"):
        git.fetch(main)

    with_repos = repos.parse_with(args.with_repo or [])
    # The hook runs in a fresh process, so what wtx go decided travels in the
    # environment, the way wt passes everything else to its hooks. Under names
    # no pane exports: see hooks.py.
    for key in (repos.WITH_ENV, GO_AGENT_ENV, GO_LLM_ENV):
        os.environ.pop(key, None)
    if with_repos:
        os.environ[repos.WITH_ENV] = repos.encode_with(with_repos)
    if args.agent:
        os.environ[GO_AGENT_ENV] = args.agent
    if args.llm:
        os.environ[GO_LLM_ENV] = args.llm
    os.environ[GO_DRIVING_ENV] = "1"

    try:
        path, _existed = wt.ensure(main, branch, base)
    except wt.WtError as exc:
        raise UserError(str(exc)) from exc
    # The hook has run. Drop what was meant for it: a tmux server started
    # below would inherit these, and every later pane would pass them on.
    for key in (repos.WITH_ENV, GO_AGENT_ENV, GO_LLM_ENV, GO_DRIVING_ENV):
        os.environ.pop(key, None)
    if path is None:
        if is_dry_run():
            say(f"dry run: the worktree for {branch} would be created and set up")
            return 0
        raise UserError(f"could not find the worktree for {branch} after creating it")

    ctx = context.for_worktree(main, path, branch, cfg)
    # A worktree that was already there did not fire post_create, and setup is
    # idempotent, so run it either way. This is also what applies --with and
    # --llm to a worktree that exists.
    setup_mod.run_setup(
        ctx,
        with_repos=with_repos,
        agent_tool=args.agent,
        llm=args.llm,
        # The session is created once, below. On a brand new worktree the
        # post_create hook already made it and this call is a no-op re-run.
        start_tmux=False,
        attach=False,
    )

    if args.prompt:
        if args.llm and cfg.agent.orchestration.enabled:
            orch = cfg.agent.orchestration
            warn(
                f"--llm {args.llm} names the model for a plain session. This brief "
                f"plans on {orch.plan_model} and implements on {orch.build_model}: "
                "see [agent.orchestration]"
            )
        _write_prompt(path, args.prompt, cfg)

    ctx.reload_env()
    resolved = repos.resolve_all(ctx)
    agent = agents.get(ctx.agent_tool)
    tmux.ensure_session(
        ctx,
        agent,
        resolved,
        attach_after=not args.no_attach,
        brief=bool(args.prompt),
    )
    return 0


def _go_all(main: Path, cfg: config_mod.WtxConfig) -> int:
    """Bring every worktree's session back, detached. After a reboot."""
    count = 0
    for info in git.list_worktrees(main):
        if info.path.resolve() == main.resolve():
            continue
        if not (info.path / envfile.ENV_FILE).is_file():
            continue
        ctx = context.for_worktree(main, info.path, info.branch, cfg)
        resolved = repos.resolve_all(ctx)
        try:
            agent = agents.get(ctx.agent_tool)
        except KeyError as exc:
            warn(str(exc))
            continue
        tmux.ensure_session(ctx, agent, resolved, attach_after=False)
        count += 1
    say(f"{count} session(s) up")
    if tmux.available() and count:
        from .proc import run

        run(["tmux", "choose-tree", "-wZ", "-O", "name"], check=False)
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    main = _main_checkout()
    cfg = _load_cfg(main)

    if args.path:
        target = Path(args.path).resolve()
        branch = git.current_branch(target)
    else:
        branch = args.branch or git.current_branch(Path.cwd())
        target = git.worktree_path_for(main, branch)
    if not branch:
        raise UserError("no branch given and none could be worked out from here")
    if branch in cfg.repo.protected_branches:
        raise UserError(f"{branch} is protected, there is no worktree to close")
    if target is None:
        raise UserError(f"no worktree for {branch}")

    session = context.session_name(cfg.repo.name or git.repo_name(main), branch)
    inside = (
        not args.from_tmux
        and os.environ.get("TMUX")
        and tmux.available()
        and tmux._tmux_out(["display-message", "-p", "#{session_name}"]) == session
    )
    if inside:
        # Removing the worktree kills the shell running this command. Hand the
        # job to the tmux server so it outlives the session.
        say("handing off to the tmux server, this session is about to close")
        force = " --force" if args.force else ""
        tmux._tmux(
            [
                "run-shell",
                "-b",
                f"wtx done --path '{target}' --from-tmux{force}",
            ]
        )
        return 0

    if not args.force and not git.is_clean(target):
        raise UserError(
            f"{target} has uncommitted changes. Commit them, or pass --force to "
            "discard them and remove the worktree."
        )
    try:
        wt.remove(main, branch, force=args.force)
    except wt.WtError as exc:
        raise UserError(str(exc)) from exc
    say(f"closed {branch}, the branch is kept")
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    ctx = _load()
    branch = args.branch or git.current_branch(Path.cwd())
    try:
        land_mod.land(
            ctx,
            branch,
            local=args.local,
            skip_checks=args.skip_checks,
            keep_branch=args.keep_branch,
        )
    except land_mod.LandError as exc:
        raise UserError(str(exc)) from exc
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    ctx = _load()
    if args.branch:
        path = git.worktree_path_for(ctx.main, args.branch)
        if path is None:
            raise UserError(f"no worktree for {args.branch}")
        ctx = context.for_worktree(ctx.main, path, args.branch, ctx.cfg)
    families = [f.name for f in ctx.cfg.ports.families]
    name = args.family or (families[0] if families else "")
    if name not in families:
        raise UserError(f"no port family called {name!r}. Known: {', '.join(families)}")
    port = ctx.port(name)
    if port is None:
        raise UserError(f"no port for {name} in this checkout")
    url = f"http://localhost:{port}/"
    print(url)
    if not args.print_only:
        from .proc import run

        run(["xdg-open", url], check=False, quiet=True)
    return 0


def cmd_curl(args: argparse.Namespace) -> int:
    """Reach one of this worktree's own servers.

    The agent cannot know its ports, and the classifier rules that stop a POST
    to the internet also stop a POST to its own backend: an ask rule beats every
    allow rule. This builds the URL itself, so it can only ever reach a port
    this checkout owns, which is why the baseline allows it with any flags.
    """
    ctx = _load()
    families = [f.name for f in ctx.cfg.ports.families]
    if not families:
        raise UserError(f"no [[ports.family]] in {config_mod.CONFIG_NAME}")
    name = args.family or families[0]
    if name not in families:
        raise UserError(f"no port family called {name!r}. Known: {', '.join(families)}")
    port = ctx.port(name)
    if port is None:
        raise UserError(f"no port for {name} in this checkout, run `wtx setup`")
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    from .proc import run

    return run(["curl", *args.args, f"http://127.0.0.1:{port}{path}"], check=False)


def cmd_status(args: argparse.Namespace) -> int:
    main = _main_checkout()
    cfg = _load_cfg(main)
    states = notify.all_states()
    rows = []
    for info in git.list_worktrees(main):
        is_main = info.path.resolve() == main.resolve()
        if args.branch and info.branch != args.branch:
            continue
        values = envfile.read_worktree(info.path)
        session = context.session_name(cfg.repo.name or git.repo_name(main), info.branch)
        rows.append(
            {
                "branch": info.branch,
                "path": str(info.path),
                "main": is_main,
                "session": session,
                "live": tmux.available() and tmux.has_session(session),
                "state": states.get(session, {}).get("state", ""),
                "ports": {
                    f.name: values.get(f.env_key) or (str(f.main) if is_main else "")
                    for f in cfg.ports.families
                },
                "agent": values.get("WTX_AGENT", cfg.agent.tool),
                "llm": values.get("WTX_LLM", cfg.agent.llm),
                "handoff": orchestrate.pending(session),
                "repos": {
                    spec.name: {
                        "path": values.get(spec.path_key, ""),
                        "branch": values.get(spec.branch_key, ""),
                    }
                    for spec in cfg.repos
                },
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        tag = " (main)" if row["main"] else ""
        live = "up" if row["live"] else "down"
        state = f" {row['state']}" if row["state"] else ""
        ports = " ".join(f"{k}={v}" for k, v in row["ports"].items() if v)
        print(f"{row['branch']}{tag}  [{live}{state}]  {ports}")
        handoff = row["handoff"]
        if handoff:
            print(
                f"    handoff pending: {handoff.get('size', '')} plan "
                f"-> {handoff.get('model', '')}"
            )
        for name, info in row["repos"].items():
            if info["branch"]:
                print(f"    {name}: {info['branch']} at {info['path']}")
            elif info["path"]:
                print(f"    {name}: read only, {info['path']}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else Path.cwd()
    ctx = _load(cwd=root)
    _require_valid(ctx.cfg)
    setup_mod.run_setup(
        ctx,
        with_repos=repos.parse_with(args.with_repo or []),
        agent_tool=args.agent,
        llm=args.llm,
        start_tmux=not args.no_tmux,
        attach=False,
    )
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else Path.cwd()
    teardown_mod.run_teardown(_load(cwd=root))
    return 0


def cmd_tmux(args: argparse.Namespace) -> int:
    ctx = _load()
    resolved = repos.resolve_all(ctx)
    agent = agents.get(ctx.agent_tool)
    tmux.ensure_session(
        ctx, agent, resolved, attach_after=not args.no_attach, brief=args.brief
    )
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    main = _main_checkout()
    cfg = _load_cfg(main)
    branch = args.branch or git.current_branch(Path.cwd())
    path = git.worktree_path_for(main, branch)
    if path is None:
        raise UserError(f"no worktree for {branch}")
    _write_prompt(path, args.prompt, cfg)
    ctx = context.for_worktree(main, path, branch, cfg)
    resolved = repos.resolve_all(ctx)
    agent = agents.get(ctx.agent_tool)
    if not tmux.has_session(ctx.session):
        tmux.ensure_session(ctx, agent, resolved, attach_after=False, brief=True)
    else:
        tmux.send_brief(ctx, agent, tmux.render_ctx(ctx, resolved, phase="plan"))
    say(f"brief sent to {ctx.session}")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return run_hook(args.event)


def cmd_notify(args: argparse.Namespace) -> int:
    return notify.handle(args.state)


def cmd_handoff(args: argparse.Namespace) -> int:
    """PostToolUse on ExitPlanMode: hand an accepted plan to its model."""
    answer = orchestrate.capture(notify.payload_from_stdin())
    if answer:
        print(answer)
    return 0


def cmd_tmux_status(args: argparse.Namespace) -> int:
    line = notify.status_line()
    if line:
        print(line)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    if args.serve:
        return monitor.serve(interval=args.interval)
    return monitor.open_board(grid=args.grid, interval=args.interval)


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor.run()
    print(doctor.as_json(report) if args.json else doctor.render(report))
    return 1 if report.failed else 0


def cmd_install_machine(args: argparse.Namespace) -> int:
    tool = args.agent or "claude"
    if args.apply:
        machine.apply(tool)
        return 0
    print(machine.show(tool))
    return 0


def cmd_shell_init(args: argparse.Namespace) -> int:
    from importlib import resources

    print(resources.files("wtx.shell").joinpath("wtx.bash").read_text())
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    main = _main_checkout()
    cfg = _load_cfg(main)
    if args.show:
        print(guard.render_hook(cfg))
        return 0
    ok = guard.install(cfg, main)
    if ok:
        say(f"guard installed in {guard.hooks_dir(main)}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wtx",
        description="Every branch gets its own worktree, ports, tmux session and coding agent.",
    )
    p.add_argument("--version", action="version", version=f"wtx {__version__}")
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the commands that change things, run none of them",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write wtx.toml and .wt.toml for this repo")
    s.add_argument("--lab", default="", help="EPFL lab short name, fills {lab} in paths")
    s.add_argument("--scan", action="store_true", help="print the guesses as JSON, write nothing")
    s.add_argument("--no-notes", action="store_true", help="with --scan, drop the _notes block")
    s.add_argument("--from-json", default="", metavar="FILE", help="write from answers ('-' for stdin)")
    s.add_argument("--force", action="store_true", help="replace an existing wtx.toml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("config", help="check wtx.toml")
    s.add_argument("action", nargs="?", default="validate", choices=["validate"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("go", help="branch, worktree, session, agent. Or 'all'.")
    s.add_argument("branch")
    s.add_argument("base", nargs="?", default="", help="default origin/<base_branch>")
    s.add_argument("--prompt", default="", help="a brief: a file, or the text itself")
    s.add_argument("--no-attach", action="store_true")
    s.add_argument("--agent", default="", choices=["", "claude", "opencode"])
    s.add_argument("--llm", default="", metavar="MODEL", help="model for this worktree")
    s.add_argument(
        "--with",
        dest="with_repo",
        action="append",
        metavar="NAME[=BRANCH]",
        help="pair an external repo: its own worktree on BRANCH, or on a branch named like this one",
    )
    s.set_defaults(func=cmd_go)

    s = sub.add_parser("all", help="bring every worktree's session back, detached")
    s.set_defaults(func=lambda a: cmd_go(argparse.Namespace(branch="all", base="", prompt="", no_attach=True, agent="", llm="", with_repo=None)))

    s = sub.add_parser("done", help="close a worktree, keep the branch")
    s.add_argument("branch", nargs="?", default="")
    s.add_argument("--path", default="", help="the worktree directory instead of a branch")
    s.add_argument("--force", action="store_true", help="discard uncommitted changes")
    s.add_argument("--from-tmux", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("land", help="rebase, pull request, squash merge, clean up")
    s.add_argument("branch", nargs="?", default="")
    s.add_argument("--local", action="store_true", help="merge locally instead of a PR")
    s.add_argument("--skip-checks", action="store_true")
    s.add_argument("--keep-branch", action="store_true")
    s.set_defaults(func=cmd_land)

    s = sub.add_parser("open", help="print and open a dev server URL")
    s.add_argument("family", nargs="?", default="")
    s.add_argument("branch", nargs="?", default="")
    s.add_argument("--print-only", action="store_true")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser(
        "curl",
        help="curl one of this worktree's servers, without knowing its port",
    )
    s.add_argument("family", nargs="?", default="", help="default: the first family")
    s.add_argument("path", nargs="?", default="/")
    s.add_argument(
        "args", nargs=argparse.REMAINDER, help="passed to curl as it stands"
    )
    s.set_defaults(func=cmd_curl)

    s = sub.add_parser("status", help="ports, sessions and pairings")
    s.add_argument("branch", nargs="?", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("setup", help="make a worktree runnable, again")
    s.add_argument("--path", default="")
    s.add_argument("--no-tmux", action="store_true")
    s.add_argument("--agent", default="", choices=["", "claude", "opencode"])
    s.add_argument("--llm", default="")
    s.add_argument("--with", dest="with_repo", action="append", metavar="NAME[=BRANCH]")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("teardown", help="kill the session and free the ports")
    s.add_argument("--path", default="")
    s.set_defaults(func=cmd_teardown)

    s = sub.add_parser("tmux", help="create or attach this checkout's session")
    s.add_argument("--no-attach", action="store_true")
    s.add_argument("--brief", action="store_true")
    s.set_defaults(func=cmd_tmux)

    s = sub.add_parser("brief", help="send a prompt to a worktree's agent pane")
    s.add_argument("branch", nargs="?", default="")
    s.add_argument("--prompt", required=True)
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("hook", help="called by .wt.toml")
    s.add_argument("event", choices=["post-create", "post-checkout", "pre-remove"])
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("notify", help="called by the agent's hooks")
    s.add_argument("state", choices=list(notify.STATES))
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("handoff", help="called by the agent's ExitPlanMode hook")
    s.set_defaults(func=cmd_handoff)

    s = sub.add_parser("tmux-status", help="one line for tmux status-right")
    s.set_defaults(func=cmd_tmux_status)

    s = sub.add_parser("monitor", help="every session on one screen")
    s.add_argument("--grid", action="store_true", help="also tile read-only views")
    s.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--interval", type=float, default=2.0)
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("doctor", help="check this machine")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("install-machine", help="the shell, tmux and agent hook setup")
    s.add_argument("--apply", action="store_true", help="make the changes, with backups")
    s.add_argument("--agent", default="", choices=["", "claude", "opencode"])
    s.set_defaults(func=cmd_install_machine)

    s = sub.add_parser("shell-init", help="print the shell functions to source")
    s.add_argument("shell", nargs="?", default="bash", choices=["bash"])
    s.set_defaults(func=cmd_shell_init)

    s = sub.add_parser("guard", help="install or show the push guard")
    s.add_argument("--show", action="store_true", help="print the hook, install nothing")
    s.set_defaults(func=cmd_guard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # `wtx` and `wtx help [command]` print help. argparse alone answers both
    # with an error, which is a poor first contact.
    if not argv:
        parser.print_help()
        return 0
    if argv[0] == "help":
        parser.parse_args([*argv[1:], "--help"] if argv[1:] else ["--help"])
        return 0
    args = parser.parse_args(argv)
    set_dry_run(bool(getattr(args, "dry_run", False)))
    try:
        return args.func(args)
    except (
        UserError,
        setup_mod.SetupError,
        context.ContextError,
        config_mod.ConfigError,
        wt.WtError,
    ) as exc:
        print(f"wtx: {exc}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(f"wtx: {exc}", file=sys.stderr)
        if exc.output:
            print(exc.output, file=sys.stderr)
        return exc.code or 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
