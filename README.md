# Roblox FastFlag Discord Bot

Tracks Roblox FastFlag changes and Roblox Client Tracker commits, with slash commands for setup and live FastFlag searches.

## How it works

- Watches commits under `FVariables/` in `MaximumADHD/Roblox-FFlag-Tracker` (main branch).
- For each such commit, looks at exactly which individual `FVariables/{Prefix}/{Letter}/{Name}.json` files changed in that commit and reports each one by name with its old → new value, rather than dumping the raw JSON diff of the whole platform file.
- State (last-seen commit sha) is saved to `data/state.json`, so it picks up exactly where it left off on restart.
- Watches `API-Dump.json` and `CppTree.txt` in `MaximumADHD/Roblox-Client-Tracker` on the `roblox` branch.
- `/setup` asks for FastFlag, API dump, and CppTree channels and saves the selected channel IDs per server.
- `/flagsearch` takes `Client` or `Studio` plus a keyword, combines all relevant Roblox settings endpoints, and returns a private JSON attachment.
- `/send-missed-updates` immediately delivers all commits missed by the saved per-source cursors, then records those commits as processed. It is safe to use while the normal polling loop is running.

### Catching up after being offline

On a normal restart, the bot pages back through commit history looking for the last commit it already posted, and posts everything newer than that — in order, oldest first. That lookback is capped at **200 commits**: if the bot's been offline long enough that the last-seen commit is more than 200 commits back (or state was reset), it stops at 200 rather than digging further into old flag history. A brand-new install with no `data/state.json` yet just posts the 10 most recent commits instead of all 200, so a first run doesn't flood the channel.

### Send rate

Posts go out with a 0.4s gap between each — quick enough that a 200-commit backfill finishes in under two minutes, while staying under Discord's per-channel rate limit (roughly 5 messages/5 seconds). If a burst still trips a 429, discord.py's own HTTP layer waits it out automatically; the 0.4s gap is just there to avoid needing that in the first place.

### GitHub rate limits — read this

GitHub allows **60 unauthenticated API requests/hour**. A single catch-up cycle needs one commit-detail request *per missed commit* (up to 200), plus the list calls to page back through history — that can burn through the unauthenticated quota in one go. **Add a `GITHUB_TOKEN`** (see the comment in `.env.example`) to raise that to 5000/hour. Without it, expect a long backfill to stall partway through with a rate-limit warning in `data/bot.log`, and finish over the next few polls instead of all at once.

## Reproducible setup

The project is pinned to Python **3.12.10** in `.python-version`. Dependencies are
locked in `requirements.lock`; `requirements.txt` contains the three direct bot
dependencies with matching exact versions. The test suite uses Python's standard
library `unittest`, so running tests does not download anything.

### Local setup

1. Install Python 3.12.10 (for example with `pyenv`, using `.python-version`).
2. Create and activate a project-local virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install the locked runtime dependencies in this folder:

   ```powershell
   python -m pip install --requirement requirements.lock
   ```

4. Copy `.env.example` to `.env`, then put your bot token in `DISCORD_BOT_TOKEN`.
5. **Add a `GITHUB_TOKEN`** — see above, this one matters more than usual now that a single backfill can need up to ~200 API calls.
6. Invite the bot with the `bot` and `applications.commands` scopes and permissions to view/send messages and attach files.
7. Run:

   ```powershell
   python bot.py
   ```

8. In each server, run `/setup` and select the three destination channels.

    If you are double-clicking and the window closes, use `run_visible_debug.bat`.

### Offline unit tests

The tests cover pure formatting/parsing helpers, state persistence, commit-history
ordering, and mocked GitHub boundaries. They never contact Discord or GitHub.

```powershell
make test
```

On Windows without `make`, run the exact underlying command:

```powershell
python -m unittest discover --start-directory tests --pattern "test_*.py" --verbose
```

### Docker

Build the image using the pinned Python base image and locked dependencies:

```powershell
docker build --tag roblox-fflag-bot:local .
```

Run it with local secrets and persistent state mounted from this folder:

```powershell
docker run --rm --env-file .env --volume "${PWD}/data:/app/data" roblox-fflag-bot:local
```

The image does not contain `.env`; provide it at runtime. The `data` mount keeps
`state.json` and `bot.log` across container restarts.

## Run Hidden On PC Startup

After `.env` is configured and dependencies are installed, run PowerShell in this folder:

```powershell
.\install_startup_task.ps1
```

That creates a Windows Scheduled Task that starts `run_bot.pyw` at logon with no visible console window.

## Notes

- `data/bot.log` has the full log, including rate-limit warnings or send failures.
- `data/state.json` is what makes catch-up-after-being-offline work. Don't delete it unless you want the bot to re-backfill (capped at 200 commits back) from scratch.
