# sdk-sleuth v1.0.0

First release. An interactive terminal tool for finding where a
function/parameter/class is used across your GitHub and GitLab repos —
across a range of SDK versions at once.

## Highlights

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

## Setup

- GitHub auth via the `gh` CLI (`gh auth login`); multiple accounts
  supported, switchable from the picker, with "+ Add a new account" built in.
- GitLab auth via `GITLAB_TOKEN` env var (personal access token, `read_api`
  scope), targeting `gitlab.appsflyer.com` by default
  (override with `GITLAB_BASE_URL`).

## Known limitations

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
