#!/usr/bin/env python3
"""
sdk-sleuth - interactive terminal tool to search for a function/parameter/class
across your GitHub and GitLab repos, optionally scoped to an SDK release
version range.

Flow:
  1. Pick platform (GitHub / GitLab), then account (GitHub) or token (GitLab)
  2. Pick a repo (fuzzy picker) or paste a URL
  3. Pick a version range from the repo's releases (or default branch only)
  4. Enter a search term; results are reported per version, with a X mark
     for versions where nothing matched

Requires: python3, fzf, curl-reachable network. GitHub auth uses the `gh`
CLI (already logged in). GitLab auth uses the GITLAB_TOKEN env var.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import error, parse, request

# ---------- colors ----------
IS_TTY = sys.stdout.isatty()


def _c(code):
    return f"\033[{code}m" if IS_TTY else ""


BOLD = _c("1")
RESET = _c("0")
DIM = _c("2")
RED = _c("31")
GREEN = _c("32")
YELLOW = _c("33")
CYAN = _c("36")
MAGENTA = _c("35")

CACHE_DIR = os.path.expanduser("~/.sdk-sleuth/cache")
os.makedirs(CACHE_DIR, exist_ok=True)

SENTINEL_DEFAULT = "(default branch — no specific version)"

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

PASTE_URL_OPTION = "+ Paste a URL..."
BROWSE_OWNER_OPTION = "+ Browse repos under an org/owner..."
LIST_ALL_OPTION = "+ List ALL projects you're a member of (may be slow / may return nothing)"


def die(msg):
    print(f"{RED}Error: {msg}{RESET}", file=sys.stderr)
    sys.exit(1)


def require(cmd):
    if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
        die(f"{cmd} is not installed or not on PATH.")


# ---------- small UI helpers ----------
def fzf_pick(options, prompt):
    if not options:
        return None
    proc = subprocess.run(
        ["fzf", f"--prompt={prompt} > ", "--height=~60%", "--layout=reverse"],
        input="\n".join(options),
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip()
    return out if out else None


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def progress(msg):
    pad = max(0, 70 - len(msg))
    sys.stdout.write(f"\r{CYAN}{msg}{RESET}{' ' * pad}")
    sys.stdout.flush()


def clear_progress():
    sys.stdout.write("\r" + " " * 78 + "\r")
    sys.stdout.flush()


# ---------- HTTP helpers ----------
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


# ---------- version sorting ----------
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


def pick_version_range(tags_with_meta):
    """tags_with_meta: dict tag_name -> metadata dict (e.g. released_at)."""
    if not tags_with_meta:
        print(f"{YELLOW}No releases found; searching default branch only.{RESET}")
        return [(None, None)]

    tags_sorted = sorted(tags_with_meta.keys(), key=version_key)
    options = [SENTINEL_DEFAULT] + tags_sorted

    frm = fzf_pick(options, "From version")
    if frm is None:
        return None
    to = fzf_pick(options, "To version")
    if to is None:
        return None

    if frm == SENTINEL_DEFAULT and to == SENTINEL_DEFAULT:
        return [(None, None)]
    if frm == SENTINEL_DEFAULT or to == SENTINEL_DEFAULT:
        single = frm if frm != SENTINEL_DEFAULT else to
        return [(single, tags_with_meta.get(single))]

    i1, i2 = tags_sorted.index(frm), tags_sorted.index(to)
    lo, hi = min(i1, i2), max(i1, i2)
    selected = tags_sorted[lo:hi + 1]
    return [(t, tags_with_meta.get(t)) for t in selected]


# ======================== GitHub ========================
def gh_accounts():
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    combined = r.stdout + r.stderr
    return re.findall(r"Logged in to github\.com account (\S+)", combined)


def gh_login():
    subprocess.run(["gh", "auth", "login"])


def gh_token_for(owner):
    r = subprocess.run(["gh", "auth", "token", "--user", owner], capture_output=True, text=True)
    tok = r.stdout.strip()
    if not tok:
        die(f"could not get a gh token for account {owner}.")
    return tok


def gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def gh_api(path, token, params=None):
    url = f"https://api.github.com/{path}"
    if params:
        url += "?" + parse.urlencode(params)
    return http_json(url, headers=gh_headers(token))


def gh_list_my_repos(token):
    """All repos the token's own account can see (owner + org member), properly
    scoped to that token rather than gh's ambient 'active' account."""
    repos = []
    page = 1
    while True:
        progress(f"  loading your repos... page {page} ({len(repos)} so far)")
        data, _ = gh_api(
            "user/repos", token,
            {"affiliation": "owner,organization_member", "per_page": 100, "page": page},
        )
        if not data:
            break
        repos.extend(r["full_name"] for r in data)
        if len(data) < 100:
            break
        page += 1
    clear_progress()
    return repos


def gh_list_owner_repos(owner, token):
    """All repos under a given org/owner name, visible to this token.
    Tries the orgs endpoint first (covers private org repos), falls back
    to the users endpoint (covers a personal account)."""
    repos = []
    for kind in ("orgs", "users"):
        repos = []
        page = 1
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
            continue
    return repos


def gh_list_releases(owner, repo, token):
    releases = {}
    page = 1
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
    safe_repo = repo_key.replace("/", "_")
    d = os.path.join(CACHE_DIR, "gh_blobs", safe_repo, sha[:2])
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


def gh_search_version(owner, repo, ref, term, token):
    sha = gh_resolve_sha(owner, repo, ref or "HEAD", token)
    tree = gh_get_tree(owner, repo, sha, token)
    candidates = [
        e for e in tree
        if e.get("type") == "blob"
        and e.get("size", 0) < 3_000_000
        and e["path"].rsplit(".", 1)[-1].lower() not in BINARY_EXT
    ]
    total = len(candidates)
    done = 0
    matches = []
    term_lower = term.lower()

    def work(entry):
        text = gh_get_blob_text(owner, repo, entry["sha"], token)
        hits = []
        if term_lower in text.lower():
            for i, line in enumerate(text.splitlines(), start=1):
                if term_lower in line.lower():
                    hits.append((entry["path"], i))
        return hits

    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(work, e): e for e in candidates}
        for fut in as_completed(futures):
            done += 1
            progress(f"  scanning {ref or 'default'}: {done}/{total} files")
            matches.extend(fut.result())
    clear_progress()

    matches.sort(key=lambda m: (m[0], m[1]))
    urls = [
        (path, line, f"https://github.com/{owner}/{repo}/blob/{ref or sha}/{path}#L{line}")
        for path, line in matches
    ]
    return urls


def run_github():
    require("gh")
    accounts = gh_accounts()
    if not accounts:
        die("no logged-in gh accounts found. Run: gh auth login")

    add_new = "+ Add a new GitHub account (gh auth login)"
    owner = None
    while owner is None:
        choice = fzf_pick(accounts + [add_new], "Choose GitHub account")
        if choice is None:
            sys.exit(0)
        if choice == add_new:
            print(f"{CYAN}Launching gh auth login...{RESET}")
            gh_login()
            accounts = gh_accounts()
            continue
        owner = choice

    print(f"{GREEN}Using account:{RESET} {BOLD}{owner}{RESET}")
    token = gh_token_for(owner)

    my_repos_option = f"+ Browse my own repos ({owner})"
    menu_options = FAVORITES_GITHUB + [my_repos_option, BROWSE_OWNER_OPTION, PASTE_URL_OPTION]
    choice = fzf_pick(menu_options, "Choose repo (or an option below)")
    if choice is None:
        sys.exit(0)

    if choice == PASTE_URL_OPTION:
        repo_input = ask("Paste a GitHub repo URL and press enter: ")
        repo = re.sub(r"^git@github\.com:|^https?://github\.com/|\.git$|/$", "", repo_input)
        if "/" not in repo:
            die(f"could not parse owner/repo from: {repo_input}")
    elif choice == BROWSE_OWNER_OPTION:
        org = ask("Org or owner name (e.g. AppsFlyerSDK): ")
        if not org:
            die("no org/owner given.")
        repos = gh_list_owner_repos(org, token)
        if not repos:
            die(f"no repos found under {org} (or this token can't see them).")
        repo = fzf_pick(repos, "Choose repo")
        if repo is None:
            sys.exit(0)
    elif choice == my_repos_option:
        repos = gh_list_my_repos(token)
        if not repos:
            die(f"no repos found for {owner}.")
        repo = fzf_pick(repos, "Choose repo")
        if repo is None:
            sys.exit(0)
    else:
        repo = choice  # a favorite

    owner_, name = repo.split("/", 1)
    print(f"{GREEN}Repo:{RESET} {BOLD}{repo}{RESET}  {DIM}https://github.com/{repo}{RESET}")

    print(f"{DIM}Loading releases...{RESET}")
    releases = gh_list_releases(owner_, name, token)
    version_range = pick_version_range(releases)
    if version_range is None:
        sys.exit(0)

    search_loop(version_range, lambda ref, term: gh_search_version(owner_, name, ref, term, token))


# ======================== GitLab ========================
def gitlab_base():
    return os.environ.get("GITLAB_BASE_URL", "https://gitlab.appsflyer.com").rstrip("/")


def gitlab_token():
    tok = os.environ.get("GITLAB_TOKEN", "").strip().strip('"').strip("'")
    if not tok:
        print(f"{RED}Error: GITLAB_TOKEN is not set.{RESET}")
        print(f"Create a personal access token (scope: read_api) at:")
        print(f"  {gitlab_base()}/-/user_settings/personal_access_tokens")
        print("Then: export GITLAB_TOKEN=glpat-xxxxxxxxxxxx")
        print(f"{DIM}(if you just added this to ~/.zshrc, open a brand-new Terminal window first){RESET}")
        sys.exit(1)
    return tok


def gitlab_headers(token):
    return {"PRIVATE-TOKEN": token}


def gitlab_api(path, token, params=None):
    url = f"{gitlab_base()}/api/v4/{path}"
    if params:
        url += "?" + parse.urlencode(params)
    return http_json(url, headers=gitlab_headers(token))


def gitlab_whoami(token):
    """Validates the token and returns the authenticated username, or raises RuntimeError."""
    data, _ = gitlab_api("user", token)
    return data.get("username") if data else None


def gitlab_list_projects(token):
    projects = []
    page = 1
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
    releases = {}
    page = 1
    while True:
        data, hdrs = gitlab_api(
            f"projects/{project_id}/releases", token, {"per_page": 100, "page": page}
        )
        if not data:
            break
        for rel in data:
            releases[rel["tag_name"]] = {"released_at": rel.get("released_at")}
        next_page = hdrs.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    return releases


def gitlab_search_version(project_id, project_path, ref, term, token):
    results = []
    page = 1
    total = None
    while True:
        params = {"scope": "blobs", "search": term, "per_page": 100, "page": page}
        if ref:
            params["ref"] = ref
        url = f"{gitlab_base()}/api/v4/projects/{project_id}/search?" + parse.urlencode(params)
        data, hdrs = http_json(url, headers=gitlab_headers(token))
        if total is None:
            total = hdrs.get("x-total")
            total = int(total) if total else None
        if not data:
            break
        results.extend(data)
        if total:
            progress(f"  scanning {ref or 'default'}: {len(results)}/{total} matches")
        next_page = hdrs.get("x-next-page")
        if not next_page:
            break
        page = int(next_page)
    clear_progress()

    urls = []
    for item in results:
        path = item.get("path")
        line = item.get("startline", 1)
        effective_ref = item.get("ref") or ref or "HEAD"
        url = f"{gitlab_base()}/{project_path}/-/blob/{effective_ref}/{path}#L{line}"
        urls.append((path, line, url))
    urls.sort(key=lambda m: (m[0], m[1]))
    return urls


def run_gitlab():
    token = gitlab_token()

    try:
        username = gitlab_whoami(token)
    except RuntimeError as e:
        print(f"{RED}Error: GitLab rejected this token.{RESET}")
        print(f"  {e}")
        print(f"{DIM}Check that GITLAB_TOKEN is current and has at least the 'read_api' scope,")
        print(f"and that this is a fresh terminal (re-exported after editing ~/.zshrc).{RESET}")
        sys.exit(1)
    print(f"{GREEN}Authenticated to GitLab as:{RESET} {BOLD}{username}{RESET}")

    menu_options = FAVORITES_GITLAB + [LIST_ALL_OPTION, PASTE_URL_OPTION]
    choice = fzf_pick(menu_options, "Choose project (or an option below)")
    if choice is None:
        sys.exit(0)

    if choice == PASTE_URL_OPTION:
        repo_input = ask("Paste a GitLab project URL/path and press enter: ")
        project_path = re.sub(rf"^https?://{re.escape(gitlab_base().split('://')[-1])}/|\.git$|/$", "", repo_input)
        try:
            data, _ = gitlab_api(f"projects/{parse.quote(project_path, safe='')}", token)
        except RuntimeError as e:
            die(str(e))
        project_id = data["id"]
        project_path = data["path_with_namespace"]
    elif choice == LIST_ALL_OPTION:
        print(f"{DIM}Loading all projects you're a member of...{RESET}")
        projects = gitlab_list_projects(token)
        if not projects:
            print(f"{YELLOW}No projects came back for membership=true even though the token is valid.{RESET}")
            print(f"{DIM}This usually means your GitLab account has no direct/group project")
            print(f"memberships recorded (unusual), or your access is via a mechanism this")
            print(f"endpoint doesn't see. Try a favorite or paste a specific project path instead.{RESET}")
            sys.exit(1)
        by_path = {p["path_with_namespace"]: p for p in projects}
        pick = fzf_pick(sorted(by_path.keys()), "Choose GitLab project")
        if pick is None:
            sys.exit(0)
        project_id = by_path[pick]["id"]
        project_path = pick
    else:
        project_path = choice  # a favorite
        try:
            data, _ = gitlab_api(f"projects/{parse.quote(project_path, safe='')}", token)
        except RuntimeError as e:
            die(f"could not resolve favorite {project_path}: {e}")
        project_id = data["id"]
        project_path = data["path_with_namespace"]

    print(f"{GREEN}Project:{RESET} {BOLD}{project_path}{RESET}  {DIM}{gitlab_base()}/{project_path}{RESET}")

    print(f"{DIM}Loading releases...{RESET}")
    releases = gitlab_list_releases(project_id, token)
    version_range = pick_version_range(releases)
    if version_range is None:
        sys.exit(0)

    search_loop(
        version_range,
        lambda ref, term: gitlab_search_version(project_id, project_path, ref, term, token),
    )


# ======================== shared search loop ========================
def search_loop(version_range, search_fn):
    while True:
        print()
        term = ask("Enter a search term (function/parameter/class name), or leave blank to quit: ")
        if not term:
            print("Bye.")
            break

        any_found = False
        for ref, meta in version_range:
            label = ref or "default branch"
            released = f"  {DIM}({meta['released_at']}){RESET}" if meta and meta.get("released_at") else ""

            try:
                matches = search_fn(ref, term)
            except RuntimeError as e:
                print(f"{BOLD}> {label}{RESET}{released}  {RED}✗ error: {e}{RESET}")
                continue

            if matches:
                any_found = True
                print(f"{BOLD}> {label}{RESET}{released}  {GREEN}✓ {len(matches)} match(es){RESET}")
                for n, (path, line, url) in enumerate(matches, start=1):
                    print(f"   {n:2d}) {BOLD}{path}{RESET}:{line}")
                    print(f"       {CYAN}{url}{RESET}")
            else:
                print(f"{BOLD}> {label}{RESET}{released}  {RED}✗ not found{RESET}")

        if not any_found:
            print(f"\n{YELLOW}No matches in any selected version.{RESET}")


# ======================== entry point ========================
def main():
    require("fzf")

    platform = fzf_pick(["GitHub", "GitLab"], "Choose platform")
    if platform is None:
        sys.exit(0)

    if platform == "GitHub":
        run_github()
    else:
        run_gitlab()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
