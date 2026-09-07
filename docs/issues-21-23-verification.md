# Local reproduction and verification: issues #21 and #23

## Findings and scope

- [#23, corrected timeline](https://github.com/leolobato/qobuz-proxy/issues/23#issuecomment-5561722651): an idle speaker's token refresh at 14:26:04 precedes the previous owner's stop. The one-second fallback then starts audio on the reconnecting speaker. Every join previously set `isActive=true` and `reason=1`. The official client identifies reason 1 as a controller request and reason 2 as a reconnection. The fix preserves server-confirmed ownership, consumes explicit selection intent, and invalidates stale commands/reports.
- [#21, reopened report](https://github.com/leolobato/qobuz-proxy/issues/21#issuecomment-5560246579): at 15:20:47 the unavailable track enters the skip wait; at 15:20:57 the wait expires. Reporting a new current item did not produce the successor response assumed by the previous fix. The renderer also discarded `SET_STATE.queueVersion`, leaving reports without the version received from the server. The fix echoes that version and explicitly requests renderer action NEXT, then follows the server-selected current item.
- Protocol definitions were checked against the [official web client bundle, 8.2.0-b034](https://play.qobuz.com/resources/8.2.0-b034/bundle.js), retrieved on 2026-09-06. NEXT is message type 24, union field 24, with action value 2 in nested field 2. The previous unused schema had union field 30 and nested field 1. The golden-byte test checks `08 18 c2 01 02 10 02` independently of our protobuf round-trip.

The logs establish the missing-response failure, but do not reveal exactly why Qobuz withheld a response. The local fake accepts version-correct reports without sending unsolicited commands. This tests the client against that behavior; it does not certify every detail of the cloud implementation.

## 1. Repeatable local reproduction — implemented and run

From the repository root, with the normal development environment:

```sh
uv run python scripts/reproduce_issue_regressions.py
```

The script uses disposable `git archive` directories and copies the same portable integration tests into them. It leaves the checkout, configuration, credentials, branches and remotes untouched. No account, network connection, audio device or running Qobuz instance is required by these tests.

| Case | Baseline | Expected failure | Fixed result |
| --- | --- | --- | --- |
| #23 inactive reconnect | `3e61d5c` (v1.7.0 source) | Join has `isActive=true` instead of false | Join has false and reconnection reason 2 |
| #21 unavailable successor | `9876048` (before #21 changes) | Only track 1 plays; skip wait expires | Tracks 1 and 6 play; NEXT advances past unavailable items |

The #21 baseline runs three variants: one unavailable track, two different unavailable tracks, and two queue occurrences of the same unavailable track. The script requires the expected assertion/log evidence, not merely any test error, then requires the current integration suite to pass.

These tests use real protocol encoding/decoding, command handlers, player state, queue version storage and state reporting. API results, WebSocket transmission and audio output are replaced at their boundaries. The #23 portable repro specifically tests the outgoing join; additional unit tests exercise three managers, token minting, playing/paused ownership and asynchronous deactivation.

Run the current checks separately:

```sh
uv run pytest tests/integration/ -q
uv run pytest tests/connect/ tests/playback/ tests/test_speaker.py tests/test_app_local_backend.py -q
uv run pytest -q
uv run ruff check qobuz_proxy/ tests/ scripts/reproduce_issue_regressions.py
```

The integration suite also verifies end-of-queue, bounded all-unavailable repeat, duplicate current-item echoes, interruption by pause/stop/deactivation/disconnect, reconnect during a skip, and an unanswered NEXT timeout. Unit tests check ownership and command validity again after acquiring the send lock, including that discarded messages consume no message IDs.

## 2. Complete local transport exercise — proposed next step

Build a test harness that runs real `Speaker` instances against loopback WebSocket and HTTP servers, with a recording audio backend. Keep it in test code, using temporary configuration and device UUIDs. This extends the current boundary fakes to exercise actual socket lifecycle and `Speaker` startup/shutdown.

For #23:

1. Start three speakers A, B and C in one fake cloud session; select B and play a test track.
2. Expire A's token and age its last-activity clock past the idle refresh threshold. Let the normal token-refresher and connection loop reconnect it. Repeat for C.
3. Assert A/C join with `isActive=false, reason=2`; B remains the sole owner and receives no stop. Only B's backend records audio.
4. Reconnect B while playing, then while paused. Assert ownership remains true, the paused state stays paused, and reporting waits for activation confirmation on the new connection.
5. Select A explicitly through discovery, including with unchanged tokens on an existing inactive connection. Assert one intentional handoff, followed by passive reconnects after A is deactivated.
6. Exercise batched and delayed deactivation, disconnect during load, and old messages arriving while a socket closes. Record joins, activation events, state reports and backend calls with speaker identities. Confirm no timer can start an unowned snapshot.

For #21:

1. Use queue items A, unavailable B, playable C, unavailable D/E, playable F. Give the queue a nonzero version and track IDs unrelated to their queue positions.
2. Have the fake API return missing metadata/URL for unavailable items. Let playable audio end naturally and exercise both gapless preloading and non-gapless advancement.
3. Accept version-correct current-item reports but send no unsolicited `SET_STATE`. Respond only to a correctly encoded NEXT action.
4. Assert A→C→F audio playback, the correct queue version on reports, and one NEXT per unavailable occurrence. Never hand unavailable audio to the backend or report it as a successful listen.
5. Repeat with shuffled server order, distinct contexts, duplicate track IDs, an unavailable final item, and an all-unavailable repeating queue. Assert server order is followed and retries are bounded.
6. Delay an outgoing NEXT behind the send lock, then pause, stop, choose another track, deactivate or disconnect. Assert the obsolete action never reaches the server. Also withhold the NEXT acknowledgement and check the bounded timeout.

Combined case: while B is waiting to skip an unavailable item, refresh inactive A and C. B must retain ownership and advance exactly once. Then deactivate B during a delayed NEXT; its request and pending timeout must be invalidated.

## 3. Live local confirmation with Qobuz and real renderers

On 2026-09-07, the deployed source matched the three fixes through `10e9d10`.
Kitchen completed approximately 62 minutes of playback with 15 gapless transitions
and no ERROR records. At 09:18:20 Europe/Berlin, after the album ended, it refreshed
its token with `isActive=True, cause=reconnect` and remained stopped. This verifies
an owner reconnect after playback ends, not an inactive-speaker reconnect or #21.

Living Room was selected at 09:58:45 and paused at 09:58:58. Kitchen was activated
at 10:00:03 and started a playlist at 10:00:26, with volume set to zero. No Living
Room deactivation was logged. Inspection revealed a local regression: requiring
`HasField("active")` discards a present SET_ACTIVE payload whose boolean is omitted
and therefore defaults to false. The INFO logs do not expose the received payload,
so this is a possible explanation for the missing deactivation, not a confirmed
capture of the live message.

Three new regression variants failed against `10e9d10`: an inactive reconnect
claimed ownership, queued playback survived deactivation, and a disconnect lost a
pending stop. Removing the boolean-presence requirement fixes all three while
still requiring the SET_ACTIVE payload. The connect/playback/integration suites
pass (271 tests). The follow-up was deployed as `4e1bbfd` at 10:49:49 Europe/Berlin;
the running module's SHA-256 matched the local source.

### Completed live results (2026-09-07, Europe/Berlin)

- **#23 passed:** at 10:51:51, Living Room received `active=False` and Kitchen
  received `active=True`. At 11:50:18, Living Room naturally refreshed its token
  and rejoined with `isActive=False, cause=reconnect`. Kitchen continued through
  subsequent tracks with no ownership loss, logged pauses/stops, or errors.
  At 11:55, direct Sonos queries confirmed Kitchen PLAYING and Living Room STOPPED.
  Living Room made no audio requests after the handoff; the container had no restarts.
- **#21 first skip passed:** on the reported album and matching track IDs,
  track 5 (`234375468`) returned metadata 404s. After track 4 ended naturally,
  the proxy sent `NEXT` at 12:14:01.548. Qobuz selected track 6 (`234375469`),
  and "Rough Rider" began at 12:14:02.003, without the former ten-second timeout.
- **#21 second unavailable item passed after manual advancement:** the user
  redeployed at 12:17:16, then resumed Kitchen and advanced through track 7.
  Track 8 (`234375471`) returned 404; `NEXT` was sent at 12:17:47.224 and track 9
  (`234375472`), "Big Shot", started at 12:17:47.623. This is a second real
  unavailable-item recovery, not a natural-end 7-to-9 transition.

Release validation for v1.7.1: 697 tests passed; Ruff passed. Mypy remains
non-clean with the previously observed 70 errors in 13 files.

These observations verify the real Qobuz service with Sonos DLNA playback.
They do not cover every app/region/backend combination; MPD, local audio,
consecutive unavailable tracks, and combined failure scenarios remain covered by
automated tests or the additional live exercises below. Continuous Docker logs
were copied off the server, including across redeployment, to preserve evidence.

### Additional live coverage

Use a separate test instance/configuration, with DEBUG logging and disposable device names. Capture sanitized protobuf fields and renderer HTTP requests; omit authentication tokens and signed streaming URLs.

- **#23:** configure at least two distinct speakers (preferably the reporter's three-speaker shape). Include Sonos and an MPD/gmediarender target where available. Play on B, then force the *normal* background refresh path for idle A/C using a test-only hook that expires their tokens and ages their activity clocks. Do not use discovery connect as the refresh trigger: that is an intentional selection. Confirm B keeps playing, no other renderer fetches audio, and inactive joins carry false/reason 2. Repeat with B paused, after manual handoffs, and over natural hourly refreshes. Include grouped Sonos if available.
- **#21:** use the reported album, The Beat's “I Just Can't Stop It (2012 Remaster)”. Verify current account-region availability first; the reported unavailable tracks 5 and 8 may be playable elsewhere. If needed, inject a missing-URL/404 result for specific track IDs in the test process while preserving the real Qobuz queue and WebSocket. Play track 4 to completion and verify playback continues to 6; repeat across 7→9. Confirm audible output, the app's current item, version-correct reports, and the NEXT/SET_STATE exchange. Repeat on MPD/DLNA and the local backend, with consecutive and final unavailable items.
- **Combined:** repeat the unavailable-track transition while an idle speaker refreshes. Verify only the selected speaker plays, and the queue advances once.

Before release, require baseline/fixed comparison on the same setup, all automated checks passing, and live evidence that intentional handoffs still work and unavailable tracks advance without unintended device changes. Record backend/app versions, account region, commit IDs and timestamps with the result. Passing the fake-server tests alone is not a claim that live Qobuz behavior has been verified.
