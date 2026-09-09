"""`wtx init`: look at a repo, guess a wtx.toml, write the files.

The scan is a proposal, never the last word. On the terminal it prints the draft
and asks. Under `--scan --json` it hands the same guesses to the /wtx-init skill,
which asks the human with real options and sends the answers back through
`--from-json`. Either way this module is the only thing that writes the files.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from . import git
from .config import CONFIG_NAME, SCHEMA_VERSION
from .proc import capture, say, warn

WT_TOML = ".wt.toml"

WT_TOML_BODY = """# Hooks for the wt worktree manager (https://github.com/timvw/wt).
# wt exports WT_PATH and WT_BRANCH to every hook. It also exports WT_MAIN, which
# wtx ignores: wt calls "main" whichever worktree holds the default branch.
# Each hook is a list, wt does not run a plain string.
# All the logic lives in wtx, so an upgrade reaches every repo at once.
[hooks]
post_create = ["wtx hook post-create"]
post_checkout = ["wtx hook post-checkout"]
pre_remove = ["wtx hook pre-remove"]
"""

GITIGNORE_LINES = [
    ".env.worktree",
    ".wt-logs/",
    "PROMPT.md",
    "PROMPT.sent.md",
    ".wt-prompts/",
    ".claude/settings.local.json",
    ".claude/agents/Explore.md",
    ".claude/worktrees/",
    "opencode.json",
]

GITIGNORE_HEADER = "# wtx: per-worktree files, local to one checkout"

K8S_ROOT = Path("~/code/enack8s-app-config").expanduser()

CLAUDE_SECTION = """## Dev servers and worktrees

Every branch has its own git worktree, tmux session and agent. The session is
`{repo}/<branch>` and its panes are {panes}.

- Ports are per worktree and live in `.env.worktree`, which every pane exports.
  Never hardcode a port, read {port_keys} from the environment.
- The dev servers are already running in their own panes. Do not start them
  again. Read `.wt-logs/*.log` to see what they are doing, the sandbox cannot
  reach the tmux socket.
- Read-only curl to localhost is allowed, that is how you check the servers.
- Work on this branch only. Never push {protected}. When the work is ready, say
  so and a human runs `wtx land <branch>` from the main checkout.
- A pre-push hook enforces this. If it refuses a push, that is the design, not a
  bug to work around.
"""

CLAUDE_REPOS_SECTION = """
### Directories outside this repo

{lines}

A `read` directory is yours to read as much as you like, with no prompt, and you
may never write in it. To change one, ask for a paired worktree instead: a human
runs `wtx go <branch> --with <name>=<branch>`, and the change lands through that
repo's own pull request.
"""


# ---------------------------------------------------------------------------
# scanning


def _has(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "package-lock.json").is_file():
        return "npm"
    if (root / "package.json").is_file():
        return "npm"
    return ""


def _install_cmd(manager: str) -> str:
    return {
        "pnpm": "pnpm install --frozen-lockfile",
        "yarn": "yarn install --frozen-lockfile",
        "npm": "npm ci",
    }.get(manager, "")


def _guess_port(root: Path, kind: str) -> int:
    """Find the port a dev server uses today, so the default keeps working."""
    patterns = {
        "frontend": [
            (Path("frontend/quasar.config.ts"), r"port:\s*(?:Number\([^)]*\)\s*\|\|\s*)?(\d{4,5})"),
            (Path("frontend/vite.config.ts"), r"port:\s*(?:Number\([^)]*\)\s*\|\|\s*)?(\d{4,5})"),
            (Path("vite.config.ts"), r"port:\s*(?:Number\([^)]*\)\s*\|\|\s*)?(\d{4,5})"),
            (Path("quasar.config.ts"), r"port:\s*(?:Number\([^)]*\)\s*\|\|\s*)?(\d{4,5})"),
        ],
        "backend": [
            (Path("backend/Makefile"), r"--port[= ]\$?\(?[A-Z_]*\)?\s*(\d{4,5})|PORT\s*\?=\s*(\d{4,5})"),
            (Path("Makefile"), r"BACKEND_PORT\s*\?=\s*(\d{4,5})"),
        ],
    }
    for rel, pattern in patterns.get(kind, []):
        f = root / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        m = re.search(pattern, text)
        for group in m.groups() if m else ():
            if group:
                return int(group)
    return {"frontend": 5173, "backend": 8000}.get(kind, 8000)


def _workflow_branches(root: Path) -> list[str]:
    """Branches a workflow deploys from. Those must not be pushed from a worktree."""
    found: list[str] = []
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return found
    for f in sorted(wf.glob("*.y*ml")):
        try:
            text = f.read_text()
        except OSError:
            continue
        head = text.split("jobs:", 1)[0]
        for name in re.findall(r"^\s*-\s*([A-Za-z0-9._/-]+)\s*$", head, re.MULTILINE):
            known = ("main", "master", "dev", "develop", "stage", "staging", "prod")
            if name in known and name not in found:
                found.append(name)
    return found


def _publishes_tags(root: Path) -> bool:
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return False
    for f in wf.glob("*.y*ml"):
        try:
            text = f.read_text()
        except OSError:
            continue
        if re.search(r"tags:\s*\n\s*-\s*['\"]?v", text):
            return True
    return False


def _npm_scripts(root: Path, sub: str = "") -> dict[str, str]:
    pkg = _read_json(root / sub / "package.json") if sub else _read_json(root / "package.json")
    scripts = pkg.get("scripts", {})
    return scripts if isinstance(scripts, dict) else {}


def _make_targets(root: Path, rel: str = "Makefile") -> list[str]:
    f = root / rel
    if not f.is_file():
        return []
    try:
        text = f.read_text()
    except OSError:
        return []
    return sorted(set(re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", text, re.MULTILINE)))


def _repo_mentions(main: Path) -> str:
    """The text of the files that would name a sibling repo this one depends on."""
    parts: list[str] = []
    for rel in (
        "pyproject.toml",
        "package.json",
        "Makefile",
        "backend/Makefile",
        "backend/pyproject.toml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "README.md",
        "CLAUDE.md",
        ".env",
    ):
        f = main / rel
        if f.is_file():
            try:
                parts.append(f.read_text())
            except OSError:
                continue
    return "\n".join(parts)


def _sibling_repos(main: Path) -> list[dict[str, str]]:
    """Git repos next to this one that this one actually refers to.

    A directory listing alone would offer every checkout on the machine. A
    sibling is only worth proposing when the repo names it somewhere: a path in
    a Makefile, a dependency, a line in the README.
    """
    out: list[dict[str, str]] = []
    parent = main.parent
    mentions = _repo_mentions(main)
    try:
        entries = sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return out
    for p in entries:
        if p.resolve() == main.resolve() or not (p / ".git").exists():
            continue
        if p.name not in mentions:
            continue
        is_python = (p / "pyproject.toml").is_file() or (p / "setup.py").is_file()
        out.append(
            {
                "name": p.name,
                "path": f"../{p.name}",
                "kind": "python-package" if is_python else "data",
            }
        )
    return out


def _k8s_entry(repo_name: str, dir_name: str = "") -> dict[str, str]:
    """The GitOps config folder for this repo, if there is one.

    The folder is <lab>/<repo>, and the repo is often named after the lab too
    (leure-speed-to-zero under epfl-leure/speed-to-zero), so the lab prefix is
    tried as well as the plain name.
    """
    if not K8S_ROOT.is_dir():
        return {}
    try:
        labs = sorted(p for p in K8S_ROOT.iterdir() if p.is_dir())
    except OSError:
        return {}
    for lab in labs:
        lab_short = lab.name.split("-", 1)[1] if "-" in lab.name else lab.name
        candidates = [repo_name, dir_name, repo_name.removeprefix(f"{lab_short}-")]
        for candidate in candidates:
            if not candidate:
                continue
            target = lab / candidate
            if not target.is_dir():
                continue
            # {repo} in the path means [repo].name, so only use the template when
            # the folder really is named after it.
            path = (
                "~/code/enack8s-app-config/epfl-{lab}/{repo}"
                if candidate == repo_name
                else f"~/code/enack8s-app-config/epfl-{{lab}}/{candidate}"
            )
            return {
                "name": "k8s",
                "lab": lab_short,
                "path": path,
                "found": str(target),
            }
    return {}


def scan(main: Path) -> dict[str, Any]:
    """Everything wtx can work out on its own. Nothing is written."""
    name = git.repo_name(main)
    branches = capture(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
        cwd=main,
    ).splitlines()
    remote = {b.removeprefix("origin/") for b in branches}
    base = "dev" if "dev" in remote else ("main" if "main" in remote else git.current_branch(main))
    protected = [base] + [b for b in _workflow_branches(main) if b != base]
    if "main" in remote and "main" not in protected:
        protected.append("main")

    has_backend = (main / "backend").is_dir()
    has_frontend = (main / "frontend").is_dir()
    manager = _package_manager(main / "frontend") if has_frontend else _package_manager(main)
    root_manager = _package_manager(main)
    uses_uv = _has(main, "uv.lock") or _has(main / "backend", "uv.lock")

    families = []
    if has_backend or (main / "pyproject.toml").is_file():
        families.append({"name": "backend", "main": _guess_port(main, "backend")})
    if has_frontend or (main / "package.json").is_file():
        families.append({"name": "frontend", "main": _guess_port(main, "frontend")})

    deps_steps = []
    if root_manager and (main / "package.json").is_file():
        deps_steps.append(
            {"if_missing": "node_modules", "run": _install_cmd(root_manager), "cwd": "."}
        )
    if has_frontend and manager:
        deps_steps.append(
            {
                "if_missing": "frontend/node_modules",
                "run": _install_cmd(manager),
                "cwd": "frontend",
            }
        )
    if uses_uv:
        cwd = "backend" if (main / "backend" / "uv.lock").is_file() else "."
        pin = " --python {python_version}" if (main / ".python-version").is_file() else ""
        deps_steps.append(
            {
                "if_missing": f"{cwd}/.venv" if cwd != "." else ".venv",
                "run": f"uv sync --frozen{pin}",
                "cwd": cwd,
            }
        )

    post_install = []
    if (main / "lefthook.yml").is_file() or (main / "lefthook.yaml").is_file():
        post_install.append("npx --no-install lefthook install")

    panes: list[dict[str, Any]] = [{"name": "agent", "role": "agent"}]
    root_targets = _make_targets(main)
    if has_backend:
        cmd = "make dev" if "dev" in _make_targets(main, "backend/Makefile") else "make run"
        panes.append(
            {"name": "backend", "role": "server", "cwd": "backend", "cmd": cmd, "log": True}
        )
    if has_frontend:
        scripts = _npm_scripts(main, "frontend")
        cmd = f"{manager} run dev" if "dev" in scripts else f"{manager} start"
        panes.append(
            {"name": "frontend", "role": "server", "cwd": "frontend", "cmd": cmd, "log": True}
        )
    panes.append({"name": "shell", "role": "shell"})

    checks_lint, checks_test = [], []
    root_scripts = _npm_scripts(main)
    if "lint" in root_targets:
        checks_lint.append("make lint")
    elif "lint" in root_scripts:
        checks_lint.append(f"{root_manager} run lint")
    if "test" in root_targets:
        checks_test.append("make test")
    elif "test" in root_scripts:
        checks_test.append(f"{root_manager} run test")

    allow = [f"Bash(make {t})" for t in root_targets if t in ("lint", "format", "test", "build")]
    allow += [
        f"Bash({root_manager} run {s})"
        for s in root_scripts
        if s in ("lint", "format", "test", "build", "type-check")
    ]
    if uses_uv:
        allow += ["Bash(uv run ruff *)", "Bash(uv run pytest *)"]

    deny = []
    if _publishes_tags(main):
        deny += ["Bash(git tag *)", "Bash(git push * --tags)"]

    k8s = _k8s_entry(name, main.name)
    ext_repos = []
    if k8s:
        ext_repos.append(
            {"name": "k8s", "path": k8s["path"], "access": "pair", "base_branch": "main"}
        )
    for sib in _sibling_repos(main):
        # The GitOps repo is already covered by the k8s entry above, which points
        # at this repo's folder inside it rather than the whole thing.
        if sib["name"] == K8S_ROOT.name:
            continue
        entry = {
            "name": sib["name"],
            "path": sib["path"],
            "access": "pair" if sib["kind"] == "python-package" else "read",
            "base_branch": "main",
        }
        if sib["kind"] == "python-package" and uses_uv:
            entry["editable_install"] = {
                "cwd": "backend" if has_backend else ".",
                "run": "uv pip install --quiet --editable {path}",
            }
        ext_repos.append(entry)

    return {
        "repo": {
            "name": name,
            "lab": k8s.get("lab", ""),
            "base_branch": base,
            "protected_branches": protected,
        },
        "ports": {"family": families},
        "seed": {"copy": [f for f in ("CLAUDE.md",) if (main / f).is_file()]},
        "deps": {
            "python_version_file": ".python-version"
            if (main / ".python-version").is_file()
            else "",
            "step": deps_steps,
            "post_install": post_install,
        },
        "env": {"extra": {}},
        "panes": {"pane": panes},
        "agent": {
            "tool": "claude",
            "llm": "opus",
            "subagent_model": "sonnet",
            "auto_compact_window": 200000,
            "explore_agent_model": "haiku",
            "disabled_mcp_servers": [],
        },
        "permissions": {"allow": allow, "deny": deny, "allowed_domains": []},
        "checks": {"lint": checks_lint, "test": checks_test},
        "repos": ext_repos,
        "hooks": {"post_setup": "", "pre_teardown": ""},
        "_notes": {
            "package_manager": root_manager or manager,
            "uses_uv": uses_uv,
            "k8s_found": k8s.get("found", ""),
            "siblings": _sibling_repos(main),
            "workflow_branches": _workflow_branches(main),
            "publishes_tags": _publishes_tags(main),
            "make_targets": root_targets,
        },
    }


# ---------------------------------------------------------------------------
# writing


def _v(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_v(x) for x in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_v(x)}" for k, x in value.items()) + " }"
    return json.dumps(str(value))


def render_toml(answers: dict[str, Any]) -> str:
    a = answers
    repo = a.get("repo", {})
    lines: list[str] = [
        "# wtx: every branch gets its own worktree, ports, tmux session and agent.",
        "# Reference for every key: https://github.com/EPFL-ENAC/wtx/blob/main/docs/schema.md",
        f"schema_version = {SCHEMA_VERSION}",
        "",
        "[repo]",
        f"name = {_v(repo.get('name', ''))}",
    ]
    if repo.get("lab"):
        lines.append(f"lab = {_v(repo['lab'])}   # fills {{lab}} in the paths below")
    lines += [
        f"base_branch = {_v(repo.get('base_branch', 'main'))}",
        "# The single source for the push guard, the completion and the deny rules.",
        f"protected_branches = {_v(repo.get('protected_branches', ['main']))}",
        "",
    ]

    families = a.get("ports", {}).get("family", [])
    if families:
        lines += ["# One offset per worktree, one port per family at that offset.", "[ports]"]
        for f in families:
            lines += [
                "",
                "[[ports.family]]",
                f"name = {_v(f['name'])}",
                f"main = {_v(f['main'])}   # what the main checkout keeps",
            ]
            if f.get("env"):
                lines.append(f"env = {_v(f['env'])}")
        lines.append("")

    seed = a.get("seed", {})
    if any(seed.get(k) for k in ("copy", "symlink", "required")):
        lines.append("# Gitignored files a fresh worktree needs. Never overwritten.")
        lines.append("[seed]")
        for key in ("copy", "symlink", "required"):
            if seed.get(key):
                lines.append(f"{key} = {_v(seed[key])}")
        lines.append("")

    deps = a.get("deps", {})
    if deps.get("step") or deps.get("python_version_file") or deps.get("post_install"):
        lines.append("[deps]")
        if deps.get("python_version_file"):
            lines.append(
                f"python_version_file = {_v(deps['python_version_file'])}"
                "   # uv does not look up the tree for it"
            )
        if deps.get("post_install"):
            lines.append(f"post_install = {_v(deps['post_install'])}")
        for step in deps.get("step", []):
            lines += ["", "[[deps.step]]"]
            if step.get("if_missing"):
                lines.append(f"if_missing = {_v(step['if_missing'])}")
            if step.get("cwd", ".") != ".":
                lines.append(f"cwd = {_v(step['cwd'])}")
            lines.append(f"run = {_v(step['run'])}")
        lines.append("")

    env = a.get("env", {})
    if env.get("extra") or env.get("computed"):
        lines.append("[env]")
        if env.get("extra"):
            lines.append(f"extra = {_v(env['extra'])}")
        if env.get("computed"):
            lines.append(
                f"computed = {_v(env['computed'])}"
                "   # written by a hook, wtx carries them over"
            )
        lines.append("")

    panes = a.get("panes", {}).get("pane", [])
    if panes:
        lines += [
            "# The agent pane fills the left half, the rest stack on the right.",
            "[panes]",
        ]
        for p in panes:
            lines += ["", "[[panes.pane]]", f"name = {_v(p['name'])}", f"role = {_v(p.get('role', 'shell'))}"]
            if p.get("cwd", ".") != ".":
                lines.append(f"cwd = {_v(p['cwd'])}")
            if p.get("cmd"):
                lines.append(f"cmd = {_v(p['cmd'])}")
            if p.get("log"):
                lines.append("log = true   # mirrored to .wt-logs/, the agent reads it")
        lines.append("")

    agent = a.get("agent", {})
    lines.append("[agent]")
    for key in (
        "tool",
        "llm",
        "brief_permission_mode",
        "subagent_model",
        "effort",
        "auto_compact_window",
        "explore_agent_model",
    ):
        if agent.get(key) not in (None, "", 0):
            lines.append(f"{key} = {_v(agent[key])}")
    if agent.get("disabled_mcp_servers"):
        lines.append(f"disabled_mcp_servers = {_v(agent['disabled_mcp_servers'])}")
    oc = agent.get("opencode", {})
    if oc:
        lines += ["", "[agent.opencode]"]
        for key, value in oc.items():
            if value:
                lines.append(f"{key} = {_v(value)}")
    lines.append("")

    perms = a.get("permissions", {})
    if any(perms.get(k) for k in ("allow", "ask", "deny", "allowed_domains")):
        lines += [
            "# Added to the baseline wtx ships. A repo can tighten, never loosen.",
            "[permissions]",
        ]
        for key in ("allow", "ask", "deny", "allowed_domains"):
            if perms.get(key):
                lines.append(f"{key} = {_v(perms[key])}")
        lines.append("")

    checks = a.get("checks", {})
    if checks.get("lint") or checks.get("test"):
        lines += ["# Run by `wtx land --local`.", "[checks]"]
        for key in ("lint", "test"):
            if checks.get(key):
                lines.append(f"{key} = {_v(checks[key])}")
        lines.append("")

    for r in a.get("repos", []):
        lines += [
            "[[repos]]",
            f"name = {_v(r['name'])}",
            f"path = {_v(r['path'])}",
            f"access = {_v(r.get('access', 'read'))}"
            + ("   # read free, edits only through a paired worktree" if r.get("access") == "pair" else "   # read free, never writable"),
        ]
        if r.get("base_branch"):
            lines.append(f"base_branch = {_v(r['base_branch'])}")
        if r.get("env_prefix"):
            lines.append(f"env_prefix = {_v(r['env_prefix'])}")
        if r.get("editable_install"):
            lines.append(f"editable_install = {_v(r['editable_install'])}")
        lines.append("")

    hooks = a.get("hooks", {})
    if hooks.get("post_setup") or hooks.get("pre_teardown"):
        lines += ["# This repo's own steps, run by wtx setup and teardown.", "[hooks]"]
        for key in ("post_setup", "pre_teardown"):
            if hooks.get(key):
                lines.append(f"{key} = {_v(hooks[key])}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def update_gitignore(main: Path) -> bool:
    f = main / ".gitignore"
    existing = f.read_text() if f.is_file() else ""
    missing = [line for line in GITIGNORE_LINES if line not in existing.splitlines()]
    if not missing:
        return False
    block = "\n".join([GITIGNORE_HEADER, *missing])
    text = existing.rstrip("\n")
    text = (text + "\n\n" if text else "") + block + "\n"
    f.write_text(text)
    return True


def claude_section(answers: dict[str, Any]) -> str:
    repo = answers.get("repo", {})
    panes = [p["name"] for p in answers.get("panes", {}).get("pane", [])]
    keys = [
        f.get("env") or f"{f['name'].upper()}_PORT"
        for f in answers.get("ports", {}).get("family", [])
    ]
    protected = " and ".join(f"`{b}`" for b in repo.get("protected_branches", []))
    text = CLAUDE_SECTION.format(
        repo=repo.get("name", "the repo"),
        panes=", ".join(f"`{p}`" for p in panes) or "one agent pane",
        port_keys=", ".join(f"`{k}`" for k in keys) or "the port variables",
        protected=protected or "the protected branches",
    )
    ext = answers.get("repos", [])
    if ext:
        lines = []
        for r in ext:
            mode = "pair" if r.get("access") == "pair" else "read"
            lines.append(f"- `{r['name']}` at `{r['path']}`, access `{mode}`.")
        text += CLAUDE_REPOS_SECTION.format(lines="\n".join(lines))
    return text


def write_all(main: Path, answers: dict[str, Any], *, force: bool = False) -> list[Path]:
    written: list[Path] = []
    cfg_path = main / CONFIG_NAME
    if cfg_path.exists() and not force:
        raise FileExistsError(f"{cfg_path} already exists, pass --force to replace it")
    cfg_path.write_text(render_toml(answers))
    written.append(cfg_path)

    wt_path = main / WT_TOML
    if not wt_path.exists() or force:
        wt_path.write_text(WT_TOML_BODY)
        written.append(wt_path)
    elif "wtx hook" not in wt_path.read_text():
        warn(f"{wt_path} exists and does not call wtx. Merge these lines by hand:")
        print(WT_TOML_BODY)

    if update_gitignore(main):
        written.append(main / ".gitignore")
    return written


def app_patches(answers: dict[str, Any]) -> list[str]:
    """What a human still has to change in the app so ports are variables."""
    out: list[str] = []
    for f in answers.get("ports", {}).get("family", []):
        key = f.get("env") or f"{f['name'].upper()}_PORT"
        out.append(
            f"{f['name']}: read the port from ${key}, defaulting to {f['main']}. "
            f"Every place that hardcodes {f['main']} needs it "
            f"(grep -rn '{f['main']}' --exclude-dir=node_modules)."
        )
    families = {f["name"] for f in answers.get("ports", {}).get("family", [])}
    if {"frontend", "backend"} <= families:
        out.append(
            "frontend dev server: proxy /api to http://127.0.0.1:${BACKEND_PORT}, "
            "and set open to false when FRONTEND_PORT is set, so each agent does "
            "not open a browser tab."
        )
    if any(r.get("editable_install") for r in answers.get("repos", [])):
        out.append(
            "backend reload: add the paired repo's source directory to the reloader "
            "so an edit there restarts the server."
        )
    out.append(
        "kill only your own servers: a `pkill -f uvicorn` style cleanup kills every "
        "other worktree's servers too. Kill the process tree you started, or the "
        "port, instead."
    )
    return out


def load_answers(path: Path) -> dict[str, Any]:
    if str(path) == "-":
        import sys

        return json.load(sys.stdin)
    text = path.read_text()
    if path.suffix == ".toml":
        return tomllib.loads(text)
    return json.loads(text)


def print_next_steps(answers: dict[str, Any]) -> None:
    say("next, by hand:")
    for i, patch in enumerate(app_patches(answers), start=1):
        print(f"  {i}. {patch}")
    print()
    say("then paste this into CLAUDE.md (or AGENTS.md):")
    print()
    print(claude_section(answers))
