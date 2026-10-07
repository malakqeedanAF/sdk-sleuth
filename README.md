<div align="center">

```
╭─────────────────────────────────────────────────────────────────────────────────────────╮
│   █████  █████    ██    ████       █████  ██      ██████  ██   ██  ██████  ███   ███    │
│  ██      ██   ██    █  ██         ██      ██      ██      ██   ██    ██     ██   ██     │
│   ████   ██    ██   ████           ████   ██      █████   ██   ██    ██     ███████     │
│      ██  ██   ██    ██  █             ██  ██      ██      ██   ██    ██     ██   ██     │
│  █████   █████    ██    ████      █████   ██████  ██████   █████     ██    ███   ███    │
│                                                                                         │
│  "Every SDK release is a crime scene."                                                  │
╰─────────────────────────────────────────────────────────────────────────────────────────╯

```

**Find where a function, parameter or class lives | what changed | across SDK versions.**
GitHub + GitLab · interactive terminal UI · macOS

</div>

---

## 📑 Contents

|                                                 |                                         |                                   |
|-------------------------------------------------|-----------------------------------------|-----------------------------------|
| [⚡ Quick start and setup](#-quick-start)        | [🔍 Search mode](#-search-mode)         | [⚖️ Compare mode](#️-compare-mode) |
| [⌨️ Keyboard cheatsheet](#️-keyboard-cheatsheet) | [🧰 Troubleshooting](#-troubleshooting) | [🔬 How it works](#-how-it-works) |

---

## ⚡ Quick Start & Setup

```bash
git clone https://github.com/malakqeedanAF/sdk-sleuth.git
cd sdk-sleuth
./setup.command          # or double-click it in Finder
```

`setup.command` is a guided, re-runnable installer. It always asks before installing anything.

```
╭──────────────────────────────────────────────────────────────╮
│  1 ▸ Tools     Homebrew · python3 (3.8+) · fzf · gh          │
│                          │                                   │
│  2 ▸ GitHub    gh auth login  (opens your browser)           │
│                          │                                   │
│  3 ▸ GitLab    paste a read_api token → verified → saved     │
│                          │                                   │
│  4 ▸ Finish    optional Desktop shortcut · launch the tool   │
╰──────────────────────────────────────────────────────────────╯
```

| Flag      | Effect                                |
|-----------|---------------------------------------|
| `--check` | Report what's missing, change nothing |
| `--yes`   | Don't ask before installing           |

> - **GitLab is internal** (`gitlab.appsflyer.com`) — connect to the VPN / office network first. Override the host with `GITLAB_BASE_URL`.
> - **GitHub SSO:** if your org uses SSO, authorize your login for it after signing in.
> - **Token:** saved to `~/.sdk-sleuth/gitlab_token` (readable only by you). A `GITLAB_TOKEN` env var also works and wins if both exist.
> - **macOS only** — Terminal.app, iTerm2 or an IDE terminal (UTF-8 + colors).

Start the tool any time with `./sdk-sleuth.command` (or double-click it).

---

## 🔍 Search mode

Find every place a symbol appears, version by version.

```
╭─────────╮   ╭──────────╮   ╭─────────╮   ╭────────╮   ╭──────────╮   ╭─────────╮
│  Mode   │ ▸ │ Platform │ ▸ │ Account │ ▸ │  Repo  │ ▸ │ Versions │ ▸ │ Search  │
╰─────────╯   ╰──────────╯   ╰─────────╯   ╰────────╯   ╰──────────╯   ╰─────────╯
```

1. **Mode** — pick *Search*.
2. **Platform** — GitHub or GitLab.
3. **Account** — a `gh` account (GitHub) or token check (GitLab).
4. **Repo** — favorites, browse an org / owner, or paste a URL.
5. **Versions** — pick *From* (oldest → newest list) and *To* (only From and newer, newest first). Majors (4.x, 5.x, 6.x, 7.x …) are collapsed groups — press ⏎ on one to open it. Names are short (`6.18.0_rc4`) and only the latest rc per version is listed. Each version shows its release date `yyyy/mm/dd`.
6. **Search** — type a term. Results open in an expandable browser:

```
╭──────────────────────────────────────────────────────────────────╮
│ ▼ 6.16.0   2025/03/07   ✓ 40 matches in 2 files                  │
│     ▼ Assets/AppsFlyer/AppsFlyerObject.prefab  (3)               │
│         L5     m_ObjectHideFlags: 0                              │
│         L22    m_ObjectHideFlags: 0                              │
│         L36    m_ObjectHideFlags: 0                              │
│     ▶ Assets/AppsFlyer/AppsFlyer.cs  (37)                        │
│ ▶ 6.15.0   2025/01/12   ✓ 12 matches in 1 file                   │
│   6.14.0   2024/11/30   ✗ not found                              │
╰──────────────────────────────────────────────────────────────────╯
```

Every step has a way back: the **← Back** entry, `Esc` / `Ctrl-B`, or an empty input at text prompts (`:q` quits).

---

## ⚖️ Compare mode

What changed between two versions? Pick **Compare** on the first screen, choose the repo, then a *From* and *To* version.

| View                    | Answers                                                                                                                       |
|-------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| 📂 **Release overview** | Which files were added / removed / modified / renamed? (+/- counts, colored patches, path filter)                             |
| 🔍 **Symbol verdict**   | Did this function / parameter / class change? Marked ✎ modified · ✚ added · ✗ removed · = unchanged, with a before/after diff **and the version(s) where it changed** (e.g. `Δ 6.17.1, 6.18.3`), each with its own diff from your From version |
| 📈 **Evolution**        | In which release did it change? Groups identical versions across the whole range                                              |

Example verdict (a real bug fix — the null-check moved inside the lambda):

```
╭─ ✎ MODIFIED  startSDKwithHandler(MethodCall call, final Result result) ──╮
│    ⋯ 3 unchanged lines                                                   │
│  6     public void onSuccess() {                                         │
│  7   + uiThreadHandler.post(() -> {                                      │
│  8       if (mMethodChannel != null) {                                   │
│  8   -     uiThreadHandler.post(() -> mMethodChannel.invokeMethod(...))  │
│  9   +     mMethodChannel.invokeMethod("onSuccess", null);               │
│ 13   + });                                                               │
╰──────────────────────────────────────────────────────────────────────────╯
```

> - Whitespace-only changes are reported as *unchanged (formatting only)*.
> - Symbol detection matches braces / indentation (not a full parser). For constants and parameters it shows the matching lines with context.
> - Tags with **unrelated git histories** still work — the tool falls back to comparing file trees by content.

---

## ⌨️ Keyboard cheatsheet

```
╭─ Pickers ─────────────────────╮  ╭─ Results & diff browsers ──────────────────╮
│ type       filter the list    │  │ ↑ ↓ / j k    move                          │
│ ⏎          choose             │  │ ⏎ / →        expand                        │
│ Esc / ^B   ← back             │  │ ←            collapse / go to parent       │
│ ← Back     last list entry    │  │ 1 2 3        expand depth (search)         │
│ :q         quit (text prompt) │  │ e / c        expand / collapse all         │
│ empty      ← back (text)      │  │ o            open the code (exact line)    │
│ ⏎ on ▸ 6.x open/close a group │  │ d            open the diff (same change)   │
│ typing     searches all       │  │ y / Y        copy code / diff link         │
│            versions, too      │  │ p            print plain report (search)   │
╰───────────────────────────────╯  │ b            ← back                        │
                                   │ q            quit                          │
                                   ╰────────────────────────────────────────────╯
```

---

## 🧰 Troubleshooting

| Symptom                                       | Fix                                                                    |
|-----------------------------------------------|------------------------------------------------------------------------|
| `GitLab rejected this token`                  | Token expired or mistyped — re-run `./setup.command`                   |
| GitLab calls time out                         | Connect to the VPN / office network                                    |
| GitHub repo 404s                              | Authorize your `gh` login for the org's SSO                            |
| "List ALL projects" returns nothing           | Use the favorites or paste a project URL                               |
| macOS blocks the launcher                     | Right-click → Open once, or run `./setup.command` (clears quarantine)  |
| Symbol shows "unchanged" but the file changed | The edit is outside the symbol's own body — check the Release overview |

---

## 🔬 How it works

```
           ╭──────────── GitHub ────────────╮   ╭──────────── GitLab ────────────╮
 version   │ tag → commit → file tree       │   │ server-side search, per ref    │
 search    │ download blobs · grep locally  │   │ /search?scope=blobs&ref=<tag>  │
           │ cached by blob SHA             │   │ (fast, no local scanning)      │
           ╰────────────────────────────────╯   ╰────────────────────────────────╯
 compare   compare API ──(404 / unrelated tags)──▶ file-tree diff by blob id
```

- **GitHub's search API can't search old tags**, so version-scoped search downloads that version's file tree and greps it directly. File contents are cached by Git blob SHA in `~/.sdk-sleuth/cache`, so overlapping version ranges only re-download files that actually changed.
- **GitLab search** is scoped server-side per ref.
- Edit `FAVORITES_GITHUB` / `FAVORITES_GITLAB` in `sdk_sleuth.py` to change the quick-pick list.

### Requirements

Installed for you by `setup.command`: `python3`, [`fzf`](https://github.com/junegunn/fzf), [`gh`](https://cli.github.com). GitLab needs a personal access token (`read_api`).

---

<div align="center">

📜 [Changelog](CHANGELOG.md) · _Case closed. Happy hunting._

</div>
