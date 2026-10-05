# sdk-sleuth

Interactive terminal tool to search for a function/parameter/class across your
GitHub and GitLab repos — optionally scoped to a range of SDK release
versions, reporting per-version whether a match was found.

## Quick start (first time)

```bash
git clone https://github.com/malakqeedanAF/sdk-sleuth.git
cd sdk-sleuth
./setup.command        # or double-click it in Finder
```

`setup.command` is a guided, re-runnable installer. It:

1. checks for **Homebrew**, **python3 (3.8+)**, **fzf** and the **GitHub CLI (`gh`)**
   and offers to install anything missing (it always asks first);
2. signs you in to **GitHub** (`gh auth login`, opens your browser) if you
   aren't already;
3. signs you in to **GitLab**: opens the token page, you paste a personal
   access token (scope `read_api`), it is verified against the server and
   saved to `~/.sdk-sleuth/gitlab_token` (readable only by you);
4. optionally adds a Desktop shortcut and launches the tool.

Flags: `--check` (report only, change nothing), `--yes` (don't ask before
installing).

Notes:
- GitLab is internal (`gitlab.appsflyer.com`): connect to the **VPN / office
  network** first. Override the host with `GITLAB_BASE_URL`.
- If your GitHub org uses SSO, authorize your login for it after signing in.
- You can use a `GITLAB_TOKEN` env var instead of the saved token file; the
  env var wins if both exist.
- macOS only (Terminal.app, iTerm2 or an IDE terminal; UTF-8 + colors).

## Usage

Double-click `sdk-sleuth.command`, or run:

```bash
./sdk-sleuth.command
```

Flow:

1. Pick platform (GitHub / GitLab).
2. Pick an account (GitHub) or verify the token (GitLab).
3. Pick a repo from your favorites, browse an org/owner, or paste a URL.
4. Pick a version range (From / To) from the repo's releases, shown with
   release dates (or default branch only).
5. Enter a search term — results open in an interactive browser (version ▸
   file ▸ line) where you can expand/collapse, open or copy links.

Every step has a way back: the `← Back` entry, `Esc` / `Ctrl-B`, or an empty
input at text prompts (`:q` quits). Browser keys: `↑↓` move, `Enter`/`→`
expand, `←` collapse, `1 2 3` depth, `e`/`c` all, `o` open, `y` copy link,
`p` print report, `b` back, `q` quit.

## Requirements

Installed for you by `setup.command`: `python3`, [`fzf`](https://github.com/junegunn/fzf),
[`gh`](https://cli.github.com). GitLab needs a personal access token
(`read_api`).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `GitLab rejected this token` | Token expired/mistyped — re-run `./setup.command` |
| GitLab calls time out | Connect to the VPN / office network |
| GitHub repo 404s | Authorize your `gh` login for the org's SSO |
| "List ALL projects" returns nothing | Use the favorites or paste a project URL |
| macOS blocks the launcher | Right-click → Open once, or run `./setup.command` (clears quarantine) |

## Notes

- GitLab search is scoped server-side per ref (fast).
- GitHub's search API can't search old tags, so version-scoped GitHub search
  downloads that version's file tree and greps it directly. File contents are
  cached by Git blob SHA in `~/.sdk-sleuth/cache`, so overlapping version
  ranges only re-download files that actually changed between versions.
- Edit `FAVORITES_GITHUB` / `FAVORITES_GITLAB` in `sdk_sleuth.py` to change
  the quick-pick list.
