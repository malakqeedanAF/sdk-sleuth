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
import json
import locale
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import error, parse, request

VERSION = "1.0.1"

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


STEP_TITLES = ["Platform", "Account", "Repository", "Versions", "Search"]


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
    dots = " ".join(
        f"{GREEN}●{RESET}" if i < idx else (f"{CYAN}{BOLD}◉{RESET}" if i == idx else f"{DIM}○{RESET}")
        for i in range(len(STEP_TITLES))
    )
    lines.append(f"{dots}  {BOLD}Step {idx + 1}/{len(STEP_TITLES)}{RESET} {DIM}·{RESET} {BOLD}{STEP_TITLES[idx]}{RESET}"
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
    t = tag.lstrip("vV")
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
                        "path": entry["path"], "line": i, "text": snippet(line, term),
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
    return os.environ.get("GITLAB_TOKEN", "").strip().strip('"').strip("'")


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


def run_search(state, term):
    versions = state.range
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
    total_files = sum(len(r.files) for r in results)
    total_lines = sum(len(r.matches) for r in results)
    if total_files <= 1:
        return 3
    if total_files + len(results) > 400:
        return 1
    if total_lines + total_files + len(results) <= 40:
        return 3
    return 2


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
        self.platform = None
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
    screen(state, 0, big=True)
    items = [
        ("GitHub", f"{BOLD}GitHub{RESET}  {DIM}github.com · uses your gh CLI login{RESET}"),
        ("GitLab", f"{BOLD}GitLab{RESET}  {DIM}{gitlab_base().split('://')[-1]} · uses $GITLAB_TOKEN{RESET}"),
    ]
    choice = fzf_pick(items, "Platform", header="Where does the case begin?", back="quit")
    if choice is BACK:
        return "quit"
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
                "Then add to ~/.zshrc:  export GITLAB_TOKEN=glpat-xxxxxxxx",
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
    if state.releases is not None:
        return
    with Spinner("Loading releases"):
        if state.platform == "GitHub":
            owner, name = state.repo.split("/", 1)
            state.releases = gh_list_releases(owner, name, state.gh_token)
        else:
            state.releases = gitlab_list_releases(state.project_id, state.gl_token)


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
        notice("warn", ["This repo has no formal releases.", "The search will run on the default branch only."],
               title="No releases")
        state.range = [(None, None)]
        time.sleep(1.2)
        return "next"

    tags = sorted(state.releases.keys(), key=version_key)
    newest_first = list(reversed(tags))
    default_item = (None, f"{GREEN}(default branch){RESET} {DIM}— latest code, no specific version{RESET}")

    def item(tag):
        date = fmt_date(state.releases[tag].get("released_at"))
        return (tag, f"{BOLD}{tag:<18}{RESET} {CYAN}{date}{RESET}")

    items = [default_item] + [item(t) for t in newest_first]
    phase, frm = "from", None
    while True:
        if phase == "from":
            screen(state, 3)
            frm = fzf_pick(items, "From", header=f"Oldest version of the range · {len(tags)} releases · newest first\n"
                                                 f"VERSION            RELEASED (yyyy/mm/dd)")
            if frm is BACK:
                return "back"
            phase = "to"
        else:
            screen(state, 3, subtitle=f"· from {frm or 'default branch'}")
            to = fzf_pick(items, "To", header=f"Newest version of the range (from: {frm or 'default branch'})\n"
                                              f"VERSION            RELEASED (yyyy/mm/dd)")
            if to is BACK:
                phase = "from"
                continue
            break

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


STEPS = [step_platform, step_account, step_repo, step_versions, step_search]


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
