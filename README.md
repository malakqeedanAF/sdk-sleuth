# sdk-sleuth

Interactive terminal tool to search for a function/parameter/class across your
GitHub and GitLab repos — optionally scoped to a range of SDK release
versions, reporting per-version whether a match was found.

## Usage

Double-click `sdk-sleuth.command`, or run:

```bash
python3 sdk_sleuth.py
```

Flow:

1. Pick platform (GitHub / GitLab).
2. Pick an account (GitHub) or token (GitLab — set `GITLAB_TOKEN` first).
3. Pick a repo from your favorites, browse an org/owner, or paste a URL.
4. Pick a version range from the repo's releases (or default branch only).
5. Enter a search term — results are reported per version, with a clear
   "✓ found" / "✗ not found" marker for each one.

## Requirements

- `python3`
- [`fzf`](https://github.com/junegunn/fzf) (`brew install fzf`)
- [`gh`](https://cli.github.com) CLI, logged in (`gh auth login`) — for GitHub
- `GITLAB_TOKEN` env var (a personal access token with `read_api` scope) —
  for GitLab, pointed at `gitlab.appsflyer.com` by default
  (override with `GITLAB_BASE_URL`)

## Notes

- GitLab search is scoped server-side per ref (fast).
- GitHub's search API can't search old tags, so version-scoped GitHub search
  downloads that version's file tree and greps it directly. File contents are
  cached by Git blob SHA in `~/.sdk-sleuth/cache`, so overlapping version
  ranges only re-download files that actually changed between versions.
- Edit `FAVORITES_GITHUB` / `FAVORITES_GITLAB` in `sdk_sleuth.py` to change
  the quick-pick list.
