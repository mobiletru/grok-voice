/* Grok Voice frontend. ES5-friendly, no frameworks, no MediaRecorder, no fetch of absolute paths.
   All URLs are relative to the current page so it works under the HA ingress prefix. */
(function () {
  'use strict';
  // Ingress URLs must end with "/" so relative URLs resolve inside the ingress prefix.
  if (location.pathname.charAt(location.pathname.length - 1) !== '/') {
    location.replace(location.pathname + '/' + location.search + location.hash);
    return;
  }

  var RATE = 24000;
  var $ = function (id) { return document.getElementById(id); };
  var elStatus = $('status'), elMeta = $('meta'), elBanner = $('banner'), elTr = $('transcript'),
      elTalk = $('talk'), elTalkLabel = $('talk-label'), elHF = $('handsfree'), elStop = $('stop'),
      elClear = $('clear'), elForm = $('typed'), elInput = $('typed-input');

  var AC = window.AudioContext || window.webkitAudioContext;
  var hasMic = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  var isSecure = window.isSecureContext !== false;

  var ws = null, wsReady = false, connecting = false;
  var actx = null, micStream = null, micNode = null, micSrc = null, sink = null;
  var state = 'off';          // off | connecting | idle | listening | thinking | speaking
  var handsFree = false;
  var micOpen = false;        // is mic audio being sent
  var playTime = 0, playing = 0, sources = [];
  var liveYou = null, liveGrok = null;
  var cfg = {};

  function wsUrl(mode) {
    var u = new URL('ws?mode=' + mode, location.href);   // relative -> stays under ingress prefix
    u.protocol = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    return u.toString();
  }

  function banner(msg, kind) {
    if (!msg) { elBanner.hidden = true; return; }
    elBanner.textContent = msg; elBanner.className = kind || ''; elBanner.hidden = false;
  }

  function setState(s, text) {
    state = s;
    var map = {
      off: ['Tap the button to start', 'TAP TO TALK', 'off'],
      connecting: ['Connecting…', 'CONNECTING…', 'thinking'],
      idle: ['Ready', 'TAP TO TALK', ''],
      listening: ['Listening… tap to send', 'LISTENING\nTAP TO SEND', 'listening'],
      thinking: ['Thinking…', 'THINKING…', 'thinking'],
      speaking: ['Grok is speaking', 'TAP TO INTERRUPT', 'speaking']
    };
    var m = map[s];
    elStatus.textContent = text || m[0];
    elTalkLabel.textContent = m[1].replace('\n', ' — ');
    elTalk.className = m[2];
    elStop.hidden = !(s === 'speaking' || s === 'thinking');
    if (s === 'idle' && handsFree) { elStatus.textContent = 'Listening (hands-free)'; }
  }

  function addLine(cls, text) {
    var hint = $('hint'); if (hint) { hint.parentNode.removeChild(hint); }
    var p = document.createElement('p'); p.className = cls; p.textContent = text;
    elTr.appendChild(p); elTr.scrollTop = elTr.scrollHeight; return p;
  }
  function scrollDown() { elTr.scrollTop = elTr.scrollHeight; }

  // ---------------------------------------------------------------- audio out
  function stopPlayback() {
    for (var i = 0; i < sources.length; i++) { try { sources[i].onended = null; sources[i].stop(0); } catch (e) {} }
    sources = []; playing = 0; playTime = 0;
  }
  function playChunk(buf) {
    if (!actx) return;
    var n = buf.byteLength >> 1; if (!n) return;
    var i16 = new Int16Array(buf.slice(0, n * 2));
    var ab = actx.createBuffer(1, n, RATE);
    var ch = ab.getChannelData(0);
    for (var i = 0; i < n; i++) ch[i] = i16[i] / 32768;
    var src = actx.createBufferSource(); src.buffer = ab; src.connect(actx.destination);
    var now = actx.currentTime;
    if (playTime < now + 0.05) playTime = now + 0.05;
    src.start(playTime); playTime += ab.duration;
    playing++; sources.push(src);
    src.onended = function () {
      playing--; var k = sources.indexOf(src); if (k >= 0) sources.splice(k, 1);
      if (playing <= 0 && state === 'speaking') { playing = 0; afterSpeech(); }
    };
    if (state !== 'speaking' && state !== 'listening') setState('speaking');
    else if (state === 'listening' && handsFree) setState('speaking');
  }
  function afterSpeech() { setState(handsFree && micOpen ? 'idle' : 'idle'); }

  // ----------------------------------------------------------------- mic in
  function downsampleToPcm16(f32, inRate) {
    var ratio = inRate / RATE, outLen = Math.floor(f32.length / ratio), out = new Int16Array(outLen), pos = 0;
    for (var i = 0; i < outLen; i++) {
      var next = Math.floor((i + 1) * ratio), sum = 0, cnt = 0;
      for (; pos < next && pos < f32.length; pos++) { sum += f32[pos]; cnt++; }
      var v = cnt ? sum / cnt : 0; v = Math.max(-1, Math.min(1, v));
      out[i] = v < 0 ? v * 32768 : v * 32767;
    }
    return out;
  }
  function onMicData(e) {
    if (!micOpen || !wsReady || ws.readyState !== 1) return;
    // Half-duplex: while Grok is talking, don't stream mic (car speakers would echo into the mic).
    if (state === 'speaking' || state === 'thinking') return;
    var pcm = downsampleToPcm16(e.inputBuffer.getChannelData(0), actx.sampleRate);
    if (pcm.length) ws.send(pcm.buffer);
  }
  function openMic(cb) {
    if (micStream) { cb(null); return; }
    if (!hasMic) { cb(new Error('nomic')); return; }
    navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(function (stream) {
        micStream = stream;
        micSrc = actx.createMediaStreamSource(stream);
        micNode = actx.createScriptProcessor(4096, 1, 1);
        micNode.onaudioprocess = onMicData;
        sink = actx.createGain(); sink.gain.value = 0;      // keep node alive without playing the mic
        micSrc.connect(micNode); micNode.connect(sink); sink.connect(actx.destination);
        cb(null);
      })
      .catch(function (err) { cb(err); });
  }
  function micError(err) {
    var name = err && err.name || '';
    var msg;
    if (err && err.message === 'nomic' || !hasMic) {
      msg = isSecure
        ? 'This browser does not expose a microphone API. You can still use the typed message box below.'
        : 'Microphone needs HTTPS. Open this page through your https:// Home Assistant address. Typed messages still work.';
    } else if (name === 'NotAllowedError' || name === 'SecurityError') {
      msg = 'Microphone blocked. Home Assistant shows this page in an iframe, and the browser or Tesla may refuse mic access there. Allow the microphone if prompted, or open the app’s direct HTTPS page (see the app documentation). Typed messages still work.';
    } else if (name === 'NotFoundError') {
      msg = 'No microphone found. Typed messages still work.';
    } else {
      msg = 'Could not start the microphone (' + (name || 'unknown error') + '). Typed messages still work.';
    }
    banner(msg);
  }

  // ---------------------------------------------------------------- websocket
  function ensureAudio() {
    if (!AC) { return false; }
    if (!actx) { try { actx = new AC(); } catch (e) { return false; } }
    if (actx.state === 'suspended' && actx.resume) actx.resume();
    return true;
  }
  function connect(cb) {
    if (ws && ws.readyState === 1 && wsReady) { cb && cb(); return; }
    if (connecting) return;
    connecting = true; wsReady = false; setState('connecting');
    try { ws = new WebSocket(wsUrl(handsFree ? 'vad' : 'ptt')); }
    catch (e) { connecting = false; setState('off'); banner('Could not open a WebSocket: ' + e.message); return; }
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function (ev) {
      if (typeof ev.data !== 'string') { if (actx) playChunk(ev.data); return; }
      var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      onServer(m, cb);
    };
    ws.onclose = function () {
      var was = wsReady; connecting = false; wsReady = false; micOpen = false;
      stopPlayback(); liveYou = liveGrok = null;
      if (state !== 'off') setState('off', was ? 'Disconnected — tap to reconnect' : 'Not connected');
    };
    ws.onerror = function () { /* onclose follows */ };
  }
  function disconnect() { try { if (ws) ws.close(); } catch (e) {} }

  function onServer(m, cb) {
    switch (m.type) {
      case 'ready': connecting = false; wsReady = true; banner(''); setState('idle'); cb && cb(); break;
      case 'draft': showDraft(m.draft); break;
      case 'draft_result': draftResult(m); break;
      case 'notice': banner(m.message, 'info'); break;
      case 'error': connecting = false; banner(m.message); if (m.code !== 'upstream_event') setState('off', 'Not connected'); else if (state === 'thinking') setState('idle'); break;
      case 'thinking': if (state !== 'speaking') setState('thinking'); break;
      case 'speech_started': stopPlayback(); setState('listening'); break;
      case 'speech_stopped': setState('thinking'); break;
      case 'user_partial': if (!liveYou) liveYou = addLine('you live', ''); liveYou.textContent = 'You: ' + m.text; scrollDown(); break;
      case 'user_final':
        if (!liveYou) liveYou = addLine('you', '');
        liveYou.className = 'you'; liveYou.textContent = 'You: ' + m.text; liveYou = null; scrollDown(); break;
      case 'assistant_delta':
        if (!liveGrok) liveGrok = addLine('grok live', 'Grok: ');
        liveGrok.textContent += m.text; scrollDown(); break;
      case 'assistant_done':
        if (liveGrok) { liveGrok.className = 'grok'; liveGrok = null; }
        if (playing <= 0 && (state === 'thinking' || state === 'speaking')) setState('idle');
        break;
    }
  }

  // ------------------------------------------------------------ invoice drafts
  // Drafts are rendered with textContent only (model text is untrusted).
  var elDrafts = $('drafts');
  function usd(n) { return n === null || n === undefined ? 'TBD' : '$' + Number(n).toFixed(2); }
  function td(tr, text, cls) { var c = document.createElement('td'); c.textContent = text; if (cls) c.className = cls; tr.appendChild(c); }
  function showDraft(d) {
    var box = document.createElement('div'); box.className = 'draft'; box.id = 'draft-' + d.id;
    var h = document.createElement('h2');
    h.textContent = 'DRAFT INVOICE - Unit ' + d.unit_number + ' - ' + d.customer + (d.po_number ? ' - PO ' + d.po_number : '');
    box.appendChild(h);
    var ai = document.createElement('div'); ai.className = 'aimark'; ai.textContent = 'AI-CREATED: ' + (d.ai_marker || 'Created by Grok Voice (AI) - review before sending'); box.appendChild(ai);
    var t = document.createElement('table'), tr;
    tr = document.createElement('tr'); ['Code / work', 'Hrs', 'Rate', 'Amount'].forEach(function (x, i) { var c = document.createElement('th'); c.textContent = x; if (i) c.className = 'n'; tr.appendChild(c); }); t.appendChild(tr);
    d.labor_lines.forEach(function (l) { tr = document.createElement('tr'); td(tr, l.code + ' - ' + l.description); td(tr, l.hours, 'n'); td(tr, usd(l.rate), 'n'); td(tr, usd(l.amount), 'n'); t.appendChild(tr); });
    d.parts.forEach(function (p) { tr = document.createElement('tr'); td(tr, (p.part_number ? p.part_number + ' ' : '') + p.description + ' x' + p.quantity); td(tr, '', 'n'); td(tr, usd(p.unit_price), 'n'); td(tr, usd(p.amount), 'n'); t.appendChild(tr); });
    box.appendChild(t);
    var s = document.createElement('div');
    s.textContent = 'Labor ' + usd(d.labor_subtotal) + '  |  Parts ' + usd(d.parts_subtotal) + '  |  Tax (' + (d.tax_rate * 100).toFixed(2) + '% parts only) ' + usd(d.tax);
    box.appendChild(s);
    var tot = document.createElement('div'); tot.className = 'tot'; tot.textContent = 'TOTAL ' + usd(d.total); box.appendChild(tot);
    if (d.open_items && d.open_items.length) { var o = document.createElement('div'); o.className = 'open'; o.textContent = 'Still missing: ' + d.open_items.join('; '); box.appendChild(o); }
    var note = document.createElement('div'); note.textContent = d.save_blockers && d.save_blockers.length ? 'Approving will NOT save: ' + d.save_blockers.join('; ') + '. Ask Grok for a new draft once that is fixed.' : 'Approve saves it to Wrenchworks as an UNSENT draft. Nothing is emailed. If anything goes wrong, ask Grok for a brand new draft.'; box.appendChild(note);
    var b = document.createElement('div'); b.className = 'btns';
    var ok = document.createElement('button'); ok.type = 'button'; ok.className = 'approve'; ok.textContent = 'APPROVE';
    var no = document.createElement('button'); no.type = 'button'; no.className = 'reject'; no.textContent = 'DISCARD';
    ok.onclick = function () { ok.disabled = true; ws.send(JSON.stringify({ type: 'approve', id: d.id })); };
    no.onclick = function () { ws.send(JSON.stringify({ type: 'reject', id: d.id })); };
    b.appendChild(ok); b.appendChild(no); box.appendChild(b);
    var r = document.createElement('div'); r.className = 'result'; r.id = 'result-' + d.id; box.appendChild(r);
    elDrafts.hidden = false; elDrafts.insertBefore(box, elDrafts.firstChild);
  }
  function draftResult(m) {
    var r = $('result-' + m.id); if (r) r.textContent = m.message || '';
    var box = $('draft-' + m.id);
    if (box && (m.status === 'saved' || m.status === 'rejected' || m.status === 'not_saved' || m.status === 'save_failed' || m.status === 'duplicate')) {
      var bt = box.getElementsByTagName('button'); for (var i = 0; i < bt.length; i++) bt[i].disabled = true;
    } else if (box) { var ba = box.getElementsByClassName('approve'); if (ba[0]) ba[0].disabled = false; }
  }

  // ------------------------------------------------------------------ actions
  function startListening() {
    banner('');
    stopPlayback();
    if (ws && wsReady) ws.send(JSON.stringify({ type: 'interrupt' }));
    openMic(function (err) {
      if (err) { micError(err); setState('idle'); return; }
      if (ws && wsReady) ws.send(JSON.stringify({ type: 'clear' }));
      micOpen = true; setState('listening');
    });
  }
  function finishListening() {
    micOpen = false;
    if (ws && wsReady) { ws.send(JSON.stringify({ type: 'commit' })); setState('thinking'); }
  }

  elTalk.addEventListener('click', function () {
    if (!isSecure) { banner('This page is not on HTTPS, so the microphone is blocked. Use your https:// Home Assistant address.'); }
    if (!ensureAudio()) { banner('This browser has no Web Audio support, so Grok cannot speak. Typed messages will show text replies only.'); }
    if (state === 'off') { connect(function () { if (!handsFree) startListening(); else beginHandsFree(); }); return; }
    if (state === 'connecting') return;
    if (handsFree) { if (state === 'speaking') { doStop(); } return; }
    if (state === 'listening') finishListening();
    else startListening();      // idle / thinking / speaking (interrupt)
  });

  function beginHandsFree() {
    openMic(function (err) {
      if (err) { micError(err); handsFree = false; syncHF(); setState('idle'); return; }
      micOpen = true; setState('idle');
    });
  }
  function syncHF() { elHF.textContent = 'Hands-free: ' + (handsFree ? 'ON' : 'OFF'); elHF.setAttribute('aria-pressed', handsFree ? 'true' : 'false'); }
  elHF.addEventListener('click', function () {
    handsFree = !handsFree; syncHF(); micOpen = false;
    ensureAudio();
    // Turn-detection mode is chosen at connect time, so reconnect.
    disconnect();
    setTimeout(function () { setState('off'); connect(function () { if (handsFree) beginHandsFree(); }); }, 150);
  });

  function doStop() { stopPlayback(); if (ws && wsReady) ws.send(JSON.stringify({ type: 'interrupt' })); setState('idle'); }
  elStop.addEventListener('click', doStop);
  elClear.addEventListener('click', function () { elTr.innerHTML = ''; /* drafts stay */ liveYou = liveGrok = null; });

  elForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var text = elInput.value.replace(/^\s+|\s+$/g, ''); if (!text) return;
    ensureAudio(); banner('');
    function send() {
      addLine('you', 'You: ' + text); elInput.value = '';
      ws.send(JSON.stringify({ type: 'text', text: text })); setState('thinking');
    }
    if (ws && wsReady) send(); else connect(send);
  });

  // -------------------------------------------------------------------- init
  if (typeof WebSocket === 'undefined') { banner('This browser does not support WebSocket.'); }
  if (!isSecure) banner('Not a secure (HTTPS) page: the microphone will be blocked. Typed messages still work.');
  else if (!hasMic) banner('Microphone API not available in this browser. Typed messages still work.', 'info');
  if (!AC) banner('No Web Audio support: Grok replies will be text only.', 'info');

  var xhr = new XMLHttpRequest();
  xhr.open('GET', 'api/config');
  xhr.onload = function () {
    try { cfg = JSON.parse(xhr.responseText); } catch (e) { return; }
    elMeta.textContent = cfg.model + ' · ' + cfg.voice;
    if (!cfg.has_key) banner('No xAI API key is set. Add it in the Grok Voice app Configuration, then restart the app.');
    if (cfg.invoicing) loadCatalogStatus();
  };
  xhr.send();
  function loadCatalogStatus() {
    var el = document.getElementById('catalog'); if (!el) return;
    el.textContent = 'Labor codes: checking Wrenchworks…'; el.className = 'sample'; el.hidden = false;
    var x = new XMLHttpRequest(); x.open('GET', 'api/catalog-status');
    x.onload = function () {
      var s; try { s = JSON.parse(x.responseText); } catch (e) { el.textContent = 'Labor codes: status unavailable.'; el.className = 'error'; return; }
      el.textContent = s.message || ''; el.className = s.state === 'loaded' ? '' : (s.state === 'sample' ? 'sample' : 'error'); el.hidden = !s.message;
    };
    x.onerror = function () { el.textContent = 'Labor codes: status unavailable (add-on not reachable).'; el.className = 'error'; };
    x.send();
  }
  setState('off');
})();
