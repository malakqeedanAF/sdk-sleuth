# Changelog

All notable changes to sdk-sleuth are documented here.

## [1.1.2] - 2026-10-07

### Added
- **`o` opens the code, `d` opens the comparison.** `o` goes to the file at the exact line (at that version); `d` opens the GitHub/GitLab compare page scrolled to the same file and line. `y` / `Y` copy the code / diff link. Works in Release overview, Symbol verdict and Evolution.
- Evolution header explains how to read the rows (first → last version with identical code, +/− vs the row above).

### Fixed
- Evolution links used the short version name (`6.16.0-rc1`) instead of the real ref (`releases/6.x.x/6.16.x/6.16.0-rc1`), opening the wrong GitLab revision.
- Evolution `d` now scrolls to the first changed file and line (it opened the compare page without an anchor).
- GitLab compare links use `?straight=true`, so branches are diffed directly instead of showing "There isn't anything to compare".

## [1.1.0] - 2026-10-05

### Added
- **Compare mode** (new top-level choice at the start): see what changed between two versions.
  - **Release overview** — every file added / removed / modified / renamed with +/- counts, optional path
    filter, expand a file to see its colored patch.
  - **Symbol verdict** — type a function / parameter / class; the tool finds its definition in both versions
    (brace/indent matching, annotations included), classifies it as ✎ modified / ✚ added / ✗ removed /
    = unchanged (whitespace-only changes are ignored) and shows a before/after diff with word-level highlights.
  - Symbol verdict scans **every version in the range** and lists *where* it changed (`Δ v6.17.1, v6.18.3`);
    each change opens a diff from your From version (`6.16.2 → 6.17.1`, `6.16.2 → 6.18.3`) and a "this step only" diff.
  - **Evolution** — scans every version in the range and groups identical bodies, so you see exactly which
    release changed the symbol ("changes at: v6.17.1").
- Works when two tags have unrelated git histories (compares file trees by blob id instead of failing).
- Same expand/collapse viewer keys everywhere. `o` / `y` on a **diff line** open / copy the link to that exact line in the file (added & unchanged lines → new version, removed lines → old version); on a header they open the compare page. Diff line numbers are real file line numbers.

### Fixed
- GitLab repos now also list `releases/x.x.x/...` branches as versions (e.g. `appsflyer-android-sdk` previously started at v6.15.2, its first formal Release). Branches whose version already has a release/tag are skipped; version pickers sort by the branch's last path segment.
- Repos with fewer than two releases (e.g. one release but more tags) now also offer their git tags, so Compare works; `v.0.0.1`-style tags sort correctly.
- GitLab repos with neither releases nor tags offer their branches (by last commit date) instead of default-branch-only.
- Repos that tag versions without publishing formal Releases (e.g. `appsflyer.sdk.ios` on GitLab) now list their **git tags** (with commit dates) in the version pickers instead of falling back to the default branch only.

### Changed
- **Android & iOS on GitLab use only the `releases/<major>.x.x/<minor>.x/<version>` branches** (no formal Releases or tags), with just the highest rc per version (e.g. `6.16.0-rc2`). Controlled by `GITLAB_BRANCHES_ONLY` in `sdk_sleuth.py`.
- **Old SDKs hidden:** `GITLAB_MIN_MAJOR` (top of `sdk_sleuth.py`) limits Android and iOS on GitLab to v6.x.x+ (deprecated versions are never listed, scanned or even requested for `releases/…` branches, so loading and scans are faster). Edit the map to change/remove, or set `SDK_SLEUTH_ALL_VERSIONS=1` to show everything.
- **Search works with collapsed groups:** typing in a version picker searches *every* version (exact match, e.g. `6.18.1`), not just the opened groups. Opening/closing a group happens inside the same fzf session — no screen flicker or restart, and the cursor stays on the group.
- **Cleaner version pickers:** majors (4.x, 5.x, 6.x, 7.x …) are collapsible groups (⏎ to open; the newest major is open in *To*); names are short (`6.18.0_rc4`, not `releases/6.x.x/6.18.x/6.18.0_rc4`); each version lists the final release plus only its **latest** rc/beta. Rc numbers sort naturally (`_rc10` after `_rc9`). The scanned range still includes every version in between.
- Results (search, symbol verdict, evolution, overview) now always open **fully collapsed**; expand with Enter, `1 2 3` or `e`.
- Version pickers: **From** lists oldest → newest; **To** lists only From and newer, newest first.
- The wizard now has 6 steps (Mode · Platform · Account · Repository · Versions · Search/Compare).

## [1.0.1] - 2026-10-05

### Added / Changed

- **`setup.command`** — guided, re-runnable first-time setup: checks/installs
  Homebrew, python3, fzf and gh (asks before installing), signs in to GitHub,
  verifies and saves a GitLab token (`~/.sdk-sleuth/gitlab_token`). Flags:
  `--check`, `--yes`. The tool now also reads that token file when
  `GITLAB_TOKEN` is not set.

- **Detective-style UI** — block-letter banner with a magnifier, rotating
  tagline, rounded boxes, a step tracker (`● ● ◉ ○ ○  Step 3/5`) and a live
  "Case file" panel showing platform, account, repo, version range and term.
- **Colors & highlights** — the search term is highlighted (black on yellow)
  in every result; green ✓ / red ✗ per version; dim paths, bold file names.
- **Release dates** — every version now shows its release date as
  `yyyy/mm/dd` in the From/To pickers, the case file and the results.
- **Back everywhere** — each picker has a `← Back` entry (`✕ Quit` on the
  first screen); `Esc` / `Ctrl-B` also go back. Text prompts: empty = back,
  `:q` = quit. The search prompt pre-fills your previous term.
- **Expand / collapse results** — a new interactive browser:
  version ▸ file ▸ matching lines. Versions with hits open automatically;
  files collapse so you can scan quickly. Many matches in one file
  (e.g. `AppsFlyerObject.prefab` with 40 hits) are one row until expanded.
  Keys: `↑↓` move · `Enter`/`→` expand · `←` collapse/parent · `1 2 3` depth ·
  `e`/`c` expand/collapse all · `o` open in browser · `y` copy link ·
  `p` print a plain report (with URLs) · `b` back · `q` quit.
- **Real progress bar** — overall percentage across versions, with
  rotating detective one-liners.
- Every result row shows the matching line (windowed around the term) and
  the selected row's link in the status bar.

## [1.0.0]

First release. An interactive terminal tool for finding where a
function/parameter/class is used across your GitHub and GitLab repos —
across a range of SDK versions at once.

### Highlights

- **Two platforms, one tool** — search GitHub and GitLab repos from the same
  interactive flow.
- **Version-range search** — pick a "from" and "to" SDK release, and it
  searches every version in between, reporting a clear `✓ found` /
  `✗ not found` per version so you can see exactly where something was
  introduced or removed.
- **Favorites + org browsing** — quick-pick list of your SDK repos on both
  platforms, plus the ability to browse any GitHub org/owner by name (e.g.
  `AppsFlyerSDK`) when you need a repo outside the list.
- **Fast, cached GitHub version search** — GitHub's search API can't search
  old tags, so this downloads each version's file tree and greps it
  directly, caching file contents by Git blob SHA so overlapping version
  ranges only re-download files that actually changed between releases.
- **Native GitLab search** — uses GitLab's server-side per-ref blob search
  (`scope=blobs&ref=<tag>`), so GitLab version search is fast with no local
  scanning needed.
- **Clear, clickable results** — flat numbered list per version, grouped
  by nothing-you-don't-need, each result showing `file:line` and the full
  GitHub/GitLab URL for Cmd+click.
- **Double-click launcher** — `sdk-sleuth.command` on your Desktop, no
  terminal typing required to start it.

### Setup

- GitHub auth via the `gh` CLI (`gh auth login`); multiple accounts
  supported, switchable from the picker, with "+ Add a new account" built in.
- GitLab auth via `GITLAB_TOKEN` env var (personal access token, `read_api`
  scope), targeting `gitlab.appsflyer.com` by default
  (override with `GITLAB_BASE_URL`).

### Known limitations

- GitHub version-scoped search is network-heavier than GitLab's (tree +
  blob fetch vs. one server-side search call) — fine for a handful of
  versions, slower on a very large version range for a very large repo
  (first run only; cached afterward).
- GitLab's `/projects?membership=true` listing doesn't reliably reflect
  access granted via SSO/group-sync — the Favorites list and direct
  paste/URL input are the reliable paths when that happens.
- GitHub Releases (not plain tags) are what populate the version picker —
  a repo that only tags, without publishing formal Releases, will show no
  versions other than the default branch.
