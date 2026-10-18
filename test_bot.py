import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
from PIL import Image


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(Path(self.temp_dir.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.store.connection.close()
        self.temp_dir.cleanup()

    def test_guild_settings_survive_reopening(self) -> None:
        self.store.set_settings(10, log_channel_id=20, voice_lobby_id=30)
        self.store.connection.close()
        self.store = bot.Store(Path(self.temp_dir.name) / "test.sqlite3")

        settings = self.store.settings(10)
        self.assertEqual(settings["log_channel_id"], 20)
        self.assertEqual(settings["voice_lobby_id"], 30)
        self.assertIn("stats_total_channel_id", settings)
        self.assertIn("welcome_channel_id", settings)
        self.assertIn("verify_panel_message_id", settings)

    def test_store_creates_database_parent_directory(self) -> None:
        database_path = Path(self.temp_dir.name) / "persistent" / "bot.sqlite3"
        store = bot.Store(database_path)
        try:
            self.assertTrue(database_path.is_file())
        finally:
            store.connection.close()

    def test_role_panel_and_announcement_opt_in(self) -> None:
        self.store.set_role_panel(10, [101, 102])
        self.assertEqual(self.store.role_panels(), [(10, [101, 102])])

        self.store.set_dm_opt_in(10, 201, True)
        self.assertEqual(self.store.dm_opt_ins(10), [201])
        self.store.set_dm_opt_in(10, 201, False)
        self.assertEqual(self.store.dm_opt_ins(10), [])

    def test_clear_auto_tts_settings_removes_only_guild_mappings(self) -> None:
        self.store.set_auto_tts(10, 101, 201)
        self.store.set_auto_tts(10, 102, 202)
        self.store.set_auto_tts(20, 103, 203)

        self.store.clear_auto_tts_settings(10)

        self.assertEqual(self.store.auto_tts_settings(10), {})
        self.assertEqual(self.store.auto_tts_settings(20), {103: 203})

    def test_server_settings_and_auto_tts_are_persistent(self) -> None:
        self.store.set_settings(10, log_channel_id=30, verify_role_id=40)
        self.store.set_auto_tts(10, 70, 80)
        self.store.connection.close()
        self.store = bot.Store(Path(self.temp_dir.name) / "test.sqlite3")

        self.assertEqual(self.store.settings(10)["log_channel_id"], 30)
        self.assertEqual(self.store.settings(10)["verify_role_id"], 40)
        self.assertEqual(self.store.auto_tts_settings(10), {70: 80})

    def test_announcement_recipients_have_no_fixed_cap(self) -> None:
        with self.store.connection:
            self.store.connection.executemany(
                "INSERT INTO dm_opt_ins (guild_id, user_id) VALUES (?, ?)",
                [(10, user_id) for user_id in range(1, 502)],
            )
        self.assertEqual(len(self.store.dm_opt_ins(10)), 501)

    def test_temporary_voice_channel_tracking(self) -> None:
        self.store.add_temporary_voice_channel(10, 301, 401)
        self.assertTrue(self.store.is_temporary_voice_channel(10, 301))
        self.assertEqual(self.store.temporary_voice_owner(10, 301), 401)
        self.store.remove_temporary_voice_channel(10, 301)
        self.assertFalse(self.store.is_temporary_voice_channel(10, 301))

    def test_captcha_is_single_use_and_request_is_rate_limited(self) -> None:
        with patch("bot.time.time", return_value=1000):
            challenge = self.store.create_captcha(10, 20, "A2BC34")
            self.assertTrue(challenge)
            self.assertFalse(self.store.create_captcha(10, 20, "A2BC34"))
        with patch("bot.time.time", return_value=1010):
            self.assertTrue(self.store.check_captcha(challenge, 10, 20, "a2bc34"))
            self.assertFalse(self.store.check_captcha(challenge, 10, 20, "A2BC34"))

    def test_captcha_expires_and_locks_after_five_attempts(self) -> None:
        with patch("bot.time.time", return_value=1000):
            challenge = self.store.create_captcha(10, 20, "A2BC34")
        with patch("bot.time.time", return_value=1300):
            self.assertFalse(self.store.check_captcha(challenge, 10, 20, "A2BC34"))

        with patch("bot.time.time", return_value=2000):
            challenge = self.store.create_captcha(10, 21, "A2BC34")
            for _ in range(5):
                self.assertFalse(self.store.check_captcha(challenge, 10, 21, "WRONG1"))
            self.assertFalse(self.store.check_captcha(challenge, 10, 21, "A2BC34"))

    def test_xp_cooldown_level_progression_and_ranking(self) -> None:
        with patch("bot.time.time", return_value=1000):
            self.assertEqual(self.store.award_xp(10, 20, 100), (2, 0, [2]))
        with patch("bot.time.time", return_value=1059):
            self.assertIsNone(self.store.award_xp(10, 20, 15))
        with patch("bot.time.time", return_value=1060):
            self.assertEqual(self.store.award_xp(10, 20, 25), (2, 25, []))
            self.assertEqual(
                self.store.award_xp(10, 21, 150, respect_cooldown=False),
                (2, 50, [2]),
            )
        self.assertEqual(self.store.level_rank(10, 20), (2, 2))
        self.assertEqual(self.store.leaderboard(10, 1)[0][0], 21)

    def test_daily_checkin_is_once_per_day_and_tracks_streak(self) -> None:
        with patch("bot.random.randint", return_value=100):
            first = self.store.checkin(10, 20, "2025-01-01")
            self.assertEqual(first, (100, 1))
            self.assertIsNone(self.store.checkin(10, 20, "2025-01-01"))
            self.assertEqual(self.store.checkin(10, 20, "2025-01-02"), (100, 2))
        self.assertEqual(self.store.checkin_streak(10, 20), 2)
        self.assertEqual(self.store.member_xp(10, 20), (2, 100))

    def test_banned_words_are_normalized_and_replaceable(self) -> None:
        self.store.set_banned_words(10, ["Bad Word", "spam"])
        self.assertEqual(self.store.banned_words(10), ["bad word", "spam"])
        self.store.set_banned_words(10, [])
        self.assertEqual(self.store.banned_words(10), [])


class BotBehaviorTests(unittest.TestCase):
    def test_verification_command_requires_admin_and_categories_description(self) -> None:
        command = next(
            command
            for command in bot.bot.tree.get_commands()
            if command.name == "보안인증서생성"
        )
        self.assertTrue(command.default_permissions.administrator)
        self.assertIn("카테고리를 숨기는", command.description)

    def test_verification_overwrites_hide_categories_and_expose_panel(self) -> None:
        default_role = object()
        verified_role = object()
        bot_member = object()
        hidden_channel = SimpleNamespace(
            id=2,
            overwrites={},
            edit=AsyncMock(),
        )
        verification_channel = SimpleNamespace(
            id=3,
            category=None,
            overwrites={},
            edit=AsyncMock(),
        )
        category = SimpleNamespace(
            overwrites={},
            channels=[hidden_channel],
            edit=AsyncMock(),
        )
        guild = SimpleNamespace(
            me=bot_member,
            default_role=default_role,
            categories=[category],
        )

        async def verify() -> None:
            await bot.restrict_unverified_category_access(
                guild, verified_role, verification_channel
            )

        asyncio.run(verify())

        self.assertFalse(category.overwrites[default_role].view_channel)
        self.assertTrue(category.overwrites[verified_role].view_channel)
        self.assertFalse(hidden_channel.overwrites[default_role].view_channel)
        self.assertTrue(hidden_channel.overwrites[verified_role].view_channel)
        self.assertTrue(verification_channel.overwrites[default_role].view_channel)

    def test_log_channel_requires_view_send_and_embed_permissions(self) -> None:
        member = object()
        channel = SimpleNamespace(
            permissions_for=lambda target: SimpleNamespace(
                view_channel=True,
                send_messages=True,
                embed_links=target is member,
            )
        )
        self.assertTrue(bot.can_send_embeds(channel, member))
        self.assertFalse(bot.can_send_embeds(channel, object()))

    def test_member_notice_text_matches_join_and_leave_format(self) -> None:
        member = SimpleNamespace(
            id=1234,
            name="Test_User",
            created_at=SimpleNamespace(timestamp=lambda: 900),
            guild=SimpleNamespace(member_count=10, members=[]),
        )
        joined = bot.member_notice_text(member, joined=True, timestamp=1000)
        left = bot.member_notice_text(member, joined=False, timestamp=2000)

        self.assertIn("[Test\\_User](https://discord.com/users/1234)", joined)
        self.assertIn("<@1234>", joined)
        self.assertIn("<t:1000:F>", joined)
        self.assertIn("<t:900:D>", joined)
        self.assertIn("현재 인원 : 10명", joined)
        self.assertIn("안녕히 가세요", left)
        self.assertIn("서버 퇴장시간 : <t:2000:F>", left)

    def test_captcha_and_profile_card_are_png_images(self) -> None:
        captcha = bot.generate_captcha("A2BC34")
        with Image.open(io.BytesIO(captcha)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (300, 100))

        card = bot.create_profile_card("테스트 멤버", 3, 125, 2, 20, captcha)
        with Image.open(io.BytesIO(card)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (960, 420))

    def test_tts_text_is_sanitized_and_filtered(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        store = bot.Store(Path(temp_dir.name) / "test.sqlite3")
        store.set_banned_words(10, ["blocked"])
        try:
            with patch.object(bot, "store", store):
                self.assertEqual(
                    bot.clean_tts_text("안녕 https://example.com @everyone", 10),
                    "안녕 링크 @\u200beveryone",
                )
                self.assertEqual(bot.clean_tts_text("this is blocked", 10), "")
                self.assertEqual(bot.clean_tts_text("please selfbot", 10), "")
        finally:
            store.connection.close()
            temp_dir.cleanup()

    def test_text_clipping_respects_embed_limit(self) -> None:
        self.assertEqual(len(bot.clipped("x" * 2000)), 1000)

    def test_slash_commands_and_persistent_views_are_registered(self) -> None:
        command_names = {command.name for command in bot.bot.tree.get_commands()}
        self.assertEqual(
            command_names,
            {
                "로그채널지정",
                "환영퇴장채널지정",
                "보안인증서생성",
                "역할지급패널생성",
                "보이스입장채널지정",
                "통계채널설정",
                "출석보상역할설정",
                "레벨",
                "랭킹",
                "출석체크",
                "고급공지작성",
                "tts",
                "tts중지",
                "tts퇴장",
                "방이름변경",
                "방인원제한",
                "방잠금",
                "방초대",
                "방추방",
                "공지수신",
                "디엠공지전송",
                "음악재생",
                "음악나가기",
                "음악볼륨",
            },
        )
        self.assertTrue(bot.VerifyView().is_persistent())
        role_view = bot.RolePanelView(123456789, [111111111])
        self.assertTrue(role_view.is_persistent())
        self.assertEqual(role_view.children[0].options[0].value, "111111111")

    def test_announcement_command_options_use_valid_names(self) -> None:
        command = next(
            command
            for command in bot.bot.tree.get_commands()
            if command.name == "고급공지작성"
        )
        self.assertEqual(
            {parameter.name for parameter in command.parameters},
            {"채널", "제목", "내용", "색상", "이미지주소"},
        )

    def test_admin_commands_are_restricted_and_member_commands_are_open(self) -> None:
        commands = {
            command.name: command for command in bot.bot.tree.get_commands()
        }
        admin_commands = {
            "로그채널지정",
            "환영퇴장채널지정",
            "보안인증서생성",
            "역할지급패널생성",
            "보이스입장채널지정",
            "통계채널설정",
            "출석보상역할설정",
            "고급공지작성",
            "디엠공지전송",
        }
        for name in admin_commands:
            self.assertTrue(
                commands[name].default_permissions.administrator,
                f"{name} should be limited to administrators in Discord",
            )
            self.assertTrue(commands[name].checks)

        for name, command in commands.items():
            if name not in admin_commands:
                self.assertIsNone(
                    command.default_permissions,
                    f"{name} should remain available to server members",
                )

    def test_tts_can_be_enabled_without_an_initial_sentence(self) -> None:
        command = next(
            command
            for command in bot.bot.tree.get_commands()
            if command.name == "tts"
        )
        self.assertEqual(len(command.parameters), 1)
        self.assertFalse(command.parameters[0].required)

    def test_voice_lobby_setup_takes_a_category(self) -> None:
        command = next(
            command
            for command in bot.bot.tree.get_commands()
            if command.name == "보이스입장채널지정"
        )
        self.assertEqual([parameter.name for parameter in command.parameters], ["카테고리"])


class AsyncBotBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_retries_when_service_returns_no_audio(self) -> None:
        speech = bot.Speech(1, 2, "테스트 문장", "ko-KR-SunHiNeural")
        audio_path = Path(tempfile.gettempdir()) / f"tts-{id(self)}.mp3"
        save_attempts = 0

        async def save(path: str) -> None:
            nonlocal save_attempts
            save_attempts += 1
            if save_attempts == 1:
                raise bot.edge_tts.exceptions.NoAudioReceived("empty response")
            Path(path).write_bytes(b"audio")

        communicate = SimpleNamespace(save=AsyncMock(side_effect=save))

        with (
            patch.object(bot.edge_tts, "Communicate", return_value=communicate),
            patch.object(bot.asyncio, "sleep", new=AsyncMock()),
        ):
            await bot.TTSManager()._synthesize_speech(speech, audio_path)

        self.assertEqual(save_attempts, 2)
        audio_path.unlink(missing_ok=True)

    async def test_empty_voice_channel_disconnects_and_stops_playback(self) -> None:
        voice = SimpleNamespace(
            channel=SimpleNamespace(members=[]),
            is_connected=lambda: True,
            is_playing=Mock(return_value=True),
            is_paused=Mock(return_value=False),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        guild = SimpleNamespace(id=1, voice_client=voice)
        manager = bot.MusicManager()

        with (
            patch.object(bot, "EMPTY_VOICE_DISCONNECT_SECONDS", 0),
            patch.object(bot.tts, "stop") as stop_tts,
        ):
            manager.schedule_idle_disconnect(guild, delay=0)
            await manager.idle_tasks[1]

        stop_tts.assert_called_once_with(guild)
        voice.stop.assert_called_once()
        voice.disconnect.assert_awaited_once()

    async def test_tts_enabled_user_is_read_in_other_text_channels(self) -> None:
        class DummyMember:
            pass

        voice_channel = SimpleNamespace(id=30, connect=AsyncMock())
        voice_client = SimpleNamespace(channel=voice_channel)
        member = DummyMember()
        member.id = 20
        member.bot = False
        member.display_name = "이름"
        member.voice = SimpleNamespace(channel=voice_channel)
        guild = SimpleNamespace(id=10, voice_client=None)
        message = SimpleNamespace(
            guild=guild,
            author=member,
            content="안녕하세요",
            channel=SimpleNamespace(id=41),
        )
        manager = bot.TTSManager()
        manager.enable_auto_reading(10, 20, 30, 40)
        manager.enqueue = Mock(return_value=1)
        manager.start = Mock()

        with (
            patch.object(bot.discord, "Member", DummyMember),
            patch.object(bot, "tts", manager),
            patch.object(bot, "moderate_message", AsyncMock(return_value=False)),
            patch.object(bot, "award_member_xp", AsyncMock()),
            patch.object(bot.store, "auto_tts_voice", return_value=None),
        ):
            await bot.on_message(message)

        speech = manager.enqueue.call_args.args[1]
        self.assertEqual(speech.text, "안녕하세요")
        self.assertNotIn(member.display_name, speech.text)
        self.assertEqual(speech.channel_id, voice_channel.id)
        voice_channel.connect.assert_awaited_once()

    async def test_music_volume_command_updates_current_source(self) -> None:
        original_source = Mock(spec=bot.discord.AudioSource)
        original_source.is_opus.return_value = False
        source = bot.discord.PCMVolumeTransformer(
            original_source,
            volume=0.5,
        )
        voice = SimpleNamespace(channel=SimpleNamespace(id=10), source=source)
        member = SimpleNamespace(
            voice=SimpleNamespace(channel=voice.channel)
        )
        guild = SimpleNamespace(id=1, voice_client=voice)
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        manager = bot.MusicManager()

        with (
            patch.object(bot.discord, "Member", type(member)),
            patch.object(bot, "music", manager),
        ):
            await bot.set_music_volume.callback(interaction, 75)

        self.assertEqual(manager.get_volume(guild.id), 0.75)
        self.assertEqual(source.volume, 0.75)
        interaction.response.send_message.assert_awaited_once_with(
            "음악 볼륨을 **75%**로 설정했습니다."
        )

    async def test_music_repeat_button_toggles_repeat_mode(self) -> None:
        class DummyMember:
            pass

        voice = SimpleNamespace(channel=SimpleNamespace(id=10))
        member = DummyMember()
        member.voice = SimpleNamespace(channel=voice.channel)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1, voice_client=voice),
            user=member,
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        manager = bot.MusicManager()
        view = bot.MusicControlsView()
        repeat_button = next(
            item for item in view.children if item.custom_id == "community:music:repeat"
        )

        with (
            patch.object(bot.discord, "Member", DummyMember),
            patch.object(bot, "music", manager),
        ):
            await bot.MusicControlsView.repeat(view, interaction, repeat_button)

        self.assertTrue(manager.is_repeating(1))
        self.assertEqual(repeat_button.label, "반복: 켬")
        interaction.response.edit_message.assert_awaited_once_with(view=view)

    async def test_music_volume_buttons_adjust_and_clamp_volume(self) -> None:
        class DummyMember:
            pass

        voice = SimpleNamespace(channel=SimpleNamespace(id=10), source=None)
        member = DummyMember()
        member.voice = SimpleNamespace(channel=voice.channel)
        guild = SimpleNamespace(id=1, voice_client=voice)
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        manager = bot.MusicManager()
        view = bot.MusicControlsView()
        volume_up = next(
            item for item in view.children if item.custom_id == "community:music:volume_up"
        )
        volume_down = next(
            item for item in view.children if item.custom_id == "community:music:volume_down"
        )

        with (
            patch.object(bot.discord, "Member", DummyMember),
            patch.object(bot, "music", manager),
        ):
            await bot.MusicControlsView.volume_up(view, interaction, volume_up)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)
            await bot.MusicControlsView.volume_down(view, interaction, volume_down)

        self.assertEqual(manager.get_volume(guild.id), 0)
        self.assertEqual(interaction.followup.send.await_count, 7)
        interaction.followup.send.assert_awaited_with(
            "음악 볼륨을 **0%**로 설정했습니다.", ephemeral=True
        )

    async def test_music_stop_button_keeps_bot_in_voice_channel(self) -> None:
        class DummyMember:
            pass

        voice = SimpleNamespace(
            channel=SimpleNamespace(id=10),
            is_connected=lambda: True,
            is_playing=Mock(return_value=True),
            is_paused=Mock(return_value=False),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        member = DummyMember()
        member.voice = SimpleNamespace(channel=voice.channel)
        guild = SimpleNamespace(id=1, voice_client=voice)
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        manager = bot.MusicManager()
        manager.enqueue(1, bot.Track("테스트", "https://example.com", 1))
        manager.toggle_repeat(1)
        view = bot.MusicControlsView()
        stop_button = next(
            item for item in view.children if item.custom_id == "community:music:stop"
        )

        with (
            patch.object(bot.discord, "Member", DummyMember),
            patch.object(bot, "music", manager),
        ):
            await bot.MusicControlsView.stop(view, interaction, stop_button)

        voice.stop.assert_called_once()
        voice.disconnect.assert_not_awaited()
        self.assertFalse(manager.queues.get(guild.id))
        self.assertFalse(manager.is_repeating(guild.id))
        interaction.followup.send.assert_awaited_once_with(
            "음악 재생과 대기열을 정지했습니다. 봇은 음성 채널에 계속 연결되어 있습니다.",
            ephemeral=True,
        )

    async def test_auto_reading_is_scoped_to_a_user_and_stops_cleanly(self) -> None:
        manager = bot.TTSManager()
        manager.enable_auto_reading(1, 2, 3, 4)
        manager.enqueue(1, bot.Speech(2, 3, "읽을 말", "ko-KR-SunHiNeural"))
        guild = SimpleNamespace(id=1, voice_client=None)

        self.assertEqual(manager.auto_reading_channel(1, 2), 3)
        self.assertEqual(manager.auto_reading_text_channel(1, 2), 4)
        queued, playing = manager.stop_user(guild, 2)

        self.assertEqual((queued, playing), (1, False))
        self.assertIsNone(manager.auto_reading_channel(1, 2))
        self.assertIsNone(manager.auto_reading_text_channel(1, 2))
        self.assertFalse(manager.queues.get(1))

    async def test_tts_does_not_play_in_another_voice_channel(self) -> None:
        voice_client = SimpleNamespace(
            channel=SimpleNamespace(id=100),
            is_connected=lambda: True,
        )
        member = SimpleNamespace(
            voice=SimpleNamespace(channel=SimpleNamespace(id=200))
        )
        guild = SimpleNamespace(
            id=1,
            voice_client=voice_client,
            get_member=lambda user_id: member,
        )
        manager = bot.TTSManager()

        with patch.object(bot.edge_tts, "Communicate") as communicate:
            await manager._play_speech(
                guild,
                bot.Speech(
                    user_id=10,
                    channel_id=200,
                    text="테스트",
                    voice="ko-KR-SunHiNeural",
                ),
            )

        communicate.assert_not_called()

    async def test_music_pause_button_defers_before_responding(self) -> None:
        class DummyMember:
            pass

        voice = SimpleNamespace(
            channel=SimpleNamespace(id=10),
            is_connected=lambda: True,
            is_playing=lambda: True,
            is_paused=lambda: False,
            pause=Mock(),
        )
        member = DummyMember()
        member.voice = SimpleNamespace(channel=voice.channel)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(voice_client=voice),
            user=member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        view = bot.MusicControlsView()
        pause_button = next(
            item for item in view.children if item.custom_id == "community:music:pause"
        )

        with patch.object(bot.discord, "Member", DummyMember):
            await bot.MusicControlsView.pause(view, interaction, pause_button)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True
        )
        voice.pause.assert_called_once()
        interaction.followup.send.assert_awaited_once_with(
            "재생을 일시정지했습니다.", ephemeral=True
        )

if __name__ == "__main__":
    unittest.main()
