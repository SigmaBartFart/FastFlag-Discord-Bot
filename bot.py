import asyncio
import difflib
import io
import json
import logging
import os
import re
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"

ROBLOX_BLUE = 0x3399FF
ROBLOX_GREEN = 0x2ECC71

FFLAG_REPO = "MaximumADHD/Roblox-FFlag-Tracker"
FFLAG_BRANCH = "main"
TRACKER_REPO = "MaximumADHD/Roblox-Client-Tracker"
TRACKER_BRANCH = "roblox"

CLIENT_SETTING_URLS = {
    "client": (
        "https://clientsettingscdn.roblox.com/v2/settings/application/PCDesktopClient/",
        "https://clientsettingscdn.roblox.com/v2/settings/application/PCClientBootstrapper/",
        "https://clientsettingscdn.roblox.com/v2/settings/application/MacDesktopClient/",
        "https://clientsettingscdn.roblox.com/v2/settings/application/MacClientBootstrapper/",
    ),
    "studio": (
        "https://clientsettingscdn.roblox.com/v2/settings/application/PCStudioBootstrapper/",
        "https://clientsettingscdn.roblox.com/v2/settings/application/MacStudioBootstrapper/",
    ),
}

# How far back (in commits) the flags-tracker will dig to catch up after being
# offline. Keeps a long outage from triggering a backfill all the way into
# ancient flag history.
MAX_LOOKBACK_COMMITS = 200
# Manual repair stays bounded so one large backlog cannot delay the other
# sources. The normal background tracker still catches up using the full limit.
MANUAL_REPLAY_COMMITS = 25


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def short(text: str, limit: int = 1000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n... (truncated)"


def code_block(text: str, lang: str = "") -> str:
    text = text.replace("```", "`\u200b``")
    return f"```{lang}\n{text}\n```"


def make_text_file(name: str, text: str) -> discord.File | None:
    if not text:
        return None
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    path.write_text(text, encoding="utf-8")
    return discord.File(str(path), filename=name)


def embed_characters(embed: discord.Embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    total += len(embed.footer.text or "") if embed.footer else 0
    total += len(embed.author.name or "") if embed.author else 0
    total += sum(len(field.name) + len(field.value) for field in embed.fields)
    return total


def fit_embed(embed: discord.Embed) -> discord.Embed:
    """Keep generated update embeds under Discord's 6000-character limit."""
    if embed_characters(embed) <= 6000:
        return embed
    embed.description = short(embed.description or "", 2200)
    original_fields = list(embed.fields)
    embed.clear_fields()
    for field in original_fields:
        embed.add_field(
            name=short(field.name, 200),
            value=short(field.value, 850),
            inline=field.inline,
        )
    if embed_characters(embed) > 6000:
        embed.description = short(embed.description or "", 1200)
        original_fields = list(embed.fields)
        embed.clear_fields()
        for field in original_fields[:6]:
            embed.add_field(name=short(field.name, 150), value=short(field.value, 600), inline=field.inline)
    return embed


def change_summary_lines(added: list[str], removed: list[str], modified: list[str], limit: int = 40) -> str:
    lines = [
        *(f"+ {short(value, 260)}" for value in added),
        *(f"- {short(value, 260)}" for value in removed),
        *(f"~ {short(value, 260)}" for value in modified),
    ]
    if len(lines) > limit:
        lines = lines[:limit] + [f"... and {len(lines) - limit} more changes"]
    return "\n".join(lines) or "No line-level changes detected."


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}

    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        elif self.path.with_suffix(".tmp").exists():
            self.data = json.loads(self.path.with_suffix(".tmp").read_text(encoding="utf-8"))
            self.save()
        else:
            self.data = {"created_at": utcnow()}

    def save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        try:
            tmp.replace(self.path)
        except OSError:
            # OneDrive/Defender can briefly lock the file during an atomic
            # replace (this project folder lives inside OneDrive). A direct
            # write is less elegant, but keeps the bot from losing state.
            try:
                self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
            except OSError:
                logging.exception("Could not write state.json; this cycle's state was not saved")
            try:
                tmp.unlink()
            except OSError:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.data["updated_at"] = utcnow()
        self.save()


class Http:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def text(self, url: str, headers: dict[str, str] | None = None) -> str:
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


def extract_application_settings(payload: Any) -> dict[str, Any]:
    """Normalize Roblox's settings response to the flag map."""
    if isinstance(payload, dict) and isinstance(payload.get("applicationSettings"), dict):
        return payload["applicationSettings"]
    return payload if isinstance(payload, dict) else {}


async def fetch_flag_export(http: Http, platform: str, keyword: str) -> dict[str, Any]:
    needle = keyword.casefold().strip()
    if platform not in CLIENT_SETTING_URLS:
        raise ValueError("platform must be client or studio")
    combined: dict[str, Any] = {}
    for url in CLIENT_SETTING_URLS[platform]:
        combined.update(extract_application_settings(await http.json(url)))
    return {name: value for name, value in sorted(combined.items()) if needle in name.casefold()}


class GithubRateLimited(RuntimeError):
    pass


class Github:
    def __init__(self, http: Http) -> None:
        self.http = http
        self.headers = {"Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        # Github is rebuilt fresh every poll cycle (see build_trackers()), so this
        # cache naturally scopes itself to "within one poll" and never grows stale.
        self._commit_cache: dict[tuple[str, str], dict[str, Any]] = {}

    async def _get_json(self, url: str) -> Any:
        try:
            return await self.http.json(url, headers=self.headers)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (403, 429):
                raise GithubRateLimited(
                    "GitHub API rate limit hit. Unauthenticated requests are capped "
                    "at 60/hour, which this bot can burn through in a single poll. "
                    "Add GITHUB_TOKEN to .env (a free classic personal access token "
                    "with no scopes raises the limit to 5000/hour)."
                ) from exc
            raise

    async def commits(self, repo: str, branch: str, per_page: int = 100, path: str | None = None, page: int = 1) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{repo}/commits?sha={branch}&per_page={per_page}&page={page}"
        if path:
            url += f"&path={path}"
        return await self._get_json(url)

    async def commit(self, repo: str, sha: str) -> dict[str, Any]:
        cache_key = (repo, sha)
        cached = self._commit_cache.get(cache_key)
        if cached is not None:
            return cached
        detail = await self._get_json(f"https://api.github.com/repos/{repo}/commits/{sha}")
        self._commit_cache[cache_key] = detail
        return detail

    async def raw(self, repo: str, ref: str, path: str) -> str:
        return await self.http.text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}", headers=self.headers)


@dataclass
class Post:
    channel_key: str
    embed: discord.Embed
    files: list[discord.File] | None = None


class DiscordPoster:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.channels = {
            "flags": os.getenv("CHANNEL_FLAGS", "flags-tracker"),
            "api": os.getenv("CHANNEL_API", "api-dumps"),
            "cpptree": os.getenv("CHANNEL_CPPTREE", "cpptree-updates"),
        }

    def configured_channel_id(self, guild_id: int, key: str) -> int | None:
        configured = self.bot.state.get("guild_channels", {}).get(str(guild_id), {}).get(key)
        return int(configured) if configured else None

    def set_channel(self, guild_id: int, key: str, channel_id: int) -> None:
        guilds = self.bot.state.get("guild_channels", {})
        guilds.setdefault(str(guild_id), {})[key] = str(channel_id)
        self.bot.state.set("guild_channels", guilds)

    def target_guilds(self) -> list[discord.Guild]:
        if not self.guild_id:
            return list(self.bot.guilds)
        guild = self.bot.get_guild(int(self.guild_id))
        return [guild] if guild else []

    def find_channel(self, key: str) -> discord.TextChannel | None:
        for guild in self.target_guilds():
            configured_id = self.configured_channel_id(guild.id, key)
            if configured_id:
                channel = guild.get_channel(configured_id)
                if isinstance(channel, discord.TextChannel):
                    return channel
            wanted = self.channels[key].lower()
            for channel in guild.text_channels:
                if channel.name.lower() == wanted:
                    return channel
        return None

    def channel_report(self) -> list[str]:
        rows: list[str] = []
        guild_names = ", ".join(guild.name for guild in self.target_guilds()) or "none"
        rows.append(f"Guilds: {guild_names}")
        for key, name in self.channels.items():
            status = "ok" if self.find_channel(key) else "missing"
            rows.append(f"{key}: #{name} - {status}")
        return rows

    async def send(self, post: Post) -> bool:
        channel = self.find_channel(post.channel_key)
        if channel is None:
            logging.warning("Missing Discord channel for %s", post.channel_key)
            return False

        files = post.files or []
        try:
            await channel.send(embed=fit_embed(post.embed), files=files)
        finally:
            for file in files:
                file.close()
        return True

    async def seen_commit_shas(self, key: str, limit: int = MAX_LOOKBACK_COMMITS + 100) -> set[str]:
        """Read recent messages so manual replay can repair deleted/missing posts."""
        channel = self.find_channel(key)
        if channel is None:
            return set()
        seen: set[str] = set()
        async for message in channel.history(limit=limit):
            for embed in message.embeds:
                if embed.footer.text and embed.footer.text.startswith("commit:"):
                    seen.add(embed.footer.text.removeprefix("commit:")[:12])
                if embed.url and "/commit/" in embed.url:
                    seen.add(embed.url.rsplit("/", 1)[-1][:12])
                for field in embed.fields:
                    if field.name == "Commit":
                        seen.add(str(field.value).strip("`")[:12])
        return seen


class Tracker:
    def __init__(self, state: StateStore, http: Http, github: Github) -> None:
        self.state = state
        self.http = http
        self.github = github
        self.backfill_first = env_bool("BACKFILL_ON_FIRST_RUN", True)

    async def run(self) -> list[Post]:
        raise NotImplementedError


async def version_at(github: "Github", repo: str, ref: str) -> str:
    """The Roblox client/studio version string (e.g. 0.740.0.7400927) as of
    a given git ref, read from version.txt. Falls back to the short sha if
    version.txt is missing or the fetch fails for any reason."""
    try:
        return (await github.raw(repo, ref, "version.txt")).strip()
    except Exception:
        return ref[:12]


class CommitTracker(Tracker):
    repo = ""
    branch = ""
    key = ""
    channel_key = ""
    path = ""

    async def changed_commits(self) -> list[dict[str, Any]]:
        """Commits newer than the last one we've seen (oldest first, ready to
        post in order). On a cold start this backfills the last 10. On a
        normal restart it pages back through commit history looking for the
        last-seen sha, up to MAX_LOOKBACK_COMMITS - so a long outage catches
        up fully, but a very long-dead bot doesn't suddenly dig into ancient
        flag history and dump hundreds of years-old changes."""
        first_page = await self.github.commits(self.repo, self.branch, per_page=100, path=self.path or None, page=1)
        if not first_page:
            return []

        latest = first_page[0]["sha"]
        last = self.state.get(self.key)

        if last is None:
            self.state.set(self.key, latest)
            if not self.backfill_first:
                return []
            return list(reversed(first_page[:10]))

        collected: list[dict[str, Any]] = []
        found = False
        for commit in first_page:
            if commit["sha"] == last:
                found = True
                break
            collected.append(commit)

        page_num = 2
        while not found and len(collected) < MAX_LOOKBACK_COMMITS:
            next_page = await self.github.commits(self.repo, self.branch, per_page=100, path=self.path or None, page=page_num)
            if not next_page:
                break
            for commit in next_page:
                if commit["sha"] == last:
                    found = True
                    break
                collected.append(commit)
                if len(collected) >= MAX_LOOKBACK_COMMITS:
                    break
            page_num += 1

        collected = collected[:MAX_LOOKBACK_COMMITS]
        self.state.set(self.key, latest)
        return list(reversed(collected))

    async def recent_commits(self, limit: int = MAX_LOOKBACK_COMMITS) -> list[dict[str, Any]]:
        """Return recent commits without changing the saved cursor."""
        collected: list[dict[str, Any]] = []
        page_num = 1
        while len(collected) < limit:
            page = await self.github.commits(self.repo, self.branch, per_page=100, path=self.path or None, page=page_num)
            if not page:
                break
            collected.extend(page)
            page_num += 1
        return list(reversed(collected[:limit]))


# Every individual flag/int/string/log variable gets its own metadata file at
# FVariables/{Prefix}/{FirstLetterOfCoreName}/{FullName}.json in the FFlag
# repo (verified against the live repo tree, NOT the simplified F/DF/SF +
# Flag/Int/String/Log table in that repo's README, which doesn't match the
# real folder names). Each file just holds {"Type": ..., "Value": ...}.
FVARIABLE_PREFIXES = ("DFFlag", "DFInt", "DFLog", "DFString", "FFlag", "FInt", "FLog", "FString", "SFFlag", "SFString")

VALUE_LINE_RE = re.compile(r'"Value"\s*:\s*(.+?),?\s*$')


def fvariable_name_from_path(path: str) -> str | None:
    # path looks like: FVariables/DFFlag/A/DFFlagAbuseReportEnrichmentIncludePresetInfo.json
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "FVariables":
        return None
    return Path(parts[3]).stem


def parse_fvariable_patch(patch: str) -> tuple[str | None, str | None]:
    """Pull the old/new "Value" out of a unified diff patch for one
    FVariables/*.json file. Only handles the common single-line scalar
    Value case (Unified-type flags); anything more structured (Channels/
    Platforms-type flags) falls back to None and gets shown as "changed"."""
    old_value = new_value = None
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")) or not line or line[0] not in "+-":
            continue
        match = VALUE_LINE_RE.search(line[1:])
        if not match:
            continue
        if line[0] == "-":
            old_value = match.group(1).strip()
        else:
            new_value = match.group(1).strip()
    return old_value, new_value


class FFlagTracker(CommitTracker):
    repo = FFLAG_REPO
    branch = FFLAG_BRANCH
    key = "fflag_last_sha"
    channel_key = "flags"
    path = "FVariables"

    async def run(self, commits: list[dict[str, Any]] | None = None) -> list[Post]:
        posts: list[Post] = []
        for item in commits if commits is not None else await self.changed_commits():
            sha = item["sha"]
            detail = await self.github.commit(self.repo, sha)
            fvar_files = [f for f in detail.get("files", []) if f.get("filename", "").startswith("FVariables/")]

            added: list[str] = []
            removed: list[str] = []
            modified: list[tuple[str, str, str]] = []

            for file in fvar_files:
                name = fvariable_name_from_path(file.get("filename", ""))
                if not name:
                    continue
                status = file.get("status")
                if status == "added":
                    added.append(name)
                elif status == "removed":
                    removed.append(name)
                else:
                    old_val, new_val = parse_fvariable_patch(file.get("patch", ""))
                    modified.append((name, old_val or "?", new_val or "changed"))

            if not (added or removed or modified):
                # This commit didn't touch any individual FVariables files
                # (rare, but possible) - nothing meaningful to post.
                continue

            roblox_version = await version_at(self.github, self.repo, sha)

            lines: list[str] = []
            for name in added[:8]:
                lines.append(f"+ {name}")
            for name in removed[:8]:
                lines.append(f"- {name}")
            for name, old_val, new_val in modified[:8]:
                lines.append(f"~ {name}: {old_val} -> {new_val}")
            shown = len(lines)
            total = len(added) + len(removed) + len(modified)
            if total > shown:
                lines.append(f"... and {total - shown} more")

            embed = discord.Embed(
                title="FFlag Update Detected",
                description=code_block("\n".join(lines), "diff"),
                color=ROBLOX_GREEN,
                url=detail.get("html_url"),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Changes", value=f"+ {len(added)} added  - {len(removed)} removed  ~ {len(modified)} modified", inline=False)
            embed.add_field(name="Roblox Version", value=f"`{roblox_version}`", inline=True)
            embed.add_field(name="Commit", value=f"`{sha[:12]}`", inline=True)
            embed.add_field(name="Source", value=f"[Roblox-FFlag-Tracker]({detail.get('html_url')})", inline=False)
            embed.set_footer(text=f"commit:{sha}")

            full_patch = "\n".join(f"diff -- {f.get('filename')}\n{f.get('patch', '')}" for f in fvar_files if f.get("patch"))
            diff_file = make_text_file(f"fflags_{sha[:12]}.diff", full_patch) if full_patch else None
            posts.append(Post("flags", embed, [diff_file] if diff_file else []))
        return posts


class GithubFileTracker(CommitTracker):
    """Posts a colored added/removed summary whenever a tracked file changes."""

    @staticmethod
    def flatten_json(value: Any, prefix: str = "") -> dict[str, Any]:
        """Flatten API-Dump's nested class/member objects into readable keys."""
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            base = prefix
            for key, child in value.items():
                if key == "Name":
                    continue
                child_prefix = f"{base}.{key}" if base else key
                result.update(GithubFileTracker.flatten_json(child, child_prefix))
            return result
        if isinstance(value, list):
            result = {}
            for index, child in enumerate(value):
                child_prefix = f"{prefix}[{index}]"
                if isinstance(child, dict) and child.get("Name"):
                    child_prefix = f"{prefix}.{child['Name']}"
                result.update(GithubFileTracker.flatten_json(child, child_prefix))
            return result
        return {prefix: value}

    def change_lines(self, previous: str, current: str) -> tuple[list[str], list[str], list[str]]:
        try:
            old_data = self.flatten_json(json.loads(previous) if previous else {})
            new_data = self.flatten_json(json.loads(current))
            if isinstance(old_data, dict) and isinstance(new_data, dict):
                added = [f"{key}: {new_data[key]}" for key in sorted(set(new_data) - set(old_data))]
                removed = [f"{key}: {old_data[key]}" for key in sorted(set(old_data) - set(new_data))]
                modified = [
                    f"{key}: {old_data[key]} -> {new_data[key]}"
                    for key in sorted(set(old_data) & set(new_data))
                    if old_data[key] != new_data[key]
                ]
                return added, removed, modified
        except (json.JSONDecodeError, TypeError):
            pass

        added: list[str] = []
        removed: list[str] = []
        for line in difflib.ndiff(previous.splitlines(), current.splitlines()):
            if line.startswith("+ "):
                added.append(line[2:])
            elif line.startswith("- "):
                removed.append(line[2:])
        return added, removed, []

    @staticmethod
    def change_field(name: str, values: list[str], emoji: str) -> str:
        shown = values[:15]
        text = "\n".join(f"`{short(value, 900)}`" for value in shown) or "None"
        if len(values) > len(shown):
            text += f"\n…and {len(values) - len(shown)} more"
        return f"{emoji} **{name} ({len(values)})**\n{text}"

    async def run(self, commits: list[dict[str, Any]] | None = None) -> list[Post]:
        posts: list[Post] = []
        for item in commits if commits is not None else await self.changed_commits():
            sha = item["sha"]
            detail = await self.github.commit(self.repo, sha)
            touched = any(file.get("filename") == self.path for file in detail.get("files", []))
            if not touched:
                continue
            snapshot = await self.github.raw(self.repo, sha, self.path)
            parent_sha = (detail.get("parents") or [{}])[0].get("sha")
            try:
                previous = await self.github.raw(self.repo, parent_sha, self.path) if parent_sha else ""
            except Exception:
                previous = ""
            added, removed, modified = self.change_lines(previous, snapshot)
            file_name = Path(self.path).name
            attachment = discord.File(io.BytesIO(snapshot.encode("utf-8")), filename=file_name)
            color = ROBLOX_GREEN if added and not removed else 0xE74C3C if removed and not added else 0xF1C40F
            embed = discord.Embed(
                title=f"{file_name} Update Detected",
                description=code_block(change_summary_lines(added, removed, modified), "diff"),
                color=color,
                url=detail.get("html_url"),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Changes", value=f"+ {len(added)} added  - {len(removed)} removed  ~ {len(modified)} modified", inline=False)
            embed.add_field(name="Commit", value=f"`{sha[:12]}`", inline=True)
            embed.add_field(name="Source", value=f"[Roblox-Client-Tracker]({detail.get('html_url')})", inline=False)
            embed.set_footer(text=f"commit:{sha}")
            posts.append(Post(self.channel_key, embed, [attachment]))
        return posts


class ApiDumpTracker(GithubFileTracker):
    repo = TRACKER_REPO
    branch = TRACKER_BRANCH
    key = "api_dump_last_sha"
    channel_key = "api"
    path = "API-Dump.json"


class CppTreeTracker(GithubFileTracker):
    repo = TRACKER_REPO
    branch = TRACKER_BRANCH
    key = "cpptree_last_sha"
    channel_key = "cpptree"
    path = "CppTree.txt"


class ChannelSetupView(discord.ui.View):
    def __init__(self, bot: "RobloxTrackerBot", guild_id: int, step: int = 0) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild_id = guild_id
        self.step = step
        keys = [("flags", "FastFlag updates"), ("api", "API dumps"), ("cpptree", "CppTree updates")]
        key, label = keys[step]
        select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder=f"Choose the channel for {label.lower()}...",
            min_values=1,
            max_values=1,
            custom_id=f"setup_{key}_{guild_id}_{step}",
        )

        async def selected(interaction: discord.Interaction) -> None:
            if interaction.guild_id != self.guild_id:
                await interaction.response.send_message("This setup belongs to another server.", ephemeral=True)
                return
            channel = select.values[0]
            if self.bot.poster is None:
                await interaction.response.send_message("The bot is still starting up.", ephemeral=True)
                return
            self.bot.poster.set_channel(self.guild_id, key, channel.id)
            if self.step + 1 < len(keys):
                next_key, next_label = keys[self.step + 1]
                await interaction.response.edit_message(
                    content=f"Saved **{label}** as {channel.mention}.\nNow choose the channel for **{next_label}**:",
                    view=ChannelSetupView(self.bot, self.guild_id, self.step + 1),
                )
            else:
                await interaction.response.edit_message(
                    content="Setup complete. FastFlag, API dump, and CppTree updates will be sent to the selected channels.",
                    view=None,
                )

        select.callback = selected
        self.add_item(select)

class RobloxTrackerBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.state = StateStore(STATE_PATH)
        self.poster: DiscordPoster | None = None
        self.poll_task: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        self.update_lock = asyncio.Lock()
        self.tree.add_command(self.setup_command)
        self.tree.add_command(self.flagsearch_command)
        self.tree.add_command(self.send_missed_updates_command)
        # Commands declared on a Bot subclass are class descriptors; explicitly
        # bind them so discord.py passes this bot instance to the callbacks.
        self.setup_command.binding = self
        self.flagsearch_command.binding = self
        self.send_missed_updates_command.binding = self

    @app_commands.command(name="setup", description="Choose channels for FastFlag, API dump, and CppTree updates")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can use /setup.", ephemeral=True)
            return
        if self.poster is None:
            await interaction.response.send_message("The bot is still starting up; try again in a moment.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose the channel for **FastFlag updates**:",
            view=ChannelSetupView(self, interaction.guild_id),
            ephemeral=True,
        )

    @app_commands.command(name="flagsearch", description="Search current Roblox client or Studio FastFlags")
    @app_commands.describe(platform="Search client or Studio flags", keyword="Text to find in flag names")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Client", value="client"),
        app_commands.Choice(name="Studio", value="studio"),
    ])
    async def flagsearch_command(
        self, interaction: discord.Interaction, platform: app_commands.Choice[str], keyword: str
    ) -> None:
        if self.session is None:
            await interaction.response.send_message("The bot is still starting up; try again in a moment.", ephemeral=True)
            return
        keyword = keyword.strip()
        if not keyword:
            await interaction.response.send_message("Enter a keyword to search for.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await fetch_flag_export(Http(self.session), platform.value, keyword)
        except Exception:
            logging.exception("Flag search failed")
            await interaction.followup.send("Roblox's FastFlag endpoints could not be reached right now.", ephemeral=True)
            return
        payload = json.dumps(results, indent=2, ensure_ascii=False).encode("utf-8")
        file = discord.File(io.BytesIO(payload), filename=f"{platform.value}_fastflags_{keyword[:32]}.json")
        await interaction.followup.send(
            f"Found **{len(results)}** matching {platform.name} FastFlags.", file=file, ephemeral=True
        )

    @app_commands.command(name="send-missed-updates", description="Send all missed FastFlag, API dump, and CppTree updates")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def send_missed_updates_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can use /send-missed-updates.", ephemeral=True)
            return
        if self.poster is None:
            await interaction.response.send_message("The bot is still starting up; try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async def progress(message: str) -> None:
            await interaction.edit_original_response(content=f"⏳ {message}")

        counts = await self.send_missing_updates(progress)
        total = sum(counts.values())
        details = ", ".join(f"{name}: {count}" for name, count in counts.items())
        await interaction.followup.send(
            f"Sent **{total}** missed updates. ({details})", ephemeral=True
        )

    async def setup_hook(self) -> None:
        headers = {
            "User-Agent": "RobloxDiscordTrackerBot/1.0",
        }
        self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=45))
        # This bot intentionally uses guild-scoped commands. Clear the old
        # global registrations created by earlier versions so Discord does not
        # show every command twice.
        commands = list(self.tree.get_commands())
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        for command in commands:
            self.tree.add_command(command)
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Synced %d guild application commands to %s", len(synced), guild_id)

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        logging.info("Logged in as %s", self.user)
        if self.poll_task is None:
            self.state.load()
            self.poster = DiscordPoster(self)
            # Also refresh guild-scoped registrations. This handles bots that
            # were previously invited with commands registered before a
            # signature changed, which otherwise causes CommandSignatureMismatch.
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            logging.info("Discord channel report:\n%s", "\n".join(self.poster.channel_report()))
            async def startup_progress(message: str) -> None:
                logging.info("Startup missed-update repair: %s", message)

            await self.send_missing_updates(startup_progress)
            self.poll_task = asyncio.create_task(self.poll_loop())

    def build_trackers(self) -> list[Tracker]:
        assert self.session is not None
        http = Http(self.session)
        github = Github(http)
        return [
            FFlagTracker(self.state, http, github),
            ApiDumpTracker(self.state, http, github),
            CppTreeTracker(self.state, http, github),
        ]

    async def poll_once(self) -> dict[str, int]:
        assert self.poster is not None
        counts = {"FastFlags": 0, "API dumps": 0, "CppTree": 0}
        async with self.update_lock:
            for tracker in self.build_trackers():
                try:
                    posts = await tracker.run()
                except GithubRateLimited as exc:
                    logging.warning("%s skipped: %s", tracker.__class__.__name__, exc)
                    continue
                except Exception:
                    logging.exception("%s failed", tracker.__class__.__name__)
                    continue
                for post in posts:
                    sent = False
                    try:
                        sent = await self.poster.send(post)
                        if sent:
                            if isinstance(tracker, FFlagTracker):
                                counts["FastFlags"] += 1
                            elif isinstance(tracker, ApiDumpTracker):
                                counts["API dumps"] += 1
                            elif isinstance(tracker, CppTreeTracker):
                                counts["CppTree"] += 1
                    except Exception:
                        # A single bad embed/send should not stop the rest of the
                        # batch, and definitely should not kill the poll loop.
                        logging.exception("Failed to send a post for %s", tracker.__class__.__name__)
                    # Discord's per-channel bucket is ~5 messages / 5 seconds.
                    if sent:
                        await asyncio.sleep(0.4)
        return counts

    async def send_missing_updates(self, progress: Callable[[str], Awaitable[None]] | None = None) -> dict[str, int]:
        """Repair channels from recent source history without trusting cursors."""
        assert self.poster is not None
        counts = {"FastFlags": 0, "API dumps": 0, "CppTree": 0}
        async with self.update_lock:
            for tracker in self.build_trackers():
                try:
                    source_name = {
                        FFlagTracker: "FastFlags",
                        ApiDumpTracker: "API dumps",
                        CppTreeTracker: "CppTree",
                    }.get(type(tracker), tracker.__class__.__name__)
                    if progress:
                        await progress(f"Checking {source_name} channel history...")
                    if isinstance(tracker, CommitTracker):
                        seen = await self.poster.seen_commit_shas(tracker.channel_key)
                        candidates = await tracker.recent_commits(MANUAL_REPLAY_COMMITS)
                        commits = [item for item in candidates if item["sha"][:12] not in seen]
                        if progress:
                            await progress(f"{source_name}: found {len(commits)} missing of the latest {len(candidates)} commits; preparing updates...")
                        posts = await tracker.run(commits)
                    else:
                        posts = await tracker.run()
                except GithubRateLimited as exc:
                    logging.warning("%s skipped: %s", tracker.__class__.__name__, exc)
                    continue
                except Exception:
                    logging.exception("%s missed-update repair failed", tracker.__class__.__name__)
                    continue
                for post in posts:
                    sent = False
                    try:
                        sent = await self.poster.send(post)
                        if sent:
                            if isinstance(tracker, FFlagTracker):
                                counts["FastFlags"] += 1
                            elif isinstance(tracker, ApiDumpTracker):
                                counts["API dumps"] += 1
                            elif isinstance(tracker, CppTreeTracker):
                                counts["CppTree"] += 1
                    except Exception:
                        logging.exception("Failed to repair a post for %s", tracker.__class__.__name__)
                    if sent:
                        await asyncio.sleep(0.4)
                if progress:
                    await progress(f"{source_name}: finished; sent {counts[source_name]} update(s). Moving to the next source...")
        return counts

    async def poll_loop(self) -> None:
        poll_seconds = int(os.getenv("POLL_SECONDS", "300"))
        while not self.is_closed():
            try:
                await self.poll_once()
            except Exception:
                # Whatever happens in a single cycle, the loop itself must
                # survive so the bot keeps polling on the next tick.
                logging.exception("poll_once() crashed; will retry next cycle")
            await asyncio.sleep(max(60, poll_seconds))


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")

    bot = RobloxTrackerBot()
    bot.run(token)


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception:
        traceback.print_exc()
        try:
            input("\nBot crashed. Press Enter to close this window...")
        except EOFError:
            pass
        raise
