#!/usr/bin/env python3
"""
sdk-sleuth - interactive terminal tool to search for a function/parameter/class
across your GitHub and GitLab repos, scoped to a range of SDK release versions.

Wizard (every step has a way back: "← Back" entry, Esc / ctrl-b, or empty input):
  1. Platform      GitHub / GitLab
  2. Account       gh account (GitHub) or GITLAB_TOKEN (GitLab)
  3. Repository    favorites, browse an org, or paste a URL
  4. Versions      pick From / To releases (with release dates)
  5. Search        results open in an interactive browser where versions and
                   files expand / collapse

Requires: python3, fzf. GitHub auth uses the `gh` CLI (already logged in).
GitLab auth uses the GITLAB_TOKEN env var.
"""
import base64
import bisect
import difflib
import hashlib
import itertools
import json
import locale
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import error, parse, request

VERSION = "1.1.0"

# ---------- colors ----------
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code):
    return f"\033[{code}m" if COLOR else ""


def fg256(n):
    return _c(f"38;5;{n}")


BOLD = _c("1")
DIM = _c("2")
ITALIC = _c("3")
UNDERLINE = _c("4")
RESET = _c("0")
RED = _c("31")
GREEN = _c("32")
YELLOW = _c("33")
BLUE = _c("34")
MAGENTA = _c("35")
CYAN = _c("36")
HL = _c("30;43")  # black on yellow: highlights the search term

CACHE_DIR = os.path.expanduser("~/.sdk-sleuth/cache")
os.makedirs(CACHE_DIR, exist_ok=True)

BINARY_EXT = {
    "png", "jpg", "jpeg", "gif", "ico", "bmp", "webp", "zip", "jar", "aar",
    "so", "dylib", "dll", "exe", "class", "woff", "woff2", "ttf", "otf",
    "eot", "pdf", "mp4", "mov", "bin", "o", "a", "keystore", "jks", "ipa",
    "apk", "gz", "tar", "7z", "framework", "xcarchive", "lock",
}

# Quick-pick favorites for the AppsFlyer SDK repos, since org-wide discovery
# isn't reliable for either platform (GitLab's membership flag doesn't
# reflect this account's real access; GitHub repos live under an org, not
# a personal account). Edit these lists freely.
FAVORITES_GITLAB = [
    "mobile/ios/appsflyer.sdk.ios",
    "mobile/appsflyer-android-sdk",
    "fe/af-web-sdk",
    "mobile/infra/af-web-sdk-demo",
    "incoming/one/af-multindex",
    "mobile/security-sdk/af-security-sdk",
]

FAVORITES_GITHUB = [
    "AppsFlyerSDK/appsflyer-react-native-plugin",
    "AppsFlyerSDK/appsflyer-unity-plugin",
    "AppsFlyerSDK/appsflyer-flutter-plugin",
    "AppsFlyerSDK/AdobeAirExtension-AppsFlyer",
    "AppsFlyerSDK/segment-appsflyer-ios",
    "AppsFlyerSDK/appsflyer-segment-android-plugin",
    "AppsFlyerSDK/appsflyer-capacitor-plugin",
    "AppsFlyerSDK/appsflyer-cordova-plugin",
    "AppsFlyerSDK/appsflyer-unreal-plugin",
    "AppsFlyerSDK/XamariniOSBinding",
    "AppsFlyerSDK/XamarinAndroidBinding",
    "AppsFlyerSDK/appsflyer-cocos2dx-plugin",
    "AppsFlyerSDK/appsflyer-nativescript-plugin",
]

TAGLINES = [
    "Elementary, my dear commit.",
    "No symbol left behind.",
    "git blame, but make it detective work.",
    "Grep happens. We find it anyway.",
    "Because \"it worked in the last version\" is not a bug report.",
    "Following the trail across releases.",
    "Every SDK release is a crime scene.",
]

PHRASES = [
    "Dusting for fingerprints",
    "Interrogating suspicious files",
    "Following the commit trail",
    "Questioning the blobs",
    "Cross-referencing the evidence",
    "Checking every release's alibi",
    "Consulting the changelog",
    "Tailing a suspicious symbol",
]

SIGN_OFFS = [
    "Case closed. Happy hunting.",
    "Until next time, detective.",
    "The truth is out there (probably in a release tag).",
]

BACK = object()


def die(msg):
    print(f"{RED}Error: {msg}{RESET}", file=sys.stderr)
    sys.exit(1)


def require(cmd):
    if shutil.which(cmd) is None:
        die(f"{cmd} is not installed or not on PATH.")


# ======================== text / layout helpers ========================
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def char_w(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s):
    return sum(char_w(ch) for ch in ANSI_RE.sub("", s))


def clip_plain(s, w):
    """Cut plain text to w display columns."""
    out, n = [], 0
    for ch in s:
        cw = char_w(ch)
        if n + cw > w:
            break
        out.append(ch)
        n += cw
    return "".join(out)


def clip_ansi(s, w):
    """Cut text containing ANSI codes to w display columns, adding an ellipsis."""
    if vlen(s) <= w:
        return s
    out, n = [], 0
    for tok in re.split(r"(\x1b\[[0-9;]*m)", s):
        if ANSI_RE.fullmatch(tok):
            out.append(tok)
            continue
        for ch in tok:
            cw = char_w(ch)
            if n + cw > w - 1:
                out.append("…" + RESET)
                return "".join(out)
            out.append(ch)
            n += cw
    return "".join(out)


def term_size():
    s = shutil.get_terminal_size((100, 30))
    return s.columns, s.lines


def box(lines, title="", color=None, pad=1, min_inner=0, max_total=None):
    """Rounded box around lines (which may contain ANSI codes)."""
    color = CYAN if color is None else color
    inner = max([vlen(l) for l in lines] + [vlen(title) + 2, min_inner])
    if max_total:
        inner = min(inner, max_total - 2 - 2 * pad)
    width = inner + 2 * pad
    if title:
        top = f"{color}╭─{RESET} {BOLD}{title}{RESET} {color}" + "─" * (width - vlen(title) - 3) + f"╮{RESET}"
    else:
        top = f"{color}╭" + "─" * width + f"╮{RESET}"
    out = [top]
    for l in lines:
        l = clip_ansi(l, inner)
        out.append(f"{color}│{RESET}" + " " * pad + l + " " * (inner - vlen(l)) + " " * pad + f"{color}│{RESET}")
    out.append(f"{color}╰" + "─" * width + f"╯{RESET}")
    return out


def fmt_date(iso):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m[1]}/{m[2]}/{m[3]}" if m else "----/--/--"


def snippet(line, term, width=240):
    s = CTRL_RE.sub(" ", line.replace("\t", "    ")).strip()
    if len(s) <= width:
        return s
    idx = s.lower().find(term.lower())
    if idx < 0:
        idx = 0
    start = max(0, idx - width // 3)
    out = s[start:start + width]
    return ("…" if start > 0 else "") + out + ("…" if start + width < len(s) else "")


def fit_around(text, term, avail):
    """Trim text to avail columns, keeping the first match of term visible."""
    if vlen(text) <= avail:
        return text
    idx = text.lower().find(term.lower())
    if idx < 0 or idx + len(term) <= avail - 1:
        return clip_plain(text, avail - 1) + "…"
    start = max(0, idx - max(4, (avail - len(term)) // 3))
    return "…" + clip_plain(text[start:], avail - 1)


def split_hl(text, term):
    """Split text into [(segment, is_match)] on case-insensitive term matches."""
    if not term:
        return [(text, False)]
    parts, low, tl, i = [], text.lower(), term.lower(), 0
    while True:
        j = low.find(tl, i)
        if j < 0:
            if i < len(text):
                parts.append((text[i:], False))
            return parts
        if j > i:
            parts.append((text[i:j], False))
        parts.append((text[j:j + len(term)], True))
        i = j + len(term)


def hl_ansi(text, term):
    return "".join(f"{HL}{seg}{RESET}" if hit else seg for seg, hit in split_hl(text, term))


# ======================== screens / banner ========================
FONT = {
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "K": ["█   █", "█  █ ", "███  ", "█  █ ", "█   █"],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "H": ["█   █", "█   █", "█████", "█   █", "█   █"],
}
GRADIENT = [51, 45, 39, 33, 63, 99, 135, 171, 207]
MAGNIFIER = [
    r"  .-----.   ",
    r" /  { }  \  ",
    r" \       /  ",
    r"  '-----'   ",
    r"         \\ ",
]

UI_USED = 0  # terminal rows used by the current screen (so fzf can size itself)


def banner_lines():
    word = "SDK SLEUTH"
    rows = []
    for r in range(5):
        parts, gi = [], 0
        for ch in word:
            if ch == " ":
                parts.append("   ")
                continue
            parts.append(f"{fg256(GRADIENT[gi % len(GRADIENT)])}{FONT[ch][r]}{RESET} ")
            gi += 1
        rows.append("".join(parts).rstrip() + f"   {CYAN}{MAGNIFIER[r]}{RESET}")
    return rows


def case_file_lines(state):
    rows = []

    def add(label, value):
        rows.append(f"{DIM}{label:<9}{RESET} {value}")

    if state.platform:
        add("Mode", f"{BOLD}{'Compare' if state.mode == 'compare' else 'Search'}{RESET}")
        add("Platform", f"{BOLD}{state.platform}{RESET}")
    if state.account_label:
        add("Account", f"{BOLD}{state.account_label}{RESET}")
    if state.repo_label:
        add("Repo", f"{BOLD}{state.repo_label}{RESET}  {DIM}{state.repo_url}{RESET}")
    if state.range:
        add("Versions", state.range_summary())
    if state.last_term:
        add("Term", f"{HL} {state.last_term} {RESET}")
    return rows


def step_titles(state):
    return ["Mode", "Platform", "Account", "Repository", "Versions",
            "Compare" if state.mode == "compare" else "Search"]


def screen(state, idx, big=False, subtitle=""):
    """Clear the terminal and draw the header, step tracker and case file."""
    global UI_USED
    cols, rows = term_size()
    lines = []
    compact = rows < 34 or cols < 78
    if big and not compact:
        art = banner_lines()
        art.append("")
        art.append(f"{ITALIC}{DIM}{random.choice(TAGLINES)}{RESET}")
        lines += box(art, color=fg256(39), pad=2)
        lines.append(f"{DIM}  v{VERSION} · GitHub + GitLab · search a function across SDK versions{RESET}")
    else:
        lines.append(f"{BOLD}{CYAN}◆ sdk-sleuth{RESET} {DIM}v{VERSION}{RESET}")
    titles = step_titles(state)
    cur = idx + 1  # callers pass the index without the Mode step
    dots = " ".join(
        f"{GREEN}●{RESET}" if i < cur else (f"{CYAN}{BOLD}◉{RESET}" if i == cur else f"{DIM}○{RESET}")
        for i in range(len(titles))
    )
    lines.append(f"{dots}  {BOLD}Step {cur + 1}/{len(titles)}{RESET} {DIM}·{RESET} {BOLD}{titles[cur]}{RESET}"
                 + (f"  {DIM}{subtitle}{RESET}" if subtitle else ""))
    case = case_file_lines(state)
    if case:
        lines += box(case, title="Case file", color=DIM, max_total=cols - 1)
    if COLOR:
        sys.stdout.write("\033[2J\033[H")
    print("\n".join(lines))
    print()
    UI_USED = len(lines) + 1


def notice(kind, lines, title=""):
    color = {"error": RED, "warn": YELLOW, "ok": GREEN}.get(kind, CYAN)
    print("\n".join(box(lines, title=title, color=color, max_total=term_size()[0] - 1)))


def pause(msg="Press Enter to go back"):
    try:
        input(f"{DIM}{msg}…{RESET} ")
    except EOFError:
        pass


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, msg):
        self.msg = msg
        self._stop = threading.Event()
        self._t = None

    def __enter__(self):
        if not COLOR:
            print(self.msg + "...")
            return self
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{CYAN}{self.FRAMES[i % len(self.FRAMES)]}{RESET} {self.msg}\033[K")
            sys.stdout.flush()
            i += 1
            time.sleep(0.08)

    def __exit__(self, *exc):
        if self._t:
            self._stop.set()
            self._t.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        return False


def progress(msg):
    sys.stdout.write(f"\r{CYAN}{msg}{RESET}\033[K")
    sys.stdout.flush()


def clear_progress():
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


_last_draw = [0.0]


def draw_progress(frac, text, force=False):
    now = time.time()
    if not force and now - _last_draw[0] < 0.04:
        return
    _last_draw[0] = now
    cols, _ = term_size()
    frac = max(0.0, min(1.0, frac))
    bar_w = 26
    filled = int(bar_w * frac)
    bar = "█" * filled + "░" * (bar_w - filled)
    phrase = PHRASES[int(now // 3) % len(PHRASES)]
    spin = Spinner.FRAMES[int(now * 12) % len(Spinner.FRAMES)]
    line = (f"  {CYAN}{bar}{RESET} {BOLD}{int(frac * 100):3d}%{RESET}  {text}  "
            f"{DIM}{spin} {phrase}…{RESET}")
    sys.stdout.write("\r" + clip_ansi(line, cols - 1) + "\033[K")
    sys.stdout.flush()


# ======================== input helpers ========================
def rl_wrap(prompt):
    """Mark ANSI codes as zero-width so readline computes the cursor correctly."""
    return ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", prompt)


def ask_line(prompt, prefill=""):
    try:
        import readline
    except ImportError:
        readline = None
    if readline and prefill:
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(rl_wrap(prompt)).strip()
    except EOFError:
        return ""
    finally:
        if readline:
            readline.set_startup_hook()


def fzf_pick(items, prompt, header="", back="back", query=""):
    """items: list of str or (value, display). Returns the value, or BACK.

    back: "back" adds a '← Back' entry, "quit" adds '✕ Quit', None adds neither.
    Esc / ctrl-b always count as going back.
    """
    values, entries = [], []
    for it in items:
        value, display = (it, it) if isinstance(it, str) else it
        entries.append(f"{len(values)}\t{display}")
        values.append(value)
    if back:
        label = "✕ Quit" if back == "quit" else "← Back"
        entries.append(f"-1\t{YELLOW}{label}{RESET}")
    hint = {"back": "esc / ctrl-b: ← back", "quit": "esc: ✕ quit"}.get(back, "")
    head = "\n".join(x for x in (header, hint) if x)
    _, rows = term_size()
    height = max(8, rows - UI_USED - 2)
    args = [
        "fzf", f"--prompt={prompt} ❯ ", f"--height={height}", "--layout=reverse",
        "--border=rounded", "--pointer=▶", "--marker=✓", "--info=inline-right",
        "--delimiter=\t", "--with-nth=2..", "--ansi", "--tiebreak=index",
        "--expect=ctrl-b", "--no-mouse",
        "--color=hl:yellow,hl+:yellow,pointer:magenta,prompt:cyan,info:dim,header:dim,border:dim",
    ]
    if head:
        args.append(f"--header={head}")
    if query:
        args.append(f"--query={query}")
    proc = subprocess.run(args, input="\n".join(entries), capture_output=True, text=True)
    if proc.returncode == 2:
        die(f"fzf failed: {proc.stderr.strip()}")
    lines = proc.stdout.split("\n")
    if proc.returncode != 0 or (lines and lines[0] == "ctrl-b") or len(lines) < 2 or not lines[1].strip():
        return BACK
    try:
        idx = int(lines[1].split("\t")[0])
    except ValueError:
        return BACK
    return BACK if idx < 0 else values[idx]


# ======================== HTTP + versions ========================
def http_json(url, headers=None, timeout=30):
    req = request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return (json.loads(body) if body else None), hdrs
    except error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} for {url}: {detail}")
    except error.URLError as e:
        raise RuntimeError(f"network error for {url}: {e.reason}")


def version_key(tag):
    t = re.sub(r"^[vV]\.?", "", tag)
    m = re.match(r"(\d+(?:\.\d+)*)(.*)$", t)
    if not m:
        return ((0,), 1, t)
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2)
    # a clean release (no "-rc1"/"-beta" suffix) sorts after its pre-releases
    suffix_rank = 1 if suffix == "" else 0
    return (nums, suffix_rank, suffix)


# ======================== GitHub ========================
def gh_accounts():
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return re.findall(r"Logged in to github\.com account (\S+)", r.stdout + r.stderr)


def gh_login():
    subprocess.run(["gh", "auth", "login"])


def gh_token_for(owner):
    r = subprocess.run(["gh", "auth", "token", "--user", owner], capture_output=True, text=True)
    return r.stdout.strip()


def gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def gh_api(path, token, params=None):
    url = f"https://api.github.com/{path}"
    if params:
        url += "?" + parse.urlencode(params)
    return http_json(url, headers=gh_headers(token))


def gh_list_my_repos(token):
    """All repos the token's own account can see (owner + org member)."""
    repos, page = [], 1
    while True:
        progress(f"  loading your repos... page {page} ({len(repos)} so far)")
        data, _ = gh_api("user/repos", token,
                         {"affiliation": "owner,organization_member", "per_page": 100, "page": page})
        if not data:
            break
        repos.extend(r["full_name"] for r in data)
        if len(data) < 100:
            break
        page += 1
    clear_progress()
    return repos


def gh_list_owner_repos(owner, token):
    """All repos under an org/owner name visible to this token (orgs first, then users)."""
    repos = []
    for kind in ("orgs", "users"):
        repos, page = [], 1
        try:
            while True:
                progress(f"  loading {owner}'s repos... page {page} ({len(repos)} so far)")
                data, _ = gh_api(f"{kind}/{owner}/repos", token, {"per_page": 100, "page": page})
                if not data:
                    break
                repos.extend(r["full_name"] for r in data)
                if len(data) < 100:
                    break
                page += 1
            clear_progress()
            if repos:
                return repos
        except RuntimeError:
            clear_progress()
    return repos


def gh_list_releases(owner, repo, token):
    releases, page = {}, 1
    while True:
        data, _ = gh_api(f"repos/{owner}/{repo}/releases", token, {"per_page": 100, "page": page})
        if not data:
            break
        for rel in data:
            releases[rel["tag_name"]] = {"released_at": rel.get("published_at")}
        if len(data) < 100:
            break
        page += 1
    return releases


def gh_resolve_sha(owner, repo, ref, token):
    data, _ = gh_api(f"repos/{owner}/{repo}/commits/{parse.quote(ref, safe='')}", token)
    return data["sha"]


def gh_get_tree(owner, repo, sha, token):
    data, _ = gh_api(f"repos/{owner}/{repo}/git/trees/{sha}", token, {"recursive": "1"})
    return data.get("tree", [])


def _blob_cache_path(repo_key, sha):
    d = os.path.join(CACHE_DIR, "gh_blobs", repo_key.replace("/", "_"), sha[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, sha)


def gh_get_blob_text(owner, repo, sha, token):
    path = _blob_cache_path(f"{owner}/{repo}", sha)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="ignore")
    data, _ = gh_api(f"repos/{owner}/{repo}/git/blobs/{sha}", token)
    raw = b""
    if data and data.get("encoding") == "base64":
        try:
            raw = base64.b64decode(data["content"])
        except Exception:
            raw = b""
    with open(path, "wb") as f:
        f.write(raw)
    return raw.decode("utf-8", errors="ignore")


def gh_search_version(owner, repo, ref, term, token, on_progress=None):
    """Returns [{path, line, text, url}] for lines containing term at ref."""
    sha = gh_resolve_sha(owner, repo, ref or "HEAD", token)
    tree = gh_get_tree(owner, repo, sha, token)
    candidates = [
        e for e in tree
        if e.get("type") == "blob"
        and e.get("size", 0) < 3_000_000
        and e["path"].rsplit(".", 1)[-1].lower() not in BINARY_EXT
    ]
    total = len(candidates)
    term_lower = term.lower()
    if on_progress:
        on_progress(0, max(total, 1))

    def work(entry):
        text = gh_get_blob_text(owner, repo, entry["sha"], token)
        hits = []
        if term_lower in text.lower():
            for i, line in enumerate(text.splitlines(), start=1):
                if term_lower in line.lower():
                    hits.append({
                        "path": entry["path"], "line": i, "text": snippet(line, term), "blob": entry["sha"],
                        "url": f"https://github.com/{owner}/{repo}/blob/{ref or sha}/{entry['path']}#L{i}",
                    })
        return hits

    matches, done = [], 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(work, e) for e in candidates]
        for fut in as_completed(futures):
            done += 1
            if on_progress:
                on_progress(done, total)
            matches.extend(fut.result())
    matches.sort(key=lambda m: (m["path"], m["line"]))
    return matches


# ======================== GitLab ========================
def gitlab_base():
    return os.environ.get("GITLAB_BASE_URL", "https://gitlab.appsflyer.com").rstrip("/")


def gitlab_token():
    """GITLAB_TOKEN env var, else the token file written by setup.command."""
    tok = os.environ.get("GITLAB_TOKEN", "").strip().strip('"').strip("'")
    if not tok:
        try:
            with open(os.path.expanduser("~/.sdk-sleuth/gitlab_token")) as f:
                tok = f.read().strip()
        except OSError:
            pass
    return tok


def gitlab_headers(token):
    return {"PRIVATE-TOKEN": token}


def gitlab_api(path, token, params=None):
    url = f"{gitlab_base()}/api/v4/{path}"
    if params:
        url += "?" + parse.urlencode(params)
    return http_json(url, headers=gitlab_headers(token))


def gitlab_whoami(token):
    """Validates the token and returns the username, or raises RuntimeError."""
    data, _ = gitlab_api("user", token)
    return data.get("username") if data else None


def gitlab_list_projects(token):
    projects, page = [], 1
    while True:
        progress(f"  loading projects... page {page} ({len(projects)} so far)")
        data, hdrs = gitlab_api(
            "projects", token,
            {"membership": "true", "simple": "true", "per_page": 100,
             "page": page, "order_by": "name", "sort": "asc"},
        )
        if not data:
            break
        projects.extend(data)
        next_page = hdrs.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    clear_progress()
    return projects


def gitlab_list_releases(project_id, token):
    releases, page = {}, 1
    while True:
        data, hdrs = gitlab_api(f"projects/{project_id}/releases", token, {"per_page": 100, "page": page})
        if not data:
            break
        for rel in data:
            releases[rel["tag_name"]] = {"released_at": rel.get("released_at")}
        next_page = hdrs.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    return releases


def gitlab_list_tags(project_id, token):
    """Plain git tags (for projects that tag versions without publishing formal Releases)."""
    tags, page = {}, 1
    while True:
        data, hdrs = gitlab_api(f"projects/{project_id}/repository/tags", token, {"per_page": 100, "page": page})
        for t in data or []:
            c = t.get("commit") or {}
            tags[t["name"]] = {"released_at": c.get("committed_date") or c.get("created_at")}
        nxt = hdrs.get("x-next-page")
        if not nxt:
            return tags
        page = int(nxt)


def gh_list_tags(owner, repo, token, max_dates=200):
    """Plain git tags, with commit dates fetched for the newest ones."""
    names, page = [], 1
    while True:
        data, _ = gh_api(f"repos/{owner}/{repo}/tags", token, {"per_page": 100, "page": page})
        if not data:
            break
        names.extend((t["name"], t["commit"]["sha"]) for t in data)
        if len(data) < 100:
            break
        page += 1
    out = {n: {"released_at": None} for n, _ in names}
    newest = sorted(names, key=lambda x: version_key(x[0]), reverse=True)[:max_dates]

    def date(item):
        try:
            d, _ = gh_api(f"repos/{owner}/{repo}/commits/{item[1]}", token)
            return item[0], d["commit"]["committer"]["date"]
        except RuntimeError:
            return item[0], None

    with ThreadPoolExecutor(max_workers=12) as ex:
        for n, d in ex.map(date, newest):
            out[n]["released_at"] = d
    return out


def gitlab_list_branches(project_id, token, limit=100):
    """Newest branches, used as 'versions' for repos that have neither releases nor tags."""
    data, _ = gitlab_api(f"projects/{project_id}/repository/branches", token, {"per_page": limit})
    out = {}
    for b in data or []:
        c = b.get("commit") or {}
        out[b["name"]] = {"released_at": c.get("committed_date") or c.get("created_at")}
    return out


def only_version_tags(tags):
    """Drop junk tags (e.g. 'latest', 'build-123') when some tags look like versions."""
    good = {k: v for k, v in tags.items() if re.match(r"^[vV]?\.?\d", k)}
    return good or tags


def gitlab_matches_from_item(item, term, project_path, ref):
    """One GitLab blob hit is a multi-line chunk; split it into per-line matches."""
    path = item.get("path")
    start = item.get("startline") or 1
    lines = (item.get("data") or "").splitlines()
    tl = term.lower()
    hits = [(start + i, ln) for i, ln in enumerate(lines) if tl in ln.lower()]
    if not hits:
        hits = [(start, lines[0] if lines else "")]
    effective_ref = item.get("ref") or ref or "HEAD"
    return [{
        "path": path, "line": n, "text": snippet(ln, term),
        "url": f"{gitlab_base()}/{project_path}/-/blob/{effective_ref}/{path}#L{n}",
    } for n, ln in hits]


def gitlab_search_version(project_id, project_path, ref, term, token, on_progress=None):
    matches, seen, page, total, fetched = [], set(), 1, None, 0
    while True:
        params = {"scope": "blobs", "search": term, "per_page": 100, "page": page}
        if ref:
            params["ref"] = ref
        url = f"{gitlab_base()}/api/v4/projects/{project_id}/search?" + parse.urlencode(params)
        data, hdrs = http_json(url, headers=gitlab_headers(token))
        if total is None:
            total = int(hdrs["x-total"]) if hdrs.get("x-total") else None
            if on_progress:
                on_progress(0, max(total or 1, 1))
        if not data:
            break
        for item in data:
            for m in gitlab_matches_from_item(item, term, project_path, ref):
                key = (m["path"], m["line"])
                if key not in seen:
                    seen.add(key)
                    matches.append(m)
        fetched += len(data)
        if on_progress and total:
            on_progress(min(fetched, total), total)
        next_page = hdrs.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    matches.sort(key=lambda m: (m["path"], m["line"]))
    return matches


# ======================== search runner + results model ========================
class VersionResult:
    def __init__(self, tag, date, matches, err=None):
        self.tag = tag
        self.label = tag or "default branch"
        self.date = date
        self.matches = matches
        self.err = err
        files = {}
        for m in matches:
            files.setdefault(m["path"], []).append(m)
        self.files = sorted(files.items())


def run_search(state, term, versions=None):
    versions = versions or state.range
    n = len(versions)
    results = []
    for i, (ref, meta) in enumerate(versions):
        label = ref or "default branch"

        def cb(done, total, i=i, label=label):
            sub = (done / total) if total else 0
            draw_progress((i + sub) / n, f"{BOLD}{label}{RESET} {DIM}({i + 1}/{n}){RESET} {done}/{total}",
                          force=(done >= total))

        draw_progress(i / n, f"{BOLD}{label}{RESET} {DIM}({i + 1}/{n}){RESET} resolving", force=True)
        try:
            matches, err = state.search_fn(ref, term, cb), None
        except Exception as e:  # keep going so one bad ref doesn't sink the whole range
            matches, err = [], str(e)
        results.append(VersionResult(ref, fmt_date((meta or {}).get("released_at")), matches, err))
    draw_progress(1.0, f"{BOLD}done{RESET}", force=True)
    clear_progress()
    return results


def report_lines(results, term):
    """Fully expanded plain report (also used for the 'print' key and fallbacks)."""
    out = []
    for r in results:
        head = f"{BOLD}{r.label}{RESET}  {DIM}{r.date}{RESET}"
        if r.err:
            out.append(f"{head}  {YELLOW}⚠ {r.err[:100]}{RESET}")
        elif not r.matches:
            out.append(f"{head}  {RED}✗ not found{RESET}")
        else:
            out.append(f"{head}  {GREEN}✓ {len(r.matches)} match(es) in {len(r.files)} file(s){RESET}")
            for path, ms in r.files:
                out.append(f"  {BOLD}{path}{RESET}  {MAGENTA}({len(ms)}){RESET}")
                for m in ms:
                    out.append(f"    {YELLOW}L{m['line']:<5}{RESET} {hl_ansi(m['text'], term)}")
                    out.append(f"          {CYAN}{m['url']}{RESET}")
        out.append("")
    return out


# ======================== results browser (curses) ========================
def build_rows(results, exp_v, exp_f):
    rows = []
    for vi, r in enumerate(results):
        rows.append(("v", vi, None, None))
        if vi in exp_v:
            for path, ms in r.files:
                rows.append(("f", vi, path, None))
                if (vi, path) in exp_f:
                    for m in ms:
                        rows.append(("l", vi, path, m))
    return rows


def row_key(row):
    kind, vi, path, m = row
    return (kind, vi, path, m["line"] if m else None)


def apply_depth(results, depth):
    exp_v, exp_f = set(), set()
    if depth >= 2:
        exp_v = {vi for vi, r in enumerate(results) if r.matches}
    if depth >= 3:
        exp_f = {(vi, p) for vi in exp_v for p, _ in results[vi].files}
    return exp_v, exp_f


def default_depth(results):
    """Results always open fully collapsed (versions only); expand with Enter / 1 2 3 / e."""
    return 1


def browse(state, results, term):
    """Interactive expand/collapse browser. Returns 'back' or 'quit'."""
    try:
        import curses
    except ImportError:
        curses = None
    if curses is None or not sys.stdout.isatty():
        print("\n".join(report_lines(results, term)))
        pause("Press Enter to go back")
        return "back"
    os.environ.setdefault("ESCDELAY", "25")
    try:
        return curses.wrapper(lambda scr: _browse(scr, state, results, term))
    except curses.error:
        print("\n".join(report_lines(results, term)))
        pause("Press Enter to go back")
        return "back"


def _browse(scr, state, results, term):
    import curses

    curses.curs_set(0)
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = {"green": 2, "red": 1, "yellow": 3, "cyan": 6, "magenta": 5, "blue": 4}
    for name, col in pairs.items():
        curses.init_pair(col + 10, col, bg)
    curses.init_pair(30, curses.COLOR_BLACK, curses.COLOR_YELLOW)   # term highlight
    curses.init_pair(31, curses.COLOR_BLACK, curses.COLOR_CYAN)     # header bar

    def col(name):
        return curses.color_pair(pairs[name] + 10)

    A_HL = curses.color_pair(30) | curses.A_BOLD
    A_BAR = curses.color_pair(31) | curses.A_BOLD

    depth = default_depth(results)
    exp_v, exp_f = apply_depth(results, depth)
    rows = build_rows(results, exp_v, exp_f)
    sel, top = 0, 0
    flash = ("", 0.0)

    total_matches = sum(len(r.matches) for r in results)
    total_files = sum(len(r.files) for r in results)
    hit_versions = sum(1 for r in results if r.matches)

    def put(y, x, text, attr=0, maxw=None):
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return x
        avail = (w - x) if maxw is None else min(maxw, w - x)
        text = clip_plain(text, avail)
        try:
            scr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return x + vlen(text)

    def row_url(row):
        kind, vi, path, m = row
        if kind == "l":
            return m["url"]
        if kind == "f":
            return results[vi].files[[p for p, _ in results[vi].files].index(path)][1][0]["url"]
        r = results[vi]
        return state.release_url(r.tag) if r.tag else None

    def rel_to_row_after_rebuild(prev_key):
        for i, r in enumerate(rows):
            if row_key(r) == prev_key:
                return i
        kind, vi, path, _ = prev_key
        for want in (("f", vi, path, None), ("v", vi, None, None)):
            for i, r in enumerate(rows):
                if row_key(r) == want:
                    return i
        return 0

    def rebuild(keep=None):
        nonlocal rows, sel
        rows = build_rows(results, exp_v, exp_f)
        sel = rel_to_row_after_rebuild(keep) if keep else 0
        sel = max(0, min(sel, len(rows) - 1))

    def toggle(row, want=None):
        """Expand/collapse row. want: True expand, False collapse, None toggle."""
        kind, vi, path, _ = row
        if kind == "v" and results[vi].matches:
            cur = vi in exp_v
            if want is None or want != cur:
                (exp_v.discard if cur else exp_v.add)(vi)
                if cur:
                    for k in [k for k in exp_f if k[0] == vi]:
                        exp_f.discard(k)
        elif kind == "f":
            k = (vi, path)
            cur = k in exp_f
            if want is None or want != cur:
                (exp_f.discard if cur else exp_f.add)(k)

    def open_url(url):
        nonlocal flash
        if url:
            webbrowser.open(url)
            flash = ("Opened in browser", time.time())

    def yank(url):
        nonlocal flash
        if url:
            try:
                subprocess.run(["pbcopy"], input=url, text=True, check=True)
                flash = ("Copied link to clipboard", time.time())
            except Exception:
                flash = ("Clipboard unavailable", time.time())

    def print_report():
        curses.def_prog_mode()
        curses.endwin()
        print("\n".join(report_lines(results, term)))
        pause("Press Enter to return to the browser")
        curses.reset_prog_mode()
        scr.clear()

    def draw():
        scr.erase()
        h, w = scr.getmaxyx()
        title = f" sdk-sleuth │ {state.repo_label} │ \"{term}\" "
        put(0, 0, title.ljust(w), A_BAR)
        summary = (f" {len(results)} version(s) · {total_matches} match(es) · {total_files} file(s)   ")
        x = put(1, 0, summary, curses.A_DIM)
        x = put(1, x, f"✓ {hit_versions}", col("green") | curses.A_BOLD)
        x = put(1, x, "  ", 0)
        put(1, x, f"✗ {len(results) - hit_versions}", col("red") | curses.A_BOLD)
        put(2, 0, "─" * w, curses.A_DIM)

        body_h = h - 5
        nonlocal top
        if sel < top:
            top = sel
        if sel >= top + body_h:
            top = sel - body_h + 1
        for i in range(body_h):
            ri = top + i
            if ri >= len(rows):
                break
            y = 3 + i
            row = rows[ri]
            is_sel = ri == sel
            rev = curses.A_REVERSE if is_sel else 0
            kind, vi, path, m = row
            if is_sel:
                put(y, 0, " " * w, rev)
            if kind == "v":
                r = results[vi]
                arrow = "▼" if vi in exp_v else ("▶" if r.matches else " ")
                x = put(y, 0, f" {arrow} ", col("cyan") | curses.A_BOLD | rev)
                x = put(y, x, f"{r.label:<16}", curses.A_BOLD | rev)
                x = put(y, x, f"{r.date}   ", curses.A_DIM | rev)
                if r.err:
                    put(y, x, f"⚠ {r.err}", col("yellow") | rev)
                elif r.matches:
                    nf = len(r.files)
                    put(y, x, f"✓ {len(r.matches)} match{'es' if len(r.matches) != 1 else ''}"
                              f" in {nf} file{'s' if nf != 1 else ''}", col("green") | curses.A_BOLD | rev)
                else:
                    put(y, x, "✗ not found", col("red") | curses.A_BOLD | rev)
            elif kind == "f":
                ms = dict(results[vi].files)[path]
                arrow = "▼" if (vi, path) in exp_f else "▶"
                x = put(y, 0, f"    {arrow} ", col("cyan") | rev)
                d, _, f = path.rpartition("/")
                if d:
                    x = put(y, x, d + "/", curses.A_DIM | rev)
                x = put(y, x, f, curses.A_BOLD | rev)
                put(y, x, f"  ({len(ms)})", col("magenta") | rev)
            else:
                x = put(y, 0, "          ", rev)
                x = put(y, x, f"L{m['line']:<5} ", col("yellow") | rev)
                text = fit_around(m["text"], term, max(10, w - x - 1))
                for seg, hit in split_hl(text, term):
                    x = put(y, x, seg, A_HL if hit else (rev or 0))

        # status line: the link for the selected row (or a flash message)
        if rows:
            url = row_url(rows[sel])
        else:
            url = None
        if flash[0] and time.time() - flash[1] < 2.5:
            put(h - 2, 0, f" ✓ {flash[0]}", col("green") | curses.A_BOLD)
        elif url:
            x = put(h - 2, 0, " ↗ ", col("cyan") | curses.A_BOLD)
            put(h - 2, x, url, col("cyan") | curses.A_UNDERLINE)
        keys = [("↑↓", "move"), ("⏎/→/←", "expand/collapse"), ("1 2 3", "depth"), ("e/c", "all"),
                ("o", "open"), ("y", "copy link"), ("p", "print"), ("b", "← back"), ("q", "quit")]
        x = 0
        for k, d in keys:
            x = put(h - 1, x, f" {k}", col("cyan") | curses.A_BOLD)
            x = put(h - 1, x, f" {d} ", curses.A_DIM)
        scr.refresh()

    scr.timeout(400)
    while True:
        draw()
        ch = scr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_RESIZE:
            scr.clear()
            continue
        row = rows[sel] if rows else None
        keep = row_key(row) if row else None
        h, _ = scr.getmaxyx()
        page = max(1, h - 5)
        if ch in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = min(len(rows) - 1, sel + 1)
        elif ch == curses.KEY_PPAGE:
            sel = max(0, sel - page)
        elif ch == curses.KEY_NPAGE:
            sel = min(len(rows) - 1, sel + page)
        elif ch in (curses.KEY_HOME, ord("g")):
            sel = 0
        elif ch in (curses.KEY_END, ord("G")):
            sel = len(rows) - 1
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")) and row:
            if row[0] == "l":
                open_url(row_url(row))
            else:
                toggle(row)
                rebuild(keep)
        elif ch == curses.KEY_RIGHT and row:
            if row[0] != "l":
                already = (row[0] == "v" and row[1] in exp_v) or (row[0] == "f" and (row[1], row[2]) in exp_f)
                if already:
                    sel = min(len(rows) - 1, sel + 1)
                else:
                    toggle(row, True)
                    rebuild(keep)
        elif ch == curses.KEY_LEFT and row:
            kind, vi, path, _ = row
            expanded = (kind == "v" and vi in exp_v) or (kind == "f" and (vi, path) in exp_f)
            if expanded:
                toggle(row, False)
                rebuild(keep)
            else:  # jump to parent
                want = ("f", vi, path, None) if kind == "l" else ("v", vi, None, None)
                if kind != "v":
                    sel = rel_to_row_after_rebuild(want)
        elif ch in (ord("e"), ord("3")):
            exp_v, exp_f = apply_depth(results, 3)
            rebuild(keep)
        elif ch == ord("2"):
            exp_v, exp_f = apply_depth(results, 2)
            rebuild(keep)
        elif ch in (ord("c"), ord("1")):
            exp_v, exp_f = apply_depth(results, 1)
            rebuild(keep)
        elif ch == ord("o") and row:
            open_url(row_url(row))
        elif ch == ord("y") and row:
            yank(row_url(row))
        elif ch == ord("p"):
            print_report()
        elif ch in (ord("b"), 27, curses.KEY_BACKSPACE, 127, 8):
            return "back"
        elif ch in (ord("q"), ord("Q")):
            return "quit"


# ======================== wizard state + steps ========================
class State:
    def __init__(self):
        self.mode = "search"
        self.platform = None
        self.default_branch = None
        self.gh_account = None
        self.gh_token = None
        self.gl_token = None
        self.gl_user = None
        self.account_label = None
        self.repo = None          # GitHub "owner/name" or GitLab path_with_namespace
        self.repo_label = None
        self.repo_url = None
        self.project_id = None    # GitLab only
        self.releases = None
        self.versions_are_branches = False
        self.vkey = version_key   # sort key for versions (by date when they are branches)
        self.range = None         # [(tag_or_None, meta_or_None)] ascending
        self.range_all = 0
        self.last_term = ""
        self.search_fn = None

    def reset_after(self, level):
        order = ["platform", "account", "repo", "range"]
        i = order.index(level)
        if i < 1:
            self.gh_account = self.gh_token = self.gl_token = self.gl_user = self.account_label = None
        if i < 2:
            self.set_repo(None)
        if i < 3:
            self.releases = None
            self.range = None
            self.last_term = ""

    def set_repo(self, repo, project_id=None):
        self.repo, self.project_id = repo, project_id
        self.default_branch = None
        self.repo_label = repo
        if not repo:
            self.repo_url = None
        elif self.platform == "GitHub":
            self.repo_url = f"https://github.com/{repo}"
        else:
            self.repo_url = f"{gitlab_base()}/{repo}"

    def release_url(self, tag):
        if self.platform == "GitHub":
            return f"https://github.com/{self.repo}/releases/tag/{parse.quote(tag, safe='')}"
        return f"{gitlab_base()}/{self.repo}/-/releases/{parse.quote(tag, safe='')}"

    def file_url(self, ref, path):
        ref = ref or self.default_branch or "HEAD"
        if self.platform == "GitHub":
            return f"https://github.com/{self.repo}/blob/{parse.quote(ref, safe='/')}/{path}"
        return f"{gitlab_base()}/{self.repo}/-/blob/{ref}/{path}"

    def compare_url(self, base, head):
        base, head = base or self.default_branch or "HEAD", head or self.default_branch or "HEAD"
        if self.platform == "GitHub":
            return f"https://github.com/{self.repo}/compare/{parse.quote(base, safe='')}...{parse.quote(head, safe='')}"
        return f"{gitlab_base()}/{self.repo}/-/compare/{parse.quote(base, safe='')}...{parse.quote(head, safe='')}"

    def range_summary(self):
        if not self.range:
            return ""
        first, last = self.range[0], self.range[-1]

        def lab(item):
            tag, meta = item
            return f"{BOLD}{tag or 'default branch'}{RESET} {DIM}{fmt_date((meta or {}).get('released_at')) if tag else ''}{RESET}".rstrip()

        if len(self.range) == 1:
            return lab(first)
        return f"{lab(first)} → {lab(last)}  {DIM}· {len(self.range)} releases{RESET}"


def step_platform(state, direction):
    screen(state, 0)
    items = [
        ("GitHub", f"{BOLD}GitHub{RESET}  {DIM}github.com · uses your gh CLI login{RESET}"),
        ("GitLab", f"{BOLD}GitLab{RESET}  {DIM}{gitlab_base().split('://')[-1]} · GitLab token{RESET}"),
    ]
    choice = fzf_pick(items, "Platform", header="Where does the case begin?")
    if choice is BACK:
        return "back"
    if state.platform != choice:
        state.reset_after("platform")
        state.platform = choice
    return "next"


def step_account(state, direction):
    if state.platform == "GitLab":
        if direction == "back":
            return "back"
        if state.gl_token and state.gl_user:
            return "next"
        screen(state, 1)
        token = gitlab_token()
        if not token:
            notice("error", [
                f"{BOLD}GITLAB_TOKEN is not set.{RESET}",
                "Create a personal access token (scope: read_api) at:",
                f"  {gitlab_base()}/-/user_settings/personal_access_tokens",
                "Then add to ~/.zshrc:  export GITLAB_TOKEN=glpat-xxxxxxxx  (or run ./setup.command)",
                f"{DIM}(open a brand-new Terminal window afterwards){RESET}",
            ], title="GitLab token missing")
            pause()
            return "back"
        try:
            with Spinner("Checking your GitLab token"):
                user = gitlab_whoami(token)
        except RuntimeError as e:
            notice("error", [
                f"{BOLD}GitLab rejected this token.{RESET}", str(e)[:200],
                f"{DIM}Check GITLAB_TOKEN is current and has the 'read_api' scope.{RESET}",
            ], title="Authentication failed")
            pause()
            return "back"
        state.gl_token, state.gl_user = token, user
        state.account_label = f"{user} {DIM}(GitLab){RESET}"
        return "next"

    require("gh")
    add_new = "+ Add a new GitHub account (gh auth login)"
    while True:
        accounts = gh_accounts()
        if not accounts:
            screen(state, 1)
            notice("warn", ["No logged-in gh accounts found.", "Run:  gh auth login"], title="No accounts")
            pause()
            return "back"
        screen(state, 1)
        items = [(a, f"{BOLD}{a}{RESET}" + (f"  {DIM}(current){RESET}" if a == state.gh_account else ""))
                 for a in accounts] + [(add_new, f"{GREEN}{add_new}{RESET}")]
        choice = fzf_pick(items, "Account", header="Which GitHub identity should investigate?")
        if choice is BACK:
            return "back"
        if choice == add_new:
            print(f"{CYAN}Launching gh auth login...{RESET}")
            gh_login()
            continue
        token = gh_token_for(choice)
        if not token:
            notice("error", [f"Could not get a gh token for {choice}."], title="Auth problem")
            pause()
            continue
        if state.gh_account != choice:
            state.reset_after("account")
        state.gh_account, state.gh_token = choice, token
        state.account_label = f"{choice} {DIM}(GitHub){RESET}"
        return "next"


PASTE_OPT = "__paste__"
OWNER_OPT = "__owner__"
MINE_OPT = "__mine__"
ALL_OPT = "__all__"


def repo_display(path):
    org, _, name = path.rpartition("/")
    return f"{DIM}{org}/{RESET}{BOLD}{name}{RESET}" if org else f"{BOLD}{name}{RESET}"


def step_repo(state, direction):
    gitlab = state.platform == "GitLab"
    while True:
        screen(state, 2)
        favs = FAVORITES_GITLAB if gitlab else FAVORITES_GITHUB
        items = [(f, repo_display(f)) for f in favs]
        if gitlab:
            items.append((ALL_OPT, f"{GREEN}+ List ALL projects you're a member of{RESET} {DIM}(may return nothing){RESET}"))
        else:
            items.append((MINE_OPT, f"{GREEN}+ Browse my own repos ({state.gh_account}){RESET}"))
            items.append((OWNER_OPT, f"{GREEN}+ Browse another org / owner…{RESET}"))
        items.append((PASTE_OPT, f"{GREEN}+ Paste a {'project' if gitlab else 'repo'} URL…{RESET}"))
        choice = fzf_pick(items, "Repository", header="Pick a repo to investigate (favorites first)",
                          query="")
        if choice is BACK:
            return "back"

        repo = None
        if choice == PASTE_OPT:
            raw = ask_line(f"{CYAN}❯{RESET} Paste URL {DIM}(empty = ← back){RESET}: ")
            if not raw:
                continue
            if gitlab:
                repo = re.sub(rf"^https?://{re.escape(gitlab_base().split('://')[-1])}/|\.git$|/$", "", raw)
            else:
                repo = re.sub(r"^git@github\.com:|^https?://github\.com/|\.git$|/$", "", raw)
                if "/" not in repo:
                    notice("error", [f"Could not parse owner/repo from: {raw}"], title="Bad URL")
                    pause()
                    continue
        elif choice == OWNER_OPT:
            org = ask_line(f"{CYAN}❯{RESET} Org or owner name, e.g. AppsFlyerSDK {DIM}(empty = ← back){RESET}: ")
            if not org:
                continue
            repos = gh_list_owner_repos(org, state.gh_token)
            if not repos:
                notice("warn", [f"No repos found under {org} (or this token can't see them)."], title="Nothing here")
                pause()
                continue
            screen(state, 2, subtitle=f"· {org}")
            sub = fzf_pick([(r, repo_display(r)) for r in repos], "Repository", header=f"{len(repos)} repos under {org}")
            if sub is BACK:
                continue
            repo = sub
        elif choice == MINE_OPT:
            repos = gh_list_my_repos(state.gh_token)
            if not repos:
                notice("warn", ["No repos found for this account."], title="Nothing here")
                pause()
                continue
            screen(state, 2, subtitle="· my repos")
            sub = fzf_pick([(r, repo_display(r)) for r in repos], "Repository", header=f"{len(repos)} repos")
            if sub is BACK:
                continue
            repo = sub
        elif choice == ALL_OPT:
            try:
                projects = gitlab_list_projects(state.gl_token)
            except RuntimeError as e:
                notice("error", [str(e)[:200]], title="Could not list projects")
                pause()
                continue
            if not projects:
                notice("warn", ["membership=true returned nothing for this token.",
                                "Pick a favorite or paste a project path instead."], title="No projects")
                pause()
                continue
            screen(state, 2, subtitle="· all projects")
            paths = sorted(p["path_with_namespace"] for p in projects)
            sub = fzf_pick([(p, repo_display(p)) for p in paths], "Project", header=f"{len(paths)} projects")
            if sub is BACK:
                continue
            repo = sub
        else:
            repo = choice

        project_id = None
        if gitlab:
            try:
                with Spinner(f"Resolving {repo}"):
                    data, _ = gitlab_api(f"projects/{parse.quote(repo, safe='')}", state.gl_token)
            except RuntimeError as e:
                notice("error", [f"Could not resolve {repo}", str(e)[:200]], title="GitLab lookup failed")
                pause()
                continue
            project_id, repo = data["id"], data["path_with_namespace"]

        if repo != state.repo:
            state.reset_after("repo")
        state.set_repo(repo, project_id)
        return "next"


def load_releases(state):
    """Versions for the picker: releases, topped up with plain git tags when there are fewer than two;
    GitLab repos with neither fall back to branches (ordered by their last commit date)."""
    if state.releases is not None:
        return
    with Spinner("Loading versions"):
        if state.platform == "GitHub":
            owner, name = state.repo.split("/", 1)
            rel = gh_list_releases(owner, name, state.gh_token)
            if len(rel) < 2:
                tags = only_version_tags(gh_list_tags(owner, name, state.gh_token))
                tags.update({k: v for k, v in rel.items() if v.get("released_at")})
                rel = {**tags, **rel} if rel else tags
        else:
            rel = gitlab_list_releases(state.project_id, state.gl_token)
            if len(rel) < 2:
                tags = only_version_tags(gitlab_list_tags(state.project_id, state.gl_token))
                rel = {**tags, **rel} if rel else tags
            if not rel:
                rel = gitlab_list_branches(state.project_id, state.gl_token)
                state.versions_are_branches = bool(rel)
        state.releases = rel
    if state.versions_are_branches:
        state.vkey = lambda t: (state.releases[t].get("released_at") or "", t)
    else:
        state.vkey = version_key


def step_versions(state, direction):
    screen(state, 3)
    try:
        load_releases(state)
    except RuntimeError as e:
        notice("error", [str(e)[:240]], title="Could not load releases")
        pause()
        return "back"

    if not state.releases:
        if direction == "back":
            return "back"
        if state.mode == "compare":
            notice("warn", ["Compare needs at least two versions to pick from.",
                            "This repo has no releases and no tags."], title="No versions")
            print()
            pause()
            return "back"
        notice("warn", ["This repo has no releases and no tags.", "The search will run on the default branch only."],
               title="No versions")
        state.range = [(None, None)]
        time.sleep(1.2)
        return "next"

    tags = sorted(state.releases.keys(), key=state.vkey)
    newest_first = list(reversed(tags))
    default_item = (None, f"{GREEN}(default branch){RESET} {DIM}— latest code, no specific version{RESET}")

    def item(tag):
        date = fmt_date(state.releases[tag].get("released_at"))
        return (tag, f"{BOLD}{tag:<18}{RESET} {CYAN}{date}{RESET}")

    cols = f"VERSION            RELEASED (yyyy/mm/dd)"
    from_items = [item(t) for t in tags] + [default_item]  # oldest -> newest

    def to_items(frm):
        """Only versions from `frm` upwards, newest first (default branch counts as newest)."""
        if frm is None:  # default branch has nothing newer: offer everything, newest first
            return [default_item] + [item(t) for t in newest_first]
        newer = [t for t in newest_first if state.vkey(t) >= state.vkey(frm)]
        if state.mode == "compare":
            newer = [t for t in newer if t != frm]
        return [default_item] + [item(t) for t in newer]

    phase, frm = "from", None
    while True:
        if phase == "from":
            screen(state, 3)
            frm = fzf_pick(from_items, "From", header=f"Oldest version of the range · {len(tags)} {'branches' if state.versions_are_branches else 'versions'} · oldest first\n{cols}")
            if frm is BACK:
                return "back"
            phase = "to"
        else:
            screen(state, 3, subtitle=f"· from {frm or 'default branch'}")
            to = fzf_pick(to_items(frm), "To", header=f"Newest version of the range (from {frm or 'default branch'} upwards) · newest first\n{cols}")
            if to is BACK:
                phase = "from"
                continue
            break

    if state.mode == "compare":
        if frm == to:
            notice("warn", ["Pick two different versions to compare."], title="Same version")
            print()
            pause()
            return step_versions(state, "fwd")
        if frm is None:
            pair = [to, None]
        elif to is None:
            pair = [frm, None]
        else:
            pair = sorted([frm, to], key=state.vkey)
        if pair[1] is None:
            state.range = [(pair[0], state.releases.get(pair[0])), (None, None)]
        else:
            i1, i2 = tags.index(pair[0]), tags.index(pair[1])
            state.range = [(t, state.releases.get(t)) for t in tags[i1:i2 + 1]]
        return "next"

    if frm is None or to is None:
        single = frm or to
        state.range = [(single, state.releases.get(single))] if single else [(None, None)]
    else:
        i1, i2 = tags.index(frm), tags.index(to)
        lo, hi = min(i1, i2), max(i1, i2)
        state.range = [(t, state.releases.get(t)) for t in tags[lo:hi + 1]]
    return "next"


def make_search_fn(state):
    if state.platform == "GitHub":
        owner, name = state.repo.split("/", 1)
        token = state.gh_token
        return lambda ref, term, cb: gh_search_version(owner, name, ref, term, token, cb)
    pid, path, token = state.project_id, state.repo, state.gl_token
    return lambda ref, term, cb: gitlab_search_version(pid, path, ref, term, token, cb)


def step_search(state, direction):
    state.search_fn = make_search_fn(state)
    while True:
        screen(state, 4)
        print(f"{DIM}  Enter = search · empty = ← back · :q = quit{RESET}")
        term = ask_line(f"{CYAN}❯{RESET} {BOLD}Search for{RESET} {DIM}(function / parameter / class){RESET}: ",
                        prefill=state.last_term)
        if not term:
            return "back"
        if term in (":q", ":quit"):
            return "quit"
        state.last_term = term

        screen(state, 4)
        print(f"  {BOLD}Investigating{RESET} {HL} {term} {RESET}\n")
        results = run_search(state, term)

        if not any(r.matches for r in results):
            screen(state, 4)
            lines = []
            for r in results:
                status = f"{YELLOW}⚠ {r.err[:80]}{RESET}" if r.err else f"{RED}✗ not found{RESET}"
                lines.append(f"{BOLD}{r.label:<18}{RESET} {DIM}{r.date}{RESET}  {status}")
            notice("warn", lines + ["", f"{ITALIC}The case went cold — no matches in any selected version.{RESET}"],
                   title="No matches")
            print()
            pause("Press Enter to search again")
            continue

        outcome = browse(state, results, term)
        if outcome == "quit":
            return "quit"


# ======================== compare: data layer ========================
def http_text(url, headers=None, timeout=30):
    req = request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}")
    except error.URLError as e:
        raise RuntimeError(f"network error for {url}: {e.reason}")


def count_patch(patch):
    a = d = 0
    for ln in (patch or "").split("\n"):
        if ln.startswith("+"):
            a += 1
        elif ln.startswith("-"):
            d += 1
    return a, d


def gh_compare(owner, repo, base, head, token):
    """Files changed between two refs (GitHub caps the list at 300 files)."""
    files, page = [], 1
    spec = f"{parse.quote(base, safe='')}...{parse.quote(head, safe='')}"
    while True:
        data, _ = gh_api(f"repos/{owner}/{repo}/compare/{spec}", token, {"per_page": 100, "page": page})
        batch = (data or {}).get("files", [])
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    out = []
    for f in files:
        status = f.get("status", "modified")
        status = status if status in ("added", "removed", "renamed") else "modified"
        out.append({"path": f["filename"], "old_path": f.get("previous_filename"), "status": status,
                    "add": f.get("additions", 0), "del": f.get("deletions", 0), "patch": f.get("patch")})
    return out, len(files) >= 300


def gl_compare(project_id, base, head, token):
    data, _ = gitlab_api(f"projects/{project_id}/repository/compare", token,
                         {"from": base, "to": head, "straight": "true"})
    out = []
    for d in (data or {}).get("diffs", []):
        status = ("added" if d.get("new_file") else "removed" if d.get("deleted_file")
                  else "renamed" if d.get("renamed_file") else "modified")
        a, r = count_patch(d.get("diff"))
        out.append({"path": d["new_path"], "old_path": d.get("old_path"), "status": status,
                    "add": a, "del": r, "patch": d.get("diff")})
    return out, bool((data or {}).get("compare_timeout"))


def resolve_ref(state, ref):
    """A concrete ref name; None means the repo's default branch."""
    if ref:
        return ref
    if not state.default_branch:
        if state.platform == "GitHub":
            data, _ = gh_api(f"repos/{state.repo}", state.gh_token)
        else:
            data, _ = gitlab_api(f"projects/{state.project_id}", state.gl_token)
        state.default_branch = (data or {}).get("default_branch") or "HEAD"
    return state.default_branch


def tree_map(state, ref):
    """path -> blob id for every file at ref."""
    if state.platform == "GitHub":
        owner, name = state.repo.split("/", 1)
        sha = gh_resolve_sha(owner, name, resolve_ref(state, ref), state.gh_token)
        return {e["path"]: e["sha"] for e in gh_get_tree(owner, name, sha, state.gh_token) if e.get("type") == "blob"}
    out, page = {}, 1
    while True:
        data, hdrs = gitlab_api(f"projects/{state.project_id}/repository/tree", state.gl_token,
                                {"recursive": "true", "ref": resolve_ref(state, ref), "per_page": 100, "page": page})
        for e in data or []:
            if e.get("type") == "blob":
                out[e["path"]] = e["id"]
        if not hdrs.get("x-next-page"):
            return out
        page = int(hdrs["x-next-page"])


def blob_text(state, sha):
    if state.platform == "GitHub":
        owner, name = state.repo.split("/", 1)
        return gh_get_blob_text(owner, name, sha, state.gh_token)
    d = os.path.join(CACHE_DIR, "gl_blobs", sha[:2])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sha)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    text = http_text(f"{gitlab_base()}/api/v4/projects/{state.project_id}/repository/blobs/{sha}/raw",
                     gitlab_headers(state.gl_token))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def tree_compare(state, base_ref, head_ref, max_patches=600):
    """Diff two refs by file tree + blob ids. Works even when the histories are unrelated."""
    A, B = tree_map(state, base_ref), tree_map(state, head_ref)
    added = [p for p in B if p not in A]
    removed = [p for p in A if p not in B]
    modified = [p for p in B if p in A and A[p] != B[p]]
    files, gone = [], {}
    for p in removed:
        gone.setdefault(A[p], []).append(p)
    for p in list(added):  # same content under a new path = rename
        olds = gone.get(B[p])
        if olds:
            old = olds.pop(0)
            files.append({"path": p, "old_path": old, "status": "renamed", "add": 0, "del": 0, "patch": None})
            added.remove(p)
            removed.remove(old)
    jobs = [("modified", p) for p in modified] + [("added", p) for p in added] + [("removed", p) for p in removed]
    done, total = 0, min(len(jobs), max_patches)

    def work(job):
        status, p = job
        if p.rsplit(".", 1)[-1].lower() in BINARY_EXT:
            return {"path": p, "old_path": None, "status": status, "add": 0, "del": 0, "patch": None}
        a = blob_text(state, A[p]) if p in A else ""
        b = blob_text(state, B[p]) if p in B else ""
        lines = list(difflib.unified_diff(a.split("\n"), b.split("\n"), lineterm="", n=3))[2:]
        patch = "\n".join(lines)
        ad, de = count_patch(patch)
        return {"path": p, "old_path": None, "status": status, "add": ad, "del": de, "patch": patch}

    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = []
        for i, job in enumerate(jobs):
            if i < max_patches:
                futs.append(ex.submit(work, job))
            else:
                files.append({"path": job[1], "old_path": None, "status": job[0], "add": 0, "del": 0, "patch": None})
        for fut in as_completed(futs):
            try:
                files.append(fut.result())
            except RuntimeError:
                pass
            done += 1
            draw_progress(done / max(total, 1), f"diffing files {done}/{total}", force=(done == total))
    clear_progress()
    note = "ℹ histories are unrelated — compared by file contents"
    if len(jobs) > max_patches:
        note += f" (diffs shown for the first {max_patches} of {len(jobs)} files)"
    return files, note


def fetch_compare(state, base_ref, head_ref):
    """Returns (files, note). Falls back to a file-tree diff when the compare API can't (unrelated tags)."""
    base, head = resolve_ref(state, base_ref), resolve_ref(state, head_ref)
    try:
        if state.platform == "GitHub":
            owner, name = state.repo.split("/", 1)
            files, truncated = gh_compare(owner, name, base, head, state.gh_token)
        else:
            files, truncated = gl_compare(state.project_id, base, head, state.gl_token)
        return files, ("⚠ the server lists at most 300 files — narrow with the filter" if truncated else "")
    except RuntimeError:
        clear_progress()
        return tree_compare(state, base_ref, head_ref)


def get_file_text(state, ref, path, blob=None):
    """Text of path at ref. GitHub uses the cached blob when we know its SHA."""
    if state.platform == "GitHub" and blob:
        owner, name = state.repo.split("/", 1)
        return gh_get_blob_text(owner, name, blob, state.gh_token)
    concrete = resolve_ref(state, ref)
    cache = None
    if ref:  # tags are stable, so they are safe to cache; the default branch is not
        key = hashlib.sha1(f"{state.repo}|{ref}|{path}".encode()).hexdigest()
        d = os.path.join(CACHE_DIR, "files", key[:2])
        os.makedirs(d, exist_ok=True)
        cache = os.path.join(d, key)
        if os.path.exists(cache):
            with open(cache, encoding="utf-8", errors="ignore") as f:
                return f.read()
    if state.platform == "GitHub":
        url = f"https://api.github.com/repos/{state.repo}/contents/{parse.quote(path)}?ref={parse.quote(concrete, safe='')}"
        hdrs = dict(gh_headers(state.gh_token), Accept="application/vnd.github.raw+json")
    else:
        url = (f"{gitlab_base()}/api/v4/projects/{state.project_id}/repository/files/"
               f"{parse.quote(path, safe='')}/raw?ref={parse.quote(concrete, safe='')}")
        hdrs = gitlab_headers(state.gl_token)
    text = http_text(url, hdrs)
    if cache:
        with open(cache, "w", encoding="utf-8") as f:
            f.write(text)
    return text


# ======================== compare: symbol extraction ========================
def _skip_literal(t, i):
    """If a comment or string starts at t[i], return the index just past it, else None."""
    c = t[i]
    if c == "/" and t.startswith("//", i):
        j = t.find("\n", i)
        return len(t) if j < 0 else j
    if c == "/" and t.startswith("/*", i):
        j = t.find("*/", i + 2)
        return len(t) if j < 0 else j + 2
    if c in "\"'`":
        if t.startswith(c * 3, i):
            j = t.find(c * 3, i + 3)
            return len(t) if j < 0 else j + 3
        j = i + 1
        while j < len(t):
            if t[j] == "\\":
                j += 2
                continue
            if t[j] == c:
                return j + 1
            if t[j] == "\n" and c != "`":
                return j
            j += 1
        return len(t)
    return None


def _match_close(t, i, open_c, close_c, limit=200000):
    """t[i] is open_c; return the index of its matching close_c, or -1."""
    depth, n = 0, min(len(t), i + limit)
    while i < n:
        s = _skip_literal(t, i)
        if s is not None:
            i = max(s, i + 1)
            continue
        c = t[i]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


_PREFIX_BAD = re.compile(r"(\.|=|\(|,|!|&&|\|\||\+|\?)\s*$|\b(return|new|throw|else|await|yield|in|is|as)\s*$")
_ANNOT = re.compile(r"^\s*(@\w|\[[A-Za-z]\w*.*\]\s*$)")


def _py_blocks(text, term):
    lines = text.split("\n")
    pat = re.compile(r"^([ \t]*)(?:async[ \t]+)?(?:def|class)[ \t]+" + re.escape(term) + r"\b", re.I | re.M)
    blocks = []
    for m in pat.finditer(text):
        li = text.count("\n", 0, m.start())
        indent = len(m.group(1).replace("\t", "    "))
        depth, k = 0, li
        while k < len(lines):  # skip a multi-line signature
            depth += lines[k].count("(") - lines[k].count(")")
            if depth <= 0:
                break
            k += 1
        end = k
        for j in range(k + 1, len(lines)):
            ln = lines[j]
            if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
                break
            if ln.strip():
                end = j
        sl = li
        while sl > 0 and lines[sl - 1].strip().startswith("@"):
            sl -= 1
        blocks.append((sl, min(end, sl + 400), li))
    return lines, blocks


def extract_blocks(text, term, path=""):
    """Definition blocks (function / method / class bodies) whose name is `term`."""
    lines = text.split("\n")
    found = []
    if path.endswith((".py", ".pyi")):
        lines, spans = _py_blocks(text, term)
        found = spans
    else:
        starts, off = [], 0
        for ln in lines:
            starts.append(off)
            off += len(ln) + 1
        pat = re.compile(r"(?<![\w$])" + re.escape(term) + r"\s*(?:<[^>\n]*>)?\s*\(", re.I)
        last_end = -1
        for m in pat.finditer(text):
            li = bisect.bisect_right(starts, m.start()) - 1
            if li <= last_end:
                continue
            prefix = text[starts[li]:m.start()]
            if prefix.strip().startswith(("//", "*", "/*", "#", '"', "'")) or _PREFIX_BAD.search(prefix):
                continue
            close_i = _match_close(text, m.end() - 1, "(", ")")
            if close_i < 0:
                continue
            j, brace, stop = close_i + 1, -1, min(len(text), close_i + 240)
            while j < stop:
                if text[j] == "/":
                    s = _skip_literal(text, j)
                    if s is not None:
                        j = max(s, j + 1)
                        continue
                c = text[j]
                if c == "{":
                    brace = j
                    break
                if c in ";()" or (c == "=" and text[j + 1:j + 2] != ">"):
                    break
                j += 1
            if brace < 0:
                continue
            end_i = _match_close(text, brace, "{", "}")
            el = (bisect.bisect_right(starts, end_i) - 1) if end_i >= 0 else li + 60
            el = min(el, li + 400)
            sl = li
            while sl > 0 and _ANNOT.match(lines[sl - 1]):
                sl -= 1
            found.append((sl, el, li))
            last_end = el
    return [{"start": sl + 1, "end": el + 1, "sig": lines[li].strip()[:120], "kind": "def",
             "text": textwrap.dedent("\n".join(lines[sl:el + 1])),
             "lines": list(range(sl + 1, el + 2))} for sl, el, li in found]


def usage_block(text, term, ctx=2, max_hits=60):
    """Fallback for parameters / constants / calls: the matching lines with a little context."""
    lines, tl = text.split("\n"), term.lower()
    hits = [i for i, ln in enumerate(lines) if tl in ln.lower()][:max_hits]
    if not hits:
        return None
    keep = set()
    for i in hits:
        keep.update(range(max(0, i - ctx), min(len(lines), i + ctx + 1)))
    out, nums, prev = [], [], -2
    for i in sorted(keep):
        if prev >= 0 and i != prev + 1:
            out.append("⋯")
            nums.append(None)
        out.append(lines[i])
        nums.append(i + 1)
        prev = i
    return {"start": hits[0] + 1, "end": hits[-1] + 1, "sig": lines[hits[0]].strip()[:120], "kind": "usage",
            "text": textwrap.dedent("\n".join(out)), "lines": nums}


def blocks_for_file(path, text, term):
    defs = extract_blocks(text, term, path)
    if defs:
        return defs
    u = usage_block(text, term)
    return [u] if u else []


def norm_text(t):
    return "\n".join(l.strip() for l in (t or "").split("\n") if l.strip())


def pair_blocks(a_map, b_map):
    """Match blocks of version A with blocks of version B and classify each pair."""
    A = {(p, i): b for p, bl in a_map.items() for i, b in enumerate(bl)}
    B = {(p, i): b for p, bl in b_map.items() for i, b in enumerate(bl)}
    pairs = [(k, k) for k in A if k in B]
    rest_a = [k for k in A if k not in B]
    rest_b = [k for k in B if k not in A]
    for ka in list(rest_a):  # moved to another file: same signature
        for kb in rest_b:
            if A[ka]["sig"] == B[kb]["sig"] and A[ka]["kind"] == B[kb]["kind"]:
                pairs.append((ka, kb))
                rest_a.remove(ka)
                rest_b.remove(kb)
                break
    entries = []
    for ka, kb in pairs:
        a, b = A[ka], B[kb]
        same = norm_text(a["text"]) == norm_text(b["text"])
        entries.append({"status": "unchanged" if same else "modified", "fmt_only": same and a["text"] != b["text"],
                        "path": kb[0], "old_path": ka[0] if ka[0] != kb[0] else None, "ka": ka, "kb": kb, "sig_a": a["sig"],
                        "sig": b["sig"], "start": b["start"], "a": a["text"], "b": b["text"], "kind": b["kind"]})
    for ka in rest_a:
        a = A[ka]
        entries.append({"status": "removed", "fmt_only": False, "path": ka[0], "old_path": None, "sig": a["sig"],
                        "start": a["start"], "a": a["text"], "b": "", "kind": a["kind"],
                        "ka": ka, "kb": None, "sig_a": a["sig"]})
    for kb in rest_b:
        b = B[kb]
        entries.append({"status": "added", "fmt_only": False, "path": kb[0], "old_path": None, "sig": b["sig"],
                        "start": b["start"], "a": "", "b": b["text"], "kind": b["kind"],
                        "ka": None, "kb": kb, "sig_a": b["sig"]})
    order = {"modified": 0, "added": 1, "removed": 2, "unchanged": 3}
    entries.sort(key=lambda e: (order[e["status"]], e["path"], e["start"]))
    return entries


# ======================== compare: diff rows ========================
_TOK = re.compile(r"\w+|\s+|[^\w\s]")


def _clean(s):
    return CTRL_RE.sub(" ", s.replace("\t", "    ")).rstrip()


def intraline(a, b):
    ta, tb = _TOK.findall(a), _TOK.findall(b)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    sa, sb = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        x, y = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        if op == "equal":
            sa.append((x, False))
            sb.append((y, False))
        else:
            if x:
                sa.append((x, True))
            if y:
                sb.append((y, True))
    return sa, sb


def diff_rows(a_text, b_text, ctx=3, a_lines=None, b_lines=None):
    """Line diff ignoring whitespace-only changes, with word-level highlights on changed lines.
    a_lines / b_lines optionally map a line index to its real file line number (None = unknown)."""
    a = a_text.split("\n") if a_text else []
    b = b_text.split("\n") if b_text else []

    def na(i):
        return (a_lines[i] if i < len(a_lines) else None) if a_lines else i + 1

    def nb(j):
        return (b_lines[j] if j < len(b_lines) else None) if b_lines else j + 1

    sm = difflib.SequenceMatcher(None, [l.strip() for l in a], [l.strip() for l in b], autojunk=False)
    ops = sm.get_opcodes()
    rows = []
    for idx, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            n = i2 - i1
            first, last = idx == 0, idx == len(ops) - 1
            head = 0 if first and not last else ctx
            tail = 0 if last and not first else ctx
            if (first and last) or n <= head + tail + 1:
                pre, post, gap = list(range(n)), [], 0
            else:
                pre, post, gap = list(range(head)), list(range(n - tail, n)), n - head - tail
            for off in pre:
                rows.append({"k": "eq", "a_no": na(i1 + off), "b_no": nb(j1 + off), "text": _clean(b[j1 + off])})
            if gap:
                rows.append({"k": "skip", "n": gap})
            for off in post:
                rows.append({"k": "eq", "a_no": na(i1 + off), "b_no": nb(j1 + off), "text": _clean(b[j1 + off])})
            continue
        dels, adds = a[i1:i2], b[j1:j2]
        d_segs = [None] * len(dels)
        a_segs = [None] * len(adds)
        if len(dels) == len(adds):
            for k in range(len(dels)):
                if difflib.SequenceMatcher(None, dels[k].strip(), adds[k].strip()).ratio() > 0.5:
                    d_segs[k], a_segs[k] = intraline(_clean(dels[k]), _clean(adds[k]))
        for k, ln in enumerate(dels):
            rows.append({"k": "del", "a_no": na(i1 + k), "text": _clean(ln), "segs": d_segs[k]})
        for k, ln in enumerate(adds):
            rows.append({"k": "add", "b_no": nb(j1 + k), "text": _clean(ln), "segs": a_segs[k]})
    return rows


def row_segs(r):
    k = r["k"]
    if k == "skip":
        return [(f"      ⋯ {r['n']} unchanged line(s)", "dim")]
    if k == "eq":
        return [(f"{r['b_no'] or '':>5}   ", "dim"), (r["text"], "ctx")]
    sign, style, no = ("-", "del", r.get("a_no")) if k == "del" else ("+", "add", r.get("b_no"))
    segs = [(f"{no or '':>5} ", "dim"), (f"{sign} ", style)]
    if r.get("segs"):
        segs += [(t, style + "_hl" if ch else style) for t, ch in r["segs"]]
    else:
        segs.append((r["text"], style))
    return segs


def patch_segs(ln):
    ln = _clean(ln)
    if ln.startswith("@@"):
        return [("      " + ln, "hunk")]
    if ln.startswith("+"):
        return [("    + ", "add"), (ln[1:], "add")]
    if ln.startswith("-"):
        return [("    - ", "del"), (ln[1:], "del")]
    if ln.startswith("\\"):
        return [("      " + ln, "dim")]
    return [("      ", "dim"), (ln[1:] if ln.startswith(" ") else ln, "ctx")]


def count_rows(rows):
    return sum(1 for r in rows if r["k"] == "add"), sum(1 for r in rows if r["k"] == "del")


def evolution_groups(items):
    """items: [(label, date, text[, meta])] in version order -> runs of identical (whitespace-insensitive) bodies."""
    groups = []
    for it in items:
        label, date, text = it[0], it[1], it[2]
        meta = it[3] if len(it) > 3 else None
        n = norm_text(text)
        if groups and groups[-1]["norm"] == n:
            g = groups[-1]
            g["labels"].append(label)
            g["dates"].append(date)
            g["meta_last"] = meta
        else:
            groups.append({"norm": n, "text": text, "labels": [label], "dates": [date],
                           "meta_first": meta, "meta_last": meta})
    return groups


def combined_text(bmap):
    parts = []
    for p in sorted(bmap):
        for b in bmap[p]:
            parts.append(f"// ── {p}")
            parts.append(b["text"])
    return "\n".join(parts)


# ======================== compare: tree viewer ========================
_NID = itertools.count(1)


def mk(segs, children=None, url=None):
    return {"id": next(_NID), "segs": segs, "children": children, "url": url}


def _kids(n):
    ch = n.get("children")
    if ch is None:
        return None
    if callable(ch):
        if "_k" not in n:
            n["_k"] = ch()
        return n["_k"]
    return ch


def _flatten(nodes, expanded, level=0, url=None, out=None):
    out = [] if out is None else out
    for n in nodes:
        u = n.get("url") or url
        out.append((level, n, u))
        if n["id"] in expanded:
            kids = _kids(n)
            if kids:
                _flatten(kids, expanded, level + 1, u, out)
    return out


def _all_ids(nodes, acc=None):
    acc = set() if acc is None else acc
    for n in nodes:
        kids = _kids(n)
        if kids is not None:
            acc.add(n["id"])
            _all_ids(kids, acc)
    return acc


_ANSI_STYLE = {
    "add": GREEN, "del": RED, "add_hl": _c("30;42"), "del_hl": _c("30;41"), "hunk": CYAN,
    "cyan": BOLD + CYAN, "ok": BOLD + GREEN, "bad": BOLD + RED, "warn": BOLD + YELLOW,
    "mag": MAGENTA, "dim": DIM, "bold": BOLD, "hl": HL,
}


def segs_ansi(segs):
    return "".join(f"{_ANSI_STYLE[s]}{t}{RESET}" if s in _ANSI_STYLE else t for t, s in segs)


def _print_tree(header, roots):
    for h in header:
        print(segs_ansi(h))
    print()
    for level, n, _ in _flatten(roots, _all_ids(roots)):
        print(" " * (1 + 2 * level) + segs_ansi(n["segs"]))


def _curses_styles(curses):
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    C = curses
    pairs = {1: (C.COLOR_RED, bg), 2: (C.COLOR_GREEN, bg), 3: (C.COLOR_YELLOW, bg), 5: (C.COLOR_MAGENTA, bg),
             6: (C.COLOR_CYAN, bg), 7: (C.COLOR_BLACK, C.COLOR_GREEN), 8: (C.COLOR_BLACK, C.COLOR_RED),
             9: (C.COLOR_BLACK, C.COLOR_YELLOW), 10: (C.COLOR_BLACK, C.COLOR_CYAN)}
    for n, (f, b) in pairs.items():
        curses.init_pair(n, f, b)
    cp = curses.color_pair
    return {"ctx": 0, "dim": C.A_DIM, "bold": C.A_BOLD, "add": cp(2), "del": cp(1),
            "add_hl": cp(7) | C.A_BOLD, "del_hl": cp(8) | C.A_BOLD, "hunk": cp(6), "cyan": cp(6) | C.A_BOLD,
            "ok": cp(2) | C.A_BOLD, "bad": cp(1) | C.A_BOLD, "warn": cp(3) | C.A_BOLD, "mag": cp(5),
            "hl": cp(9) | C.A_BOLD, "bar": cp(10) | C.A_BOLD, "url": cp(6) | C.A_UNDERLINE}


def view_tree(title, header, roots, expanded=()):
    """Expand/collapse tree browser. header: list of seg-lists. Returns 'back' or 'quit'."""
    expanded = set(expanded)
    try:
        import curses
    except ImportError:
        curses = None
    if curses is None or not sys.stdout.isatty():
        _print_tree(header, roots)
        pause("Press Enter to go back")
        return "back"
    os.environ.setdefault("ESCDELAY", "25")
    try:
        return curses.wrapper(lambda scr: _view_tree(scr, title, header, roots, expanded))
    except curses.error:
        _print_tree(header, roots)
        pause("Press Enter to go back")
        return "back"


def _view_tree(scr, title, header, roots, expanded):
    import curses

    curses.curs_set(0)
    S = _curses_styles(curses)
    rows = _flatten(roots, expanded)
    sel, top, flash = 0, 0, ("", 0.0)

    def put(y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return x
        text = clip_plain(text, w - x)
        try:
            scr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return x + vlen(text)

    def rebuild(keep_id=None):
        nonlocal rows, sel
        rows = _flatten(roots, expanded)
        if keep_id is not None:
            for i, (_, n, _) in enumerate(rows):
                if n["id"] == keep_id:
                    sel = i
                    break
        sel = max(0, min(sel, len(rows) - 1))

    def draw():
        nonlocal top
        scr.erase()
        h, w = scr.getmaxyx()
        put(0, 0, f" sdk-sleuth │ {title} ".ljust(w), S["bar"])
        for i, segs in enumerate(header):
            x = 0
            for t, st in segs:
                x = put(1 + i, x, t, S.get(st, 0))
        bs = 2 + len(header)
        put(bs, 0, "─" * w, S["dim"])
        bs += 1
        body_h = max(1, h - bs - 2)
        if sel < top:
            top = sel
        if sel >= top + body_h:
            top = sel - body_h + 1
        for i in range(body_h):
            ri = top + i
            if ri >= len(rows):
                break
            level, n, _ = rows[ri]
            rev = curses.A_REVERSE if ri == sel else 0
            y = bs + i
            if rev:
                put(y, 0, " " * w, rev)
            x = put(y, 0, " " * (1 + 2 * level), rev)
            kids = _kids(n)
            if kids is not None:
                x = put(y, x, ("▼ " if n["id"] in expanded else "▶ "), S["cyan"] | rev)
            else:
                x = put(y, x, "  ", rev)
            for t, st in n["segs"]:
                x = put(y, x, t, S.get(st, 0) | rev)
        url = rows[sel][2] if rows else None
        if flash[0] and time.time() - flash[1] < 2.5:
            put(h - 2, 0, f" ✓ {flash[0]}", S["ok"])
        elif url:
            x = put(h - 2, 0, " ↗ ", S["cyan"])
            put(h - 2, x, url, S["url"])
        x = 0
        for k, d in [("↑↓", "move"), ("⏎/→/←", "expand/collapse"), ("e/c", "all"), ("o", "open"),
                     ("y", "copy link"), ("b", "← back"), ("q", "quit")]:
            x = put(h - 1, x, f" {k}", S["cyan"])
            x = put(h - 1, x, f" {d} ", S["dim"])
        scr.refresh()

    scr.timeout(400)
    while True:
        draw()
        ch = scr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_RESIZE:
            scr.clear()
            continue
        if not rows:
            if ch in (ord("b"), 27, ord("q")):
                return "quit" if ch == ord("q") else "back"
            continue
        level, node, url = rows[sel]
        expandable = _kids(node) is not None
        h, _ = scr.getmaxyx()
        page = max(1, h - 6)
        if ch in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = min(len(rows) - 1, sel + 1)
        elif ch == curses.KEY_PPAGE:
            sel = max(0, sel - page)
        elif ch == curses.KEY_NPAGE:
            sel = min(len(rows) - 1, sel + page)
        elif ch in (curses.KEY_HOME, ord("g")):
            sel = 0
        elif ch in (curses.KEY_END, ord("G")):
            sel = len(rows) - 1
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            if expandable:
                expanded.symmetric_difference_update({node["id"]})
                rebuild(node["id"])
            elif url:
                webbrowser.open(url)
                flash = ("Opened in browser", time.time())
        elif ch == curses.KEY_RIGHT:
            if expandable and node["id"] not in expanded:
                expanded.add(node["id"])
                rebuild(node["id"])
            else:
                sel = min(len(rows) - 1, sel + 1)
        elif ch == curses.KEY_LEFT:
            if expandable and node["id"] in expanded:
                expanded.discard(node["id"])
                rebuild(node["id"])
            elif level > 0:
                for i in range(sel - 1, -1, -1):
                    if rows[i][0] == level - 1:
                        sel = i
                        break
        elif ch == ord("e"):
            expanded |= _all_ids(roots)
            rebuild(node["id"])
        elif ch == ord("c"):
            expanded.clear()
            rebuild()
            sel = 0
        elif ch == ord("o") and url:
            webbrowser.open(url)
            flash = ("Opened in browser", time.time())
        elif ch == ord("y") and url:
            try:
                subprocess.run(["pbcopy"], input=url, text=True, check=True)
                flash = ("Copied link to clipboard", time.time())
            except Exception:
                flash = ("Clipboard unavailable", time.time())
        elif ch in (ord("b"), 27, curses.KEY_BACKSPACE, 127, 8):
            return "back"
        elif ch in (ord("q"), ord("Q")):
            return "quit"


# ======================== compare: flows ========================
FILE_STATUS = {"added": ("A", "ok"), "removed": ("D", "bad"), "modified": ("M", "warn"), "renamed": ("R", "mag")}
VERDICT_BADGE = {"modified": ("✎ MODIFIED", "warn"), "added": ("✚ ADDED", "ok"),
                 "removed": ("✗ REMOVED", "bad"), "unchanged": ("= UNCHANGED", "dim")}


def compare_labels(state):
    base, head = state.range[0], state.range[-1]
    return (base[0] or "default branch"), (head[0] or "default branch")


def compare_overview(state):
    base_ref, head_ref = state.range[0][0], state.range[-1][0]
    blabel, hlabel = compare_labels(state)
    screen(state, 4, subtitle=f"· {blabel} → {hlabel}")
    with Spinner("Asking the server for the diff"):
        files, note = fetch_compare(state, base_ref, head_ref)
    if not files:
        notice("ok", [f"{BOLD}{blabel}{RESET} and {BOLD}{hlabel}{RESET} have identical files."], title="No differences")
        print()
        pause()
        return "back"
    flt = ask_line(f"{CYAN}❯{RESET} Filter by path {DIM}(text or regex, empty = all {len(files)} files){RESET}: ")
    if flt:
        try:
            rx = re.compile(flt, re.I)
            files = [f for f in files if rx.search(f["path"])]
        except re.error:
            files = [f for f in files if flt.lower() in f["path"].lower()]
        if not files:
            notice("warn", [f"No changed file matches “{flt}”."], title="Filter")
            print()
            pause()
            return "back"
    order = {"added": 0, "removed": 1, "renamed": 2, "modified": 3}
    files.sort(key=lambda f: (order[f["status"]], f["path"]))
    counts = {k: sum(1 for f in files if f["status"] == k) for k in order}
    tot_a, tot_d = sum(f["add"] for f in files), sum(f["del"] for f in files)
    header = [[(" ", "ctx"), (blabel, "bold"), (" → ", "dim"), (hlabel, "bold"),
               (f"   {len(files)} file(s)  ", "dim"), (f"+{tot_a}", "add"), (" ", "ctx"), (f"−{tot_d}", "del")],
              [(" ", "ctx"), (f"A {counts['added']} added", "ok"), ("   ", "ctx"),
               (f"D {counts['removed']} removed", "bad"), ("   ", "ctx"),
               (f"M {counts['modified']} modified", "warn"), ("   ", "ctx"),
               (f"R {counts['renamed']} renamed", "mag")]
              + ([(f"    {note}", "warn")] if note else [])]

    def patch_children(f):
        def build():
            if not f.get("patch"):
                return [mk([("(no textual patch — binary file or diff too large; open the link)", "dim")])]
            old_no = new_no = 0
            old_path = f.get("old_path") or f["path"]
            out = []
            for ln in f["patch"].split("\n"):
                url = None
                m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", ln)
                if m:
                    old_no, new_no = int(m.group(1)), int(m.group(2))
                elif ln.startswith("-"):
                    url = f"{state.file_url(base_ref, old_path)}#L{old_no}"
                    old_no += 1
                elif ln.startswith("+") or ln.startswith(" "):
                    url = f"{state.file_url(head_ref, f['path'])}#L{new_no}"
                    new_no += 1
                    if ln.startswith(" "):
                        old_no += 1
                out.append(mk(patch_segs(ln), url=url))
            return out
        return build

    roots = []
    for f in files:
        letter, style = FILE_STATUS[f["status"]]
        d, _, name = f["path"].rpartition("/")
        segs = [(f"{letter} ", style), ((d + "/") if d else "", "dim"), (name, "bold"),
                (f"  +{f['add']}", "add"), (f" −{f['del']}", "del")]
        if f["status"] == "renamed" and f.get("old_path"):
            segs.append((f"  ← {f['old_path']}", "dim"))
        ref = head_ref if f["status"] != "removed" else base_ref
        roots.append(mk(segs, patch_children(f), state.file_url(ref, f["path"])))
    return view_tree(f"{state.repo_label} │ compare", header, roots)


def collect_blocks(state, results, term):
    """Read the files each version matched in and extract the symbol's blocks. -> [path -> blocks]"""
    jobs = []
    for vi, r in enumerate(results):
        seen = {}
        for m in r.matches:
            seen.setdefault(m["path"], m.get("blob"))
        for p in sorted(seen)[:300]:
            jobs.append((vi, p, seen[p]))
    out = [dict() for _ in results]
    total, done = len(jobs), 0

    def work(job):
        vi, p, blob = job
        try:
            text = get_file_text(state, results[vi].tag, p, blob)
        except RuntimeError:
            text = ""
        return vi, p, blocks_for_file(p, text, term)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, j) for j in jobs]):
            vi, p, bl = fut.result()
            done += 1
            draw_progress(done / max(total, 1), f"reading files {done}/{total}", force=(done == total))
            if bl:
                out[vi][p] = bl
    clear_progress()
    return out


def ask_term(state, hint):
    screen(state, 4, subtitle="· " + " → ".join(compare_labels(state)))
    print(f"{DIM}  {hint} · empty = ← back{RESET}")
    term = ask_line(f"{CYAN}❯{RESET} {BOLD}Symbol{RESET} {DIM}(function / parameter / class){RESET}: ",
                    prefill=state.last_term)
    if term:
        state.last_term = term
    return term


def entry_block_at(entry, bmap):
    """(path, block) of this symbol in one version (bmap: path -> blocks), or (None, None) if absent."""
    for k in (entry.get("kb"), entry.get("ka")):
        if k and k[0] in bmap and k[1] < len(bmap[k[0]]):
            return k[0], bmap[k[0]][k[1]]
    sigs = {entry["sig"], entry.get("sig_a")}
    for path, bl in bmap.items():  # moved to another file
        for blk in bl:
            if blk["sig"] in sigs:
                return path, blk
    return None, None


def change_points(entry, ok, blocks):
    """Versions (in range order) where this symbol's body differs from the previous version."""
    series = []
    for i, r in enumerate(ok):
        path, blk = entry_block_at(entry, blocks[i])
        series.append((r.label, r.date, blk["text"] if blk else "",
                       (r.tag, path, blk["lines"]) if blk else None))
    groups = evolution_groups(series)
    cps = []
    for gi in range(1, len(groups)):
        prev, g = groups[gi - 1], groups[gi]
        status = "added" if (g["norm"] and not prev["norm"]) else "removed" if (prev["norm"] and not g["norm"]) else "modified"
        cps.append({"label": g["labels"][0], "date": g["dates"][0], "status": status,
                    "prev_text": prev["text"], "text": g["text"], "prev_label": prev["labels"][-1],
                    "meta": g["meta_first"], "prev_meta": prev["meta_last"]})
    return groups[0], cps


def diff_children(state, a_text, b_text, a_meta=None, b_meta=None, ctx=3):
    """Diff rows as tree nodes. Each row links to its exact line on GitHub / GitLab.
    meta = (ref, path, line_numbers) of the before / after text."""
    rows = diff_rows(a_text, b_text, ctx, a_meta[2] if a_meta else None, b_meta[2] if b_meta else None)
    nodes = []
    for r in rows:
        url = None
        if r["k"] in ("eq", "add") and b_meta and r.get("b_no") and b_meta[1]:
            url = f"{state.file_url(b_meta[0], b_meta[1])}#L{r['b_no']}"
        elif r["k"] == "del" and a_meta and r.get("a_no") and a_meta[1]:
            url = f"{state.file_url(a_meta[0], a_meta[1])}#L{r['a_no']}"
        nodes.append(mk(row_segs(r), url=url))
    return nodes, count_rows(rows)


def diff_node(state, segs, a_text, b_text, url=None, a_meta=None, b_meta=None):
    kids, (ad, de) = diff_children(state, a_text, b_text, a_meta, b_meta)
    return mk(segs + [(f"  +{ad}", "add"), (f" −{de}", "del")], kids, url)


def compare_symbol(state):
    term = ask_term(state, f"Scans all {len(state.range)} versions in the range and tells you where it changed.")
    if not term:
        return "back"
    blabel, hlabel = compare_labels(state)
    screen(state, 4)
    print(f"  {BOLD}Investigating{RESET} {HL} {term} {RESET}  {DIM}{blabel} → {hlabel} ({len(state.range)} versions){RESET}\n")
    results = run_search(state, term, state.range)
    ok = [r for r in results if not r.err]
    if len(ok) < 2 or results[0].err or results[-1].err:
        bad = [r for r in results if r.err]
        notice("error", [f"{r.label}: {r.err[:160]}" for r in bad] or ["Not enough versions to compare."], title="Search failed")
        print()
        pause()
        return "back"
    if not any(r.matches for r in ok):
        screen(state, 4)
        notice("warn", [f"“{term}” does not appear in any version from {blabel} to {hlabel}.",
                        f"{ITALIC}The case went cold.{RESET}"], title="Not found")
        print()
        pause()
        return "back"
    blocks = collect_blocks(state, ok, term)
    entries = pair_blocks(blocks[0], blocks[-1])
    counts = {k: sum(1 for e in entries if e["status"] == k) for k in VERDICT_BADGE}
    changed = counts["modified"] + counts["added"] + counts["removed"]
    verdict = ("CHANGED", "warn") if changed else ("UNCHANGED", "ok")
    base_label, head_label = ok[0].label, ok[-1].label
    tags = {r.label: r.tag for r in ok}
    roots, all_cps = [], []
    for e in entries:
        base_group, cps = change_points(e, ok, blocks)
        cp_labels = [c["label"] for c in cps]
        for c in cp_labels:
            if c not in all_cps:
                all_cps.append(c)
        badge, style = VERDICT_BADGE[e["status"]]
        if e["fmt_only"]:
            badge += " (formatting only)"
        segs = [(badge + "  ", style), (e["sig"][:60], "bold"), (f"   {e['path']}", "dim"), (f":{e['start']}", "dim")]
        if e["old_path"]:
            segs.append((f"  ← moved from {e['old_path']}", "mag"))
        if cps and e["status"] != "unchanged":
            shown = ", ".join(cp_labels[:4]) + (f" +{len(cp_labels) - 4}" if len(cp_labels) > 4 else "")
            segs.append((f"   Δ {shown}", "warn"))
        pa, ba = entry_block_at(e, blocks[0])
        pb, bb = entry_block_at(e, blocks[-1])
        a_meta = (ok[0].tag, pa, ba["lines"]) if ba else None
        b_meta = (ok[-1].tag, pb, bb["lines"]) if bb else None
        ref_tag, ref_path = (ok[0].tag, pa) if e["status"] == "removed" else (ok[-1].tag, pb or e["path"])
        url = state.file_url(ref_tag, ref_path or e["path"])
        kids = []
        if e["status"] == "unchanged":
            kids, _ = diff_children(state, e["b"], e["b"], b_meta, b_meta)
        else:
            if len(cps) >= 2:
                kids.append(diff_node(state, [("Σ whole range   ", "cyan"), (f"{base_label} → {head_label}", "bold")],
                                      e["a"], e["b"], state.compare_url(tags[base_label], tags[head_label]),
                                      a_meta, b_meta))
            for ci, c in enumerate(cps):
                icon, st = {"added": ("✚", "ok"), "removed": ("✗", "bad"), "modified": ("✎", "warn")}[c["status"]]
                head = [(f"{icon} {c['label']:<14}", st), (f" {c['date']}   ", "dim")]
                cum_url = state.compare_url(tags[base_label], tags[c["label"]])
                cum = diff_node(state, head + [(f"{base_label} → {c['label']}", "bold")],
                                base_group["text"], c["text"], cum_url, base_group["meta_first"], c["meta"])
                if ci == 0:
                    kids.append(cum)  # the first change: since-the-start == this step
                    continue
                since = diff_node(state, [("since the start   ", "dim"), (f"{base_label} → {c['label']}", "bold")],
                                  base_group["text"], c["text"], cum_url, base_group["meta_first"], c["meta"])
                step = diff_node(state, [("this step only   ", "dim"), (f"{c['prev_label']} → {c['label']}", "bold")],
                                 c["prev_text"], c["text"],
                                 state.compare_url(tags[c["prev_label"]], tags[c["label"]]), c["prev_meta"], c["meta"])
                kids.append(mk(cum["segs"], [since, step], cum_url))
        roots.append(mk(segs, kids, url))
    skipped = [r.label for r in results if r.err]
    header = [[(" ", "ctx"), (f" {term} ", "hl"), ("  ", "ctx"), (blabel, "bold"), (" → ", "dim"), (hlabel, "bold"),
               (f"   {len(ok)} versions scanned", "dim"), ("    Verdict: ", "dim"), (verdict[0], verdict[1])],
              [(" ", "ctx"), (f"✎ {counts['modified']} modified", "warn"), ("   ", "ctx"),
               (f"✚ {counts['added']} added", "ok"), ("   ", "ctx"), (f"✗ {counts['removed']} removed", "bad"),
               ("   ", "ctx"), (f"= {counts['unchanged']} unchanged", "dim")],
              [(" ", "ctx"), ("changed in: ", "dim"),
               (", ".join(all_cps) or "no version in the range", "warn" if all_cps else "ok")]
              + ([(f"    ⚠ skipped (error): {', '.join(skipped)}", "bad")] if skipped else [])]
    return view_tree(f"{state.repo_label} │ “{term}”", header, roots)


def compare_evolution(state):
    term = ask_term(state, f"Scan all {len(state.range)} versions to find where it changed.")
    if not term:
        return "back"
    screen(state, 4)
    print(f"  {BOLD}Tracing{RESET} {HL} {term} {RESET}  {DIM}across {len(state.range)} versions{RESET}\n")
    results = run_search(state, term, state.range)
    ok = [r for r in results if not r.err]
    skipped = [r.label for r in results if r.err]
    if not ok or not any(r.matches for r in ok):
        screen(state, 4)
        notice("warn", [f"“{term}” was not found in any selected version."], title="Not found")
        print()
        pause()
        return "back"
    blocks = collect_blocks(state, ok, term)
    groups = evolution_groups([(r.label, r.date, combined_text(b)) for r, b in zip(ok, blocks)])
    roots, expand, change_labels = [], set(), []
    for gi, g in enumerate(groups):
        prev = groups[gi - 1] if gi else None
        present = bool(g["norm"])
        if gi == 0:
            status = ("● present (baseline)", "cyan") if present else ("✗ not present", "bad")
        elif not present:
            status = ("✗ REMOVED", "bad")
        elif not prev["norm"]:
            status = ("✚ ADDED", "ok")
        else:
            status = ("✎ CHANGED", "warn")
        if gi and status[0] != "✗ not present":
            change_labels.append(g["labels"][0])
        labels = g["labels"]
        span = labels[0] if len(labels) == 1 else f"{labels[0]} → {labels[-1]}"
        d0, d1 = g["dates"][0], g["dates"][-1]
        dates = d0 if d0 == d1 else f"{d0} … {d1}"
        rows = diff_rows(g["text"], g["text"]) if gi == 0 else diff_rows(prev["text"], g["text"])
        a, d = count_rows(rows)
        segs = [(f"{span:<26}", "bold"), (f" {dates:<24}", "dim"), (status[0], status[1])]
        if gi:
            segs += [(f"  +{a}", "add"), (f" −{d}", "del")]
        segs.append((f"   {len(labels)} version(s)" if len(labels) > 1 else "", "dim"))
        url = state.compare_url(prev["labels"][-1], labels[0]) if gi else None
        node = mk(segs, [mk(row_segs(r)) for r in rows], url)
        roots.append(node)
    header = [[(" ", "ctx"), (f" {term} ", "hl"), (f"   {len(ok)} version(s) checked · {len(groups)} distinct variant(s)", "dim")],
              [(" ", "ctx"), ("changes at: ", "dim"), (", ".join(change_labels) or "none — identical in every version", "warn" if change_labels else "ok")]
              + ([(f"    ⚠ skipped (error): {', '.join(skipped)}", "bad")] if skipped else [])]
    return view_tree(f"{state.repo_label} │ evolution of “{term}”", header, roots, expand)


def step_compare(state, direction):
    state.search_fn = make_search_fn(state)
    blabel, hlabel = compare_labels(state)
    sub = f"· {blabel} → {hlabel}"
    items = [
        ("overview", f"{BOLD}📂 Release overview{RESET}  {DIM}every file added / removed / modified{RESET}"),
        ("symbol", f"{BOLD}🔍 Symbol verdict{RESET}  {DIM}did one function / parameter / class change? (with before/after diff){RESET}"),
        ("evolution", f"{BOLD}📈 Evolution{RESET}  {DIM}which version changed it, across all {len(state.range)} in the range{RESET}"),
    ]
    funcs = {"overview": compare_overview, "symbol": compare_symbol, "evolution": compare_evolution}
    while True:
        screen(state, 4, subtitle=sub)
        choice = fzf_pick(items, "Compare", header=f"{blabel} → {hlabel}  ·  what do you want to know?")
        if choice is BACK:
            return "back"
        try:
            outcome = funcs[choice](state)
        except RuntimeError as e:
            clear_progress()
            notice("error", [str(e)[:240]], title="Compare failed")
            print()
            pause()
            continue
        if outcome == "quit":
            return "quit"


def step_mode(state, direction):
    screen(state, -1, big=True)
    items = [
        ("search", f"{BOLD}🔍 Search{RESET}   {DIM}where does a function / parameter / class appear, per version{RESET}"),
        ("compare", f"{BOLD}⚖  Compare{RESET}  {DIM}what changed between two versions — files, functions, fixes{RESET}"),
    ]
    choice = fzf_pick(items, "Mode", header="What are we investigating?", back="quit")
    if choice is BACK:
        return "quit"
    state.mode = choice
    return "next"


def step_final(state, direction):
    return (step_compare if state.mode == "compare" else step_search)(state, direction)


STEPS = [step_mode, step_platform, step_account, step_repo, step_versions, step_final]


def main():
    if "--version" in sys.argv[1:]:
        print(f"sdk-sleuth {VERSION}")
        return
    require("fzf")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if "utf" not in (locale.getpreferredencoding(False) or "").lower():
        try:
            locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
        except locale.Error:
            pass

    state = State()
    i, direction = 0, "fwd"
    while 0 <= i < len(STEPS):
        result = STEPS[i](state, direction)
        if result == "next":
            i, direction = i + 1, "fwd"
        elif result == "back":
            i, direction = i - 1, "back"
        else:
            break
    if COLOR:
        print()
    print("\n".join(box([f"{ITALIC}{random.choice(SIGN_OFFS)}{RESET}"], color=fg256(39))))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{DIM}Bye.{RESET}")
