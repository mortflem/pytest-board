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

Running / Passed / Failed are set by the server. A repeating (cron) card goes back to **Scheduled** after each run, and the badge shows the last result. **Clear** on the Passed/Failed headers removes finished copies from the board. Their run history stays with the library test: open a library test to see every run of every copy, including which machine it ran on.

Other features: **Run lane** queues every card on that machine (only the selected headline's when the headline menu is set). **Discover** (in the library, or the magnifier on a headline) runs `pytest --collect-only` in that headline's folder and adds files or single tests under it, skipping ones already there. The **All headlines** menu in the top bar narrows both the board and the library to one headline. Clicking a card shows its run history and a **live log** while it runs, with the junit.xml for each run. You can also stop, set timeouts and filter cards.

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

When you drop a library test under that headline on the board, a dialog lists these with checkboxes, plus **No extra options**, plus a box for more option sets (one per line, for one-offs). You get **one card per ticked line**, and each card's runs get its option set added to the pytest command line. The dialog remembers what you picked for each test last time. A test whose headline lists no options goes straight onto the board as one card, as before; **Run a copy now** / **Schedule a copy…** in its dialog still let you type option sets.

- **On the board:** inside each column of a machine's lane (Backlog, Queued, Running, …) cards are **grouped by option set**, labelled with the label from the config file (or the options themselves). When a column holds two or more option sets, the groups sit **side by side** with compact cards (title plus one status line: queue position, elapsed time or result); hover a compact card for its details, click it to open it. Cards without extra options get a "none" group when a column has both.
- **Changing a card's option set:** drop it on another group (in any column), or edit **Option set** in the card's dialog. Dropping a library test on a group ticks that group's option set in the dialog.
- The ▶ on a group in Backlog, Scheduled, Passed or Failed queues just those cards, and the run history shows each run's options.
- **Options your own tests define** (like `--device=A`) need a `pytest_addoption` in a `conftest.py` that pytest loads at start-up: in the folder the tests run in, or at the project root. Otherwise pytest stops at once and the card shows *pytest usage error*:

  ```python
  # conftest.py
  import pytest

  def pytest_addoption(parser):
      parser.addoption("--device", default=None, help="device to test against")

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
- **Secrets:** `env` values never leave the server; the UI only shows their names.
- **`tests =` lines are added once.** Deleting one in the UI doesn't bring it back. Tests from a section you remove stay in the library, marked "not in the config file".
- **Every test and card has a headline** (changeable in its dialog), and board cards carry a coloured headline tag.

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
2. It `cd`s to `workdir` plus the headline's `dir`, sets the env, and runs the pytest command.
3. The script goes over ssh's stdin, so env values (tokens, passwords) never appear on a command line on either side.
4. The live log streams back as it runs.
5. Afterwards the junit.xml is copied back and the temp files on the machine are removed.

With `sync = yes`, the project (`--root`) is first copied to `workdir` with rsync, skipping `.git`, caches and the board's data.

**Stop and timeout** kill the pytest process on the machine too, not just the local ssh connection.

**Open shell:** the machine dialog has an **Open shell** button that opens a terminal on that machine, right in the browser. It connects with ssh as the board's user and starts in the machine's `workdir`. If that folder doesn't exist yet, you land in your home folder with the `mkdir` command to create it. Use it to create folders, set up a venv, `pip install -r requirements.txt` and so on. It's a real terminal: colours, Tab completion, Ctrl+C, and `vim`, `top` or `less` all work. Select text to copy, Ctrl+V to paste, and Shift+PgUp/PgDn to scroll back. Unlike test runs, it may ask for a password or key passphrase, so it also works before key login is set up. Closing it ends the session and re-tests the machine, and a shell left open in a closed browser tab is ended after about 90 seconds.

**Health checks** run every 2 minutes (`--check-interval`) and whenever you press **Check all** or **Test connection**. Each one connects, checks that `workdir` exists and runs `pytest --version`. A machine is shown green (ready), amber (connected but folder or pytest missing) or red (can't connect).

**Failure outcomes:** a failed run on a machine is labelled *machine unreachable* (ssh exit 255), *remote folder missing*, *sync failed*, *pytest not found* or *machine not defined*, instead of a bare exit code.

**Slots:** each machine runs up to `slots` tests at once. Queued cards for a busy machine wait, while cards for other machines go ahead.

**One-time ssh setup** (as the user that runs the board, e.g. the systemd `User=`). The **Help setup** button in the top bar shows these steps with the commands filled in for a machine, ready to copy:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/pytest_board_ed25519 -N ''
ssh-copy-id -i ~/.ssh/pytest_board_ed25519.pub tester@10.0.4.21
ssh -i ~/.ssh/pytest_board_ed25519 tester@10.0.4.21 'cd ~/git && .venv/bin/python -m pytest --version'
```
New host keys are accepted on first connect (`StrictHostKeyChecking=accept-new`); changed keys are refused. Anything in `~/.ssh/config` (aliases, ProxyJump, ports) applies too, and so does anything you put in *Extra ssh options*.

## Requirements

- Python 3.8+ on the box. The server uses **only the standard library**, so there's nothing to pip install.
- pytest installed wherever your tests run (for example your project's venv, locally or on each machine).
- For machines: the OpenSSH client on the board server (`ssh`), and `rsync` on both ends only if you use `sync = yes`. The machines need `sh` and Python/pytest, and nothing else from pytest-board.

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

**Security:** this UI runs commands as the service user, so don't expose it without `--token`. Better still, keep the default `127.0.0.1` binding and use an SSH tunnel: `ssh -L 8765:localhost:8765 box`, then browse to `http://localhost:8765`. Requests that another website tries to send through your browser (cross-origin POST/PATCH/DELETE) are refused, and when bound to localhost only `localhost` / `127.0.0.1` Host headers are accepted. If you put a reverse proxy in front, make it pass the original `Host` header through. Tests never see `PYTEST_BOARD_TOKEN` in their environment.

## Run as a service

Edit `pytest-board.service` (user, paths, token), then:

```bash
sudo cp pytest-board.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pytest-board
```

The board survives restarts. Runs that were in progress when the server stopped are marked *interrupted*.

**Upgrading from an earlier version:** replace the files, add your `pytest-board.ini`, and restart. The existing `board.db` is upgraded automatically and your cards and run history are kept. Existing library tests appear under "No headline"; open one and pick a headline to move it. Custom swimlanes (Smoke, Regression, …) from older versions are gone: every card moves to the row of the machine it runs on.

## Notes

- Cron is evaluated in the **server's** local time zone. Standard 5 fields plus `@hourly`, `@daily`, `@weekly` and `@monthly`.
- Each run sets `PYTEST_BOARD_RUN_ID`, `PYTEST_BOARD_GROUP` (the headline) and, on a machine, `PYTEST_BOARD_MACHINE` in the environment, which is handy for naming reports or screenshots.
- **Discover** collects tests on this server (in `--root`), so keep a checkout of the tests on the board server too, or use `sync = yes`.
- `pytest-xdist` works as usual: put `-n 4` in the card's extra args.
- API: everything the UI does is plain JSON under `/api/…` (see `App.__init__` in `server.py`). For example, from CI: `curl -u x:TOKEN -X POST http://box:8765/api/machines/lab-a/run` (use `local` for this server).
