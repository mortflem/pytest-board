# pytest-board

A Jira-style drag & drop board for scheduling pytest runs on a headless Linux box.

- **Machines** (the left column) are the Linux boxes pytest runs on, reached with **ssh**. "This server" is always there.
- **Test library** (the second column) is your collection of test definitions: a file, folder, node ID or the whole suite, plus extra args, env vars, a timeout and **the machine it runs on** (a dropdown on each test). You create, edit and delete tests there. **Library tests never move.** Dragging one onto the board drops a **copy**, and the original stays in the library.
- **Headlines** group the library, e.g. `TEST_SYSTEM` for the system you test. They come from a config file (see below), and each one can bring its own test folder, env vars, pytest args and option sets.
- **Columns** are run states: Backlog → Scheduled → Queued → Running → Passed / Failed
- **Swimlanes** are the machines: one row for **this server** and one for every machine. The row a card is in is the machine it runs on
- **Cards** on the board are copies you can run, schedule and move around

| Drag… | onto… | What happens |
|---|---|---|
| a library test | **Backlog** | A copy is parked there, and runs on that row's machine |
| a library test | **Queued** | A copy runs as soon as a worker is free |
| a library test | **Scheduled** | Asks for a time (**once**, or **repeat** on cron), then adds a scheduled copy |
| a board card | another column | Moves that card (Queued = run now, Backlog = unschedule) |
| a board card | another machine's row | That card runs on that machine from now on |
| a board card | a **library headline** | Saves its settings as a new library test under that headline |
| a **machine** | a card | That card runs on the machine (it moves to that row) |
| a **machine** | a library test | Future copies of that test run on the machine (same as its machine dropdown) |

If the test's headline offers **option sets**, dropping it on the board first asks which ones to run it with (see [Option sets](#option-sets)): you get one copy per option set.

You can drop the same library test onto the board as often as you like, for example a nightly copy on one machine and an ad-hoc run on another. Editing a copy only changes that copy.

Running / Passed / Failed are set by the server. A repeating (cron) card goes back to **Scheduled** after each run, and the badge shows the last result. **Clear** on the Passed/Failed headers removes finished copies from the board; **Clear** on a machine's row removes just that row's passed and failed cards (only the selected headline's when the headline menu is set). Their run history stays with the library test: open a library test to see every run of every copy, including which machine it ran on.

Other features: **Run lane** queues every card on that machine (only the selected headline's when the headline menu is set). **Discover** (the button in the library, which asks which machine; or the magnifier on a headline, which uses the headline's machine) runs `pytest --collect-only` on a machine and adds files or single tests to the library, skipping ones already there. The dialog's **On** menu changes the machine: on a machine it runs over ssh in the machine's project folder (plus the headline's `dir`), with its pytest command and environment, on whatever branch is checked out there (shown in the dialog; Discover never switches branches). Tests found on a machine other than their headline's get that machine set. The **All headlines** menu in the top bar narrows both the board and the library to one headline. Clicking a card shows its run history and a **live log** while it runs, with the junit.xml for each run. You can also stop, set timeouts and filter cards.

## Option sets

An option set is a few extra pytest arguments for one card, e.g. `-x`, `-m smoke` or `-m slow --maxfail=3`. List the ones you use under a headline in `pytest-board.ini`, one per line, optionally with a label in front:

```ini
[TEST_SYSTEM]
dir     = tests
options =
    Smoke only | -m smoke
    Slow tests | -m slow --maxfail=3
    Stop at first failure | -x
    Only last failures | --lf
    -v
```

When you drop a library test under that headline on the board, a dialog lists these with checkboxes, plus an **Extra arguments** line. You get **one card**: its runs get every ticked option, plus the extra arguments as you typed them, on the pytest command line. Ticking *Smoke only* and *-x* and typing `--set_temperature 0 10 20` gives one card that runs `pytest … --set_temperature 0 10 20 -m smoke -x`. The dialog remembers what you ticked and typed for each test last time. A test whose headline lists no options goes straight onto the board as one card, as before; **Run a copy now** / **Schedule a copy…** in its dialog still open the dialog, so you can type extra arguments.

- **Extra arguments** go into the card's own *Extra pytest args* (so they show on the card and can be edited there later); the ticked options become the card's **option set**. An option that takes several values needs `nargs="+"` in its `pytest_addoption` (see below); otherwise pytest reads `10` and `20` as test paths.
- **Coloured dots:** every option in `options =` gets its own colour, and each card on the board shows one dot per option it runs with (hover the dots for their names). The checkboxes in the dialog and the option-set groups use the same colours; a group with several options shows their dots.
- **On the board:** inside each column of a machine's lane (Backlog, Queued, Running, …) cards are **grouped by option set**, labelled with the label from the config file, *Smoke only + Stop at first failure* for a card with several (or the options themselves). When a column holds two or more option sets, the groups sit **side by side** with compact cards (title plus one status line: queue position, elapsed time or result); hover a compact card for its details, click it to open it. Cards without extra options get a "none" group when a column has both.
- **Changing a card's option set:** drop it on another group (in any column), or edit **Option set** in the card's dialog. Dropping a library test on a group ticks that group's option set in the dialog.
- The ▶ on a group in Backlog, Scheduled, Passed or Failed queues just those cards, and the run history shows each run's options.
- **Options your own tests define** (like `--device=A`) need a `pytest_addoption` in a `conftest.py` that pytest loads at start-up: in the folder the tests run in, or at the project root. Otherwise pytest stops at once and the card shows *pytest usage error*:

  ```python
  # conftest.py
  import pytest

  def pytest_addoption(parser):
      parser.addoption("--device", default=None, help="device to test against")
      parser.addoption("--set_temperature", nargs="+", type=int, default=[], help="temperatures to test at")

  @pytest.fixture
  def device(request):
      return request.config.getoption("--device")
  ```

- `;` starts a comment in `pytest-board.ini`, so an option set can't contain one.

## Headlines config file

Put a `pytest-board.ini` in your `--root` (or point `--groups` at one anywhere). Each `[SECTION]` is a headline in the library, shown in file order. `pytest-board.example.ini` has every option documented. A typical setup:

```ini
[TEST_SYSTEM]
title = System under test
dir   = tests                          ; tests under this headline run here
args  = -ra                            ; added to every run under this headline
env   =
    BASE_URL=https://system.staging.example.com
tests =                                ; starter tests for the library
    All tests | .
    Health check | test_api.py::test_health
```

Add more `[SECTION]`s later if you test several systems or components.

- **No restart needed:** save the file and the board updates within a few seconds. If the file has an error, the last good headlines stay and the error shows at the bottom of the library.
- **Headline settings apply when a run starts**, so changing `env`/`args`/`dir` in the file affects the next run of every card under that headline. A test's own env values win over the headline's.
- **Secrets:** `env` values never leave the server; the UI only shows their names. To keep them out of the config file, use `.env` files (next section).
- **`tests =` lines are added once.** Deleting one in the UI doesn't bring it back. Tests from a section you remove stay in the library, marked "not in the config file".
- **Every test and card has a headline** (changeable in its dialog), and board cards carry a coloured headline tag.

## Git branches

A card can name a **git branch** (the *Git branch* field in its dialog). Before pytest starts, the board switches the project folder to the latest version of that branch:

1. `git fetch` from the folder's remote (`origin`, or its only remote);
2. `git checkout <branch>`, creating it from the remote branch the first time;
3. `git merge --ff-only`, so the branch is exactly what's on the remote.

The folder is the machine's `workdir` for runs on a machine, and `--root` for runs on this server. The folder stays on that branch afterwards. The log shows the commit that was tested (`[pytest-board] at 3f2a1c9 Fix login (anna, 2 hours ago)`), and the run history has a *Branch* column.

**Where a branch comes from:** the card's own branch, else its headline's `branch =` in pytest-board.ini, else none. With none, pytest runs on whatever is checked out. Library tests have the field too, and copies dragged onto the board take it along. A branch inherited from the headline shows dashed on the card.

**One branch per machine at a time:** because runs switch the project folder itself, the runs going on at the same time on one machine all use the same branch. A card on another branch waits in Queued (*after feature/x* on the card) until those runs are done. Cards on the same branch run side by side, up to the machine's `slots`: the first one fetches and checks out, and the others use that checkout as it is. Cards without a branch count as one more group, "the folder as it is".

**The run doesn't start** (*git checkout failed*, with the reason in the log) when:
- the folder isn't a git checkout, or has no remote;
- the branch isn't on the remote. Give the name without `origin/`;
- the folder has uncommitted changes to tracked files (`git status`): the board won't switch branches over your work. Untracked files, like `.env` or test output, don't count;
- the local branch has commits that aren't on the remote, so it can't be fast-forwarded;
- `git fetch` fails, e.g. the machine can't log in to the git server.

git never waits for a password there (`GIT_TERMINAL_PROMPT=0`, ssh in batch mode). Machines need read access of their own, e.g. a deploy key or a public https remote. Branches don't work with `sync = yes`, because the machine then gets a copy of the project without git. Keep a git checkout on the machine instead.

## Environment variables and .env files

Besides `env =` in pytest-board.ini and the Environment box of a test, variables can come from `.env` files. These are shell-style files, one variable per line, the way you'd `source` them:

```bash
# pytest-board/.env
export BASE_URL=https://staging.example.com
export API_TOKEN='s3cr3t'           # 'single quotes' are literal, "double quotes" expand $VAR
DATA_DIR="${HOME}/test-data"        # export is optional; comments and blank lines are fine
```

| File | Lives on | Applies to |
|---|---|---|
| `.env` next to pytest-board.ini (or `--env-file PATH`) | the board server | every run |
| `env_file = path` under a `[machine:NAME]` (or *Env file on this server* in the machine dialog) | the board server | runs on that machine |
| `env_file = path` under a headline | the board server | that headline's tests |
| the machine's own `.env` in its project folder (`remote_env_file =` / *Env file on the machine* to change it, `none` to turn it off) | the machine | runs on that machine |

Relative `env_file` paths start at the folder of pytest-board.ini. When the same variable is set in several places, the most specific one wins:

**`.env` on the server < the machine's own `.env` < machine `env_file` < machine `env` < headline `env_file` < headline `env` < the test's own env.**

- **No restart needed:** files are read when a run starts, so edits apply to the next run.
- **Files on the board server** are read by the board and sent to runs like `env =` values: never on a command line, and the UI only shows the variable names. `$VAR` / `${VAR}` refer to earlier lines, then to the board's own environment (so `$HOME` is the board server's). Commands (`$(…)`, backticks) are not run there. A line the board can't read is skipped and shown as a problem at the bottom of the library and in the run's log.
- **The machine's own `.env`** is loaded there with `set -a; . ./.env` just before pytest starts, so any sh syntax works and `$HOME` is the machine's. If it has an error, the run doesn't start (*could not start*, with sh's message). The health check shows "loads …/.env" when it finds one.
- `PYTEST_BOARD_TOKEN` in a `.env` file is ignored: tests never get the board's password.
- With `sync = yes`, files called `.env` are not copied to machines, so each machine keeps its own.

## Machines (remote execution over ssh)

**Adding machines:** add them with **Add** in the Machines column, or as `[machine:NAME]` sections in `pytest-board.ini`. To add a machine like one you already have, pick it under **Copy settings from** in the Add dialog, or press **Duplicate** in that machine's dialog: everything is copied except the name and the host. Env values of config-file machines, which the browser never sees, are copied on the server. Config-file machines show a *config* badge and are edited in the file. `pytest-board.example.ini` documents every key.

```ini
[machine:lab-a]
host     = 10.0.4.21
user     = tester
identity = ~/.ssh/pytest_board_ed25519   ; key file on the board server
workdir  = ~/git                         ; project checkout on lab-a (also the default)
python   = .venv/bin/python -m pytest    ; how to start pytest there
slots    = 2                             ; parallel runs on this box

[TEST_SYSTEM]
machine  = lab-a                         ; this headline's tests run on lab-a by default
dir      = tests
```

**Picking a machine for a library test:** every library test has a machine dropdown:
- **default** (dashed) follows the headline's `machine =`, or this server if the headline has none;
- **this server**;
- **any machine**. Offline or undefined machines are flagged in the list.

Copies dragged onto the board take the test's machine with them. In the config file, a `tests =` line can name one as a 4th field: `Nightly | test_nightly.py | -q | lab-a`.

**Which machine runs a card:** the card's own machine (copied from its library test, or set by dragging it into a machine's row), else its headline's `machine`, else this server. The card sits in that machine's row, and every run records where it ran. If a card names a machine that was removed, a red row for it appears until you drag the card elsewhere.

**How a run works:**
1. The board connects with `ssh -T -o BatchMode=yes …` as the user the board runs as.
2. It starts the run **on the machine, independent of that connection**: in a `screen` session named `pb-run-<run>-<board>` when screen is installed there, otherwise as a background process. The run `cd`s to `workdir` plus the headline's `dir`, sets the env, and runs the pytest command.
3. The script goes over ssh's stdin, so env values (tokens, passwords) never appear on a command line or in a file on either side.
4. The board follows the run's log and shows it live.
5. When it's done, the board fetches the exit code and junit.xml and removes the run's folder on the machine (`~/.pytest-board/runs/`).

**Network drops and board restarts don't end runs.** Because the run doesn't depend on the ssh connection:
- **Connection lost:** the run goes on. The card shows *reconnecting*, and the board keeps trying (every 5 s at first, then up to every minute). Once it gets through, it catches up on the output from where it left off, so the log has no gaps and no repeats. A line in the log says when the connection went and came back.
- **Board restarted** (update, reboot, `systemctl restart`): runs on machines carry on, and the board picks them up again when it starts. Runs that finished meanwhile get their results collected. Runs on **this server** are still ended by a restart and marked *interrupted*.
- **Stop and timeout** end the run on the machine (pytest and everything it started). If the machine can't be reached at that moment, the card is stopped right away and the run there is ended as soon as the machine answers again.
- **Queued cards wait** while their machine is unreachable (*waiting for …* on the card) and start once it answers again; they don't fail. The board checks an unreachable machine every 30 s while something is waiting for it.
- A run that ends without an exit code (the machine restarted, or someone killed it there) is labelled *run lost*.

**Attach:** while a card runs in a screen session, its dialog has an **Attach** button. It opens a terminal in the browser, inside that session: you can watch pytest live, or use a debugger (`breakpoint()`, `--pdb`). **Ctrl+A, then D** detaches (the run keeps going), and closing the window does the same. **Ctrl+C** interrupts pytest, which still prints its summary and writes the junit.xml. From any other terminal: `screen -ls`, then `screen -x pb-run-…`. Without screen on the machine, runs work the same, but there's no session to attach to: `sudo apt install screen` there.

**Tidying up:** run folders are removed once their results are in. Leftovers (from a run stopped while its machine was down, or a board that crashed) are cleaned up by the next successful health check: anything still running there is ended first. Temp files from older pytest-board versions in `/tmp` are removed too.

With `sync = yes`, the project (`--root`) is first copied to `workdir`, skipping `.git`, caches and the board's data. The board uses rsync when it's installed (only changed files are sent); otherwise, and always on Windows, it streams the project as a tar archive over ssh (everything is sent each time, and the machine needs `tar`).

**Open shell:** the machine dialog has an **Open shell** button that opens a terminal on that machine, right in the browser. It connects with ssh as the board's user and starts in the machine's `workdir`. If that folder doesn't exist yet, you land in your home folder with the `mkdir` command to create it. Use it to create folders, set up a venv, `pip install -r requirements.txt` and so on. It's a real terminal: colours, Tab completion, Ctrl+C, and `vim`, `top` or `less` all work. Select text to copy, Ctrl+V to paste, and Shift+PgUp/PgDn to scroll back. Unlike test runs, it may ask for a password or key passphrase, so it also works before key login is set up. Closing it ends the session and re-tests the machine, and a shell left open in a closed browser tab is ended after about 90 seconds.

**Health checks** run every 2 minutes (`--check-interval`) and whenever you press **Check all** or **Test connection**. Each one connects, checks that `workdir` exists and runs `pytest --version`. A machine is shown green (ready), amber (connected but folder or pytest missing) or red (can't connect). A red machine with queued cards or runs waiting for it is checked every 30 s, even with `--check-interval 0`.

**Failure outcomes:** a failed run on a machine is labelled *remote folder missing*, *sync failed*, *pytest not found*, *could not start*, *run lost* or *machine not defined*, instead of a bare exit code. *Machine unreachable* in a card's history is an attempt that couldn't connect: the card went back to Queued and waited.

**Slots:** each machine runs up to `slots` tests at once. Queued cards for a busy machine wait, while cards for other machines go ahead.

**One-time ssh setup** (as the user that runs the board, e.g. the systemd `User=`). The **Help setup** button in the top bar shows these steps with the commands filled in for a machine, ready to copy:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/pytest_board_ed25519 -N ''
ssh-copy-id -i ~/.ssh/pytest_board_ed25519.pub tester@10.0.4.21
ssh -i ~/.ssh/pytest_board_ed25519 tester@10.0.4.21 'cd ~/git && .venv/bin/python -m pytest --version'
```
New host keys are accepted on first connect (`StrictHostKeyChecking=accept-new`); changed keys are refused. Anything in `~/.ssh/config` (aliases, ProxyJump, ports) applies too, and so does anything you put in *Extra ssh options*.

## Requirements

- Python 3.8+ on the board server, which can run **Linux, macOS or Windows** (see [Running on Windows](#running-on-windows)). The server uses **only the standard library**, so there's nothing to pip install.
- pytest installed wherever your tests run (for example your project's venv, locally or on each machine).
- For machines: the OpenSSH client on the board server (`ssh`). The machines are Linux (or other Unix) boxes with `sh` and Python/pytest, plus `tar` if you use `sync = yes` (or `rsync` on both ends, which makes sync faster). `screen` is recommended (needed for **Attach**), and `git` is needed for cards with a [git branch](#git-branches). Nothing from pytest-board is installed on them; runs use a folder in the ssh user's home, `~/.pytest-board/runs/`.

## Run it

```bash
# quick try with the bundled examples (listens on localhost only)
python3 server.py --root sample_tests

# typical headless setup, reachable from your laptop
python3 server.py \
  --root /home/tester/my-project \
  --pytest "/home/tester/my-project/.venv/bin/python -m pytest" \
  --host 0.0.0.0 --port 8765 --workers 2 --token change-me
```

Open `http://<box>:8765`. With `--token`, the browser asks for a login: any username, and the token as the password.

| Option | Default | Meaning |
|---|---|---|
| `--root` | cwd | project directory pytest runs in |
| `--pytest` | `<this python> -m pytest` | how to invoke pytest. Point it at your venv |
| `--host` / `--port` | 127.0.0.1 / 8765 | use `0.0.0.0` to allow other machines |
| `--token` | none (or `PYTEST_BOARD_TOKEN`) | password protection |
| `--data` | `<root>/.pytest-board` | SQLite DB, logs and junit reports |
| `--groups` | `<root>/pytest-board.ini` | headline + machine config file (optional) |
| `--workers` | 2 | parallel runs on **this server**; machines use their own `slots` |
| `--ssh` / `--rsync` | `ssh` / `rsync` | client binaries used for machines |
| `--check-interval` | 120 | seconds between automatic machine health checks (0 = manual only) |
| `--env-file` | `.env` next to the config file | variables for every run (see [.env files](#environment-variables-and-env-files)) |

**Security:** this UI runs commands as the service user, so don't expose it without `--token`. Better still, keep the default `127.0.0.1` binding and use an SSH tunnel: `ssh -L 8765:localhost:8765 box`, then browse to `http://localhost:8765`. Requests that another website tries to send through your browser (cross-origin POST/PATCH/DELETE) are refused, and when bound to localhost only `localhost` / `127.0.0.1` Host headers are accepted. If you put a reverse proxy in front, make it pass the original `Host` header through. Tests never see `PYTEST_BOARD_TOKEN` in their environment.

## Run as a service

Edit `pytest-board.service` (user, paths, token), then:

```bash
sudo cp pytest-board.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pytest-board
```

The board survives restarts. Runs on machines go on while it's stopped and are picked up again when it starts; runs on this server that were in progress are marked *interrupted*.

**Upgrading from an earlier version:** replace the files, add your `pytest-board.ini`, and restart. An open board page reloads itself when its files change (once no dialog is open), and shows *board server needs a restart* while the running server is older than the page. The existing `board.db` is upgraded automatically and your cards and run history are kept. Existing library tests appear under "No headline"; open one and pick a headline to move it. Custom swimlanes (Smoke, Regression, …) from older versions are gone: every card moves to the row of the machine it runs on.

## Running on Windows

The board server also runs on Windows 10/11 (the machines it runs tests on stay Linux boxes; "This server" runs tests on the Windows PC itself).

1. Install Python 3.8+ from python.org, and the **OpenSSH Client** (*Settings → Apps → Optional features*; `ssh -V` in PowerShell shows whether it's there).
2. Start the board in PowerShell. Backslash paths are fine; put paths with spaces in double quotes:

   ```powershell
   py C:\pytest-board\server.py --root C:\work\my-project `
      --pytest "C:\work\my-project\.venv\Scripts\python.exe -m pytest" --token change-me
   ```

3. ssh keys live in `%USERPROFILE%\.ssh` of the account the board runs as, and `~` in a machine's *Private key file* means `%USERPROFILE%`. **Help setup** shows the PowerShell versions of the key commands (Windows has no `ssh-copy-id`).

Differences from Linux:
- **Sync** copies the project with tar over ssh (no rsync needed).
- **Open shell** works through ssh's own pipes: login must be key-based (it can't show a password prompt), and the terminal keeps the size it was opened with.
- **Stop** and timeouts end pytest and everything it started (`taskkill /T`).
- **Starting with Windows:** use Task Scheduler instead of the systemd file. *Create Task* → "Run whether user is logged on or not", trigger *At startup*, action *Start a program*: `py`, arguments `C:\pytest-board\server.py --root C:\work\my-project --token change-me`. The task's account is the one whose `.ssh` keys are used.

## Notes

- Cron is evaluated in the **server's** local time zone. Standard 5 fields plus `@hourly`, `@daily`, `@weekly` and `@monthly`.
- Each run sets `PYTEST_BOARD_RUN_ID`, `PYTEST_BOARD_GROUP` (the headline) and, on a machine, `PYTEST_BOARD_MACHINE` in the environment, which is handy for naming reports or screenshots.
- **Discover** on *This server* collects in `--root`; on a machine it collects in that machine's own checkout, so the board server needs no copy of the tests.
- `pytest-xdist` works as usual: put `-n 4` in the card's extra args.
- API: everything the UI does is plain JSON under `/api/…` (see `App.__init__` in `server.py`). For example, from CI: `curl -u x:TOKEN -X POST http://box:8765/api/machines/lab-a/run` (use `local` for this server).
