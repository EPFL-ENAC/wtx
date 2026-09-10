# wtx shell helpers. Source with:  eval "$(wtx shell-init bash)"
#
# wtgo and wtdone act on the repo you are standing in, not on the one this file
# came from. Completion reads the repo's own wtx.toml, so protected branches are
# defined in one place and never drift.

wtgo()  { command wtx go "$@"; }
wtdone(){ command wtx done "$@"; }
wtland(){ command wtx land "$@"; }
wtmon() { command wtx monitor "$@"; }

_wtx_main() {
  local common
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
  printf '%s' "${common%/.git}"
}

_wtx_protected() {
  local main; main="$(_wtx_main)" || return 1
  [ -f "$main/wtx.toml" ] || return 1
  sed -n 's/^protected_branches[[:space:]]*=[[:space:]]*\[\(.*\)\]/\1/p' "$main/wtx.toml" \
    | tr -d '"' | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$'
}

# The [[repos]] names, for --with. Only the name lines under a [[repos]] header.
_wtx_families() {
  local main; main="$(_wtx_main)" || return 1
  awk '/^\[\[ports\.family\]\]/ {in_f=1; next} /^\[/ {in_f=0}
       in_f && /^name[[:space:]]*=/ {gsub(/^name[[:space:]]*=[[:space:]]*"|".*$/, ""); print}' \
    "$main/wtx.toml" 2>/dev/null
}

_wtx_repos() {
  local main; main="$(_wtx_main)" || return 1
  awk '/^\[\[repos\]\]/ {in_repo=1; next} /^\[/ {in_repo=0}
       in_repo && /^name[[:space:]]*=/ {gsub(/^name[[:space:]]*=[[:space:]]*"|".*$/, ""); print}' \
    "$main/wtx.toml" 2>/dev/null
}

# Branches that already have a worktree: the "what was I working on" list.
_wtx_worktrees() {
  local main; main="$(_wtx_main)" || return 1
  git -C "$main" worktree list --porcelain 2>/dev/null \
    | sed -n 's|^branch refs/heads/||p'
}

_wtx_branches() {
  local main; main="$(_wtx_main)" || return 1
  git -C "$main" for-each-ref --format='%(refname:short)' refs/heads refs/remotes/origin 2>/dev/null \
    | sed 's|^origin/||' | grep -v '^HEAD$' | sort -u
}

_wtx_filter_protected() {
  local protected; protected="$(_wtx_protected)"
  if [ -z "$protected" ]; then cat; return; fi
  grep -vxF "$protected"
}

_wtx_complete_go() {
  local cur="$1" list
  list="$(_wtx_worktrees | _wtx_filter_protected)"
  if [ -n "$cur" ]; then
    local wide; wide="$(_wtx_branches | _wtx_filter_protected)"
    list="$(printf '%s\n%s\n' "$list" "$wide" | sort -u)"
  fi
  printf '%s\n' "$list"
}

_wtx() {
  local cur prev words cword
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  local subs="init config go all done land open status curl setup teardown tmux brief hook notify tmux-status monitor doctor install-machine shell-init guard"

  if [ "$COMP_CWORD" -eq 1 ]; then
    mapfile -t COMPREPLY < <(compgen -W "$subs" -- "$cur")
    return
  fi
  case "${COMP_WORDS[1]}" in
    go|brief)
      if [ "$prev" = "--with" ]; then
        local main; main="$(_wtx_main)"
        mapfile -t COMPREPLY < <(compgen -W "$(_wtx_repos)" -- "$cur")
        return
      fi
      mapfile -t COMPREPLY < <(compgen -W "$(_wtx_complete_go "$cur") all" -- "$cur")
      ;;
    done|land|open|status)
      mapfile -t COMPREPLY < <(compgen -W "$(_wtx_worktrees | _wtx_filter_protected)" -- "$cur")
      ;;
    curl)
      [ "$COMP_CWORD" -eq 2 ] &&
        mapfile -t COMPREPLY < <(compgen -W "$(_wtx_families)" -- "$cur")
      ;;
    hook)
      mapfile -t COMPREPLY < <(compgen -W "post-create post-checkout pre-remove" -- "$cur")
      ;;
    *)
      mapfile -t COMPREPLY < <(compgen -W "--help" -- "$cur")
      ;;
  esac
}

_wtgo() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  mapfile -t COMPREPLY < <(compgen -W "$(_wtx_complete_go "$cur") all" -- "$cur")
}
_wtdone() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  mapfile -t COMPREPLY < <(compgen -W "$(_wtx_worktrees | _wtx_filter_protected)" -- "$cur")
}

complete -F _wtx wtx
complete -F _wtgo wtgo
complete -F _wtdone wtdone wtland
