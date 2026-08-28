"""Tests for multi-speaker orchestration and auth lifecycle in QobuzProxy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config, QobuzConfig, SpeakerConfig


def _make_mock_speaker(name: str, starts: bool) -> MagicMock:
    speaker = MagicMock()
    speaker.start = AsyncMock(return_value=starts)
    speaker.stop = AsyncMock()
    speaker.name = name
    return speaker


def _make_speaker_config(
    name: str = "Test Speaker", http_port: int = 8689, **kwargs
) -> SpeakerConfig:
    """Return a minimal SpeakerConfig for tests."""
    defaults = dict(
        name=name,
        uuid=f"uuid-{name.lower().replace(' ', '-')}",
        backend_type="dlna",
        max_quality=27,
        http_port=http_port,
        bind_address="0.0.0.0",
        dlna_ip="192.168.1.100",
        dlna_port=1400,
        dlna_fixed_volume=False,
        proxy_port=7120,
        audio_device="default",
        audio_buffer_size=2048,
    )
    defaults.update(kwargs)
    return SpeakerConfig(**defaults)


def _make_config(*speaker_configs: SpeakerConfig) -> Config:
    """Return a Config with the given speakers and auth credentials."""
    config = Config()
    config.qobuz = QobuzConfig(email="test@example.com", auth_token="secret", user_id="12345")
    config.speakers = list(speaker_configs)
    return config


class TestMultiSpeakerOrchestration:
    async def test_starts_multiple_speakers(self):
        """Two speakers constructed, both start() called, app is running."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        mock_speaker_instances = [MagicMock(), MagicMock()]
        for m in mock_speaker_instances:
            m.start = AsyncMock(return_value=True)
            m.stop = AsyncMock()
            m.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=mock_speaker_instances) as MockSpeaker,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert MockSpeaker.call_count == 2
            for instance in mock_speaker_instances:
                instance.start.assert_called_once()
            assert app.is_running

    async def test_continues_when_one_speaker_fails(self):
        """One speaker succeeds, one returns False -> app still running with one speaker."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        good = MagicMock()
        good.start = AsyncMock(return_value=True)
        good.stop = AsyncMock()
        good.name = "Living Room"

        bad = MagicMock()
        bad.start = AsyncMock(return_value=False)
        bad.stop = AsyncMock()
        bad.name = "Bedroom"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=[good, bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert len(app._speakers) == 1
            assert app._speakers[0] is good

    async def test_no_speakers_started_still_running(self):
        """All speakers fail -> app stays running (waiting for auth or retry)."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        config = _make_config(sc1)

        bad = MagicMock()
        bad.start = AsyncMock(return_value=False)
        bad.stop = AsyncMock()
        bad.name = "Living Room"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=[bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            # App stays running even if speakers failed (no RuntimeError)
            assert app.is_running
            assert len(app._speakers) == 0

    async def test_failed_speaker_retries_in_background(self):
        """A speaker that fails at boot is retried and eventually joins the running list."""
        sc1 = _make_speaker_config("Living Room")
        config = _make_config(sc1)

        bad = _make_mock_speaker("Living Room", starts=False)
        still_bad = _make_mock_speaker("Living Room", starts=False)
        good = _make_mock_speaker("Living Room", starts=True)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=[bad, still_bad, good]),
            patch("qobuz_proxy.app.SPEAKER_RETRY_DELAYS_SECONDS", (0.01,)),
            patch("qobuz_proxy.app.SPEAKER_RETRY_STEADY_DELAY_SECONDS", 0.01),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app._speakers == []
            retry_task = app._speaker_retry_tasks["living-room"]

            await asyncio.wait_for(retry_task, timeout=2.0)

            assert app._speakers == [good]
            assert app._speaker_retry_tasks == {}
            still_bad.start.assert_awaited_once()
            good.start.assert_awaited_once()

    async def test_stop_cancels_pending_retries(self):
        """Shutting down cancels background speaker retries."""
        sc1 = _make_speaker_config("Living Room")
        config = _make_config(sc1)

        bad = _make_mock_speaker("Living Room", starts=False)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=[bad]),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            retry_task = app._speaker_retry_tasks["living-room"]
            await app.stop()

            assert app._speaker_retry_tasks == {}
            await asyncio.sleep(0)
            assert retry_task.cancelled()

    async def test_stop_stops_all_speakers(self):
        """After a successful start, stop() calls stop() on all started speakers."""
        sc1 = _make_speaker_config("Living Room", http_port=8689)
        sc2 = _make_speaker_config("Bedroom", http_port=8690)
        config = _make_config(sc1, sc2)

        mock_instances = []
        for name in ("Living Room", "Bedroom"):
            m = MagicMock()
            m.start = AsyncMock(return_value=True)
            m.stop = AsyncMock()
            m.name = name
            mock_instances.append(m)

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", side_effect=mock_instances),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()
            await app.stop()

            for instance in mock_instances:
                instance.stop.assert_called_once()
            assert not app.is_running


class TestGracefulStartup:
    """Tests for the new graceful startup behavior."""

    async def test_starts_without_credentials(self):
        """App starts in waiting-for-auth state when no credentials in config or cache."""
        config = Config()  # No qobuz credentials
        config.speakers = [_make_speaker_config()]

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token", return_value=None),
        ):
            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert app._auth_state["authenticated"] is False
            assert len(app._speakers) == 0

    async def test_starts_with_cached_token(self):
        """App picks up cached token and authenticates automatically."""
        config = Config()  # No config credentials
        config.speakers = [_make_speaker_config()]

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={
                    "user_id": "99",
                    "user_auth_token": "cached_tok",
                    "email": "cached@example.com",
                },
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", return_value=mock_speaker),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "99"
            assert len(app._speakers) == 1

    async def test_config_token_takes_priority_over_cache(self):
        """Config credentials are used even if cache exists."""
        config = _make_config(_make_speaker_config())

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token") as mock_load_cache,
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", return_value=mock_speaker),
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()

            # Cache should not have been consulted
            mock_load_cache.assert_not_called()
            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "12345"

    async def test_invalid_cached_token_enters_waiting(self):
        """When cached token is invalid, app enters waiting-for-auth state."""
        config = Config()
        config.speakers = [_make_speaker_config()]

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch(
                "qobuz_proxy.app.load_user_token",
                return_value={
                    "user_id": "99",
                    "user_auth_token": "bad_tok",
                    "email": "",
                },
            ),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=False)

            app = QobuzProxy(config)
            await app.start()

            assert app.is_running
            assert app._auth_state["authenticated"] is False
            assert len(app._speakers) == 0


class TestWebUICallbacks:
    """Tests for auth token submission and logout callbacks."""

    async def test_on_auth_token_success(self):
        """Successful token submission sets up client and starts speakers."""
        config = Config()
        config.speakers = [_make_speaker_config()]

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.load_user_token", return_value=None),
            patch("qobuz_proxy.app.QobuzAPIClient"),
            patch("qobuz_proxy.app.Speaker", return_value=mock_speaker),
            patch("qobuz_proxy.app.save_user_token", return_value=True),
        ):
            app = QobuzProxy(config)
            await app.start()
            assert not app._auth_state["authenticated"]

            result = await app._on_auth_token("42", "valid_token")

            assert result is True
            assert app._auth_state["authenticated"] is True
            assert app._auth_state["user_id"] == "42"
            assert len(app._speakers) == 1

    async def test_on_logout_stops_speakers_and_clears_state(self):
        """Logout stops speakers and resets auth state."""
        config = _make_config(_make_speaker_config())

        mock_speaker = MagicMock()
        mock_speaker.start = AsyncMock(return_value=True)
        mock_speaker.stop = AsyncMock()
        mock_speaker.name = "mock"

        with (
            patch.object(QobuzProxy, "_start_web_server", new_callable=AsyncMock),
            patch.object(QobuzProxy, "_stop_web_server", new_callable=AsyncMock),
            patch("qobuz_proxy.app.QobuzAPIClient") as MockAPIClient,
            patch("qobuz_proxy.app.Speaker", return_value=mock_speaker),
            patch("qobuz_proxy.app.clear_user_token") as mock_clear,
        ):
            MockAPIClient.return_value.login_with_token = AsyncMock(return_value=True)

            app = QobuzProxy(config)
            await app.start()
            assert app._auth_state["authenticated"] is True
            assert len(app._speakers) == 1

            await app._on_logout()

            assert app._auth_state["authenticated"] is False
            assert app._auth_state["user_id"] == ""
            assert len(app._speakers) == 0
            mock_speaker.stop.assert_called_once()
            mock_clear.assert_called_once()


class TestSpeakerEditCallbacks:
    """Tests for runtime add/edit speaker callbacks from the web UI."""

    async def test_edit_persists_config_when_restart_fails(self):
        """Edits must be saved even if the speaker can't start (e.g. device offline)."""
        import pytest  # noqa: F401

        config = _make_config(_make_speaker_config(name="Wyse", max_quality=6))
        old_speaker = MagicMock()
        old_speaker.name = "Wyse"
        old_speaker.stop = AsyncMock()
        old_speaker.start = AsyncMock(return_value=True)

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [old_speaker]

        new_speaker = MagicMock()
        new_speaker.name = "Wyse"
        new_speaker.start = AsyncMock(return_value=False)  # device unreachable
        new_speaker.get_status.return_value = {"id": "wyse", "status": "disconnected"}

        with (
            patch("qobuz_proxy.app.Speaker", return_value=new_speaker),
            patch.object(app, "_save_config") as mock_save,
        ):
            result = await app._on_edit_speaker(
                "wyse", {"max_quality": 27, "dlna_ip": "192.168.1.60"}
            )

        assert config.speakers[0].max_quality == 27
        assert config.speakers[0].dlna_ip == "192.168.1.60"
        mock_save.assert_called_once()
        assert app._speakers[0] is new_speaker
        assert result["status"] == "disconnected"

    async def test_edit_toggles_fixed_volume(self):
        """The edit form exposes Fixed volume; the flag must round-trip and survive
        edits that don't mention it."""
        config = _make_config(_make_speaker_config(name="Moode", dlna_fixed_volume=False))
        old_speaker = MagicMock()
        old_speaker.name = "Moode"
        old_speaker.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [old_speaker]

        new_speaker = MagicMock()
        new_speaker.name = "Moode"
        new_speaker.start = AsyncMock(return_value=True)
        new_speaker.stop = AsyncMock()
        new_speaker.get_status.return_value = {"id": "moode", "status": "idle"}

        with (
            patch("qobuz_proxy.app.Speaker", return_value=new_speaker) as mock_speaker_cls,
            patch.object(app, "_save_config"),
        ):
            await app._on_edit_speaker("moode", {"fixed_volume": True})
            assert config.speakers[0].dlna_fixed_volume is True
            assert mock_speaker_cls.call_args.kwargs["config"].dlna_fixed_volume is True

            # An edit that doesn't mention the flag keeps the current value
            await app._on_edit_speaker("moode", {"max_quality": 6})
            assert config.speakers[0].dlna_fixed_volume is True

            await app._on_edit_speaker("moode", {"fixed_volume": False})
            assert config.speakers[0].dlna_fixed_volume is False

    async def test_add_rejects_duplicate_name_from_config(self):
        """A config entry whose speaker isn't running must still block the name."""
        import pytest

        config = _make_config(_make_speaker_config(name="Living Room"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = []  # speaker failed to start, so it's not running

        with pytest.raises(ValueError, match="already exists"):
            await app._on_add_speaker(
                {"name": "Living Room", "backend": "dlna", "dlna_ip": "1.2.3.4"}
            )

    async def test_edit_rejects_rename_to_existing_name(self):
        import pytest

        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        speaker_a = MagicMock()
        speaker_a.name = "Speaker A"
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_a, speaker_b]

        with pytest.raises(ValueError, match="already exists"):
            await app._on_edit_speaker("speaker-b", {"name": "Speaker A"})

    async def test_edit_uses_config_entry_matched_by_name(self):
        """Config entry must be matched by name, not by running-list index."""
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689, max_quality=6),
            _make_speaker_config(name="Speaker B", http_port=8690, max_quality=6),
        )
        # Speaker A failed to start, so only B is running (index misalignment)
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        new_speaker = MagicMock()
        new_speaker.name = "Speaker B"
        new_speaker.start = AsyncMock(return_value=True)
        new_speaker.get_status.return_value = {"id": "speaker-b", "status": "idle"}

        with (
            patch("qobuz_proxy.app.Speaker", return_value=new_speaker),
            patch.object(app, "_save_config"),
        ):
            await app._on_edit_speaker("speaker-b", {"max_quality": 27})

        assert config.speakers[1].max_quality == 27  # B updated
        assert config.speakers[0].max_quality == 6  # A untouched

    async def test_remove_deletes_config_entry_matched_by_name(self):
        """Config entry must be removed by name, not by running-list index.

        When an earlier speaker failed to start, the running list and the
        config list are misaligned; popping the config by the running index
        would delete the wrong speaker.
        """
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        # Speaker A failed to start, so only B is running (index misalignment):
        # B is at running index 0 but config index 1.
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        with patch.object(app, "_save_config"):
            await app._on_remove_speaker("speaker-b")

        # Only B removed; A's config must survive.
        assert [sc.name for sc in config.speakers] == ["Speaker A"]
        assert app._speakers == []
        speaker_b.stop.assert_called_once()

    async def test_remove_non_running_speaker(self):
        """A configured speaker that failed to start can still be removed."""
        config = _make_config(
            _make_speaker_config(name="Speaker A", http_port=8689),
            _make_speaker_config(name="Speaker B", http_port=8690),
        )
        # A failed to start; only B is running.
        speaker_b = MagicMock()
        speaker_b.name = "Speaker B"
        speaker_b.stop = AsyncMock()

        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = [speaker_b]

        with patch.object(app, "_save_config"):
            await app._on_remove_speaker("speaker-a")

        # A removed from config; B left untouched and still running.
        assert [sc.name for sc in config.speakers] == ["Speaker B"]
        assert app._speakers == [speaker_b]
        speaker_b.stop.assert_not_called()

    async def test_remove_unknown_speaker_raises(self):
        """Removing a speaker that isn't configured raises KeyError."""
        import pytest

        config = _make_config(_make_speaker_config(name="Speaker A"))
        app = QobuzProxy(config)
        app._api_client = MagicMock()
        app._speakers = []

        with pytest.raises(KeyError):
            await app._on_remove_speaker("nope")
