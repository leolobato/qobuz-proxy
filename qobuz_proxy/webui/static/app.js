(function () {
    "use strict";

    var pollTimer = null;
    var editingSpeakerId = null;
    var lastSpeakersJson = null;
    var addPanelOpen = false;
    var selectedBackend = null;
    var selectedDevice = null;
    var volumeDragging = false;
    var seekDragging = false;
    var lastUnmutedVolume = {};

    // -------------------------------------------------------------------------
    // Auth
    // -------------------------------------------------------------------------

    function showAuthState(state) {
        document.getElementById("auth-disconnected").style.display = "none";
        document.getElementById("auth-connected").style.display = "none";

        if (state === "disconnected") {
            document.getElementById("auth-disconnected").style.display = "";
            document.getElementById("add-speaker-btn").style.display = "none";
        } else if (state === "connected") {
            document.getElementById("auth-connected").style.display = "";
            if (!addPanelOpen) {
                document.getElementById("add-speaker-btn").style.display = "";
            }
        }
    }

    function startLogin() {
        // Use the document base (honors HA ingress <base href>) so the OAuth
        // callback returns to this exact UI, prefix and all. Strip the trailing
        // slash so the server can append "/auth/callback".
        var base = document.baseURI.replace(/\/$/, "");
        var url = "auth/login?origin=" + encodeURIComponent(base);
        // Behind Home Assistant ingress the UI runs in an iframe. Qobuz (and the
        // third-party providers on its sign-in page, e.g. Google) refuse to be
        // framed, AND the OAuth callback can't return through ingress (HA's
        // ingress_session cookie isn't sent on the cross-site return from Qobuz,
        // so the callback 401s). So run login against the add-on's DIRECT port
        // in a new top-level tab — host networking exposes it and /data (the
        // saved token) is shared with this panel, which flips to "connected" via
        // the /api/status poll once login completes.
        if (window.self !== window.top) {
            var port = window.QOBUZ_DIRECT_PORT || "8689";
            var direct = window.location.protocol + "//" + window.location.hostname + ":" + port;
            window.open(direct + "/auth/login?origin=" + encodeURIComponent(direct), "_blank");
        } else {
            window.location.href = url;
        }
    }

    function logout() {
        fetch("api/auth/logout", { method: "POST" })
            .then(function () {
                showAuthState("disconnected");
                lastSpeakersJson = null;
                document.getElementById("speakers-list").innerHTML =
                    '<p class="muted">Waiting for authentication...</p>';
            })
            .catch(function () {
                showAuthState("disconnected");
            });
    }

    // -------------------------------------------------------------------------
    // Utilities
    // -------------------------------------------------------------------------

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.appendChild(document.createTextNode(String(text || "")));
        return div.innerHTML;
    }

    // Escape a value for embedding inside an HTML attribute. Text-node
    // escaping doesn't cover " and ', but those terminate attribute values
    // and break inline onclick handlers.
    function escapeAttr(text) {
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function showError(msg) {
        var el = document.getElementById("speaker-error");
        el.textContent = msg;
        el.style.display = "";
        setTimeout(function () {
            el.style.display = "none";
        }, 5000);
    }

    function qualityLabel(q) {
        var labels = { 27: "Hi-Res 192k", 7: "Hi-Res 96k", 6: "CD", 5: "MP3", auto: "Auto" };
        return labels[String(q)] || String(q || "");
    }

    function qualityOptions(selected) {
        var opts = [
            { value: "auto", label: "Auto" },
            { value: "27", label: "Hi-Res 192k" },
            { value: "7", label: "Hi-Res 96k" },
            { value: "6", label: "CD" },
            { value: "5", label: "MP3" },
        ];
        var html = "";
        for (var i = 0; i < opts.length; i++) {
            var sel = String(selected) === String(opts[i].value) ? ' selected' : '';
            html += '<option value="' + opts[i].value + '"' + sel + '>' + opts[i].label + '</option>';
        }
        return html;
    }

    // -------------------------------------------------------------------------
    // Speaker rendering
    // -------------------------------------------------------------------------

    function renderSpeakerHeader(s) {
        var html = '<div class="speaker-header">';
        html += '<span class="speaker-name" style="font-weight:500;color:#fff;font-size:14px;">' + escapeHtml(s.name) + '</span>';

        // Status badge
        var state = (s.status || "idle").toLowerCase();
        var badgeClass = "badge-idle";
        var badgeLabel = "Idle";
        if (state === "disconnected") {
            badgeClass = "badge-disconnected";
            badgeLabel = "Disconnected";
        } else if (state === "starting") {
            badgeClass = "badge-starting";
            badgeLabel = "Starting";
        } else if (state === "playing") {
            badgeClass = "badge-playing";
            badgeLabel = "Playing";
        } else if (state === "paused") {
            badgeClass = "badge-paused";
            badgeLabel = "Paused";
        } else if (state === "idle" || state === "stopped") {
            badgeClass = "badge-idle";
            badgeLabel = "Idle";
        }
        html += '<span class="speaker-badge ' + badgeClass + '">' + badgeLabel + '</span>';

        // Backend badge
        if (s.backend) {
            var backendClass = s.backend === "dlna" ? "badge-dlna" : "badge-local";
            html += '<span class="speaker-badge ' + backendClass + '">' + escapeHtml(s.backend.toUpperCase()) + '</span>';
        }

        html += '</div>';
        return html;
    }

    function playbackIcon(kind) {
        if (kind === "prev") {
            return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6 6h2.2v12H6V6zm3.3 6 9.7 6.2V5.8L9.3 12z"/></svg>';
        }
        if (kind === "next") {
            return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M15.8 6H18v12h-2.2V6zM5 18.2V5.8L14.7 12 5 18.2z"/></svg>';
        }
        if (kind === "pause") {
            return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6 5h4v14H6V5zm8 0h4v14h-4V5z"/></svg>';
        }
        if (kind === "volume") {
            return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 10v4h4l5 5V5L7 10H3zm13.5 2c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>';
        }
        if (kind === "volume-mute") {
            return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v4h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z"/></svg>';
        }
        return '<svg class="playback-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M8 5.5v13l11-6.5L8 5.5z"/></svg>';
    }

    function playbackPosition(s) {
        if (typeof s.position_ms === "number") return s.position_ms;
        var device = s.device || {};
        if (typeof device.position_ms === "number") return device.position_ms;
        return 0;
    }

    function playbackDuration(s) {
        if (typeof s.duration_ms === "number" && s.duration_ms > 0) return s.duration_ms;
        var device = s.device || {};
        var np = s.now_playing || {};
        if (typeof device.duration_ms === "number" && device.duration_ms > 0) return device.duration_ms;
        if (typeof np.duration_ms === "number" && np.duration_ms > 0) return np.duration_ms;
        return 0;
    }

    function renderPlaybackControls(s) {
        var state = (s.status || "idle").toLowerCase();
        if (state === "disconnected" || state === "starting") return "";
        var idArg = escapeAttr(JSON.stringify(s.id));
        var duration = playbackDuration(s);
        var position = playbackPosition(s);
        if (duration > 0 && position > duration) position = duration;

        var html = '<div class="playback-bar">';
        if (duration > 0) {
            html += '<div class="playback-seek">';
            html += '<span class="playback-seek-current">' + escapeHtml(formatClock(position)) + "</span>";
            html += '<input type="range" min="0" max="' + duration + '" value="' + position + '"';
            html += ' aria-label="Seek"';
            html += ' onpointerdown="beginSeekDrag()"';
            html += ' oninput="previewSeek(this)"';
            html += ' onchange="commitSeek(' + idArg + ', this)">';
            html += '<span class="playback-seek-duration">' + escapeHtml(formatClock(duration)) + "</span>";
            html += "</div>";
        }

        html += '<div class="playback-controls">';
        html += '<button type="button" class="playback-btn" title="Previous" aria-label="Previous" onclick="speakerControl(' + idArg + ', \'previous\')">' + playbackIcon("prev") + "</button>";
        if (state === "playing") {
            html += '<button type="button" class="playback-btn playback-btn-main" title="Pause" aria-label="Pause" onclick="speakerControl(' + idArg + ', \'pause\')">' + playbackIcon("pause") + "</button>";
        } else {
            html += '<button type="button" class="playback-btn playback-btn-main" title="Play" aria-label="Play" onclick="speakerControl(' + idArg + ', \'play\')">' + playbackIcon("play") + "</button>";
        }
        html += '<button type="button" class="playback-btn" title="Next" aria-label="Next" onclick="speakerControl(' + idArg + ', \'next\')">' + playbackIcon("next") + "</button>";

        var vol = s.volume;
        if (vol === undefined && s.now_playing && s.now_playing.volume !== undefined) {
            vol = s.now_playing.volume;
        }
        var cfg = s.config || {};
        if (!cfg.fixed_volume && vol !== undefined && vol !== null) {
            vol = parseInt(vol, 10);
            if (isNaN(vol)) vol = 0;
            if (vol > 0) lastUnmutedVolume[s.id] = vol;
            var muted = vol <= 0;
            var muteLabel = muted ? "Unmute" : "Mute";
            html += '<div class="playback-volume">';
            html += '<button type="button" class="playback-btn' + (muted ? " playback-btn-muted" : "") + '" title="' + muteLabel + '" aria-label="' + muteLabel + '" onclick="toggleMute(' + idArg + ', this)">' + playbackIcon(muted ? "volume-mute" : "volume") + "</button>";
            html += '<input type="range" min="0" max="100" value="' + escapeAttr(String(vol)) + '"';
            html += ' aria-label="Volume"';
            html += ' onpointerdown="beginVolumeDrag()"';
            html += ' oninput="previewVolume(' + idArg + ', this)"';
            html += ' onchange="commitVolume(' + idArg + ', this)">';
            html += '<span class="playback-volume-value">' + escapeHtml(String(vol)) + "</span>";
            html += "</div>";
        }
        html += "</div></div>";
        return html;
    }

    function beginVolumeDrag() {
        volumeDragging = true;
    }

    function previewVolume(id, input) {
        var vol = parseInt(input.value, 10);
        if (isNaN(vol)) vol = 0;
        if (vol > 0) lastUnmutedVolume[id] = vol;
        var label = input.parentNode && input.parentNode.querySelector(".playback-volume-value");
        if (label) label.textContent = String(vol);
        var btn = input.parentNode && input.parentNode.querySelector(".playback-btn");
        if (btn) {
            var muted = vol <= 0;
            btn.title = muted ? "Unmute" : "Mute";
            btn.setAttribute("aria-label", btn.title);
            btn.classList.toggle("playback-btn-muted", muted);
            btn.innerHTML = playbackIcon(muted ? "volume-mute" : "volume");
        }
    }

    function commitVolume(id, input) {
        previewVolume(id, input);
        speakerControl(id, "volume", { volume: parseInt(input.value, 10) || 0 });
        setTimeout(function () {
            volumeDragging = false;
        }, 500);
    }

    function toggleMute(id, btn) {
        var wrap = btn && btn.parentNode;
        var input = wrap && wrap.querySelector('input[type="range"]');
        var current = input ? parseInt(input.value, 10) : 0;
        if (isNaN(current)) current = 0;
        var next = current > 0 ? 0 : (lastUnmutedVolume[id] || 50);
        if (current > 0) lastUnmutedVolume[id] = current;
        volumeDragging = true;
        if (input) {
            input.value = String(next);
            previewVolume(id, input);
        }
        speakerControl(id, "volume", { volume: next });
        setTimeout(function () {
            volumeDragging = false;
        }, 500);
    }

    function beginSeekDrag() {
        seekDragging = true;
    }

    function previewSeek(input) {
        var el = input.parentNode && input.parentNode.querySelector(".playback-seek-current");
        if (el) el.textContent = formatClock(parseInt(input.value, 10) || 0);
    }

    function commitSeek(id, input) {
        previewSeek(input);
        speakerControl(id, "seek", { position_ms: parseInt(input.value, 10) || 0 });
        setTimeout(function () {
            seekDragging = false;
        }, 500);
    }

    function speakerControl(id, action, extra) {
        var payload = { action: action };
        if (extra && extra.volume != null) payload.volume = extra.volume;
        if (extra && extra.position_ms != null) payload.position_ms = extra.position_ms;
        fetch("api/speakers/" + encodeURIComponent(id) + "/control", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) {
                        throw new Error(d.error || "Control failed");
                    });
                }
                return r.json();
            })
            .then(function () {
                lastSpeakersJson = null;
                fetchStatus();
            })
            .catch(function (err) {
                showError(err.message);
            });
    }

    function renderActions(s) {
        var html = '<div class="speaker-actions">';
        var idArg = escapeAttr(JSON.stringify(s.id));
        html += '<button onclick="editSpeaker(' + idArg + ')">Edit</button>';
        html += '</div>';
        return html;
    }

    function formatMs(ms) {
        if (ms == null || ms === "" || isNaN(ms) || ms < 0) return "";
        var s = Math.floor(Number(ms) / 1000);
        var m = Math.floor(s / 60);
        s = s % 60;
        return m + ":" + (s < 10 ? "0" : "") + s;
    }

    function formatClock(ms) {
        return formatMs(ms) || "0:00";
    }

    function nowPlayingView(s) {
        var np = s.now_playing || {};
        var device = s.device || {};
        return {
            title: np.title || device.title || "",
            artist: np.artist || device.artist || "",
            album: np.album || device.album || "",
            album_art_url: np.album_art_url || device.album_art_url || "",
            quality: np.quality || "",
            next_title: device.next_title || "",
            next_track_id: device.next_track_id || "",
        };
    }

    function renderSpeakerCard(s) {
        var state = (s.status || "idle").toLowerCase();
        var np = nowPlayingView(s);
        var cfg = s.config || {};
        var isActive = state === "playing" || state === "paused" || !!(s.device && s.device.track_id);

        var html = '<div class="speaker-card">';

        if (isActive) {
            html += '<div class="speaker-card-playing">';

            if (np.album_art_url) {
                html += '<img class="speaker-album-art" src="' + escapeHtml(np.album_art_url) + '" alt="Album art">';
            } else {
                html += '<div class="speaker-album-art-placeholder">&#9835;</div>';
            }

            html += '<div class="speaker-info">';
            html += renderSpeakerHeader(s);

            var headline = "";
            if (np.artist && np.title) headline = np.artist + " — " + np.title;
            else headline = np.title || np.artist;
            if (headline) {
                html += '<div class="speaker-track">' + escapeHtml(headline) + '</div>';
            }
            if (np.album) {
                html += '<div class="speaker-artist-album">' + escapeHtml(np.album) + '</div>';
            }
            var nextLine = np.next_title || (np.next_track_id ? "Track " + np.next_track_id : "");
            if (nextLine) {
                html += '<div class="speaker-artist-album">Next: ' + escapeHtml(nextLine) + '</div>';
            }

            var meta = [];
            if (np.quality) meta.push(escapeHtml(np.quality));
            if (cfg.fixed_volume) {
                meta.push('Fixed volume');
            }
            if (meta.length) {
                html += '<div class="speaker-meta">' + meta.join(' · ') + '</div>';
            }

            html += renderPlaybackControls(s);
            html += '</div>'; // speaker-info
            html += renderActions(s);
            html += '</div>'; // speaker-card-playing
        } else {
            // Idle / disconnected layout
            html += '<div style="display:flex;align-items:flex-start;gap:8px;">';
            html += '<div style="flex:1;min-width:0;">';
            html += renderSpeakerHeader(s);

            var idleParts = [];
            if (s.backend === "dlna" && cfg.dlna_ip) {
                idleParts.push(escapeHtml(cfg.dlna_ip + ':' + (cfg.dlna_port || 1400)));
            } else if (s.backend === "local" && cfg.audio_device) {
                idleParts.push(escapeHtml(cfg.audio_device));
            }
            if (cfg.effective_quality) {
                var qText = qualityLabel(cfg.effective_quality);
                if (cfg.max_quality === "auto") {
                    qText += " (auto)";
                }
                idleParts.push(escapeHtml(qText));
            }
            if (idleParts.length) {
                html += '<div class="speaker-idle-info">' + idleParts.join(' · ') + '</div>';
            }
            if (cfg.quality_source === "auto_fallback" && state !== "disconnected") {
                html += '<div class="speaker-idle-info" style="color:#e0a030;">' +
                    'Couldn\'t detect this device\'s hi-res support. ' +
                    'Pick a quality in Edit if it supports better than CD.</div>';
            }

            html += renderPlaybackControls(s);
            html += '</div>'; // flex child
            html += renderActions(s);
            html += '</div>';
        }

        html += '</div>'; // speaker-card
        return html;
    }

    function renderEditForm(s) {
        var html = '<div class="speaker-edit-card">';
        html += '<div style="font-weight:600;margin-bottom:12px;color:#fff;">Edit Speaker</div>';

        html += '<div class="form-group">';
        html += '<label>Name</label>';
        html += '<input type="text" id="edit-name" value="' + escapeHtml(s.name) + '">';
        html += '</div>';

        var cfg = s.config || {};
        if (s.backend === "dlna") {
            html += '<div class="form-group">';
            html += '<label>IP Address</label>';
            html += '<input type="text" id="edit-dlna-ip" value="' + escapeHtml(cfg.dlna_ip || "") + '" placeholder="192.168.1.x">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Port</label>';
            html += '<input type="number" id="edit-dlna-port" value="' + escapeHtml(String(cfg.dlna_port || 1400)) + '">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Description URL (optional)</label>';
            html += '<input type="text" id="edit-dlna-url" value="' + escapeHtml(cfg.description_url || "") + '" placeholder="http://192.168.1.x:1400/xml/device_description.xml">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="edit-fixed-vol" style="width:auto;display:inline;margin-right:6px;"' + (cfg.fixed_volume ? ' checked' : '') + '> Fixed volume</label></div>';
        } else if (s.backend === "local") {
            html += '<div class="form-group">';
            html += '<label>Audio Device (leave blank for default)</label>';
            html += '<input type="text" id="edit-audio-device" value="' + escapeHtml(cfg.audio_device || "") + '" placeholder="default">';
            html += '</div>';
        }

        html += '<div class="form-group">';
        html += '<label>Max Quality</label>';
        html += '<select id="edit-quality">' + qualityOptions(cfg.max_quality || "auto") + '</select>';
        if (cfg.quality_source === "auto_fallback") {
            html += '<div class="muted" style="font-size:12px;margin-top:4px;color:#e0a030;">' +
                'This device doesn\'t report its hi-res support, so Auto uses CD (16/44). ' +
                'Pick a quality if it supports better than CD.</div>';
        }
        html += '</div>';

        html += '<div class="button-group">';
        html += '<button id="edit-speaker-submit" onclick="submitEditSpeaker(' + escapeAttr(JSON.stringify(s.id)) + ', ' + escapeAttr(JSON.stringify(s.backend)) + ')">Save</button>';
        html += '<button class="button-secondary" onclick="cancelEdit()">Cancel</button>';
        html += '<button class="button-danger" style="margin-left:auto;" onclick="removeSpeaker(' + escapeAttr(JSON.stringify(s.id)) + ')">Remove</button>';
        html += '</div>';

        html += '</div>';
        return html;
    }

    function updateSpeakers(speakers) {
        if (addPanelOpen) return;
        if (volumeDragging || seekDragging) return;

        // While editing, skip re-render only after the form is in the DOM —
        // otherwise the initial click-to-edit never gets a chance to render it.
        if (editingSpeakerId && document.querySelector(".speaker-edit-card")) return;

        var json = JSON.stringify(speakers);
        if (json === lastSpeakersJson) return;
        lastSpeakersJson = json;

        var container = document.getElementById("speakers-list");

        if (!speakers || speakers.length === 0) {
            container.innerHTML = '<p class="muted">No speakers configured.</p>';
            return;
        }

        var html = "";
        for (var i = 0; i < speakers.length; i++) {
            var s = speakers[i];
            if (s.id === editingSpeakerId) {
                html += renderEditForm(s);
            } else {
                html += renderSpeakerCard(s);
            }
        }
        container.innerHTML = html;
    }

    // -------------------------------------------------------------------------
    // Add speaker flow
    // -------------------------------------------------------------------------

    function showAddSpeaker() {
        addPanelOpen = true;
        selectedBackend = null;
        selectedDevice = null;
        document.getElementById("add-speaker-btn").style.display = "none";

        var panel = document.getElementById("add-speaker-panel");
        panel.style.display = "";
        panel.innerHTML = renderStep1();
    }

    function hideAddSpeaker() {
        addPanelOpen = false;
        selectedBackend = null;
        selectedDevice = null;
        lastSpeakersJson = "";
        var panel = document.getElementById("add-speaker-panel");
        panel.style.display = "none";
        panel.innerHTML = "";
        document.getElementById("add-speaker-btn").style.display = "";
    }

    function renderStep1() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">1</div>';
        html += '<span style="font-weight:600;color:#fff;">Choose backend</span>';
        html += '</div>';

        html += '<div class="backend-cards">';
        html += '<div class="backend-card" id="bc-dlna" onclick="selectBackend(\'dlna\')">';
        html += '<h3>DLNA</h3><p>Sonos, Denon HEOS, and other UPnP/DLNA renderers</p>';
        html += '</div>';
        html += '<div class="backend-card" id="bc-local" onclick="selectBackend(\'local\')">';
        html += '<h3>Local</h3><p>Built-in speakers or headphones via PortAudio</p>';
        html += '</div>';
        html += '</div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="hideAddSpeaker()">Cancel</button>';
        html += '</div>';
        return html;
    }

    function selectBackend(type) {
        selectedBackend = type;
        var panel = document.getElementById("add-speaker-panel");

        if (type === "dlna") {
            panel.innerHTML = renderStep2DLNA();
            startDLNADiscovery();
        } else if (type === "local") {
            panel.innerHTML = renderStep2Local();
            startAudioDeviceDiscovery();
        }
    }

    function renderStep2DLNA() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">2</div>';
        html += '<span style="font-weight:600;color:#fff;">Select DLNA device</span>';
        html += '</div>';

        html += '<div class="scan-status">';
        html += '<span id="scan-status-text">Scanning...</span>';
        html += '<div class="scan-actions">';
        html += '<button id="rescan-btn" class="manual-entry-link" onclick="startDLNADiscovery()" disabled>Rescan</button>';
        html += '<button class="manual-entry-link" onclick="selectManualDevice()">Enter URL manually</button>';
        html += '</div>';
        html += '</div>';

        html += '<div id="device-list" class="device-list"></div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="showAddSpeaker()">Back</button>';
        html += '</div>';
        return html;
    }

    function renderStep2Local() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">2</div>';
        html += '<span style="font-weight:600;color:#fff;">Select audio device</span>';
        html += '</div>';

        html += '<div class="scan-status">';
        html += '<span id="scan-status-text">Scanning...</span>';
        html += '<button class="manual-entry-link" onclick="selectManualDevice()">Enter device name manually</button>';
        html += '</div>';

        html += '<div id="device-list" class="device-list"></div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="showAddSpeaker()">Back</button>';
        html += '</div>';
        return html;
    }

    function startDLNADiscovery() {
        var statusEl = document.getElementById("scan-status-text");
        var listEl = document.getElementById("device-list");
        var rescanBtn = document.getElementById("rescan-btn");
        if (statusEl) statusEl.textContent = "Scanning...";
        if (listEl) {
            listEl.innerHTML = "";
            listEl.removeAttribute("data-devices");
        }
        if (rescanBtn) rescanBtn.disabled = true;

        fetch("api/discover/dlna", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timeout: 5 }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                var rescanBtn = document.getElementById("rescan-btn");
                if (!statusEl || !listEl) return;

                var devices = data.devices || [];
                statusEl.textContent = devices.length + " device" + (devices.length !== 1 ? "s" : "") + " found";

                if (devices.length === 0) {
                    listEl.innerHTML = '<p class="muted" style="margin:0;">No DLNA devices found. Try entering the URL manually.</p>';
                } else {
                    var html = "";
                    for (var i = 0; i < devices.length; i++) {
                        var d = devices[i];
                        var encoded = escapeHtml(JSON.stringify(d).replace(/'/g, "&#39;"));
                        html += '<div class="device-item" id="di-' + i + '" onclick="selectDLNADevice(' + i + ')">';
                        html += '<div class="device-item-name">' + escapeHtml(d.friendly_name || d.name || "Unknown") + '</div>';
                        if (d.location || d.url) {
                            html += '<div class="device-item-detail">' + escapeHtml(d.location || d.url) + '</div>';
                        }
                        html += '</div>';
                    }
                    listEl.innerHTML = html;
                    listEl.setAttribute("data-devices", JSON.stringify(devices));
                }
                if (rescanBtn) rescanBtn.disabled = false;
            })
            .catch(function (err) {
                var statusEl = document.getElementById("scan-status-text");
                var rescanBtn = document.getElementById("rescan-btn");
                if (statusEl) statusEl.textContent = "Discovery failed";
                if (rescanBtn) rescanBtn.disabled = false;
            });
    }

    function startAudioDeviceDiscovery() {
        fetch("api/discover/audio-devices")
            .then(function (r) {
                if (r.status === 404) throw new Error("not_supported");
                return r.json();
            })
            .then(function (data) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                if (!statusEl || !listEl) return;

                var devices = data.devices || [];
                statusEl.textContent = devices.length + " device" + (devices.length !== 1 ? "s" : "") + " found";

                if (devices.length === 0) {
                    listEl.innerHTML = '<p class="muted" style="margin:0;">No audio devices found.</p>';
                    return;
                }

                var html = "";
                for (var i = 0; i < devices.length; i++) {
                    var d = devices[i];
                    html += '<div class="device-item" id="di-' + i + '" onclick="selectLocalDevice(' + i + ')">';
                    html += '<div class="device-item-name">' + escapeHtml(d.name || "Unknown") + '</div>';
                    if (d.info) {
                        html += '<div class="device-item-detail">' + escapeHtml(d.info) + '</div>';
                    }
                    html += '</div>';
                }
                listEl.innerHTML = html;
                listEl.setAttribute("data-devices", JSON.stringify(devices));
            })
            .catch(function (err) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                if (err.message === "not_supported") {
                    if (statusEl) statusEl.textContent = "Not available";
                    if (listEl) listEl.innerHTML = '<p class="muted" style="margin:0;">Local audio backend not installed. Use manual entry.</p>';
                } else {
                    if (statusEl) statusEl.textContent = "Discovery failed";
                }
            });
    }

    function selectDLNADevice(idx) {
        var listEl = document.getElementById("device-list");
        if (!listEl) return;
        var devices = JSON.parse(listEl.getAttribute("data-devices") || "[]");
        var d = devices[idx];
        if (!d) return;

        // Highlight selection
        var items = listEl.querySelectorAll(".device-item");
        for (var i = 0; i < items.length; i++) items[i].classList.remove("selected");
        var el = document.getElementById("di-" + idx);
        if (el) el.classList.add("selected");

        selectedDevice = d;
        showConfigForm("dlna", d);
    }

    function selectLocalDevice(idx) {
        var listEl = document.getElementById("device-list");
        if (!listEl) return;
        var devices = JSON.parse(listEl.getAttribute("data-devices") || "[]");
        var d = devices[idx];
        if (!d) return;

        var items = listEl.querySelectorAll(".device-item");
        for (var i = 0; i < items.length; i++) items[i].classList.remove("selected");
        var el = document.getElementById("di-" + idx);
        if (el) el.classList.add("selected");

        selectedDevice = d;
        showConfigForm("local", d);
    }

    function selectManualDevice() {
        showConfigForm(selectedBackend, null);
    }

    function showConfigForm(backend, device) {
        var panel = document.getElementById("add-speaker-panel");
        if (!panel) return;

        var html = '<div class="add-step-header">';
        html += '<div class="step-number">3</div>';
        html += '<span style="font-weight:600;color:#fff;">Configure speaker</span>';
        html += '</div>';

        var rawName = device ? (device.friendly_name || "") : "";
        // If friendly_name looks like it contains an IP, prefer model_name
        var defaultName = (/\d+\.\d+\.\d+\.\d+/.test(rawName) && device && device.model_name)
            ? device.model_name : rawName;
        var defaultIp = device ? (device.ip || "") : "";
        var defaultPort = device ? (device.port || 1400) : 1400;
        var defaultUrl = device ? (device.location || "") : "";
        var defaultDevice = device ? (device.name || "") : "";

        html += '<div class="form-group">';
        html += '<label>Speaker name</label>';
        html += '<input type="text" id="new-speaker-name" value="' + escapeHtml(defaultName) + '" placeholder="My Speaker">';
        html += '</div>';

        if (backend === "dlna") {
            html += '<div class="form-row">';
            html += '<div class="form-group" style="flex:2;"><label>IP Address</label>';
            html += '<input type="text" id="new-dlna-ip" value="' + escapeHtml(defaultIp) + '" placeholder="192.168.1.50"></div>';
            html += '<div class="form-group" style="flex:1;"><label>Port</label>';
            html += '<input type="text" id="new-dlna-port" value="' + defaultPort + '"></div>';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Description URL <span style="color:#666">(optional — auto-discovered if empty)</span></label>';
            html += '<input type="text" id="new-dlna-url" value="' + escapeHtml(defaultUrl) + '" placeholder="Leave empty for auto-discovery">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="new-fixed-vol" style="width:auto;display:inline;margin-right:6px;"> Fixed volume</label></div>';
        } else if (backend === "local") {
            html += '<div class="form-group">';
            html += '<label>Audio device (leave blank for default)</label>';
            html += '<input type="text" id="new-audio-device" value="' + escapeHtml(defaultDevice) + '" placeholder="default">';
            html += '</div>';
        }

        html += '<div class="form-group">';
        html += '<label>Max quality</label>';
        html += '<select id="new-quality">' + qualityOptions("auto") + '</select>';
        html += '</div>';

        html += '<div class="button-group">';
        html += '<button id="add-speaker-submit" onclick="submitAddSpeaker()">Add Speaker</button>';
        html += '<button class="button-secondary" onclick="' + (backend === "dlna" ? "selectBackend(\'dlna\')" : "selectBackend(\'local\')") + '">Back</button>';
        html += '<button class="button-secondary" onclick="hideAddSpeaker()">Cancel</button>';
        html += '</div>';

        panel.innerHTML = html;
    }

    function submitAddSpeaker() {
        var nameEl = document.getElementById("new-speaker-name");
        var name = nameEl ? nameEl.value.trim() : "";
        if (!name) {
            showError("Speaker name is required.");
            return;
        }

        var quality = document.getElementById("new-quality");
        var payload = {
            name: name,
            backend: selectedBackend,
            max_quality: quality ? quality.value : "auto",
        };

        if (selectedBackend === "dlna") {
            var ipEl = document.getElementById("new-dlna-ip");
            var ip = ipEl ? ipEl.value.trim() : "";
            if (!ip) {
                showError("IP address is required.");
                return;
            }
            payload.dlna_ip = ip;
            payload.dlna_port = parseInt(document.getElementById("new-dlna-port").value) || 1400;
            var urlEl = document.getElementById("new-dlna-url");
            payload.description_url = urlEl ? urlEl.value.trim() : "";
            var fixedVolEl = document.getElementById("new-fixed-vol");
            payload.fixed_volume = fixedVolEl ? fixedVolEl.checked : false;
        } else if (selectedBackend === "local") {
            var devEl = document.getElementById("new-audio-device");
            payload.audio_device = devEl ? devEl.value.trim() : "";
        }

        var submitBtn = document.getElementById("add-speaker-submit");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Adding…";
        }

        fetch("api/speakers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to add speaker"); });
                }
                return r.json();
            })
            .then(function () {
                hideAddSpeaker();
                lastSpeakersJson = null;
                fetchStatus();
            })
            .catch(function (err) {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Add Speaker";
                }
                showError(err.message);
            });
    }

    // -------------------------------------------------------------------------
    // Edit speaker
    // -------------------------------------------------------------------------

    function editSpeaker(id) {
        editingSpeakerId = id;
        lastSpeakersJson = null;
        fetchStatus();
    }

    function cancelEdit() {
        editingSpeakerId = null;
        lastSpeakersJson = null;
        fetchStatus();
    }

    function submitEditSpeaker(id, backend) {
        var nameEl = document.getElementById("edit-name");
        var name = nameEl ? nameEl.value.trim() : "";
        if (!name) {
            showError("Speaker name is required.");
            return;
        }

        var quality = document.getElementById("edit-quality");
        var payload = {
            name: name,
            max_quality: quality ? quality.value : "auto",
        };

        if (backend === "dlna") {
            var ipEl = document.getElementById("edit-dlna-ip");
            var portEl = document.getElementById("edit-dlna-port");
            var urlEl = document.getElementById("edit-dlna-url");
            var ip = ipEl ? ipEl.value.trim() : "";
            if (!ip) {
                showError("IP address is required.");
                return;
            }
            payload.dlna_ip = ip;
            payload.dlna_port = portEl ? (parseInt(portEl.value) || 1400) : 1400;
            payload.description_url = urlEl ? urlEl.value.trim() : "";
            var fixedVolEl = document.getElementById("edit-fixed-vol");
            if (fixedVolEl) payload.fixed_volume = fixedVolEl.checked;
        } else if (backend === "local") {
            var devEl = document.getElementById("edit-audio-device");
            payload.audio_device = devEl ? devEl.value.trim() : "";
        }

        var submitBtn = document.getElementById("edit-speaker-submit");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Saving…";
        }

        fetch("api/speakers/" + encodeURIComponent(id), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to update speaker"); });
                }
                return r.json();
            })
            .then(function (data) {
                editingSpeakerId = null;
                lastSpeakersJson = null;
                if (data && data.warning) {
                    showError(data.warning);
                }
                fetchStatus();
            })
            .catch(function (err) {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Save";
                }
                showError(err.message);
            });
    }

    // -------------------------------------------------------------------------
    // Remove speaker
    // -------------------------------------------------------------------------

    function removeSpeaker(id) {
        if (!confirm("Remove this speaker?")) return;

        fetch("api/speakers/" + encodeURIComponent(id), { method: "DELETE" })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to remove speaker"); });
                }
            })
            .then(function () {
                editingSpeakerId = null;
                lastSpeakersJson = null;
                fetchStatus();
            })
            .catch(function (err) {
                showError(err.message);
            });
    }

    // -------------------------------------------------------------------------
    // System info
    // -------------------------------------------------------------------------

    function updateSystemInfo(system) {
        if (!system) return;
        var versionLabel = system.version ? "v" + system.version : "--";
        if (system.version && system.commit) {
            versionLabel += " (" + system.commit + ")";
        }
        document.getElementById("system-version").textContent = versionLabel;
        document.getElementById("system-uptime").textContent = system.uptime || "--";
    }

    // -------------------------------------------------------------------------
    // Polling
    // -------------------------------------------------------------------------

    function fetchStatus() {
        fetch("api/status")
            .then(function (response) {
                if (!response.ok) throw new Error("HTTP " + response.status);
                return response.json();
            })
            .then(function (data) {
                var auth = data.auth || {};

                if (auth.authenticated) {
                    var displayName = auth.name || auth.email || "User " + auth.user_id;
                    document.getElementById("auth-name").textContent = displayName;
                    document.getElementById("auth-email").textContent = auth.email && auth.name ? auth.email : "";
                    var avatarEl = document.getElementById("auth-avatar");
                    if (auth.avatar) {
                        avatarEl.src = auth.avatar;
                        avatarEl.style.display = "";
                    } else {
                        avatarEl.style.display = "none";
                    }
                    showAuthState("connected");
                } else {
                    showAuthState("disconnected");
                }

                if (auth.authenticated && data.speakers) {
                    updateSpeakers(data.speakers);
                } else if (!auth.authenticated) {
                    lastSpeakersJson = null;
                    document.getElementById("speakers-list").innerHTML =
                        '<p class="muted">Waiting for authentication...</p>';
                }

                updateSystemInfo({ version: data.version, commit: data.commit, uptime: data.uptime });
            })
            .catch(function () {
                // Silently ignore fetch errors (server may be restarting)
            });
    }

    // -------------------------------------------------------------------------
    // Global exports
    // -------------------------------------------------------------------------

    window.startLogin = startLogin;
    window.logout = logout;
    window.showAddSpeaker = showAddSpeaker;
    window.hideAddSpeaker = hideAddSpeaker;
    window.selectBackend = selectBackend;
    window.selectDLNADevice = selectDLNADevice;
    window.selectLocalDevice = selectLocalDevice;
    window.selectManualDevice = selectManualDevice;
    window.startDLNADiscovery = startDLNADiscovery;
    window.submitAddSpeaker = submitAddSpeaker;
    window.editSpeaker = editSpeaker;
    window.cancelEdit = cancelEdit;
    window.submitEditSpeaker = submitEditSpeaker;
    window.removeSpeaker = removeSpeaker;
    window.speakerControl = speakerControl;
    window.beginVolumeDrag = beginVolumeDrag;
    window.previewVolume = previewVolume;
    window.commitVolume = commitVolume;
    window.toggleMute = toggleMute;
    window.beginSeekDrag = beginSeekDrag;
    window.previewSeek = previewSeek;
    window.commitSeek = commitSeek;

    // Show OAuth error if redirected back with one
    (function checkOAuthError() {
        var params = new URLSearchParams(window.location.search);
        var error = params.get("error");
        if (error) {
            var messages = {
                missing_code: "Login was cancelled or the authorization code was missing.",
                exchange_failed: "Failed to exchange authorization code. Please try again.",
                auth_failed: "Authentication failed. Please try again.",
            };
            var errorEl = document.getElementById("login-error");
            errorEl.textContent = messages[error] || "Login failed. Please try again.";
            errorEl.style.display = "";
            // Clean up URL
            window.history.replaceState({}, "", "/");
        }
    })();

    // Start polling on page load
    fetchStatus();
    pollTimer = setInterval(fetchStatus, 1000);
})();
