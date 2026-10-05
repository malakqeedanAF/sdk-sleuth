#!/usr/bin/env bash
# sdk-sleuth setup: checks / installs prerequisites and signs you in to GitHub and GitLab.
# Double-click it, or run:  ./setup.command   (flags: --check = report only, --yes = don't ask before installing)
# Safe to re-run: it only does what is still missing.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"

CHECK_ONLY=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
  esac
done

GITLAB_BASE="${GITLAB_BASE_URL:-https://gitlab.appsflyer.com}"
GITLAB_BASE="${GITLAB_BASE%/}"
TOKEN_DIR="$HOME/.sdk-sleuth"
TOKEN_FILE="$TOKEN_DIR/gitlab_token"

if [ -t 1 ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'
else
  B=""; D=""; R=""; RED=""; GRN=""; YEL=""; CYN=""
fi

ok()   { echo "  ${GRN}✓${R} $*"; }
bad()  { echo "  ${RED}✗${R} $*"; }
warn() { echo "  ${YEL}!${R} $*"; }
step() { echo; echo "${B}${CYN}[$1]${R} ${B}$2${R}"; }
FAILED=0
pause_exit() { echo; read -r -p "${D}Press Enter to close…${R} " _ || true; exit "${1:-0}"; }

confirm() {  # confirm "question"  -> 0 yes / 1 no
  [ "$ASSUME_YES" = 1 ] && return 0
  local ans; read -r -p "  ${YEL}?${R} $1 [Y/n] " ans || return 1
  case "$ans" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

echo "${B}${CYN}"
echo "  ╭──────────────────────────────────────╮"
echo "  │   🕵️  sdk-sleuth · setup assistant    │"
echo "  ╰──────────────────────────────────────╯${R}"
[ "$CHECK_ONLY" = 1 ] && echo "  ${D}(check-only mode: nothing will be installed or changed)${R}"
if [ "$(uname -s)" != "Darwin" ]; then
  warn "This tool is built for macOS; continuing, but installs via Homebrew may not apply."
fi

# ---------------------------------------------------------------- 1. Homebrew
step "1/5" "Homebrew (package manager)"
have_brew() { command -v brew >/dev/null 2>&1; }
load_brew() {
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)" && return 0
  done
  return 1
}
have_brew || load_brew
NEED_BREW=0
for t in python3 fzf gh; do command -v "$t" >/dev/null 2>&1 || NEED_BREW=1; done
if have_brew; then
  ok "Homebrew found ($(command -v brew))"
elif [ "$NEED_BREW" = 0 ]; then
  ok "not needed — all tools already installed"
elif [ "$CHECK_ONLY" = 1 ]; then
  bad "Homebrew is missing (needed to install tools)"; FAILED=1
else
  bad "Homebrew is not installed (needed to install the missing tools)"
  if confirm "Install Homebrew now? (official installer from brew.sh; may ask for your Mac password)"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    load_brew
    have_brew && ok "Homebrew installed" || { bad "Homebrew install failed"; FAILED=1; }
  else
    bad "Skipped. Install Homebrew from https://brew.sh and re-run setup."; FAILED=1
  fi
fi

# ---------------------------------------------------------------- 2. tools
step "2/5" "Required tools: python3, fzf, gh"
install_tool() {  # install_tool <command> <brew formula> <label>
  local cmd="$1" formula="$2" label="$3"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$label already installed ${D}($(command -v "$cmd"))${R}"; return
  fi
  if [ "$CHECK_ONLY" = 1 ]; then bad "$label is missing"; FAILED=1; return; fi
  if ! have_brew; then bad "$label is missing and Homebrew is unavailable"; FAILED=1; return; fi
  if confirm "$label is missing. Install it with 'brew install $formula'?"; then
    brew install "$formula"
    hash -r
    command -v "$cmd" >/dev/null 2>&1 && ok "$label installed" || { bad "$label install failed"; FAILED=1; }
  else
    bad "$label skipped — sdk-sleuth cannot run without it"; FAILED=1
  fi
}
# macOS ships a python3 stub that opens an installer popup; make sure a real one runs.
if command -v python3 >/dev/null 2>&1 && ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  warn "python3 is missing or older than 3.8"
  PATH_PY_BAD=1
else
  PATH_PY_BAD=0
fi
if [ "$PATH_PY_BAD" = 1 ]; then
  if [ "$CHECK_ONLY" = 1 ]; then bad "python3 (>= 3.8) is missing"; FAILED=1
  elif have_brew && confirm "Install Python with 'brew install python'?"; then
    brew install python; hash -r
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null && ok "Python installed" || { bad "Python install failed"; FAILED=1; }
  else bad "python3 (>= 3.8) skipped"; FAILED=1; fi
else
  ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
fi
install_tool fzf fzf "fzf (fuzzy picker)"
install_tool gh gh "GitHub CLI (gh)"

# ---------------------------------------------------------------- 3. GitHub
step "3/5" "GitHub sign-in"
if command -v gh >/dev/null 2>&1; then
  gh_accounts() { gh auth status 2>&1 | sed -n 's/.*Logged in to github\.com account \([^ ]*\).*/\1/p'; }
  ACCTS="$(gh_accounts)"
  if [ -n "$ACCTS" ]; then
    for a in $ACCTS; do ok "signed in as ${B}$a${R}"; done
  elif [ "$CHECK_ONLY" = 1 ]; then
    bad "not signed in to GitHub"; FAILED=1
  else
    echo "  Not signed in yet. A browser window will open — log in with your GitHub account."
    if confirm "Run 'gh auth login' now?"; then
      gh auth login -h github.com -p https -w
      ACCTS="$(gh_accounts)"
      [ -n "$ACCTS" ] && ok "signed in as ${B}$(echo "$ACCTS" | head -1)${R}" || { bad "GitHub sign-in did not complete"; FAILED=1; }
    else
      bad "Skipped GitHub sign-in (GitHub searches won't work)"; FAILED=1
    fi
  fi
  if [ -n "${ACCTS:-}" ]; then
    echo "  ${D}Tip: if your org uses SSO, authorize this login for it:"
    echo "       https://github.com/settings/applications  (or the SSO prompt on github.com)${R}"
    echo "  ${D}More accounts later: use \"+ Add a new GitHub account\" inside sdk-sleuth.${R}"
  fi
else
  bad "gh is not installed — skipping"; FAILED=1
fi

# ---------------------------------------------------------------- 4. GitLab
step "4/5" "GitLab sign-in ($GITLAB_BASE)"
gl_check() {  # prints username on success; returns 0 if token valid
  local tok="$1" body code
  body="$(printf 'header = "PRIVATE-TOKEN: %s"\n' "$tok" | curl -s -m 15 -K - -w '\n%{http_code}' "$GITLAB_BASE/api/v4/user" 2>/dev/null)"
  code="$(echo "$body" | tail -1)"
  if [ "$code" = "200" ]; then
    echo "$body" | head -1 | sed -n 's/.*"username":"\([^"]*\)".*/\1/p'
    return 0
  fi
  echo "$code"; return 1
}
CUR_TOKEN="${GITLAB_TOKEN:-}"
CUR_TOKEN="${CUR_TOKEN%\"}"; CUR_TOKEN="${CUR_TOKEN#\"}"
[ -z "$CUR_TOKEN" ] && [ -f "$TOKEN_FILE" ] && CUR_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
GL_DONE=0
if [ -n "$CUR_TOKEN" ]; then
  if USER_OR_CODE="$(gl_check "$CUR_TOKEN")"; then
    ok "GitLab token works — signed in as ${B}$USER_OR_CODE${R}"; GL_DONE=1
  else
    case "$USER_OR_CODE" in
      000|"") warn "Could not reach $GITLAB_BASE — are you on the VPN / office network?" ;;
      *)      warn "The saved GitLab token was rejected (HTTP $USER_OR_CODE) — it may be expired." ;;
    esac
  fi
fi
if [ "$GL_DONE" = 0 ]; then
  if [ "$CHECK_ONLY" = 1 ]; then
    bad "no working GitLab token"; FAILED=1
  else
    echo "  A personal access token is needed (scope: ${B}read_api${R})."
    TOKEN_URL="$GITLAB_BASE/-/user_settings/personal_access_tokens?name=sdk-sleuth&scopes=read_api"
    echo "  Create one here:  ${CYN}${TOKEN_URL}${R}"
    if confirm "Open that page in your browser?"; then open "$TOKEN_URL" 2>/dev/null || true; fi
    echo "  ${D}On the page: set a name, pick an expiry, tick 'read_api', click Create, copy the token.${R}"
    for attempt in 1 2 3; do
      read -r -s -p "  Paste your GitLab token (hidden, Enter to skip): " NEW_TOKEN || NEW_TOKEN=""
      echo
      NEW_TOKEN="$(echo "$NEW_TOKEN" | tr -d '[:space:]"'"'")"
      [ -z "$NEW_TOKEN" ] && { bad "Skipped GitLab sign-in (GitLab searches won't work)"; FAILED=1; break; }
      if USER_OR_CODE="$(gl_check "$NEW_TOKEN")"; then
        mkdir -p "$TOKEN_DIR" && chmod 700 "$TOKEN_DIR"
        ( umask 077; printf '%s\n' "$NEW_TOKEN" > "$TOKEN_FILE" )
        ok "Verified — signed in as ${B}$USER_OR_CODE${R}"
        ok "Token saved to ${D}$TOKEN_FILE${R} (readable only by you)"
        GL_DONE=1; break
      fi
      case "$USER_OR_CODE" in
        000|"") bad "Cannot reach $GITLAB_BASE — connect to the VPN / office network and try again." ;;
        401)    bad "GitLab rejected that token (401). Check you copied all of it." ;;
        *)      bad "Unexpected response (HTTP $USER_OR_CODE)." ;;
      esac
      [ "$attempt" = 3 ] && FAILED=1
    done
  fi
fi

# ---------------------------------------------------------------- 5. finish
step "5/5" "Launcher"
chmod +x setup.command sdk-sleuth.command 2>/dev/null
xattr -d com.apple.quarantine setup.command sdk-sleuth.command sdk_sleuth.py 2>/dev/null || true
ok "launchers are executable"
if [ "$CHECK_ONLY" = 0 ] && [ -d "$HOME/Desktop" ] && [ ! -e "$HOME/Desktop/sdk-sleuth.command" ]; then
  if confirm "Add a double-click shortcut to your Desktop?"; then
    ln -s "$PWD/sdk-sleuth.command" "$HOME/Desktop/sdk-sleuth.command" && ok "Desktop shortcut created"
  fi
fi

echo
if [ "$FAILED" = 0 ]; then
  echo "${B}${GRN}╭──────────────────────────────────────────╮"
  echo "│  All set. Case file ready, detective! 🕵️  │"
  echo "╰──────────────────────────────────────────╯${R}"
  echo "  Start with:  ${B}./sdk-sleuth.command${R}"
  if [ "$CHECK_ONLY" = 0 ] && confirm "Launch sdk-sleuth now?"; then
    exec ./sdk-sleuth.command
  fi
  pause_exit 0
else
  echo "${B}${YEL}Setup finished with issues (see ✗ above). Fix them and re-run ./setup.command${R}"
  pause_exit 1
fi
