from __future__ import annotations

import asyncio
import collections
import datetime
import hashlib
import io
import logging
import os
import random
import re
import secrets
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import discord
import edge_tts
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from captcha.image import ImageCaptcha
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("BOT_DATABASE_PATH") or BASE_DIR / "bot_data.sqlite3")
MAX_BULK_LOG_BYTES = 2_000_000
MUSIC_IDLE_TIMEOUT_SECONDS = 180
EMPTY_VOICE_DISCONNECT_SECONDS = 5
MAX_TTS_TEXT_LENGTH = 100
TTS_VOICES = {
    "sunhi": ("ko-KR-SunHiNeural", "한국어 여성 - 선희"),
    "injoon": ("ko-KR-InJoonNeural", "한국어 남성 - 인준"),
    "hyunsu": ("ko-KR-HyunsuMultilingualNeural", "한국어 남성 - 현수"),
    "bongjin": ("ko-KR-BongJinNeural", "한국어 남성 - 봉진"),
    "gookmin": ("ko-KR-GookMinNeural", "한국어 여성 - 국민"),
}
LEVEL_XP_COOLDOWN_SECONDS = 60
VOICE_XP_INTERVAL_SECONDS = 600
KOREA_TIMEZONE = datetime.timezone(datetime.timedelta(hours=9), name="KST")
SPAM_WINDOW_SECONDS = 3
SPAM_MESSAGE_LIMIT = 5
SPAM_TIMEOUT_SECONDS = 60

BAD_WORDS = ("selfbot", "셀프봇", "discord.gg/", "discord.com/invite/")

file_handler = RotatingFileHandler(
    BASE_DIR / "discord_bot.log",
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), file_handler],
)
logger = logging.getLogger("community_bot")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER,
                verify_role_id INTEGER,
                voice_lobby_id INTEGER,
                stats_category_id INTEGER,
                stats_total_channel_id INTEGER,
                stats_online_channel_id INTEGER,
                stats_verified_channel_id INTEGER,
                attendance_reward_role_id INTEGER,
                welcome_channel_id INTEGER,
                verify_panel_channel_id INTEGER,
                verify_panel_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS role_panel_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS dm_opt_ins (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS temporary_voice_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                owner_id INTEGER,
                PRIMARY KEY (guild_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS verification_captchas (
                challenge_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                answer_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS captcha_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                requested_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS member_xp (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                last_xp_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS level_rewards (
                guild_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, level)
            );
            CREATE TABLE IF NOT EXISTS daily_checkins (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_date TEXT NOT NULL,
                streak INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS member_voices (
                user_id INTEGER PRIMARY KEY,
                voice TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_tts_channels (
                guild_id INTEGER NOT NULL,
                text_channel_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, text_channel_id)
            );
            CREATE TABLE IF NOT EXISTS guild_banned_words (
                guild_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                PRIMARY KEY (guild_id, word)
            );
            CREATE TABLE IF NOT EXISTS moderation_warnings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        settings_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(guild_settings)")
        }
        for column in (
            "stats_category_id",
            "stats_total_channel_id",
            "stats_online_channel_id",
            "stats_verified_channel_id",
            "attendance_reward_role_id",
            "welcome_channel_id",
            "verify_panel_channel_id",
            "verify_panel_message_id",
        ):
            if column not in settings_columns:
                self.connection.execute(
                    f"ALTER TABLE guild_settings ADD COLUMN {column} INTEGER"
                )
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(temporary_voice_channels)"
            )
        }
        if "owner_id" not in columns:
            self.connection.execute(
                "ALTER TABLE temporary_voice_channels ADD COLUMN owner_id INTEGER"
            )
        self.connection.commit()

    def settings(self, guild_id: int) -> dict[str, int | None]:
        row = self.connection.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return dict(row) if row else {"guild_id": guild_id}

    def set_settings(self, guild_id: int, **values: int | None) -> None:
        allowed = {
            "log_channel_id",
            "verify_role_id",
            "voice_lobby_id",
            "stats_category_id",
            "stats_total_channel_id",
            "stats_online_channel_id",
            "stats_verified_channel_id",
            "attendance_reward_role_id",
            "welcome_channel_id",
            "verify_panel_channel_id",
            "verify_panel_message_id",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("Unknown or empty settings update.")
        self.connection.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
        )
        assignments = ", ".join(f"{column} = ?" for column in values)
        self.connection.execute(
            f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
            (*values.values(), guild_id),
        )
        self.connection.commit()

    def set_role_panel(self, guild_id: int, role_ids: list[int]) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM role_panel_roles WHERE guild_id = ?", (guild_id,)
            )
            self.connection.executemany(
                "INSERT INTO role_panel_roles (guild_id, role_id) VALUES (?, ?)",
                [(guild_id, role_id) for role_id in role_ids],
            )

    def role_panels(self) -> list[tuple[int, list[int]]]:
        rows = self.connection.execute(
            "SELECT guild_id, role_id FROM role_panel_roles ORDER BY guild_id, role_id"
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(row["guild_id"], []).append(row["role_id"])
        return list(result.items())

    def set_dm_opt_in(self, guild_id: int, user_id: int, enabled: bool) -> None:
        if enabled:
            self.connection.execute(
                "INSERT OR IGNORE INTO dm_opt_ins (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
        else:
            self.connection.execute(
                "DELETE FROM dm_opt_ins WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        self.connection.commit()

    def dm_opt_ins(self, guild_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT user_id FROM dm_opt_ins WHERE guild_id = ? ORDER BY user_id",
            (guild_id,),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def add_temporary_voice_channel(
        self, guild_id: int, channel_id: int, owner_id: int
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO temporary_voice_channels (guild_id, channel_id, owner_id) VALUES (?, ?, ?)",
            (guild_id, channel_id, owner_id),
        )
        self.connection.commit()

    def temporary_voice_owner(self, guild_id: int, channel_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT owner_id FROM temporary_voice_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return row["owner_id"] if row else None

    def is_temporary_voice_channel(self, guild_id: int, channel_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM temporary_voice_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return row is not None

    def remove_temporary_voice_channel(self, guild_id: int, channel_id: int) -> None:
        self.connection.execute(
            "DELETE FROM temporary_voice_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        self.connection.commit()

    def temporary_voice_channels(self) -> list[tuple[int, int]]:
        rows = self.connection.execute(
            "SELECT guild_id, channel_id FROM temporary_voice_channels"
        ).fetchall()
        return [(row["guild_id"], row["channel_id"]) for row in rows]

    def create_captcha(self, guild_id: int, user_id: int, answer: str) -> str:
        challenge_id = secrets.token_urlsafe(18)
        now = int(time.time())
        with self.connection:
            row = self.connection.execute(
                "SELECT requested_at FROM captcha_cooldowns WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row and now - row["requested_at"] < 10:
                return ""
            self.connection.execute(
                "INSERT INTO captcha_cooldowns (guild_id, user_id, requested_at) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "requested_at = excluded.requested_at",
                (guild_id, user_id, now),
            )
            self.connection.execute(
                "DELETE FROM verification_captchas WHERE expires_at <= ? OR attempts >= 5",
                (now,),
            )
            self.connection.execute(
                "DELETE FROM verification_captchas WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self.connection.execute(
                "INSERT INTO verification_captchas "
                "(challenge_id, guild_id, user_id, answer_hash, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    challenge_id,
                    guild_id,
                    user_id,
                    _captcha_hash(challenge_id, answer),
                    int(time.time()) + 300,
                ),
            )
        return challenge_id

    def check_captcha(
        self, challenge_id: str, guild_id: int, user_id: int, answer: str
    ) -> bool:
        now = int(time.time())
        with self.connection:
            row = self.connection.execute(
                "SELECT answer_hash, expires_at, attempts FROM verification_captchas "
                "WHERE challenge_id = ? AND guild_id = ? AND user_id = ?",
                (challenge_id, guild_id, user_id),
            ).fetchone()
            if row is None or row["expires_at"] <= now or row["attempts"] >= 5:
                self.connection.execute(
                    "DELETE FROM verification_captchas WHERE challenge_id = ?",
                    (challenge_id,),
                )
                return False
            if secrets.compare_digest(
                row["answer_hash"], _captcha_hash(challenge_id, answer.upper().strip())
            ):
                self.connection.execute(
                    "DELETE FROM verification_captchas WHERE challenge_id = ?",
                    (challenge_id,),
                )
                return True
            self.connection.execute(
                "UPDATE verification_captchas SET attempts = attempts + 1 "
                "WHERE challenge_id = ?",
                (challenge_id,),
            )
        return False

    def award_xp(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        respect_cooldown: bool = True,
    ) -> tuple[int, int, list[int]] | None:
        now = int(time.time())
        with self.connection:
            row = self.connection.execute(
                "SELECT xp, level, last_xp_at FROM member_xp "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            xp = row["xp"] if row else 0
            level = row["level"] if row else 1
            last_xp_at = row["last_xp_at"] if row else 0
            if respect_cooldown and now - last_xp_at < LEVEL_XP_COOLDOWN_SECONDS:
                return None
            xp += amount
            levels_gained: list[int] = []
            while xp >= level * 100:
                xp -= level * 100
                level += 1
                levels_gained.append(level)
            self.connection.execute(
                "INSERT INTO member_xp (guild_id, user_id, xp, level, last_xp_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "xp = excluded.xp, level = excluded.level, last_xp_at = excluded.last_xp_at",
                (guild_id, user_id, xp, level, now),
            )
        return level, xp, levels_gained

    def member_xp(self, guild_id: int, user_id: int) -> tuple[int, int] | None:
        row = self.connection.execute(
            "SELECT level, xp FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return (row["level"], row["xp"]) if row else None

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
        rows = self.connection.execute(
            "SELECT user_id, level, xp FROM member_xp WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return sorted(
            ((row["user_id"], row["level"], row["xp"]) for row in rows),
            key=lambda row: ((row[1] - 1) * row[1] * 50 + row[2], row[0]),
            reverse=True,
        )[:limit]

    def level_rank(self, guild_id: int, user_id: int) -> tuple[int, int] | None:
        rows = self.leaderboard(guild_id, 100_000)
        for rank, (member_id, _, _) in enumerate(rows, 1):
            if member_id == user_id:
                return rank, len(rows)
        return None

    def set_level_reward(self, guild_id: int, level: int, role_id: int) -> None:
        self.connection.execute(
            "INSERT INTO level_rewards (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id",
            (guild_id, level, role_id),
        )
        self.connection.commit()

    def level_rewards(self, guild_id: int, levels: list[int]) -> list[int]:
        if not levels:
            return []
        placeholders = ",".join("?" for _ in levels)
        rows = self.connection.execute(
            f"SELECT role_id FROM level_rewards WHERE guild_id = ? AND level IN ({placeholders})",
            (guild_id, *levels),
        ).fetchall()
        return [row["role_id"] for row in rows]

    def checkin(self, guild_id: int, user_id: int, today: str) -> tuple[int, int] | None:
        previous = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
        with self.connection:
            row = self.connection.execute(
                "SELECT last_date, streak FROM daily_checkins WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row and row["last_date"] == today:
                return None
            streak = row["streak"] + 1 if row and row["last_date"] == previous else 1
            bonus = random.randint(70, 150)
            xp_row = self.connection.execute(
                "SELECT xp, level, last_xp_at FROM member_xp WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            xp = (xp_row["xp"] if xp_row else 0) + bonus
            level = xp_row["level"] if xp_row else 1
            levels_gained: list[int] = []
            while xp >= level * 100:
                xp -= level * 100
                level += 1
                levels_gained.append(level)
            self.connection.execute(
                "INSERT INTO daily_checkins (guild_id, user_id, last_date, streak) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "last_date = excluded.last_date, streak = excluded.streak",
                (guild_id, user_id, today, streak),
            )
            self.connection.execute(
                "INSERT INTO member_xp (guild_id, user_id, xp, level, last_xp_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "xp = excluded.xp, level = excluded.level",
                (guild_id, user_id, xp, level, xp_row["last_xp_at"] if xp_row else 0),
            )
        return bonus, streak

    def checkin_streak(self, guild_id: int, user_id: int) -> int:
        row = self.connection.execute(
            "SELECT streak FROM daily_checkins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row["streak"] if row else 0

    def set_attendance_reward(self, guild_id: int, role_id: int | None) -> None:
        self.set_settings(guild_id, attendance_reward_role_id=role_id)

    def set_member_voice(self, user_id: int, voice: str) -> None:
        self.connection.execute(
            "INSERT INTO member_voices (user_id, voice) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET voice = excluded.voice",
            (user_id, voice),
        )
        self.connection.commit()

    def member_voice(self, user_id: int) -> str:
        row = self.connection.execute(
            "SELECT voice FROM member_voices WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["voice"] if row else TTS_VOICES["sunhi"][0]

    def set_auto_tts(
        self, guild_id: int, text_channel_id: int, voice_channel_id: int | None
    ) -> None:
        if voice_channel_id is None:
            self.connection.execute(
                "DELETE FROM auto_tts_channels WHERE guild_id = ? AND text_channel_id = ?",
                (guild_id, text_channel_id),
            )
        else:
            self.connection.execute(
                "INSERT INTO auto_tts_channels (guild_id, text_channel_id, voice_channel_id) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id, text_channel_id) DO UPDATE SET "
                "voice_channel_id = excluded.voice_channel_id",
                (guild_id, text_channel_id, voice_channel_id),
            )
        self.connection.commit()

    def auto_tts_voice(self, guild_id: int, text_channel_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT voice_channel_id FROM auto_tts_channels "
            "WHERE guild_id = ? AND text_channel_id = ?",
            (guild_id, text_channel_id),
        ).fetchone()
        return row["voice_channel_id"] if row else None

    def auto_tts_settings(self, guild_id: int) -> dict[int, int]:
        rows = self.connection.execute(
            "SELECT text_channel_id, voice_channel_id FROM auto_tts_channels "
            "WHERE guild_id = ? ORDER BY text_channel_id",
            (guild_id,),
        ).fetchall()
        return {row["text_channel_id"]: row["voice_channel_id"] for row in rows}

    def clear_auto_tts_settings(self, guild_id: int) -> None:
        self.connection.execute(
            "DELETE FROM auto_tts_channels WHERE guild_id = ?", (guild_id,)
        )
        self.connection.commit()

    def set_banned_words(self, guild_id: int, words: list[str]) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM guild_banned_words WHERE guild_id = ?", (guild_id,)
            )
            self.connection.executemany(
                "INSERT INTO guild_banned_words (guild_id, word) VALUES (?, ?)",
                [(guild_id, word.casefold()) for word in words],
            )

    def banned_words(self, guild_id: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT word FROM guild_banned_words WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return [row["word"] for row in rows]

    def add_warning(self, guild_id: int, user_id: int) -> int:
        with self.connection:
            self.connection.execute(
                "INSERT INTO moderation_warnings (guild_id, user_id, count) VALUES (?, ?, 1) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + 1",
                (guild_id, user_id),
            )
            row = self.connection.execute(
                "SELECT count FROM moderation_warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return row["count"]


store = Store(DATABASE_PATH)


def _captcha_hash(challenge_id: str, answer: str) -> str:
    return hashlib.sha256(f"{challenge_id}:{answer.casefold()}".encode("utf-8")).hexdigest()


def generate_captcha(answer: str) -> bytes:
    image = ImageCaptcha(width=300, height=100)
    return image.generate(answer).getvalue()

def clipped(text: str | None, limit: int = 1000) -> str:
    if not text:
        return "(내용 없음)"
    return text if len(text) <= limit else text[: limit - 3] + "..."


class CommunityBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.maintenance_tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        self.add_view(VerifyView())
        self.add_view(MusicControlsView())
        for guild_id, role_ids in store.role_panels():
            valid_roles = [
                role_id
                for role_id in role_ids
                if self.get_guild(guild_id) is None
                or self.get_guild(guild_id).get_role(role_id) is not None
            ]
            if valid_roles:
                self.add_view(RolePanelView(guild_id, valid_roles))
        await self.tree.sync()
        self.maintenance_tasks = [
            asyncio.create_task(self._voice_xp_loop()),
            asyncio.create_task(self._empty_room_cleanup_loop()),
        ]

    async def _voice_xp_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(VOICE_XP_INTERVAL_SECONDS)
            for guild in self.guilds:
                for voice_channel in guild.voice_channels:
                    if voice_channel == guild.afk_channel:
                        continue
                    for member in voice_channel.members:
                        if member.bot or (member.voice and member.voice.self_deaf):
                            continue
                        await award_member_xp(
                            member,
                            random.randint(15, 25),
                            channel=guild.system_channel,
                            respect_cooldown=False,
                        )

    async def _empty_room_cleanup_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(1800)
            for guild_id, channel_id in store.temporary_voice_channels():
                guild = self.get_guild(guild_id)
                channel = guild.get_channel(channel_id) if guild else None
                if guild is None or channel is None:
                    store.remove_temporary_voice_channel(guild_id, channel_id)
                elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                    try:
                        await channel.delete(reason="Periodic empty temporary-room cleanup")
                    except discord.NotFound:
                        store.remove_temporary_voice_channel(guild_id, channel_id)
                    except discord.Forbidden:
                        logger.warning("Missing permission to clean temporary room %s", channel_id)
                    except discord.HTTPException:
                        logger.exception("Could not clean temporary room %s", channel_id)
                    else:
                        store.remove_temporary_voice_channel(guild_id, channel_id)

    async def close(self) -> None:
        for task in self.maintenance_tasks:
            task.cancel()
        if self.maintenance_tasks:
            await asyncio.gather(*self.maintenance_tasks, return_exceptions=True)
        await super().close()


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True
bot = CommunityBot()


def can_send_embeds(channel: discord.TextChannel, member: discord.Member) -> bool:
    permissions = channel.permissions_for(member)
    return (
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
    )


async def send_log(guild: discord.Guild, embed: discord.Embed) -> None:
    channel_id = store.settings(guild.id).get("log_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.warning(
            "Cannot send audit log to channel %s: grant the bot View Channel, "
            "Send Messages, and Embed Links permissions.",
            channel.id,
        )
    except discord.HTTPException:
        logger.exception("Could not send an audit log to channel %s", channel.id)


async def send_member_notice(guild: discord.Guild, content: str) -> None:
    settings = store.settings(guild.id)
    channel_id = settings.get("welcome_channel_id") or settings.get("log_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else guild.system_channel
    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "No welcome/leave announcement channel is configured for guild %s.",
            guild.id,
        )
        return
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        logger.warning("Missing permission to send member notice to channel %s", channel.id)
    except discord.HTTPException:
        logger.exception("Could not send member notice to channel %s", channel.id)
        return
    log_channel_id = settings.get("log_channel_id")
    if log_channel_id and log_channel_id != channel.id:
        await send_log(
            guild,
            discord.Embed(
                title="입퇴장 안내",
                description=clipped(content, 4000),
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            ),
        )


def member_notice_text(
    member: discord.Member, *, joined: bool, timestamp: int
) -> str:
    safe_name = discord.utils.escape_markdown(member.name)
    greeting = (
        "님, 서버에 오신 것을 환영합니다! 🎉"
        if joined
        else "님, 안녕히 가세요 😢"
    )
    event_label = "서버 입장시간" if joined else "서버 퇴장시간"
    details = [
        f"### [{safe_name}](https://discord.com/users/{member.id}){greeting}",
        f"**유저 : <@{member.id}> / {safe_name} ({member.id})**",
        f"**{event_label} : <t:{timestamp}:F>**",
    ]
    if joined:
        created_at = int(member.created_at.timestamp())
        details.extend(
            [
                f"**Discord 가입일 : <t:{created_at}:D> (<t:{created_at}:R>)**",
                f"**현재 인원 : {member.guild.member_count or len(member.guild.members)}명**",
            ]
        )
    return "\n".join(details)


async def send_bulk_delete_log(
    guild: discord.Guild,
    source_channel: discord.abc.GuildChannel,
    messages: list[discord.Message],
) -> None:
    channel_id = store.settings(guild.id).get("log_channel_id")
    log_channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(log_channel, discord.TextChannel):
        return
    messages = sorted(messages, key=lambda message: message.created_at)
    lines: list[str] = []
    size = 0
    for message in messages:
        attachments = "".join(
            f"  첨부: {item.filename} — {item.url}\n" for item in message.attachments
        )
        line = (
            f"[{message.created_at:%Y-%m-%d %H:%M:%S} UTC] "
            f"{message.author} ({message.author.id}) / {message.id}\n"
            f"{message.content or '(텍스트 없음)'}\n{attachments}\n"
        )
        encoded = line.encode("utf-8")
        if size + len(encoded) > MAX_BULK_LOG_BYTES:
            lines.append("[파일 크기 제한으로 나머지 메시지는 생략되었습니다.]\n")
            break
        size += len(encoded)
        lines.append(line)
    embed = discord.Embed(
        title="🧹 메시지 대량 삭제",
        description=f"{source_channel.mention}에서 메시지 {len(messages)}개가 삭제되었습니다.",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    try:
        await log_channel.send(
            embed=embed,
            file=discord.File(
                io.BytesIO("".join(lines).encode("utf-8")),
                filename="대량삭제기록.txt",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        logger.warning(
            "Cannot send bulk-delete log to channel %s: grant the bot View Channel, "
            "Send Messages, Attach Files, and Embed Links permissions.",
            log_channel.id,
        )
    except discord.HTTPException:
        logger.exception("Could not send a bulk-delete log to channel %s", log_channel.id)


class VerifyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="인증하기",
        emoji="🛡️",
        style=discord.ButtonStyle.success,
        custom_id="community:verify",
    )
    async def verify(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        role_id = store.settings(interaction.guild.id).get("verify_role_id")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role is None:
            await interaction.response.send_message(
                "인증 역할이 설정되지 않았거나 삭제되었습니다. 관리자에게 문의해 주세요.",
                ephemeral=True,
            )
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(
                "이미 인증이 완료되었습니다.", ephemeral=True
            )
            return
        answer = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)
        )
        challenge_id = store.create_captcha(
            interaction.guild.id, interaction.user.id, answer
        )
        if not challenge_id:
            await interaction.response.send_message(
                "새 인증 그림은 10초에 한 번만 요청할 수 있습니다. 잠시 후 다시 눌러 주세요.",
                ephemeral=True,
            )
            return
        image_bytes = await asyncio.to_thread(generate_captcha, answer)
        embed = discord.Embed(
            title="보안 문자 인증",
            description="그림에 표시된 영문/숫자 6자를 입력해 주세요. 대소문자는 구분하지 않습니다.",
            color=discord.Color.blurple(),
        )
        embed.set_image(url="attachment://verification-captcha.png")
        await interaction.response.send_message(
            embed=embed,
            file=discord.File(io.BytesIO(image_bytes), filename="verification-captcha.png"),
            view=CaptchaPromptView(
                interaction.guild.id, interaction.user.id, challenge_id
            ),
            ephemeral=True,
        )


class CaptchaAnswerModal(discord.ui.Modal, title="보안 문자 입력"):
    answer = discord.ui.TextInput(
        label="그림의 영문/숫자 6자를 입력하세요",
        min_length=6,
        max_length=6,
        required=True,
    )

    def __init__(self, guild_id: int, user_id: int, challenge_id: str) -> None:
        super().__init__(timeout=180, custom_id=f"community:captcha:{challenge_id}")
        self.guild_id = guild_id
        self.user_id = user_id
        self.challenge_id = challenge_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.guild_id
            or interaction.user.id != self.user_id
            or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "이 인증은 요청한 멤버만 제출할 수 있습니다.", ephemeral=True
            )
            return
        if not store.check_captcha(
            self.challenge_id,
            self.guild_id,
            self.user_id,
            str(self.answer.value),
        ):
            await interaction.response.send_message(
                "인증 문자가 틀렸거나 만료되었습니다. 인증 버튼을 다시 눌러 새 그림을 받아 주세요.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        settings = store.settings(self.guild_id)
        role_id = settings.get("verify_role_id")
        role = guild.get_role(role_id) if guild and role_id else None
        if (
            role is None
            or guild is None
            or guild.me is None
            or role >= guild.me.top_role
        ):
            await interaction.response.send_message(
                "인증 역할 설정이 없거나 봇이 지급할 수 없습니다. 관리자에게 문의해 주세요.",
                ephemeral=True,
            )
            return
        try:
            await interaction.user.add_roles(role, reason="Completed Discord CAPTCHA")
        except discord.Forbidden:
            await interaction.response.send_message(
                "봇이 인증 역할을 지급할 수 없습니다. 역할 순서와 권한을 확인해 주세요.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Failed to grant CAPTCHA verification role %s", role.id)
            await interaction.response.send_message(
                "역할 지급에 실패했습니다. 잠시 후 인증을 다시 시도해 주세요.",
                ephemeral=True,
            )
            return
        schedule_server_stats(guild)
        await interaction.response.send_message(
            f"✅ 인증 완료! {role.mention} 역할을 지급했습니다.",
            ephemeral=True,
        )


class CaptchaPromptView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, challenge_id: str) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.challenge_id = challenge_id

    @discord.ui.button(
        label="인증 문자 입력",
        emoji="⌨️",
        style=discord.ButtonStyle.primary,
    )
    async def enter_answer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if (
            interaction.guild_id != self.guild_id
            or interaction.user.id != self.user_id
        ):
            await interaction.response.send_message(
                "이 인증은 요청한 멤버만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            CaptchaAnswerModal(self.guild_id, self.user_id, self.challenge_id)
        )


class TTSVoiceSelect(discord.ui.Select):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        super().__init__(
            placeholder="목소리 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key)
                for key, (_, label) in TTS_VOICES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "이 메뉴를 연 멤버만 사용할 수 있습니다.", ephemeral=True
            )
            return
        key = self.values[0]
        store.set_member_voice(interaction.user.id, TTS_VOICES[key][0])
        await interaction.response.send_message(
            f"목소리를 **{TTS_VOICES[key][1]}**(으)로 저장했습니다.",
            ephemeral=True,
        )


class TTSVoiceSelectView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.add_item(TTSVoiceSelect(owner_id))


class RolePanelSelect(discord.ui.Select):
    def __init__(self, guild_id: int, role_ids: list[int]) -> None:
        guild = bot.get_guild(guild_id)
        options = [
            discord.SelectOption(
                label=(role.name if role else f"역할 {role_id}")[:100],
                value=str(role_id),
            )
            for role_id in role_ids
            for role in [guild.get_role(role_id) if guild else None]
        ]
        super().__init__(
            placeholder="원하는 역할을 선택하거나 다시 눌러 해제하세요",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            custom_id=f"community:roles:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        if interaction.guild_id != int(self.custom_id.rsplit(":", 1)[-1]):
            await interaction.response.send_message(
                "이 역할 패널은 다른 서버용입니다.", ephemeral=True
            )
            return

        added: list[str] = []
        removed: list[str] = []
        try:
            for value in self.values:
                role = interaction.guild.get_role(int(value))
                if role is None or role.managed or role >= interaction.guild.me.top_role:
                    continue
                if role in interaction.user.roles:
                    await interaction.user.remove_roles(role, reason="Self-service role panel")
                    removed.append(role.name)
                else:
                    await interaction.user.add_roles(role, reason="Self-service role panel")
                    added.append(role.name)
        except discord.Forbidden:
            await interaction.response.send_message(
                "일부 역할을 변경하지 못했습니다. 봇 역할의 위치와 권한을 확인해 주세요.",
                ephemeral=True,
            )
            return

        changes = []
        if added:
            changes.append("추가: " + ", ".join(added))
        if removed:
            changes.append("해제: " + ", ".join(removed))
        await interaction.response.send_message(
            "\n".join(changes) if changes else "변경 가능한 역할이 없습니다.",
            ephemeral=True,
        )


class RolePanelView(discord.ui.View):
    def __init__(self, guild_id: int, role_ids: list[int]) -> None:
        super().__init__(timeout=None)
        self.add_item(RolePanelSelect(guild_id, role_ids))


class AnnouncementConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, author_id: int, content: str) -> None:
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.author_id = author_id
        self.content = content

    @discord.ui.button(
        label="동의한 멤버에게 전송",
        emoji="📨",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if (
            interaction.guild_id != self.guild_id
            or interaction.user.id != self.author_id
            or not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "이 발송은 요청한 관리자만 확인할 수 있습니다.", ephemeral=False
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "서버 정보를 확인할 수 없습니다.", ephemeral=False
            )
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        recipients = store.dm_opt_ins(guild.id)
        if not recipients:
            await interaction.followup.send(
                "DM 수신에 동의한 멤버가 없습니다.", ephemeral=False
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(
                "이 채널에 발송 진행 상황을 표시할 수 없습니다.",
                ephemeral=False,
            )
            return
        status_message = await channel.send(
            f"📨 공지 DM 발송을 시작합니다. 수신 동의자 **{len(recipients)}명** 대상입니다."
        )
        success = 0
        failed = 0
        failed_members: list[str] = []
        for index, user_id in enumerate(recipients, 1):
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    failed += 1
                    failed_members.append(f"{user_id} (서버 멤버가 아님)")
                    store.set_dm_opt_in(guild.id, user_id, False)
                    member = None
                except discord.HTTPException as error:
                    failed += 1
                    failed_members.append(f"{user_id} (조회 오류 {error.status})")
                    member = None
            if member and member.bot:
                failed += 1
                failed_members.append(f"{user_id} (봇 계정)")
            elif member:
                embed = discord.Embed(
                    title=f"{guild.name} 공지",
                    description=self.content,
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await member.send(embed=embed)
                    success += 1
                except discord.Forbidden:
                    failed += 1
                    failed_members.append(f"{member.id} (DM 차단)")
                except discord.HTTPException as error:
                    failed += 1
                    failed_members.append(f"{member.id} (Discord 오류 {error.status})")
            await asyncio.sleep(1.0)
            if index % 30 == 0 and index < len(recipients):
                await asyncio.sleep(5.0)
            if index % 25 == 0:
                await status_message.edit(
                    content=(
                        f"📨 공지 DM 발송 중: **{index}/{len(recipients)}명** "
                        f"(성공 {success}명, 실패 {failed}명)"
                    )
                )
        report = f"발송 완료 — 성공 {success}명, 실패 {failed}명."
        if failed_members:
            details = "\n".join(failed_members[:20])
            if len(failed_members) > 20:
                details += f"\n그 외 {len(failed_members) - 20}명"
            report += f"\n\n실패 내역 (최대 20명):\n{details}"
        await status_message.edit(content=clipped(report, 1900))
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "발송 요청자만 취소할 수 있습니다.", ephemeral=False
            )
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="발송을 취소했습니다.", view=self)
        self.stop()


@dataclass
class Track:
    title: str
    webpage_url: str
    requester_id: int


@dataclass
class Speech:
    user_id: int
    channel_id: int
    text: str
    voice: str


YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}

audio_locks: collections.defaultdict[int, asyncio.Lock] = collections.defaultdict(
    asyncio.Lock
)


def extract_track(query: str, requester_id: int) -> Track:
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
        info = ytdl.extract_info(query, download=False)
    if "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info or not info.get("webpage_url"):
        raise yt_dlp.utils.DownloadError("검색 결과에서 재생 가능한 곡을 찾지 못했습니다.")
    return Track(
        title=str(info.get("title") or "제목 없음"),
        webpage_url=str(info["webpage_url"]),
        requester_id=requester_id,
    )


def extract_stream_url(url: str) -> str:
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
        info = ytdl.extract_info(url, download=False)
    if "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info or not info.get("url"):
        raise yt_dlp.utils.DownloadError("오디오 스트림 주소를 가져오지 못했습니다.")
    return str(info["url"])


class MusicManager:
    def __init__(self) -> None:
        self.queues: dict[int, list[Track]] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.idle_tasks: dict[int, asyncio.Task[None]] = {}
        self.volumes: dict[int, float] = {}
        self.repeat_tracks: set[int] = set()
        self.skipped_tracks: set[int] = set()

    def get_volume(self, guild_id: int) -> float:
        return self.volumes.get(guild_id, 0.5)

    def is_repeating(self, guild_id: int) -> bool:
        return guild_id in self.repeat_tracks

    def toggle_repeat(self, guild_id: int) -> bool:
        if guild_id in self.repeat_tracks:
            self.repeat_tracks.remove(guild_id)
            return False
        self.repeat_tracks.add(guild_id)
        return True

    def mark_track_skipped(self, guild_id: int) -> None:
        self.skipped_tracks.add(guild_id)

    def consume_track_skipped(self, guild_id: int) -> bool:
        if guild_id in self.skipped_tracks:
            self.skipped_tracks.remove(guild_id)
            return True
        return False

    def set_volume(self, guild: discord.Guild, volume: int) -> None:
        normalized_volume = volume / 100
        self.volumes[guild.id] = normalized_volume
        voice = guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = normalized_volume

    def enqueue(self, guild_id: int, track: Track) -> int:
        queue = self.queues.setdefault(guild_id, [])
        queue.append(track)
        return len(queue)

    async def play_queue(self, guild: discord.Guild) -> None:
        guild_id = guild.id
        try:
            while self.queues.get(guild_id):
                voice = guild.voice_client
                if not voice or not voice.is_connected():
                    break
                track = self.queues[guild_id].pop(0)
                try:
                    while True:
                        stream_url = await asyncio.to_thread(
                            extract_stream_url, track.webpage_url
                        )
                        async with audio_locks[guild_id]:
                            if not voice.is_connected():
                                break
                            source = discord.PCMVolumeTransformer(
                                discord.FFmpegPCMAudio(
                                    stream_url,
                                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                                    options="-vn -threads 2",
                                ),
                                volume=self.get_volume(guild_id),
                            )
                            loop = asyncio.get_running_loop()
                            finished: asyncio.Future[None] = loop.create_future()

                            def after(error: Exception | None) -> None:
                                def complete() -> None:
                                    if finished.done():
                                        return
                                    if error:
                                        finished.set_exception(error)
                                    else:
                                        finished.set_result(None)

                                loop.call_soon_threadsafe(complete)

                            voice.play(source, after=after)
                            await finished
                        if self.consume_track_skipped(guild_id) or not self.is_repeating(
                            guild_id
                        ):
                            break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Could not play track %s", track.title)
        finally:
            self.tasks.pop(guild_id, None)
            if not self.queues.get(guild_id):
                self.queues.pop(guild_id, None)
                self.schedule_idle_disconnect(guild)

    def start(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        current = self.tasks.get(guild.id)
        if current is None or current.done():
            self.tasks[guild.id] = asyncio.create_task(self.play_queue(guild))

    def clear(self, guild_id: int) -> None:
        self.queues.pop(guild_id, None)
        self.repeat_tracks.discard(guild_id)
        self.skipped_tracks.discard(guild_id)
        self.cancel_idle_disconnect(guild_id)

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def schedule_idle_disconnect(
        self, guild: discord.Guild, delay: int = MUSIC_IDLE_TIMEOUT_SECONDS
    ) -> None:
        self.cancel_idle_disconnect(guild.id)
        voice = guild.voice_client
        if not voice or not voice.is_connected():
            return

        async def disconnect_if_still_idle() -> None:
            try:
                await asyncio.sleep(delay)
                current = guild.voice_client
                if (
                    current
                    and current.is_connected()
                    and not any(not member.bot for member in current.channel.members)
                ):
                    self.queues.pop(guild.id, None)
                    self.repeat_tracks.discard(guild.id)
                    self.skipped_tracks.discard(guild.id)
                    tts.stop(guild)
                    if current.is_playing() or current.is_paused():
                        current.stop()
                    await current.disconnect()
            except asyncio.CancelledError:
                raise
            except discord.HTTPException:
                logger.exception("Could not disconnect idle voice client in guild %s", guild.id)
            finally:
                self.idle_tasks.pop(guild.id, None)

        self.idle_tasks[guild.id] = asyncio.create_task(disconnect_if_still_idle())


music = MusicManager()


def clean_tts_text(text: str, guild_id: int | None = None) -> str:
    text = re.sub(r"(.)\1{3,}", r"\1\1\1", text)
    text = re.sub(r"https?://\S+|discord\.gg/\S+", "링크", text, flags=re.IGNORECASE)
    text = discord.utils.escape_mentions(text).strip()
    blocked = (*BAD_WORDS, *(store.banned_words(guild_id) if guild_id else []))
    if any(word and word.casefold() in text.casefold() for word in blocked):
        return ""
    return text[:MAX_TTS_TEXT_LENGTH]


class TTSManager:
    def __init__(self) -> None:
        self.queues: dict[int, collections.deque[Speech]] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.playing_guilds: set[int] = set()
        self.active_users: dict[int, int] = {}
        self.auto_readers: dict[tuple[int, int], int] = {}
        self.auto_reader_text_channels: dict[tuple[int, int], int] = {}
        self.connection_locks: collections.defaultdict[int, asyncio.Lock] = (
            collections.defaultdict(asyncio.Lock)
        )

    def enable_auto_reading(
        self, guild_id: int, user_id: int, voice_channel_id: int, text_channel_id: int
    ) -> None:
        key = (guild_id, user_id)
        self.auto_readers[key] = voice_channel_id
        self.auto_reader_text_channels[key] = text_channel_id

    def auto_reading_channel(self, guild_id: int, user_id: int) -> int | None:
        return self.auto_readers.get((guild_id, user_id))

    def auto_reading_text_channel(self, guild_id: int, user_id: int) -> int | None:
        return self.auto_reader_text_channels.get((guild_id, user_id))

    def enqueue(self, guild_id: int, speech: Speech) -> int:
        queue = self.queues.setdefault(guild_id, collections.deque())
        queue.append(speech)
        return len(queue)

    async def _synthesize_speech(self, speech: Speech, audio_path: Path) -> None:
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(speech.text, speech.voice)
                await communicate.save(str(audio_path))
                if audio_path.stat().st_size == 0:
                    raise edge_tts.exceptions.NoAudioReceived(
                        "TTS service created an empty audio file."
                    )
                return
            except edge_tts.exceptions.NoAudioReceived:
                if attempt == 2:
                    raise
                logger.warning(
                    "TTS service returned no audio; retrying guild playback "
                    "(attempt %s/3).",
                    attempt + 2,
                )
                await asyncio.sleep(attempt + 1)

    async def _play_speech(self, guild: discord.Guild, speech: Speech) -> None:
        voice_client = guild.voice_client
        member = guild.get_member(speech.user_id)
        if (
            voice_client is None
            or not voice_client.is_connected()
            or voice_client.channel.id != speech.channel_id
            or member is None
            or member.voice is None
            or member.voice.channel.id != speech.channel_id
        ):
            return
        temp_dir = BASE_DIR / "tts_temp"
        temp_dir.mkdir(exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="speech_", suffix=".mp3", dir=temp_dir, delete=False
        )
        audio_path = Path(handle.name)
        handle.close()
        try:
            await self._synthesize_speech(speech, audio_path)
            async with audio_locks[guild.id]:
                voice_client = guild.voice_client
                member = guild.get_member(speech.user_id)
                if (
                    voice_client is None
                    or not voice_client.is_connected()
                    or voice_client.channel.id != speech.channel_id
                    or member is None
                    or member.voice is None
                    or member.voice.channel.id != speech.channel_id
                ):
                    return
                loop = asyncio.get_running_loop()
                finished: asyncio.Future[None] = loop.create_future()

                def after(error: Exception | None) -> None:
                    def complete() -> None:
                        if finished.done():
                            return
                        if error:
                            finished.set_exception(error)
                        else:
                            finished.set_result(None)

                    loop.call_soon_threadsafe(complete)

                source = discord.FFmpegPCMAudio(str(audio_path), options="-vn -threads 2")
                self.playing_guilds.add(guild.id)
                self.active_users[guild.id] = speech.user_id
                voice_client.play(source, after=after)
                try:
                    await finished
                finally:
                    self.playing_guilds.discard(guild.id)
                    self.active_users.pop(guild.id, None)
        finally:
            audio_path.unlink(missing_ok=True)

    async def _worker(self, guild: discord.Guild) -> None:
        try:
            queue = self.queues[guild.id]
            while queue:
                speech = queue.popleft()
                try:
                    await self._play_speech(guild, speech)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("TTS playback failed in guild %s", guild.id)
        finally:
            self.tasks.pop(guild.id, None)
            if not self.queues.get(guild.id):
                self.queues.pop(guild.id, None)

    def start(self, guild: discord.Guild) -> None:
        task = self.tasks.get(guild.id)
        if task is None or task.done():
            self.tasks[guild.id] = asyncio.create_task(self._worker(guild))

    def clear_user(self, guild_id: int, user_id: int) -> int:
        queue = self.queues.get(guild_id)
        if queue is None:
            return 0
        original = len(queue)
        self.queues[guild_id] = collections.deque(
            item for item in queue if item.user_id != user_id
        )
        return original - len(self.queues[guild_id])

    def stop_user(self, guild: discord.Guild, user_id: int) -> tuple[int, bool]:
        key = (guild.id, user_id)
        self.auto_readers.pop(key, None)
        self.auto_reader_text_channels.pop(key, None)
        queued = self.clear_user(guild.id, user_id)
        voice = guild.voice_client
        playing = self.active_users.get(guild.id) == user_id
        if playing and voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        return queued, playing

    def stop(self, guild: discord.Guild) -> tuple[int, bool]:
        self.auto_readers = {
            key: channel_id
            for key, channel_id in self.auto_readers.items()
            if key[0] != guild.id
        }
        self.auto_reader_text_channels = {
            key: channel_id
            for key, channel_id in self.auto_reader_text_channels.items()
            if key[0] != guild.id
        }
        queued = len(self.queues.pop(guild.id, ()))
        voice = guild.voice_client
        was_playing = guild.id in self.playing_guilds
        if was_playing and voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        task = self.tasks.pop(guild.id, None)
        if task and not task.done():
            task.cancel()
        return queued, was_playing


tts = TTSManager()


async def apply_level_rewards(
    member: discord.Member, gained_levels: list[int]
) -> list[str]:
    added: list[str] = []
    for role_id in store.level_rewards(member.guild.id, gained_levels):
        role = member.guild.get_role(role_id)
        me = member.guild.me
        if (
            role is None
            or role in member.roles
            or role.managed
            or me is None
            or role >= me.top_role
        ):
            continue
        try:
            await member.add_roles(role, reason="Community level reward")
        except discord.Forbidden:
            logger.warning("Could not grant level reward role %s in guild %s", role_id, member.guild.id)
        except discord.HTTPException:
            logger.exception("Failed to grant level reward role %s", role_id)
        else:
            added.append(role.name)
    return added


async def award_member_xp(
    member: discord.Member,
    amount: int,
    *,
    channel: discord.abc.Messageable | None = None,
    respect_cooldown: bool = True,
) -> tuple[int, int] | None:
    result = store.award_xp(
        member.guild.id,
        member.id,
        amount,
        respect_cooldown=respect_cooldown,
    )
    if result is None:
        return None
    level, xp, levels_gained = result
    if not levels_gained:
        return level, xp
    role_names = await apply_level_rewards(member, levels_gained)
    embed = discord.Embed(
        title="🎉 레벨 업!",
        description=f"{member.mention}님의 레벨이 **Lv. {level}**이 되었습니다!",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if role_names:
        embed.add_field(name="레벨 보상", value=", ".join(role_names))
    destination = channel or member.guild.system_channel
    if destination:
        try:
            await destination.send(
                embed=embed, allowed_mentions=discord.AllowedMentions(users=True)
            )
        except discord.HTTPException:
            logger.exception("Could not announce a level-up in guild %s", member.guild.id)
    return level, xp


def create_profile_card(
    display_name: str,
    level: int,
    xp: int,
    rank: int,
    member_count: int,
    avatar_bytes: bytes,
) -> bytes:
    width, height = 960, 420
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        draw.line(
            (0, y, width, y),
            fill=(int(35 + 20 * ratio), int(46 + 14 * ratio), int(80 + 35 * ratio)),
        )
    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=28, fill="#182338")
    font_paths = (
        Path(os.getenv("BOT_FONT_PATH", "")) if os.getenv("BOT_FONT_PATH") else None,
        Path("C:\\Windows\\Fonts\\malgun.ttf"),
        Path("C:\\Windows\\Fonts\\arial.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )

    def font(size: int) -> ImageFont.ImageFont:
        for candidate in font_paths:
            if candidate and candidate.is_file():
                try:
                    return ImageFont.truetype(str(candidate), size=size)
                except OSError:
                    continue
        return ImageFont.load_default()

    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((220, 220))
    mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 219, 219), fill=255)
    image.paste(avatar, (76, 100), mask)
    draw = ImageDraw.Draw(image)
    draw.text((340, 90), display_name[:32], font=font(42), fill="#f9fafb")
    draw.text((340, 155), f"LEVEL {level}", font=font(30), fill="#93c5fd")
    needed = max(1, level * 100)
    percent = min(1.0, xp / needed)
    draw.rounded_rectangle((340, 220, 860, 264), radius=18, fill="#334155")
    if percent:
        draw.rounded_rectangle(
            (340, 220, 340 + int(520 * percent), 264),
            radius=18,
            fill="#60a5fa",
        )
    draw.text(
        (340, 280),
        f"{xp:,} / {needed:,} XP  ({int(percent * 100)}%)",
        font=font(22),
        fill="#dbeafe",
    )
    draw.text(
        (340, 335),
        f"서버 순위 {rank}위 / {member_count}명",
        font=font(22),
        fill="#cbd5e1",
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def profile_card_for_member(
    member: discord.Member,
) -> tuple[bytes, int, int, int, int] | None:
    stats = store.member_xp(member.guild.id, member.id)
    ranking = store.level_rank(member.guild.id, member.id)
    if not stats or not ranking:
        return None
    level, xp = stats
    rank, count = ranking
    avatar_bytes = await member.display_avatar.read()
    card = await asyncio.to_thread(
        create_profile_card,
        member.display_name,
        level,
        xp,
        rank,
        count,
        avatar_bytes,
    )
    return card, level, xp, rank, count


async def refresh_server_stats(guild: discord.Guild) -> None:
    settings = store.settings(guild.id)
    verified_role_id = settings.get("verify_role_id")
    verified_role = guild.get_role(verified_role_id) if verified_role_id else None
    values = (
        (settings.get("stats_total_channel_id"), f"📊 전체 회원수 : {guild.member_count or len(guild.members)}명"),
        (settings.get("stats_online_channel_id"), f"🟢 온라인 : {sum(member.status != discord.Status.offline for member in guild.members)}명"),
        (settings.get("stats_verified_channel_id"), f"🌐 인증 완료 : {sum(verified_role in member.roles for member in guild.members) if verified_role else 0}명"),
    )
    for channel_id, name in values:
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.VoiceChannel) or channel.name == name:
            continue
        try:
            await channel.edit(name=name, reason="Refresh server member statistics")
        except discord.Forbidden:
            logger.warning("Missing permission to update stats channel %s", channel_id)
        except discord.HTTPException:
            logger.exception("Could not update stats channel %s", channel_id)


stats_refresh_tasks: dict[int, asyncio.Task[None]] = {}
stats_last_refreshed: dict[int, float] = {}
stats_refresh_generation: dict[int, int] = {}
SERVER_STATS_MIN_INTERVAL_SECONDS = 10 * 60


def schedule_server_stats(guild: discord.Guild) -> None:
    stats_refresh_generation[guild.id] = stats_refresh_generation.get(guild.id, 0) + 1
    current = stats_refresh_tasks.get(guild.id)
    if current and not current.done():
        return

    async def refresh_later() -> None:
        try:
            while True:
                next_refresh = (
                    stats_last_refreshed.get(guild.id, 0)
                    + SERVER_STATS_MIN_INTERVAL_SECONDS
                )
                await asyncio.sleep(max(3, next_refresh - time.time()))
                generation = stats_refresh_generation.get(guild.id, 0)
                await refresh_server_stats(guild)
                stats_last_refreshed[guild.id] = time.time()
                if stats_refresh_generation.get(guild.id, 0) == generation:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not refresh server statistics for guild %s", guild.id)
        finally:
            if stats_refresh_tasks.get(guild.id) is asyncio.current_task():
                stats_refresh_tasks.pop(guild.id, None)

    stats_refresh_tasks[guild.id] = asyncio.create_task(refresh_later())


class MusicControlsView(discord.ui.View):
    def __init__(self, repeating: bool = False) -> None:
        super().__init__(timeout=None)
        repeat_button = next(
            item for item in self.children if item.custom_id == "community:music:repeat"
        )
        repeat_button.label = "반복: 켬" if repeating else "반복: 끔"
        repeat_button.style = (
            discord.ButtonStyle.success if repeating else discord.ButtonStyle.secondary
        )

    async def get_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return None
        voice = interaction.guild.voice_client
        if (
            voice is None
            or interaction.user.voice is None
            or interaction.user.voice.channel != voice.channel
        ):
            await interaction.followup.send(
                "봇과 같은 음성 채널에 입장한 뒤 사용해 주세요.", ephemeral=True
            )
            return None
        return voice

    @discord.ui.button(
        label="일시정지 / 재개",
        emoji="⏯️",
        style=discord.ButtonStyle.secondary,
        custom_id="community:music:pause",
    )
    async def pause(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice = await self.get_voice(interaction)
        if voice is None:
            return
        if voice.is_playing():
            voice.pause()
            message = "재생을 일시정지했습니다."
        elif voice.is_paused():
            voice.resume()
            message = "재생을 다시 시작했습니다."
        else:
            message = "현재 재생 중인 곡이 없습니다."
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(
        label="건너뛰기",
        emoji="⏭️",
        style=discord.ButtonStyle.primary,
        custom_id="community:music:skip",
    )
    async def skip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice = await self.get_voice(interaction)
        if voice is None:
            return
        if voice.is_playing() or voice.is_paused():
            if interaction.guild:
                music.mark_track_skipped(interaction.guild.id)
            voice.stop()
            await interaction.followup.send("현재 곡을 건너뜁니다.", ephemeral=True)
        else:
            await interaction.followup.send(
                "현재 재생 중인 곡이 없습니다.", ephemeral=True
            )

    async def adjust_volume(
        self, interaction: discord.Interaction, change: int
    ) -> None:
        voice = await self.get_voice(interaction)
        if voice is None or not interaction.guild:
            return
        current_volume = round(music.get_volume(interaction.guild.id) * 100)
        volume = min(100, max(0, current_volume + change))
        music.set_volume(interaction.guild, volume)
        await interaction.followup.send(
            f"음악 볼륨을 **{volume}%**로 설정했습니다.", ephemeral=True
        )

    @discord.ui.button(
        label="볼륨 낮추기 (-10%)",
        emoji="🔉",
        style=discord.ButtonStyle.secondary,
        custom_id="community:music:volume_down",
        row=0,
    )
    async def volume_down(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.adjust_volume(interaction, -10)

    @discord.ui.button(
        label="볼륨 높이기 (+10%)",
        emoji="🔊",
        style=discord.ButtonStyle.secondary,
        custom_id="community:music:volume_up",
        row=0,
    )
    async def volume_up(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.adjust_volume(interaction, 10)

    @discord.ui.button(
        label="반복: 끔",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        custom_id="community:music:repeat",
        row=1,
    )
    async def repeat(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        voice = interaction.guild.voice_client
        if (
            voice is None
            or interaction.user.voice is None
            or interaction.user.voice.channel != voice.channel
        ):
            await interaction.response.send_message(
                "봇과 같은 음성 채널에 입장한 뒤 사용해 주세요.", ephemeral=True
            )
            return
        repeating = music.toggle_repeat(interaction.guild.id)
        button.label = "반복: 켬" if repeating else "반복: 끔"
        button.style = (
            discord.ButtonStyle.success if repeating else discord.ButtonStyle.secondary
        )
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="재생 정지 (봇 유지)",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        custom_id="community:music:stop",
        row=1,
    )
    async def stop(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voice = await self.get_voice(interaction)
        if voice is None or not interaction.guild:
            return
        music.clear(interaction.guild.id)
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        await interaction.followup.send(
            "음악 재생과 대기열을 정지했습니다. 봇은 음성 채널에 계속 연결되어 있습니다.",
            ephemeral=True,
        )


@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id)
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="꼬꼬마와 함께하는 서버"
            )
        )
        for guild in bot.guilds:
            schedule_server_stats(guild)
            await restore_verification_panel(guild)
        for guild_id, channel_id in store.temporary_voice_channels():
            guild = bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if guild is None or channel is None:
                store.remove_temporary_voice_channel(guild_id, channel_id)
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                try:
                    await channel.delete(reason="Recovering empty temporary room after restart")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.warning("Missing permission to remove stale temporary room %s", channel_id)
                except discord.HTTPException:
                    logger.exception("Could not remove stale temporary room %s", channel_id)
                else:
                    store.remove_temporary_voice_channel(guild_id, channel_id)


async def restore_verification_panel(guild: discord.Guild) -> None:
    settings = store.settings(guild.id)
    channel_id = settings.get("verify_panel_channel_id")
    message_id = settings.get("verify_panel_message_id")
    role_id = settings.get("verify_role_id")
    if not channel_id or not message_id or not role_id:
        return
    channel = guild.get_channel(channel_id)
    role = guild.get_role(role_id)
    if not isinstance(channel, discord.TextChannel) or role is None:
        logger.warning("Cannot restore verification panel in guild %s: channel or role is missing.", guild.id)
        return
    try:
        await channel.fetch_message(message_id)
        return
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning("Cannot inspect verification panel in channel %s.", channel.id)
        return
    except discord.HTTPException:
        logger.exception("Could not inspect verification panel in channel %s.", channel.id)
        return

    embed = discord.Embed(
        title="서버 인증",
        description="아래 버튼을 눌러 서버 이용을 위한 인증을 완료하세요.",
        color=discord.Color.blurple(),
    )
    try:
        panel_message = await channel.send(embed=embed, view=VerifyView())
    except discord.Forbidden:
        logger.warning("Cannot repost verification panel in channel %s.", channel.id)
        return
    except discord.HTTPException:
        logger.exception("Could not repost verification panel in channel %s.", channel.id)
        return
    store.set_settings(
        guild.id,
        verify_panel_channel_id=channel.id,
        verify_panel_message_id=panel_message.id,
    )


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member) -> None:
    schedule_server_stats(after.guild)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    now = int(time.time())
    content = member_notice_text(member, joined=True, timestamp=now)
    embed = discord.Embed(
        title="새로운 멤버가 도착했어요",
        description=content,
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    await send_member_notice(member.guild, content)
    schedule_server_stats(member.guild)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    content = member_notice_text(
        member, joined=False, timestamp=int(time.time())
    )
    embed = discord.Embed(
        title="멤버 퇴장",
        description=content,
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    await send_member_notice(member.guild, content)
    schedule_server_stats(member.guild)


@bot.event
async def on_member_update(
    before: discord.Member, after: discord.Member
) -> None:
    if before.nick != after.nick:
        embed = discord.Embed(
            title="멤버 닉네임 변경",
            description=(
                f"{after.mention} (`{after.id}`)\n"
                f"이전: {before.nick or before.name}\n"
                f"변경: {after.nick or after.name}"
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(after.guild, embed)
    if before.roles != after.roles:
        schedule_server_stats(after.guild)


@bot.event
async def on_guild_role_create(role: discord.Role) -> None:
    await send_log(
        role.guild,
        discord.Embed(
            title="역할 생성",
            description=f"{discord.utils.escape_markdown(role.name)} (`{role.id}`)",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        ),
    )


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    await send_log(
        role.guild,
        discord.Embed(
            title="역할 삭제",
            description=f"{discord.utils.escape_markdown(role.name)} (`{role.id}`)",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        ),
    )


spam_messages: collections.defaultdict[
    tuple[int, int], collections.deque[tuple[float, discord.Message]]
] = collections.defaultdict(collections.deque)
INVITE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+",
    re.IGNORECASE,
)


async def moderate_message(message: discord.Message) -> bool:
    if not message.guild or not isinstance(message.author, discord.Member):
        return False
    member = message.author
    if member.bot or member.guild_permissions.manage_messages:
        return False
    normalized = message.content.casefold()
    banned_word = next(
        (
            word
            for word in (*store.banned_words(message.guild.id), *BAD_WORDS)
            if word.casefold() in normalized
        ),
        None,
    )
    violation = banned_word or (INVITE_PATTERN.search(message.content) and "서버 초대 링크")
    if violation:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning("Missing permission to delete filtered message in %s", message.channel.id)
        except discord.HTTPException:
            logger.exception("Could not delete filtered message %s", message.id)
            return True
        warning_count = store.add_warning(message.guild.id, member.id)
        try:
            warning = await message.channel.send(
                f"⚠️ {member.mention} 금지된 단어 또는 초대 링크가 감지되어 메시지를 삭제했습니다. "
                f"(경고 {warning_count}회)",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            asyncio.create_task(delete_warning_later(warning))
        except discord.HTTPException:
            logger.exception("Could not send moderation notice in channel %s", message.channel.id)
        await send_log(
            message.guild,
            discord.Embed(
                title="보안 필터 위반",
                description=(
                    f"사용자: {member.mention} (`{member.id}`)\n"
                    f"채널: {message.channel.mention}\n"
                    f"감지: {discord.utils.escape_mentions(clipped(message.content, 700))}\n"
                    f"경고 횟수: {warning_count}"
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            ),
        )
        return True

    now = time.monotonic()
    key = (message.guild.id, member.id)
    recent = spam_messages[key]
    while recent and now - recent[0][0] > SPAM_WINDOW_SECONDS:
        recent.popleft()
    recent.append((now, message))
    if len(recent) < SPAM_MESSAGE_LIMIT:
        return False

    to_delete = [entry[1] for entry in recent]
    recent.clear()
    try:
        await message.channel.delete_messages(to_delete, reason="Anti-spam: message burst")
    except discord.Forbidden:
        logger.warning("Missing permission to bulk-delete spam messages in %s", message.channel.id)
    except discord.HTTPException:
        logger.exception("Could not remove spam messages in %s", message.channel.id)
    try:
        await member.timeout(
            datetime.timedelta(seconds=SPAM_TIMEOUT_SECONDS),
            reason="Anti-spam: 5 messages within 3 seconds",
        )
    except discord.Forbidden:
        logger.warning("Could not timeout spammer %s", member.id)
    except discord.HTTPException:
        logger.exception("Could not apply anti-spam timeout to %s", member.id)
    await send_log(
        message.guild,
        discord.Embed(
            title="자동 도배 제재",
            description=f"{member.mention} (`{member.id}`) — {SPAM_TIMEOUT_SECONDS}초 타임아웃",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        ),
    )
    return True


async def delete_warning_later(message: discord.Message) -> None:
    await asyncio.sleep(5)
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        logger.exception("Could not remove temporary moderation warning.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
        return
    if await moderate_message(message):
        return
    if message.content.strip():
        await award_member_xp(
            message.author,
            random.randint(15, 25),
            channel=message.channel,
        )
    member_voice = message.author.voice
    reader_channel_id = tts.auto_reading_channel(
        message.guild.id, message.author.id
    )
    configured_channel_id = store.auto_tts_voice(
        message.guild.id, message.channel.id
    )
    reader_is_here = (
        reader_channel_id is not None
        and member_voice is not None
        and member_voice.channel.id == reader_channel_id
    )
    configured_channel_is_here = (
        configured_channel_id is not None
        and member_voice is not None
        and member_voice.channel.id == configured_channel_id
    )
    if not reader_is_here and not configured_channel_is_here:
        if reader_channel_id is not None and (
            member_voice is None or member_voice.channel.id != reader_channel_id
        ):
            tts.stop_user(message.guild, message.author.id)
        return
    spoken = clean_tts_text(message.content, message.guild.id)
    if not spoken:
        return
    target_voice_channel_id = (
        reader_channel_id if reader_is_here else configured_channel_id
    )
    if target_voice_channel_id is None:
        return
    async with tts.connection_locks[message.guild.id]:
        member_voice = message.author.voice
        if member_voice is None or member_voice.channel.id != target_voice_channel_id:
            return
        voice_client = message.guild.voice_client
        if voice_client and voice_client.channel != member_voice.channel:
            logger.warning(
                "Skipping auto TTS for guild %s because the bot is in another voice channel.",
                message.guild.id,
            )
            return
        if voice_client is None:
            try:
                await member_voice.channel.connect()
            except discord.Forbidden:
                logger.warning("Missing voice permissions for automatic TTS in guild %s", message.guild.id)
                return
            except discord.HTTPException:
                logger.exception("Could not connect for automatic TTS in guild %s", message.guild.id)
                return
        tts.enqueue(
            message.guild.id,
            Speech(
                user_id=message.author.id,
                channel_id=member_voice.channel.id,
                text=spoken,
                voice=store.member_voice(message.author.id),
            ),
        )
        tts.start(message.guild)


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return
    embed = discord.Embed(
        title="메시지 삭제",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="작성자",
        value=f"{message.author.mention} (`{message.author.id}`)",
        inline=True,
    )
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="내용", value=clipped(message.content), inline=False)
    if message.attachments:
        embed.add_field(
            name="첨부파일",
            value=clipped(
                "\n".join(
                    f"{item.filename}: {item.url}" for item in message.attachments
                )
            ),
            inline=False,
        )
    await send_log(message.guild, embed)


@bot.event
async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
    if not messages or not messages[0].guild:
        return
    await send_bulk_delete_log(
        messages[0].guild,
        messages[0].channel,
        [message for message in messages if message.guild == messages[0].guild],
    )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if (
        not before.guild
        or before.author.bot
        or before.content == after.content
    ):
        return
    embed = discord.Embed(
        title="메시지 수정",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="작성자", value=before.author.mention, inline=True)
    embed.add_field(name="채널", value=before.channel.mention, inline=True)
    embed.add_field(name="수정 전", value=clipped(before.content), inline=False)
    embed.add_field(name="수정 후", value=clipped(after.content), inline=False)
    embed.add_field(name="바로가기", value=f"[메시지 보기]({after.jump_url})", inline=False)
    await send_log(before.guild, embed)


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    if before.channel != after.channel:
        if before.channel is None and after.channel is not None:
            action = f"{member.mention} 음성 채널에 입장: {after.channel.mention}"
        elif after.channel is None:
            action = f"{member.mention} 음성 채널에서 퇴장: {before.channel.mention}"
        elif before.channel is not None:
            action = (
                f"{member.mention} 음성 채널 이동\n"
                f"이전: {before.channel.mention} → 이후: {after.channel.mention}"
            )
        else:
            action = ""
        if action:
            await send_log(
                member.guild,
                discord.Embed(
                    title="음성 채널 활동",
                    description=action,
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                ),
            )
        if before.channel and after.channel != before.channel:
            removed_count, stopped = tts.stop_user(member.guild, member.id)
            if removed_count or stopped:
                logger.info(
                    "Stopped TTS after member %s left voice (%s queued removed, playing=%s).",
                    removed_count,
                    member.id,
                    stopped,
                )

    if before.channel and before.channel != after.channel:
        if store.is_temporary_voice_channel(member.guild.id, before.channel.id):
            if not before.channel.members:
                try:
                    await before.channel.delete(reason="Temporary voice room is empty")
                    store.remove_temporary_voice_channel(
                        member.guild.id, before.channel.id
                    )
                except discord.NotFound:
                    store.remove_temporary_voice_channel(
                        member.guild.id, before.channel.id
                    )
                except discord.Forbidden:
                    logger.warning("Missing permission to delete temporary voice room.")
                except discord.HTTPException:
                    logger.exception("Could not delete empty temporary voice room.")

    voice = member.guild.voice_client
    if voice and before.channel == voice.channel and after.channel != voice.channel:
        if not any(not current.bot for current in voice.channel.members):
            music.schedule_idle_disconnect(
                member.guild, delay=EMPTY_VOICE_DISCONNECT_SECONDS
            )
    if voice and after.channel == voice.channel and not member.bot:
        music.cancel_idle_disconnect(member.guild.id)

    if member.bot or not after.channel or before.channel == after.channel:
        return
    lobby_id = store.settings(member.guild.id).get("voice_lobby_id")
    if after.channel.id != lobby_id:
        return
    channel: discord.VoiceChannel | None = None
    try:
        channel = await member.guild.create_voice_channel(
            name=f"{member.display_name[:80]}님의 방",
            category=after.channel.category,
            user_limit=10,
            reason=f"Temporary voice room for {member}",
        )
        store.add_temporary_voice_channel(member.guild.id, channel.id, member.id)
        await member.move_to(channel)
    except discord.Forbidden:
        logger.warning("Missing permissions to create or move temporary voice rooms.")
        if channel:
            store.remove_temporary_voice_channel(member.guild.id, channel.id)
            try:
                await channel.delete(reason="Could not move member into temporary room")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.exception("Could not remove unused temporary voice room.")
    except discord.HTTPException:
        logger.exception("Could not create a temporary voice room.")
        if channel:
            store.remove_temporary_voice_channel(member.guild.id, channel.id)
        return


@bot.tree.command(name="로그채널지정", description="입퇴장 및 메시지 로그를 받을 채널을 지정합니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_log(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    if guild.me is None or not can_send_embeds(channel, guild.me):
        await interaction.response.send_message(
            "봇이 이 채널에 로그를 쓸 수 없습니다. 채널 권한에서 봇에 "
            "`채널 보기`, `메시지 보내기`, `링크 첨부`를 허용한 뒤 다시 설정해 주세요.",
            ephemeral=False,
        )
        return
    store.set_settings(guild.id, log_channel_id=channel.id)
    await interaction.response.send_message(
        f"로그 채널을 {channel.mention}(으)로 설정했습니다.", ephemeral=False
    )


@bot.tree.command(
    name="환영퇴장채널지정",
    description="멤버 환영 및 퇴장 안내를 보낼 채널을 설정합니다.",
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_member_notice_channel(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
) -> None:
    if interaction.guild is None:
        return
    store.set_settings(
        interaction.guild.id,
        welcome_channel_id=channel.id if channel else None,
    )
    if channel:
        message = f"환영/퇴장 안내 채널을 {channel.mention}(으)로 지정했습니다."
    else:
        message = "환영/퇴장 안내를 로그 채널로 보내도록 설정을 초기화했습니다."
    await interaction.response.send_message(message, ephemeral=False)


async def restrict_unverified_category_access(
    guild: discord.Guild,
    verified_role: discord.Role,
    verification_channel: discord.TextChannel,
) -> None:
    bot_member = guild.me
    if bot_member is None:
        raise discord.ClientException("Bot member is not available in this server.")

    for category in guild.categories:
        category_overwrites = category.overwrites
        everyone_overwrite = category_overwrites.get(
            guild.default_role, discord.PermissionOverwrite()
        )
        everyone_overwrite.view_channel = False
        category_overwrites[guild.default_role] = everyone_overwrite

        verified_overwrite = category_overwrites.get(
            verified_role, discord.PermissionOverwrite()
        )
        verified_overwrite.view_channel = True
        category_overwrites[verified_role] = verified_overwrite

        bot_overwrite = category_overwrites.get(
            bot_member, discord.PermissionOverwrite()
        )
        bot_overwrite.view_channel = True
        bot_overwrite.send_messages = True
        bot_overwrite.read_message_history = True
        bot_overwrite.embed_links = True
        category_overwrites[bot_member] = bot_overwrite

        await category.edit(
            overwrites=category_overwrites,
            reason="Hide server categories until CAPTCHA verification",
        )

        for channel in category.channels:
            channel_overwrites = channel.overwrites
            everyone_channel_overwrite = channel_overwrites.get(
                guild.default_role, discord.PermissionOverwrite()
            )
            is_verification_channel = channel.id == verification_channel.id
            everyone_channel_overwrite.view_channel = is_verification_channel
            if is_verification_channel:
                everyone_channel_overwrite.send_messages = True
                everyone_channel_overwrite.read_message_history = True
            channel_overwrites[guild.default_role] = everyone_channel_overwrite

            verified_channel_overwrite = channel_overwrites.get(
                verified_role, discord.PermissionOverwrite()
            )
            verified_channel_overwrite.view_channel = True
            channel_overwrites[verified_role] = verified_channel_overwrite

            bot_channel_overwrite = channel_overwrites.get(
                bot_member, discord.PermissionOverwrite()
            )
            bot_channel_overwrite.view_channel = True
            bot_channel_overwrite.send_messages = True
            bot_channel_overwrite.read_message_history = True
            bot_channel_overwrite.embed_links = True
            channel_overwrites[bot_member] = bot_channel_overwrite

            await channel.edit(
                overwrites=channel_overwrites,
                reason="Apply CAPTCHA verification visibility",
            )

    if verification_channel.category is None:
        channel_overwrites = verification_channel.overwrites
        everyone_overwrite = channel_overwrites.get(
            guild.default_role, discord.PermissionOverwrite()
        )
        everyone_overwrite.view_channel = True
        everyone_overwrite.send_messages = True
        everyone_overwrite.read_message_history = True
        channel_overwrites[guild.default_role] = everyone_overwrite

        bot_overwrite = channel_overwrites.get(
            bot_member, discord.PermissionOverwrite()
        )
        bot_overwrite.view_channel = True
        bot_overwrite.send_messages = True
        bot_overwrite.read_message_history = True
        bot_overwrite.embed_links = True
        channel_overwrites[bot_member] = bot_overwrite
        await verification_channel.edit(
            overwrites=channel_overwrites,
            reason="Keep CAPTCHA verification channel visible",
        )


@bot.tree.command(name="보안인증서생성", description="인증 전에는 카테고리를 숨기는 인증 패널을 게시합니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_verification(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
) -> None:
    if (
        not interaction.guild
        or role.is_default()
        or role.managed
        or interaction.guild.me is None
        or role >= interaction.guild.me.top_role
    ):
        await interaction.response.send_message(
            "봇의 최상위 역할보다 낮은 일반 역할만 인증 역할로 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    if not interaction.guild.me.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "인증 전 카테고리 숨김을 설정하려면 봇에 `채널 관리` 권한이 필요합니다.",
            ephemeral=False,
        )
        return
    if not can_send_embeds(channel, interaction.guild.me):
        await interaction.response.send_message(
            "선택한 인증 채널에 봇의 `채널 보기`, `메시지 보내기`, `링크 첨부` 권한이 필요합니다.",
            ephemeral=False,
        )
        return
    store.set_settings(interaction.guild.id, verify_role_id=role.id)
    embed = discord.Embed(
        title="서버 인증",
        description="아래 버튼을 눌러 서버 이용을 위한 인증을 완료하세요.",
        color=discord.Color.blurple(),
    )
    try:
        panel_message = await channel.send(embed=embed, view=VerifyView())
    except discord.Forbidden:
        await interaction.response.send_message(
            "선택한 채널에 인증 패널을 게시할 권한이 없습니다.", ephemeral=False
        )
        return
    except discord.HTTPException:
        logger.exception("Could not publish verification panel in channel %s", channel.id)
        await interaction.response.send_message(
            "인증 패널 게시 중 오류가 발생했습니다.", ephemeral=False
        )
        return
    store.set_settings(
        interaction.guild.id,
        verify_panel_channel_id=channel.id,
        verify_panel_message_id=panel_message.id,
    )
    try:
        await restrict_unverified_category_access(
            interaction.guild, role, channel
        )
    except discord.Forbidden:
        logger.exception(
            "Could not apply verification category permissions in guild %s",
            interaction.guild.id,
        )
        await interaction.response.send_message(
            "인증 패널은 게시했지만 일부 카테고리 권한을 변경하지 못했습니다. "
            "봇에 채널 관리 권한을 주고 명령을 다시 실행해 주세요. "
            "이미 변경된 카테고리는 다시 실행하면 이어서 설정됩니다.",
            ephemeral=False,
        )
        return
    except discord.HTTPException:
        logger.exception(
            "Could not apply verification category permissions in guild %s",
            interaction.guild.id,
        )
        await interaction.response.send_message(
            "인증 패널은 게시했지만 카테고리 권한 적용 중 Discord 오류가 발생했습니다. "
            "잠시 후 명령을 다시 실행해 주세요.",
            ephemeral=False,
        )
        return
    await interaction.response.send_message(
        f"{channel.mention}에 인증 패널을 게시하고 {role.mention} 역할을 설정했습니다. "
        "인증 전에는 이 채널만 보이며, 인증 후 모든 기존 카테고리를 볼 수 있습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="역할지급패널생성", description="선택 가능한 셀프 역할 패널을 게시합니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def setup_role_panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role1: discord.Role,
    role2: discord.Role | None = None,
    role3: discord.Role | None = None,
    role4: discord.Role | None = None,
    role5: discord.Role | None = None,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    roles = [role for role in (role1, role2, role3, role4, role5) if role is not None]
    if len({role.id for role in roles}) != len(roles):
        await interaction.response.send_message("중복된 역할이 있습니다.", ephemeral=False)
        return
    if any(
        role.is_default()
        or role.managed
        or interaction.guild.me is None
        or role >= interaction.guild.me.top_role
        for role in roles
    ):
        await interaction.response.send_message(
            "봇의 최상위 역할보다 낮은 일반 역할만 선택해 주세요.",
            ephemeral=False,
        )
        return
    role_ids = [role.id for role in roles]
    store.set_role_panel(interaction.guild.id, role_ids)
    view = RolePanelView(interaction.guild.id, role_ids)
    bot.add_view(view)
    embed = discord.Embed(
        title="역할 선택",
        description="메뉴에서 역할을 선택하면 추가되고, 다시 선택하면 해제됩니다.",
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        f"{channel.mention}에 역할 패널을 게시했습니다.", ephemeral=False
    )


@bot.tree.command(name="보이스입장채널지정", description="입장 시 임시 음성방을 만드는 로비를 설정합니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.bot_has_permissions(manage_channels=True)
@app_commands.guild_only()
@app_commands.describe(카테고리="음성채널 생성 로비와 임시 게임방을 둘 카테고리")
async def setup_voice_lobby(
    interaction: discord.Interaction, 카테고리: discord.CategoryChannel
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    settings = store.settings(guild.id)
    configured_channel_id = settings.get("voice_lobby_id")
    lobby = guild.get_channel(configured_channel_id) if configured_channel_id else None
    await interaction.response.defer(ephemeral=False, thinking=True)
    try:
        if isinstance(lobby, discord.VoiceChannel):
            await lobby.edit(
                name="음성채널 생성",
                category=카테고리,
                reason="Configure temporary voice-room lobby",
            )
        else:
            lobby = await guild.create_voice_channel(
                name="음성채널 생성",
                category=카테고리,
                reason="Create temporary voice-room lobby",
            )
    except discord.Forbidden:
        await interaction.followup.send(
            "봇에 채널 관리 권한이 없어 음성방 생성 로비를 설정하지 못했습니다.",
            ephemeral=False,
        )
        return
    except discord.HTTPException:
        logger.exception(
            "Could not create or configure voice-room lobby in guild %s", guild.id
        )
        await interaction.followup.send(
            "음성방 생성 로비 설정 중 오류가 발생했습니다.", ephemeral=False
        )
        return
    store.set_settings(guild.id, voice_lobby_id=lobby.id)
    await interaction.followup.send(
        f"{lobby.mention} 로비에 입장하면 개인 게임방이 자동으로 생성됩니다.",
        ephemeral=False,
    )


@bot.tree.command(name="통계채널설정", description="전체/온라인/인증 인원 통계 음성 채널을 생성합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(1, 30.0, key=lambda interaction: interaction.guild_id)
async def setup_stats_channels(
    interaction: discord.Interaction, category_name: app_commands.Range[str, 1, 90] = "📊 서버 현황"
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    current = store.settings(guild.id)
    category = guild.get_channel(current.get("stats_category_id")) if current.get("stats_category_id") else None
    permissions = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, connect=False, send_messages=False
        )
    }
    if guild.me:
        permissions[guild.me] = discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True
        )
    try:
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(
                category_name,
                overwrites=permissions,
                position=0,
                reason="Create server statistics channels",
            )
        existing = [
            guild.get_channel(current.get(key))
            if current.get(key)
            else None
            for key in (
                "stats_total_channel_id",
                "stats_online_channel_id",
                "stats_verified_channel_id",
            )
        ]
        names = [
            f"📊 전체 회원수 : {guild.member_count or len(guild.members)}명",
            f"🟢 온라인 : {sum(m.status != discord.Status.offline for m in guild.members)}명",
            "🌐 인증 완료 : 0명",
        ]
        channel_ids = []
        for index, (channel, name) in enumerate(zip(existing, names)):
            if not isinstance(channel, discord.VoiceChannel):
                channel = await guild.create_voice_channel(
                    name, category=category, overwrites=permissions
                )
            elif channel.name != name:
                await channel.edit(name=name)
            channel_ids.append(channel.id)
        store.set_settings(
            guild.id,
            stats_category_id=category.id,
            stats_total_channel_id=channel_ids[0],
            stats_online_channel_id=channel_ids[1],
            stats_verified_channel_id=channel_ids[2],
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "카테고리와 음성 채널을 만들 권한이 없습니다.", ephemeral=False
        )
        return
    except discord.HTTPException:
        logger.exception("Could not set up server stats channels for guild %s", guild.id)
        await interaction.followup.send(
            "통계 채널 설정에 실패했습니다. 봇의 채널 관리 권한을 확인해 주세요.",
            ephemeral=False,
        )
        return
    await refresh_server_stats(guild)
    await interaction.followup.send(
        f"{category.mention}에 서버 통계 음성 채널 3개를 설정했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="출석보상역할설정", description="7일 연속 출석 보상 역할을 설정합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_attendance_reward(
    interaction: discord.Interaction, 보상역할: discord.Role | None = None
) -> None:
    guild = interaction.guild
    if guild is None:
        return
    if 보상역할 and (
        보상역할.is_default()
        or 보상역할.managed
        or guild.me is None
        or 보상역할 >= guild.me.top_role
    ):
        await interaction.response.send_message(
            "봇이 지급할 수 있는 일반 역할을 선택해 주세요.", ephemeral=False
        )
        return
    store.set_attendance_reward(
        guild.id, 보상역할.id if 보상역할 else None
    )
    await interaction.response.send_message(
        f"7일 연속 출석 역할을 {보상역할.mention}으로 설정했습니다."
        if 보상역할
        else "7일 연속 출석 역할 보상을 해제했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="레벨", description="경험치와 서버 순위가 담긴 프로필 카드를 확인합니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(3, 10.0, key=lambda interaction: interaction.user.id)
async def show_level(
    interaction: discord.Interaction, 대상: discord.Member | None = None
) -> None:
    member = 대상 or interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=False)
        return
    stats = await profile_card_for_member(member)
    if not stats:
        await interaction.response.send_message(
            "아직 경험치 기록이 없습니다. 채팅이나 음성 활동으로 XP를 쌓아보세요.",
            ephemeral=False,
        )
        return
    card, level, xp, rank, count = stats
    await interaction.response.send_message(
        content=f"**{discord.utils.escape_markdown(member.display_name)}** · Lv.{level} · {xp}/{level * 100} XP · {rank}/{count}위",
        file=discord.File(io.BytesIO(card), filename="kokkoma-profile.png"),
    )


@bot.tree.command(name="랭킹", description="서버 경험치 TOP 10 순위를 확인합니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(3, 10.0, key=lambda interaction: interaction.user.id)
async def show_leaderboard(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    leaders = store.leaderboard(guild.id, 10)
    if not leaders:
        await interaction.response.send_message(
            "아직 서버에 경험치 기록이 없습니다.", ephemeral=False
        )
        return
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for index, (user_id, level, xp) in enumerate(leaders):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"알 수 없는 멤버 ({user_id})"
        marker = medals[index] if index < len(medals) else f"`{index + 1:02}`"
        total = (level - 1) * level * 50 + xp
        lines.append(
            f"{marker} **{discord.utils.escape_markdown(name)}** · Lv.{level} · {total:,} XP"
        )
    embed = discord.Embed(
        title=f"🏆 {guild.name} 활동 랭킹 TOP 10",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="출석체크", description="하루 한 번 출석하고 70~150 XP를 받습니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def daily_checkin(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return
    previous = store.member_xp(guild.id, member.id)
    before_level = previous[0] if previous else 1
    today = datetime.datetime.now(KOREA_TIMEZONE).date().isoformat()
    result = store.checkin(guild.id, member.id, today)
    if result is None:
        await interaction.response.send_message(
            "오늘은 이미 출석했습니다. 내일 다시 도전해 주세요!", ephemeral=False
        )
        return
    bonus, streak = result
    xp_state = store.member_xp(guild.id, member.id)
    level, xp = xp_state if xp_state else (1, 0)
    if level > before_level:
        await apply_level_rewards(member, list(range(before_level + 1, level + 1)))
    settings = store.settings(guild.id)
    attendance_role_id = settings.get("attendance_reward_role_id")
    attendance_role = guild.get_role(attendance_role_id) if attendance_role_id else None
    reward_text = ""
    if streak >= 7 and attendance_role and attendance_role not in member.roles:
        try:
            await member.add_roles(attendance_role, reason="Seven-day attendance streak")
            reward_text = f"\n🏅 7일 연속 출석 보상: {attendance_role.mention}"
        except discord.Forbidden:
            logger.warning("Could not grant attendance role %s", attendance_role.id)
    embed = discord.Embed(
        title="📆 출석 완료!",
        description=f"{member.mention}님, **+{bonus} XP**를 받았습니다.\n연속 출석: **{streak}일**{reward_text}",
        color=discord.Color.green(),
    )
    embed.add_field(name="현재 레벨", value=f"Lv.{level} · {xp}/{level * 100} XP")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="고급공지작성", description="선택한 채널에 디자인 임베드 공지를 게시합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def create_announcement(
    interaction: discord.Interaction,
    채널: discord.TextChannel,
    제목: app_commands.Range[str, 1, 200],
    내용: app_commands.Range[str, 1, 4000],
    색상: str = "blue",
    이미지주소: str | None = None,
) -> None:
    colors = {
        "red": discord.Color.red(),
        "blue": discord.Color.blurple(),
        "green": discord.Color.green(),
        "gold": discord.Color.gold(),
    }
    color = colors.get(색상.casefold())
    if color is None:
        await interaction.response.send_message(
            "색상은 red, blue, green, gold 중에서 선택해 주세요.",
            ephemeral=False,
        )
        return
    if 이미지주소 and not re.match(r"^https://", 이미지주소, re.IGNORECASE):
        await interaction.response.send_message(
            "이미지 주소는 HTTPS 링크만 사용할 수 있습니다.", ephemeral=False
        )
        return
    embed = discord.Embed(
        title=f"📢 {제목}",
        description=내용.replace("\\n", "\n"),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"꼬꼬마 공식 안내 · {interaction.user.display_name}")
    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if 이미지주소:
        embed.set_image(url=이미지주소)
    try:
        await 채널.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        await interaction.response.send_message(
            "선택한 채널에 공지를 게시할 권한이 없습니다.", ephemeral=False
        )
        return
    await interaction.response.send_message(
        f"{채널.mention}에 공지를 게시했습니다.", ephemeral=False
    )


@bot.tree.command(name="tts", description="TTS를 켜고 이후 내가 채팅하는 내용을 계속 읽게 합니다.")
@app_commands.describe(할말="선택 사항: 켜는 동시에 먼저 읽을 문장")
@app_commands.guild_only()
@app_commands.checks.cooldown(3, 10.0, key=lambda interaction: interaction.user.id)
async def speak_tts(
    interaction: discord.Interaction,
    할말: app_commands.Range[str, 1, MAX_TTS_TEXT_LENGTH] | None = None,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member) or member.voice is None:
        await interaction.response.send_message(
            "먼저 음성 채널에 입장해 주세요.", ephemeral=False
        )
        return
    text = clean_tts_text(할말, guild.id) if 할말 else None
    if 할말 and not text:
        await interaction.response.send_message(
            "읽을 수 있는 문장이 없습니다.", ephemeral=False
        )
        return
    voice_channel = member.voice.channel
    await interaction.response.defer(ephemeral=False, thinking=True)
    async with tts.connection_locks[guild.id]:
        voice_state = member.voice
        if voice_state is None or voice_state.channel != voice_channel:
            await interaction.followup.send(
                "요청 처리 중 음성 채널에서 나갔습니다. 다시 시도해 주세요.",
                ephemeral=False,
            )
            return
        voice_client = guild.voice_client
        if voice_client and voice_client.channel != voice_channel:
            await interaction.followup.send(
                "봇이 다른 음성 채널에 있어 이 요청을 실행할 수 없습니다. "
                "봇이 있는 채널로 이동하거나 관리자에게 확인해 주세요.",
                ephemeral=False,
            )
            return
        try:
            if voice_client is None:
                await voice_channel.connect()
        except discord.Forbidden:
            await interaction.followup.send(
                "음성 채널에 연결할 권한이 없습니다.", ephemeral=False
            )
            return
        except discord.HTTPException:
            logger.exception("Could not connect to TTS voice channel.")
            await interaction.followup.send(
                "음성 채널 연결에 실패했습니다.", ephemeral=False
            )
            return
        tts.enable_auto_reading(
            guild.id, member.id, voice_channel.id, interaction.channel_id
        )
        queue_position = None
        if text:
            queue_position = tts.enqueue(
                guild.id,
                Speech(
                    member.id,
                    voice_channel.id,
                    text,
                    store.member_voice(member.id),
                ),
            )
            tts.start(guild)
    response = (
        "✅ 자동 TTS를 켰습니다. 이 텍스트 채널에서 내가 작성하는 메시지를 계속 읽습니다. "
        "`/tts중지`를 실행하거나 음성 채널에서 나가면 꺼집니다."
    )
    if queue_position is not None:
        response += f" 시작 문장을 대기열 {queue_position}번에 추가했습니다."
    await interaction.followup.send(response, ephemeral=False)


@bot.tree.command(name="tts중지", description="진행 중인 TTS와 대기열을 중지합니다.")
@app_commands.guild_only()
async def stop_tts(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "서버 안에서만 사용할 수 있습니다.", ephemeral=False
        )
        return
    queued, playing = tts.stop_user(guild, member.id)
    await interaction.response.send_message(
        f"자동 읽기를 끄고 내 TTS를 정리했습니다. "
        f"재생 중 {1 if playing else 0}개, 대기열 {queued}개를 종료했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="tts퇴장", description="TTS 대기열을 비우고 음악 재생이 없으면 음성 채널에서 퇴장합니다.")
@app_commands.guild_only()
async def leave_tts(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    if (
        guild is None
        or not isinstance(member, discord.Member)
        or member.voice is None
        or guild.voice_client is None
        or member.voice.channel != guild.voice_client.channel
    ):
        await interaction.response.send_message(
            "봇과 같은 음성 채널에 입장해 주세요.", ephemeral=False
        )
        return
    tts.stop(guild)
    voice_client = guild.voice_client
    if voice_client and (music.tasks.get(guild.id) or voice_client.is_playing()):
        await interaction.response.send_message(
            "음악이 재생 중이어서 봇은 퇴장하지 않았습니다. TTS 대기열은 비웠습니다.",
            ephemeral=False,
        )
        return
    if voice_client:
        await voice_client.disconnect()
    await interaction.response.send_message("TTS 대기열을 비우고 음성 채널에서 퇴장했습니다.")


def owned_voice_room(
    interaction: discord.Interaction,
) -> tuple[discord.Member, discord.VoiceChannel] | None:
    guild = interaction.guild
    member = interaction.user
    if (
        guild is None
        or not isinstance(member, discord.Member)
        or member.voice is None
        or not isinstance(member.voice.channel, discord.VoiceChannel)
    ):
        return None
    channel = member.voice.channel
    if store.temporary_voice_owner(guild.id, channel.id) != member.id:
        return None
    return member, channel


@bot.tree.command(name="방이름변경", description="내 임시 음성방 이름을 변경합니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def rename_voice_room(
    interaction: discord.Interaction,
    이름: app_commands.Range[str, 1, 90],
) -> None:
    owned = owned_voice_room(interaction)
    if not owned:
        await interaction.response.send_message(
            "내가 만든 임시 음성방에 입장한 상태에서만 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    _, channel = owned
    try:
        await channel.edit(name=이름, reason=f"Room renamed by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "음성 채널을 수정할 권한이 없습니다.", ephemeral=False
        )
        return
    await interaction.response.send_message(
        f"음성방 이름을 **{discord.utils.escape_markdown(이름)}**(으)로 변경했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="방인원제한", description="내 임시 음성방의 인원 제한을 설정합니다 (0은 제한 없음).")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def set_voice_room_limit(
    interaction: discord.Interaction,
    인원: app_commands.Range[int, 0, 99],
) -> None:
    owned = owned_voice_room(interaction)
    if not owned:
        await interaction.response.send_message(
            "내가 만든 임시 음성방에 입장한 상태에서만 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    _, channel = owned
    try:
        await channel.edit(
            user_limit=인원, reason=f"Room limit changed by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "음성 채널을 수정할 권한이 없습니다.", ephemeral=False
        )
        return
    await interaction.response.send_message(
        f"인원 제한을 {'없음' if 인원 == 0 else f'{인원}명'}(으)로 설정했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="방잠금", description="내 임시 음성방의 입장을 잠그거나 해제합니다.")
@app_commands.describe(잠금="켜면 다른 멤버가 입장할 수 없습니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def set_voice_room_lock(
    interaction: discord.Interaction, 잠금: bool
) -> None:
    owned = owned_voice_room(interaction)
    if not owned or not interaction.guild:
        await interaction.response.send_message(
            "내가 만든 임시 음성방에 입장한 상태에서만 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    _, channel = owned
    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.connect = False if 잠금 else None
    try:
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Room {'locked' if 잠금 else 'unlocked'} by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "음성방 권한을 변경할 수 없습니다.", ephemeral=False
        )
        return
    await interaction.response.send_message(
        "음성방을 잠갔습니다." if 잠금 else "음성방 잠금을 해제했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="방초대", description="잠긴 내 임시 음성방에 멤버를 초대합니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(3, 10.0, key=lambda interaction: interaction.user.id)
async def invite_voice_member(
    interaction: discord.Interaction, 초대유저: discord.Member
) -> None:
    owned = owned_voice_room(interaction)
    if not owned or not interaction.guild:
        await interaction.response.send_message(
            "내가 만든 임시 음성방에 입장한 상태에서만 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    _, channel = owned
    if 초대유저.bot or 초대유저.id == interaction.user.id:
        await interaction.response.send_message(
            "초대할 서버 멤버를 선택해 주세요.", ephemeral=False
        )
        return
    try:
        await channel.set_permissions(
            초대유저,
            view_channel=True,
            connect=True,
            reason=f"Invited by room owner {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "음성방 권한을 변경할 수 없습니다.", ephemeral=False
        )
        return
    await interaction.response.send_message(
        f"{초대유저.mention}님을 음성방에 초대했습니다.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@bot.tree.command(name="방추방", description="내 임시 음성방에서 멤버를 내보내고 재입장을 차단합니다.")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 10.0, key=lambda interaction: interaction.user.id)
async def kick_voice_member(
    interaction: discord.Interaction, 대상: discord.Member
) -> None:
    owned = owned_voice_room(interaction)
    if not owned or not interaction.guild:
        await interaction.response.send_message(
            "내가 만든 임시 음성방에 입장한 상태에서만 사용할 수 있습니다.",
            ephemeral=False,
        )
        return
    _, channel = owned
    if 대상.id == interaction.user.id or 대상.bot:
        await interaction.response.send_message(
            "내보낼 수 없는 대상입니다.", ephemeral=False
        )
        return
    if 대상.voice is None or 대상.voice.channel != channel:
        await interaction.response.send_message(
            "해당 멤버가 현재 내 음성방에 없습니다.", ephemeral=False
        )
        return
    try:
        await channel.set_permissions(
            대상,
            view_channel=True,
            connect=False,
            reason=f"Removed by room owner {interaction.user}",
        )
        await 대상.move_to(None, reason=f"Removed from room by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "해당 멤버를 내보내거나 음성방 접근을 차단할 권한이 없습니다.",
            ephemeral=False,
        )
        return
    await interaction.response.send_message(
        f"{discord.utils.escape_markdown(대상.display_name)}님을 내보내고 재입장을 차단했습니다.",
        ephemeral=False,
    )


@bot.tree.command(name="공지수신", description="서버 공지 DM 수신 여부를 설정합니다.")
@app_commands.describe(수신="켜면 관리자 공지 DM을 받습니다.")
@app_commands.guild_only()
async def set_announcement_opt_in(
    interaction: discord.Interaction, 수신: bool
) -> None:
    if interaction.guild:
        store.set_dm_opt_in(interaction.guild.id, interaction.user.id, 수신)
    response = (
        "공지 DM 수신에 동의했습니다. 다시 실행해 언제든 해제할 수 있습니다."
        if 수신
        else "공지 DM 수신 동의를 해제했습니다."
    )
    await interaction.response.send_message(response, ephemeral=False)


@bot.tree.command(name="디엠공지전송", description="DM 수신 동의 멤버에게 서버 공지를 보냅니다.")
@app_commands.describe(내용="공지할 내용 (최대 1500자)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 60.0, key=lambda interaction: interaction.guild_id)
async def dm_announcement(
    interaction: discord.Interaction, 내용: app_commands.Range[str, 1, 1500]
) -> None:
    await interaction.response.send_message(
        f"이 공지를 수신 동의 멤버에게만 보냅니다 (인원 제한 없음).\n"
        f"```{내용[:900]}```\n전송 전에 내용을 확인해 주세요.",
        view=AnnouncementConfirmView(
            interaction.guild_id or 0, interaction.user.id, 내용
        ),
        ephemeral=False,
    )


@bot.tree.command(name="음악재생", description="음성 채널에서 곡을 검색하고 재생 대기열에 추가합니다.")
@app_commands.describe(검색어="YouTube URL 또는 곡/아티스트 검색어")
@app_commands.guild_only()
@app_commands.checks.cooldown(2, 5.0, key=lambda interaction: interaction.user.id)
async def play_music(
    interaction: discord.Interaction, 검색어: app_commands.Range[str, 1, 300]
) -> None:
    if (
        not interaction.guild
        or not isinstance(interaction.user, discord.Member)
        or interaction.user.voice is None
        or interaction.user.voice.channel is None
    ):
        await interaction.response.send_message(
            "먼저 음성 채널에 입장해 주세요.", ephemeral=False
        )
        return
    await interaction.response.defer(thinking=True)
    voice_channel = interaction.user.voice.channel
    voice = interaction.guild.voice_client
    try:
        if voice is None:
            voice = await voice_channel.connect()
        elif voice.channel != voice_channel:
            if voice.is_playing() or voice.is_paused():
                await interaction.followup.send(
                    "봇이 이미 다른 음성 채널에서 재생 중입니다.", ephemeral=False
                )
                return
            await voice.move_to(voice_channel)
        track = await asyncio.to_thread(extract_track, 검색어, interaction.user.id)
    except discord.Forbidden:
        await interaction.followup.send(
            "음성 채널에 연결할 권한이 없습니다.", ephemeral=False
        )
        return
    except (discord.ClientException, discord.HTTPException, yt_dlp.utils.DownloadError) as error:
        logger.warning("Could not queue music: %s", error)
        await interaction.followup.send(
            "곡을 찾거나 음성 채널에 연결하지 못했습니다. 검색어 또는 봇 권한을 확인해 주세요.",
            ephemeral=False,
        )
        return

    position = music.enqueue(interaction.guild.id, track)
    music.start(interaction.guild)
    embed = discord.Embed(
        title="🎵 대기열에 추가했습니다",
        description=f"[{discord.utils.escape_markdown(track.title)}]({track.webpage_url})",
        color=discord.Color.green(),
    )
    embed.add_field(name="요청자", value=interaction.user.mention)
    embed.add_field(name="대기 순서", value=str(position))
    await interaction.followup.send(
        embed=embed,
        view=MusicControlsView(repeating=music.is_repeating(interaction.guild.id)),
    )


@bot.tree.command(name="음악나가기", description="음악 대기열을 비우고 봇을 음성 채널에서 내보냅니다.")
@app_commands.guild_only()
async def leave_music(interaction: discord.Interaction) -> None:
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message(
            "봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=False
        )
        return
    voice = interaction.guild.voice_client
    if isinstance(interaction.user, discord.Member) and (
        interaction.user.voice is None or interaction.user.voice.channel != voice.channel
    ):
        await interaction.response.send_message(
            "봇과 같은 음성 채널에 입장해 주세요.", ephemeral=False
        )
        return
    music.clear(interaction.guild.id)
    if voice.is_playing() or voice.is_paused():
        voice.stop()
    await voice.disconnect()
    await interaction.response.send_message("음악을 종료했습니다.")


@bot.tree.command(name="음악볼륨", description="음악 재생 볼륨을 0~100%로 조절합니다.")
@app_commands.describe(볼륨="재생 볼륨 (0~100%)")
@app_commands.guild_only()
async def set_music_volume(
    interaction: discord.Interaction, 볼륨: app_commands.Range[int, 0, 100]
) -> None:
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message(
            "봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=False
        )
        return
    voice = interaction.guild.voice_client
    if isinstance(interaction.user, discord.Member) and (
        interaction.user.voice is None or interaction.user.voice.channel != voice.channel
    ):
        await interaction.response.send_message(
            "봇과 같은 음성 채널에 입장해 주세요.", ephemeral=False
        )
        return
    music.set_volume(interaction.guild, 볼륨)
    await interaction.response.send_message(
        f"음악 볼륨을 **{볼륨}%**로 설정했습니다."
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어를 사용할 권한이 없습니다."
        logger.warning("Permission denied for command by user %s", interaction.user.id)
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"잠시 기다려 주세요. {error.retry_after:.1f}초 후 다시 사용할 수 있습니다."
    else:
        original = getattr(error, "original", error)
        logger.error(
            "Slash command failed",
            exc_info=(type(original), original, original.__traceback__),
        )
        message = "명령어를 처리하지 못했습니다. 봇 권한과 설정을 확인해 주세요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=False)
    else:
        await interaction.response.send_message(message, ephemeral=False)


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN이 없습니다. .env.example을 참고해 .env 파일을 설정하세요."
        )
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
