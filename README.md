# ESO Addon Manager

Install, scan, match and update Elder Scrolls Online addons from ESOUI.com.
Python 3.9+, standard library only.

## Install (put the script anywhere you like)
It is a single file with no dependencies — you only need **Python 3.9 or newer**. Check with `python3 --version`
(Windows: `py --version`). Not tested on real Windows or macOS yet — please report anything odd.

**Linux**
1. Python 3 is already there on most distros. If not: `sudo pacman -S python`, `sudo apt install python3` or `sudo dnf install python3`.
2. Save the script somewhere on your PATH, for example:
   `mkdir -p ~/.local/bin && cp eso_addon_manager.py ~/.local/bin/eso-addons && chmod +x ~/.local/bin/eso-addons`
3. Run `eso-addons setup` (or `python3 eso_addon_manager.py setup` from wherever you saved it).
   Proton/Wine/Lutris game folders are found by the search in `setup`.

**macOS**
1. The `python3` that Apple's Command Line Tools provide is 3.9, which works. A newer one (Homebrew
   `brew install python`, or python.org) also works.
2. Save the script in a folder you can find, e.g. `~/bin` (`mkdir -p ~/bin`, then copy it there).
   Mac is Unix underneath, so the Linux steps work the same: `python3 ~/bin/eso_addon_manager.py setup`.
3. The game folder is normally `~/Documents/Elder Scrolls Online`. `setup` will look for it.

**Windows**
1. Install Python from python.org (tick **"Add python.exe to PATH"** in the installer) or from the Microsoft Store.
2. Save the script in a folder of your own, e.g. `C:\Users\<you>\Tools\` (avoid `Program Files`).
3. Open Windows Terminal or PowerShell there and run `py eso_addon_manager.py setup`.
4. The game folder is normally `Documents\Elder Scrolls Online` (if OneDrive moved your Documents, `setup` finds it).
   To run it on a schedule use Task Scheduler (action: `py`, arguments: `C:\Users\<you>\Tools\eso_addon_manager.py update -y`).

On macOS a scheduled run is done with `launchd` (or `cron`) rather than a systemd timer; the command is the same.

## Tab completion (zsh, bash, fish)
The completion script is generated from the script itself, so it always matches the commands and options.
Regenerate it after updating the script. Pick your shell:

**zsh**
```
mkdir -p ~/.zfunc
eso-addons completion zsh > ~/.zfunc/_eso-addons
```
Make sure `~/.zshrc` has these lines, in this order (skip any you already have), then open a new terminal:
```
fpath=(~/.zfunc $fpath)
autoload -Uz compinit && compinit
```

*Or, without a file:* put this in `~/.zshrc` **after** `compinit` — it regenerates the completion each time a shell
starts, so it is always current (it costs one quick Python start per new shell):
```
if command -v eso-addons > /dev/null; then
  source <(eso-addons completion zsh)
fi
```
It registers the completion for the command name you ran it as, so this also works for a copy or symlink with another
name such as `eso-addons.py`.

**bash** (needs no extra package)
```
mkdir -p ~/.local/share/bash-completion/completions
eso-addons completion bash > ~/.local/share/bash-completion/completions/eso-addons
```
Open a new terminal. (If your bash doesn't load that folder, add `source <(eso-addons completion bash)` to `~/.bashrc`.)

**fish**
```
mkdir -p ~/.config/fish/completions
eso-addons completion fish > ~/.config/fish/completions/eso-addons.fish
```
Fish picks it up automatically.

What completes (same in all three; zsh and fish also show a description next to each item):
- commands and their options
- addon IDs and names for `list`, `lock`, `unlock`, `versions`, `info` and `remove` (plus folder names for `remove`)
- `live` / `pts` after `-e` and for `copy`, and after a `pts` prefix (`eso-addons pts lock <Tab>` offers the pts addons)
- backup numbers and `latest` for `restore`, fixed choices such as `--saved-vars` and `--with`, and folders for `--set-dir` / `--backup-dir`

It works for the command names `eso-addons` and `eso_addon_manager.py` when run directly (the file must be executable or on
your PATH). Completion only reads your config: it never touches the network or writes anything. Bash leaves out
`local:Folder` IDs because bash splits words at `:`; the folder names are offered instead.

## Where files are kept
| File | Where | What |
|---|---|---|
| `config.json` | **inside your `Elder Scrolls Online` folder**, next to `live/` and `pts/` | tracked addons, links, settings, TTC times — back it up with your addons |
| `location.json` | app folder (below) | one line: where the game folder is |
| `filelist.json` | app folder | cached ESOUI addon list (safe to delete) |

App folder: Linux `~/.config/eso_addon_manager` (or `$XDG_CONFIG_HOME`), macOS `~/Library/Application Support/eso_addon_manager`,
Windows `%LOCALAPPDATA%\eso_addon_manager`.

Before a game folder is set, `config.json` starts in the app folder and moves next to `live/` when you set it
(the old copy is kept as `config.moved.json.bak`). If you restore a backup of the game folder, or wipe the app folder, just run
`setup` / `config --set-dir` on the game folder: the `config.json` found there is used, with all your links.

## Data source
Uses the public MMOUI API that ESOUI belongs to:
- `https://api.mmoui.com/v3/game/ESO/filelist.json` – every addon (cached for 1 hour)
- `https://api.mmoui.com/v3/game/ESO/filedetails/<id>.json` – one addon + download URL

`UIDir` in the file list names the folders each addon installs, so folders are matched automatically.

## Guided setup (easiest)
```
python eso_addon_manager.py setup
```
Three short questions, each showing the current value (Enter keeps it):
1. **Game folder** – keep it, search for it (`s`, or `search /some/folder`), or type a path — a full path, or just a folder above the game like `/home/me` (or even `Gaming`, tried under your home folder), and it searches inside for the game folder
2. **Default game version** – only asked if more than one is found (e.g. `live` and `pts`)
3. **Saved settings on remove** – ask each time / always delete / never delete

It then offers to run `scan`. On the very first run of any command that uses the game folder, the tool
offers to run this setup for you. Run `setup` again whenever you want to change something.

## Tamriel Trade Centre price table
TTC needs a price table that is not part of the addon zip. If `TamrielTradeCentre` is in your AddOns folder,
`update` also downloads and unzips it, but at most once every N hours:
```
python eso_addon_manager.py ttc                      # download it right now (ignores the interval)
python eso_addon_manager.py config --ttc-hours 6     # minimum hours between downloads (default 6)
python eso_addon_manager.py config --ttc off         # stop doing it during update (default: on)
python eso_addon_manager.py config --ttc-region eu   # us (default) or eu server
```
The last download time is kept per environment (live / pts) in `config.json`, and shown by `config`.
A failed download leaves your current table and the timestamp alone. If the TTC addon itself was just
updated, the table is downloaded again regardless of the interval.

## Running it automatically (systemd user timer)
`update -y` needs no prompts, so it is safe for a timer. Example, `~/.config/systemd/user/eso-addons.service`:
```
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /path/to/eso_addon_manager.py update -y
```
and `eso-addons.timer`:
```
[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
[Install]
WantedBy=timers.target
```
**Exit codes:** `0` = everything fine, `1` = something failed (network or ESOUI error, a failed addon or TTC
download, an unusable game folder, another run already in progress), `130` = cancelled with Ctrl+C. A summary of
the problems is printed at the end, so a failed timer run shows as *failed* in `systemctl --user status eso-addons`
and the details are in `journalctl --user -u eso-addons`. A normal "all up to date" run exits `0`.

Enable with `systemctl --user enable --now eso-addons.timer`. Run `setup`, `scan` and `match` by hand first.

## Older versions and locking
If a new release breaks something, go back to a version that worked and lock it:
```
python eso_addon_manager.py versions HarvestMap                       # what ESOUI has (current + archived)
python eso_addon_manager.py install 57 --version 3.16.1 --lock        # that version, held there
python eso_addon_manager.py lock 57        # or: hold whatever is installed now      (ID or full addon name)
python eso_addon_manager.py unlock 57      # updates allowed again
```
- `update` asks `Update [a]ll, [c]hoose one by one, or [N]one?` (Enter = none). In *choose* mode each addon asks
  `[y]es, [N]o, [l]ock at <installed version>, [q]uit`.
- `lock`, `unlock` and `versions` take an ESOUI ID or a full addon/folder name (no prompt). A partial name such as `harvest`
  always asks first — `Did you mean HarvestMap [57]? [y/N]` for one match, or a numbered list when there are several (Enter = cancel).
- Locked addons are skipped by `update` and `update -y` (so also by a timer) and shown as
  `locked at 3.16.1 (3.17.0 available)`. The lock survives `scan`; `list` marks locked addons.
- Older versions are not in ESOUI’s API, so they are read from the addon’s web page (the archived-files table).
  Authors can delete old versions, and the page may still list a version that has been removed, so not every listed
  version can be installed. If ESOUI changes the page layout or refuses the request, the tool says so and saves the
  page it received to `last_esoui_page.html` in the app folder.
- Installing an older version replaces the addon's folders cleanly, so no files from the newer version are left behind.
  SavedVariables are never touched.

## Backups
```
python eso_addon_manager.py backup                 # live (the default environment)
python eso_addon_manager.py backup -e pts           # or: pts backup
python eso_addon_manager.py backup --with screenshots,addons
python eso_addon_manager.py backups                 # list the zips
python eso_addon_manager.py restore                 # pick one from a list (or: restore 1 / restore latest)
```
One zip per run for one environment (`live` unless you say `-e pts`). It always contains `config.json`,
`AddOnSettings.txt`, `UserSettings.txt`, `SavedVariables/` and `GuildHistory/` (if present). `Screenshots/` and `AddOns/`
are optional. Logs, Errors and the shader cache are never included. With those files you can rebuild your setup and
(through `config.json`, or `scan` + `update`) your addons.

- **Where:** the default folder is `Documents/ESO Addon Manager Backups` (Linux, macOS and Windows). It is created
  only when a backup first runs. To use another folder, set it first and it is created right away:
  `config --backup-dir /path/to/folder` (`config --backup-dir default` goes back to the default). Putting it outside the game
  folder is safer, because a wiped Wine/Proton prefix or Documents folder would take backups stored inside it too.
- **How many:** the newest 7 per environment (`config --backup-keep N`). Only files named `eso-<env>-<date>.zip` are
  ever deleted; anything else in the folder is left alone, and live and pts are counted separately.
- **Unchanged = skipped:** if nothing has changed since the last backup, no new zip is made, so a daily timer doesn't push
  your good older backups out. `--force` makes one anyway. (`config.json` changes in the backup/TTC settings don't count.)
- **Optional parts:** `config --backup-with screenshots,addons` makes them the default, or use `--with` for one run
  (`none` = only the basics).
- **Restore:** `restore` shows what will be overwritten and asks (`-y` skips the question). Files with the same name are
  replaced, files that aren't in the backup are left alone, and a safety copy of what is being replaced is saved first as
  `eso-<env>-before-restore-<date>.zip` (never deleted automatically). `config.json` is restored only if you answer yes
  or pass `--config`. Run `scan` afterwards if you restored AddOns or `config.json`. Close the game first.
- **Timer:** same as `update`, e.g. a second timer running `backup` later in the day, or run
  `python eso_addon_manager.py backup` from a Bottles post-run script. Exit code is `1` if a backup fails.
  Use a separate command per environment (`backup` and `backup -e pts`) if you want both.

## Folder setup (without the wizard)
Don't know where your game folder is? Let the tool find it:
```
python eso_addon_manager.py config --set-dir search              # usual places first, then your home folder
python eso_addon_manager.py config --set-dir search /home/nvme   # search somewhere specific
```
It looks for an `Elder Scrolls Online` folder that contains `live` / `pts`, shows what it found
(with the number of addon folders in each environment) and asks "Is this correct?" (Enter = yes).
If you run a command that needs the folder before setting it up, the tool offers to search right then.

Or point it at the folder yourself — the **Elder Scrolls Online** folder (the one that contains `live/`, `pts/`):

```
python eso_addon_manager.py config --set-dir "/home/nvme/Gaming/drive_c/users/steamuser/Documents/Elder Scrolls Online"
```
(Forgot the quotes around a path with spaces? That works too. A partial path works as well: `config --set-dir /home/nvme` searches inside it and asks you to confirm what it finds.)

It then knows the layout: `<root>/<env>/AddOns` and `<root>/<env>/SavedVariables`, where `<env>` is
`live` (default; used for both the NA and EU servers) or `pts`. You can also give it a `live/AddOns` or `pts` path; the environment is detected.
Each environment keeps its own tracked addons.

## First run
```
python eso_addon_manager.py scan      # auto-match folders to ESOUI
python eso_addon_manager.py match     # resolve whatever scan couldn't
python eso_addon_manager.py update    # check + apply updates
```

## Choosing an environment
```
python eso_addon_manager.py pts list          # prefix shortcut
python eso_addon_manager.py list --env pts    # same thing (-e pts works too)
python eso_addon_manager.py config --default-env pts   # change what "no environment" means
```

## Commands
| Command | What it does |
|---|---|
| `scan [--refresh]` | Match folders via `UIDir`, then fall back to manifest Title/Author/Version |
| `match` | Interactive: pick from ranked suggestions, type a new search, or `id 123` |
| `update [-y]` | Rescan the folder, compare with ESOUI's last-updated date, then `[a]ll / [c]hoose one by one / [N]one`. `-y` = all except locked addons |
| `search <words>` | Search ESOUI by name (local, instant); offers to install results (Enter = no) |
| `install <ID...>` | Install by ESOUI ID. One ID can take `--version <v>` (older version) and `--lock` |
| `versions <ID or name>` | List the current and older versions ESOUI has (`--all` for every one) |
| `lock` / `unlock <ID or name>` | Hold an addon at its installed version / allow updates again |
| `list [ID or name …]` | Show all tracked addons, or only those matching an ID, name, or part of a name or folder name |
| `info <ID>` | Inspect one addon on ESOUI |
| `remove <ID / local:Folder / folder name>` | Uninstall; a plain folder name also works for untracked folders (case-insensitive on Windows and macOS, case-sensitive on Linux) |
| `copy [SOURCE] DEST` | Copy an environment's AddOns to another, e.g. `copy live pts` |
| `backup` / `backups` / `restore` | Zip your settings and SavedVariables (`-e pts` for pts), list the zips, restore one |
| `ttc` | Download the Tamriel Trade Centre price table now |
| `completion zsh` / `bash` / `fish` | Print the tab-completion script for that shell |
| `setup` | Guided setup: game folder, default version, saved-settings behaviour |
| `config` | Show settings; `--set-dir <path>` / `--set-dir search [folder]`, `--saved-vars`, `--default-env` change them |

## SavedVariables on remove
`config --saved-vars ask|always|never` (default `ask`):
- **ask** – after you confirm the removal, asks whether to also delete the addon's saved variables (Enter = keep)
- **always** – deletes them without asking
- **never** – always keeps them

Files are found from the names the addon declares in its manifest (`## SavedVariables:`) plus the folder name,
and only if they exist (libraries usually have none). A file still declared by another installed addon is never deleted.
Flags on `remove`:
- `-y` skips the confirmation but keeps saved variables (unless the setting is `always`)
- `-yy` skips the confirmation **and deletes saved variables**, whatever the setting (including `never`)

## Copying live → pts
```
python eso_addon_manager.py copy live pts                 # add/replace addon folders, keep extras in pts
python eso_addon_manager.py copy pts                      # one name: default environment is the source
python eso_addon_manager.py copy live pts --saved-vars    # also copy SavedVariables (overwrites same-named files)
python eso_addon_manager.py copy live pts --mirror        # also delete addon folders in pts that live doesn't have
```
It shows what will happen and asks first (Enter = no). Afterwards the destination's tracking list is rebuilt.

## Notes
- `Lib*` folders are never merged into another addon's group.
- Update checks use ESOUI's last-updated timestamp rather than version strings (authors format versions inconsistently).
- `scan` and `update` check the files actually on disk, so restoring a backup or an older copy shows as `update → <version>`.
- Links you make in `match` are remembered in `config.json` (in the game folder) and re-applied on every `scan`.
- The folder search skips obvious dead ends (`node_modules`, other games' Steam prefixes, `steamapps/common`, Windows/Program Files inside Wine prefixes), never follows symlink loops, and stops after 90 seconds.
