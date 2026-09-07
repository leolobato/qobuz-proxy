# Spotify source takeover hotfix (v1.7.2)

On September 7, 2026 at 20:09:31 Europe/Berlin, Kitchen refreshed its Qobuz
WebSocket token, rejoined as active, and applied a cached seek to 4000 ms while
the user was listening to Spotify. The same seek occurred on earlier hourly
refreshes. This matched the reported song restart.

The DLNA backend now checks the actual source before transport and queue
commands. Sonos uses `GetPositionInfo.TrackURI`; other DLNA renderers use
`GetMediaInfo.CurrentURI`. Both the current Qobuz URL and its armed gapless
successor count as owned audio. Unknown/empty URI responses block transport
commands and session snapshots without permanently releasing ownership.

A confirmed foreign source releases Qobuz session ownership, invalidates pending
playback commands, ends the Qobuz listening report, and discards local gapless
state without editing the foreign queue. Background reconnects remain inactive;
stale cloud activations cannot take control back. Select the speaker again in
the Qobuz app to resume Qobuz playback. Source checks also protect shutdown from
stopping another controller's audio.

`tests/integration/test_external_playback.py` exercises the real player, command
handler, WebSocket protocol and speaker handoff with a simulated DLNA client.
It covers stopped/playing/paused reconnect snapshots, explicit reselection,
gapless ownership, unknown URI recovery, external stops, delayed loads and
renderer loading grace periods. These tests do not constitute live verification
of the released build on a physical speaker.

Validation: the full suite passed (713 tests), followed by all 17 takeover
regression cases after adding the in-flight URI-read guard. A direct comparison
against the v1.7.1 backend reproduced one seek sent to Spotify; the patched
backend sent none. Ruff passes. Mypy retains the previously documented 70 errors
in 13 files.
