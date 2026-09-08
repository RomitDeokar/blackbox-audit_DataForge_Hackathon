/* BlackBox Audit demo console.
 *
 * The browser owns only playback and rendering. Every decision shown -- the
 * commit, the rollback, the mismatch verdict -- is computed server-side by the
 * real AudioFence and the real SQLite store; this file just reports playback
 * position to the API and draws the answer.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- helpers

  var $ = function (id) { return document.getElementById(id); };

  function api(path, body) {
    var opts = body === undefined
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    return fetch(path, opts).then(function (r) {
      return r.text().then(function (txt) {
        var data;
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { throw new Error('bad response: ' + txt.slice(0, 200)); }
        if (!r.ok || data.error) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmt(n, d) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toFixed(d == null ? 3 : d);
  }

  function statusBadge(status) {
    var cls = status === 'COMMITTED' ? 'ok'
      : status === 'ROLLED_BACK' ? 'err'
      : status === 'PENDING_AUDIO' ? 'pend' : 'mute';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }

  function table(el, cols, rows, rowClass) {
    var head = '<thead><tr>' + cols.map(function (c) { return '<th>' + esc(c.label) + '</th>'; }).join('') + '</tr></thead>';
    var body = rows.length
      ? rows.map(function (row) {
          var cls = rowClass ? rowClass(row) : '';
          return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>' +
            cols.map(function (c) { return '<td>' + (c.render ? c.render(row) : esc(row[c.key])) + '</td>'; }).join('') +
            '</tr>';
        }).join('')
      : '<tr><td colspan="' + cols.length + '" class="muted">No rows.</td></tr>';
    el.innerHTML = head + '<tbody>' + body + '</tbody>';
  }

  // ---------------------------------------------------------------- state

  var TL = null;            // word timeline from the server
  var CFG = null;           // provider/config payload
  var variant = 'fenced';
  var call = null;          // { session_id, ... }
  var raf = null;
  var playStart = 0;
  var playPos = 0;
  var speed = 1;
  var utterance = null;
  var feedTimer = null;

  // ---------------------------------------------------------------- tabs

  Array.prototype.forEach.call($('tabs').querySelectorAll('button'), function (btn) {
    btn.addEventListener('click', function () {
      Array.prototype.forEach.call($('tabs').querySelectorAll('button'), function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      Array.prototype.forEach.call(document.querySelectorAll('section.tab'), function (s) { s.classList.remove('active'); });
      $('tab-' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'audit') loadDb();
      if (btn.dataset.tab === 'mobile') { setupMobile(); loadFeed(); }
    });
  });

  // ---------------------------------------------------------------- timeline viz

  function drawTimeline() {
    if (!TL) return;
    var wordsEl = $('tlWords');
    var axisEl = $('tlAxis');
    var dur = TL.duration_s || 1;
    var pct = function (t) { return (t / dur) * 100; };

    wordsEl.innerHTML = TL.words.map(function (w, i) {
      var left = pct(w.start);
      var width = Math.max(pct(w.end - w.start), 1.4);
      return '<div class="tl-word' + (w.is_gating ? ' gating' : '') + '" data-i="' + i + '" ' +
        'style="left:' + left + '%;width:' + width + '%" title="' + esc(w.text) + ' [' + fmt(w.start) + 's → ' + fmt(w.end) + 's]">' +
        esc(w.text) + '</div>';
    }).join('');

    var ticks = [];
    for (var t = 0; t <= dur + 0.001; t += 0.5) ticks.push(t);
    axisEl.innerHTML = ticks.map(function (t) {
      return '<div class="tl-tick" style="left:' + pct(t) + '%">' + t.toFixed(1) + 's</div>';
    }).join('');

    var gate = $('tlGate');
    gate.style.left = pct(TL.gating_word_end_s) + '%';
    gate.style.display = 'block';
  }

  function paintHeard(position, cancelledAt) {
    if (!TL) return;
    var dur = TL.duration_s || 1;
    var cutoff = cancelledAt != null ? cancelledAt : position;

    Array.prototype.forEach.call($('tlWords').children, function (el) {
      var w = TL.words[+el.dataset.i];
      el.classList.toggle('heard', w.end <= cutoff + 1e-9);
    });

    var cursor = $('tlCursor');
    cursor.style.display = 'block';
    cursor.style.left = Math.min((position / dur) * 100, 100) + '%';

    var heardWords = TL.words.filter(function (w) { return w.end <= cutoff + 1e-9; });
    var box = $('heardBox');
    if (!heardWords.length) {
      box.className = 'heard-box empty';
      box.textContent = '— nothing heard yet —';
    } else {
      box.className = 'heard-box';
      var heardTxt = heardWords.map(function (w) { return esc(w.text); }).join(' ');
      var restTxt = TL.words.filter(function (w) { return w.end > cutoff + 1e-9; })
        .map(function (w) { return esc(w.text); }).join(' ');
      box.innerHTML = heardTxt + (restTxt ? ' <span class="unheard">' + restTxt + '</span>' : '');
    }

    $('playBar').style.width = Math.min((position / dur) * 100, 100) + '%';
    $('playClock').textContent = fmt(position) + 's / ' + fmt(dur) + 's' +
      (cancelledAt != null ? '  · cut at ' + fmt(cancelledAt) + 's' : '');
  }

  // ---------------------------------------------------------------- variant toggle

  function setVariant(v) {
    variant = v;
    Array.prototype.forEach.call($('variantToggle').querySelectorAll('button'), function (b) {
      b.className = b.dataset.variant === v ? ('on-' + v) : '';
    });
    $('variantNote').innerHTML = v === 'naive'
      ? '<strong>Deliberately broken baseline.</strong> Calls <code>book_naive()</code> the instant the LLM tool call executes — the row is COMMITTED before a single word has left the speaker. This is how most tool-calling voice agents are written.'
      : '<strong>The fix.</strong> The tool call only creates a <code>PENDING_AUDIO</code> row. It is promoted to COMMITTED only once the word-timestamp stream proves “confirmed” fully played, and rolled back if playback is cut first.';
  }

  Array.prototype.forEach.call($('variantToggle').querySelectorAll('button'), function (btn) {
    btn.addEventListener('click', function () { if (!call) setVariant(btn.dataset.variant); });
  });

  // ---------------------------------------------------------------- speech

  function speechSupported() { return 'speechSynthesis' in window; }

  function speak(text) {
    if (!$('inVoice').checked || !speechSupported()) return;
    try {
      window.speechSynthesis.cancel();
      utterance = new SpeechSynthesisUtterance(text);
      // The cached Rime timeline is the clock; nudge the synthesiser toward the
      // same total duration so what you hear lines up with what is scored.
      utterance.rate = Math.max(0.5, Math.min(1.6, speed));
      utterance.pitch = 1.0;
      window.speechSynthesis.speak(utterance);
    } catch (e) { /* speech is a nicety, never a dependency */ }
  }

  function stopSpeech() {
    if (speechSupported()) { try { window.speechSynthesis.cancel(); } catch (e) {} }
  }

  // ---------------------------------------------------------------- call flow

  function tick() {
    var elapsed = ((performance.now() - playStart) / 1000) * speed;
    playPos = elapsed;
    var dur = TL.duration_s;

    if (playPos >= dur) {
      playPos = dur;
      paintHeard(playPos, null);
      finishCall();
      return;
    }
    paintHeard(playPos, null);
    raf = requestAnimationFrame(tick);
  }

  function renderCall(data) {
    call = data;
    $('callStateBadge').className = 'badge ' + (data.resolved ? (data.mismatch ? 'err' : 'ok') : 'pend');
    $('callStateBadge').textContent = data.resolved
      ? (data.variant + ' · resolved')
      : (data.variant + ' · in flight');

    $('stDb').textContent = data.db_status;
    $('stDb').className = 'v ' + (data.db_status === 'COMMITTED' ? 'good' : data.db_status === 'ROLLED_BACK' ? 'bad' : 'warn');
    $('stDbSub').textContent = data.booking_id != null ? ('row #' + data.booking_id + ' · ' + data.variant) : 'no booking yet';

    $('stHeard').textContent = data.resolved ? (data.expected_committed ? 'YES' : 'NO') : '…';
    $('stHeard').className = 'v ' + (data.expected_committed ? 'good' : 'bad');

    $('stFence').textContent = data.fence_outcome || (data.variant === 'naive' ? 'no fence' : '…');
    $('stFence').className = 'v ' + (data.fence_outcome === 'CANCELLED_BEFORE_GATING_WORD' ? 'bad' : data.fence_outcome ? 'good' : 'warn');
    $('stFenceSub').textContent = data.fence_state ? ('state ' + data.fence_state) : 'naive agent has no fence';

    if (data.resolution_latency_ms != null) {
      $('latencyChip').textContent = 'resolved in ' + fmt(data.resolution_latency_ms, 2) + ' ms';
    }

    $('eventLog').innerHTML = (data.events || []).map(function (e) {
      return '<div class="event ' + esc(e.kind) + '">' +
        '<div class="t">' + (e.at_s != null ? fmt(e.at_s) + 's' : '—') + '</div>' +
        '<div class="d">' + esc(e.detail) + '</div></div>';
    }).join('') || '<div class="small muted">No events.</div>';

    if (data.resolved) {
      var vb = $('verdictBadge');
      vb.className = 'badge ' + (data.mismatch ? 'err' : 'ok');
      vb.textContent = data.mismatch ? 'STATE MISMATCH' : 'state matches the ear';

      var note;
      if (data.mismatch) {
        note = '<div class="note warn"><strong>Phantom booking.</strong> The database says <code>' +
          esc(data.db_status) + '</code> but the caller only heard “' + esc(data.heard_text || '(nothing)') +
          '” — the word “' + esc(data.gating_word) + '” never finished playing. Row #' + data.booking_id +
          ' is a table booked for a time nobody confirmed. That is a double-booking, a wrong slot, or a ' +
          'charge the customer will dispute.</div>';
      } else if (data.db_status === 'ROLLED_BACK') {
        note = '<div class="note ok"><strong>Rollback — no phantom booking.</strong> Playback was cut at ' +
          fmt(data.cancelled_at_s) + 's, before “' + esc(data.gating_word) + '” finished at ' +
          fmt(data.gating_word_end_s) + 's. The fence moved row #' + data.booking_id +
          ' <code>PENDING_AUDIO → ROLLED_BACK</code> in ' + fmt(data.resolution_latency_ms, 2) +
          ' ms. The backend and the caller’s ear agree.</div>';
      } else {
        note = '<div class="note ok"><strong>Committed, correctly.</strong> The caller heard “' +
          esc(data.gating_word) + '” finish at ' + fmt(data.gating_word_end_s) +
          's, so the commit is authorised. Row #' + data.booking_id + ' is real and matches what was said.</div>';
      }
      $('resultNote').innerHTML = note;
    }
  }

  function placeCall() {
    stopSpeech();
    if (raf) cancelAnimationFrame(raf);
    $('tlCancel').style.display = 'none';
    $('resultNote').innerHTML = '';
    $('latencyChip').textContent = '';
    $('verdictBadge').className = 'badge pend';
    $('verdictBadge').textContent = 'call in progress';

    api('/api/call/start', {
      variant: variant,
      party_size: +$('inParty').value || 4,
      time_str: $('inTime').value || '7:00 PM'
    }).then(function (data) {
      renderCall(data);
      $('btnCall').disabled = true;
      $('btnBarge').disabled = false;
      $('btnHangUp').disabled = false;
      playPos = 0;
      playStart = performance.now();
      speak(data.sentence);
      raf = requestAnimationFrame(tick);
    }).catch(function (e) {
      $('resultNote').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    });
  }

  function bargeIn() {
    if (!call) return;
    if (raf) cancelAnimationFrame(raf);
    stopSpeech();
    var at = Math.min(playPos, TL.duration_s);

    var cancelEl = $('tlCancel');
    cancelEl.style.left = (at / TL.duration_s) * 100 + '%';
    cancelEl.style.display = 'block';

    $('btnBarge').disabled = true;
    $('btnHangUp').disabled = true;
    $('btnCall').disabled = false;

    api('/api/call/barge-in', { session_id: call.session_id, position_s: at })
      .then(function (data) { paintHeard(at, at); renderCall(data); })
      .catch(function (e) { $('resultNote').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>'; });
  }

  function finishCall() {
    if (!call) return;
    if (raf) cancelAnimationFrame(raf);
    $('btnBarge').disabled = true;
    $('btnHangUp').disabled = true;
    $('btnCall').disabled = false;
    api('/api/call/finish', { session_id: call.session_id, position_s: TL.duration_s })
      .then(renderCall)
      .catch(function () {});
  }

  function hangUp() {
    if (!call) return;
    if (raf) cancelAnimationFrame(raf);
    stopSpeech();
    $('btnBarge').disabled = true;
    $('btnHangUp').disabled = true;
    $('btnCall').disabled = false;
    api('/api/call/hang-up', { session_id: call.session_id }).then(renderCall).catch(function () {});
  }

  $('btnCall').addEventListener('click', placeCall);
  $('btnBarge').addEventListener('click', bargeIn);
  $('btnHangUp').addEventListener('click', hangUp);

  $('inSpeed').addEventListener('input', function () {
    speed = +this.value;
    $('speedVal').textContent = speed.toFixed(2).replace(/0$/, '') + '×';
  });

  // ---------------------------------------------------------------- compare tab

  function updateComparePlan() {
    var off = +$('cmpOffset').value;
    $('cmpOffsetVal').textContent = (off > 0 ? '+' : '') + off + ' ms';
    api('/api/plan?offset_ms=' + off).then(function (p) {
      $('cmpHeard').textContent = p.heard_text || '(nothing)';
      $('cmpPlanNote').innerHTML = 'Barge-in at <span class="mono">' + fmt(p.cancel_at_s) +
        's</span> — the gating word “' + esc(p.gating_word) + '” ends at <span class="mono">' +
        fmt(p.gating_word_end_s) + 's</span>. Oracle says a booking <strong>' +
        (p.expected_heard_gating_word ? 'should' : 'must not') + '</strong> exist.' +
        (p.clipped ? ' <span class="badge pend">clipped to audio bounds</span>' : '');
    }).catch(function () {});
  }

  $('cmpOffset').addEventListener('input', updateComparePlan);

  $('btnCompare').addEventListener('click', function () {
    var off = +$('cmpOffset').value;
    var btn = this;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> running…';

    Promise.all(['naive', 'fenced'].map(function (t) {
      return api('/api/sweep', { target: t, min_ms: off, max_ms: off, step_ms: 10, runs_per_offset: 1 });
    })).then(function (res) {
      [['naive', res[0]], ['fenced', res[1]]].forEach(function (pair) {
        var name = pair[0], data = pair[1], rec = data.records[0] || {};
        var key = name === 'naive' ? 'cmpNaive' : 'cmpFenced';
        $(key + 'Db').textContent = rec.db_status_after || '—';
        $(key + 'Db').className = 'v ' + (rec.db_status_after === 'COMMITTED' ? 'good' : 'bad');
        $(key + 'Mm').textContent = rec.mismatch ? 'YES' : 'no';
        $(key + 'Mm').className = 'v ' + (rec.mismatch ? 'bad' : 'good');
        $(key + 'Note').innerHTML = rec.mismatch
          ? '<span style="color:var(--red-2)">Phantom booking: row #' + rec.booking_id +
            ' is COMMITTED but the caller only heard “' + esc(rec.heard_text || '') + '”.</span>'
          : 'Resolved as <code>' + esc(rec.fence_outcome || 'committed on tool call') + '</code> in ' +
            fmt(rec.cancel_latency_ms, 2) + ' ms — backend agrees with the ear.';
      });
    }).catch(function (e) {
      $('cmpSweepOut').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = 'Run both variants at this offset';
    });
  });

  $('btnCompareSweep').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> sweeping…';
    $('cmpSweepOut').innerHTML = '';
    api('/api/acceptance', { runs_per_offset: 1 }).then(function (data) {
      var n = data.sweeps.naive, f = data.sweeps.fenced;
      $('cmpSweepOut').innerHTML =
        '<div class="grid g2">' +
        '<div class="panel"><h2><span class="badge err">naive</span> ' + n.trials + ' trials</h2>' +
        '<div class="stat mt"><div class="k">Phantom bookings</div><div class="v bad">' + n.mismatches + '</div>' +
        '<div class="s">' + (100 * n.mismatches / Math.max(n.trials, 1)).toFixed(1) + '% of trials committed something unheard</div></div></div>' +
        '<div class="panel"><h2><span class="badge ok">fenced</span> ' + f.trials + ' trials</h2>' +
        '<div class="stat mt"><div class="k">Phantom bookings</div><div class="v good">' + f.mismatches + '</div>' +
        '<div class="s">' + f.orphans + ' rows left pending · ' + f.elapsed_ms + ' ms wall clock</div></div></div></div>';
    }).catch(function (e) {
      $('cmpSweepOut').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = 'Sweep both across ±500 ms';
    });
  });

  // ---------------------------------------------------------------- sweep tab

  function renderSummary(summary) {
    var targets = summary.targets || {};
    var verdict = summary.verdict || 'UNKNOWN';
    var fenced = targets.fenced, naive = targets.naive;

    $('sweepVerdict').innerHTML =
      '<div class="verdict ' + (verdict === 'PASS' ? 'pass' : 'fail') + '">' +
      '<div class="big">' + esc(verdict) + '</div><div class="sub">' +
      (fenced ? ('fenced agent: ' + fenced.mismatches + ' state mismatches across ' + fenced.trials + ' trials') : 'no fenced trials yet') +
      (naive ? (' · naive control: ' + naive.mismatches + ' phantom bookings across ' + naive.trials + ' trials') : '') +
      '</div></div>';

    var cards = [];
    if (fenced) {
      cards.push(['Fenced mismatches', fenced.mismatches, fenced.mismatches === 0 ? 'good' : 'bad', fenced.trials + ' trials']);
      cards.push(['Fenced p95 latency', fmt((fenced.cancel_latency_ms || {}).p95, 2) + ' ms', 'info', 'decision → readable state']);
    }
    if (naive) {
      cards.push(['Naive phantom bookings', naive.mismatches, naive.mismatches ? 'bad' : 'good', (100 * naive.mismatch_rate).toFixed(1) + '% of trials']);
      cards.push(['Total trials', (summary.totals || {}).trials || 0, 'info', 'both variants, replay mode']);
    }
    $('sweepStats').innerHTML = cards.map(function (c) {
      return '<div class="stat"><div class="k">' + esc(c[0]) + '</div><div class="v ' + c[2] + '">' +
        esc(c[1]) + '</div><div class="s">' + esc(c[3]) + '</div></div>';
    }).join('');

    var rows = Object.keys(targets).map(function (k) { return targets[k]; });
    $('sweepTablePanel').style.display = rows.length ? 'block' : 'none';
    table($('sweepTable'), [
      { label: 'target', render: function (r) { return '<span class="badge ' + (r.target === 'fenced' ? 'ok' : 'err') + '">' + esc(r.target) + '</span>'; } },
      { label: 'trials', key: 'trials' },
      { label: 'committed', key: 'committed' },
      { label: 'mismatches', render: function (r) { return '<span style="color:' + (r.mismatches ? 'var(--red-2)' : 'var(--green-2)') + '">' + r.mismatches + '</span>'; } },
      { label: 'p50 ms', render: function (r) { return fmt((r.cancel_latency_ms || {}).p50, 3); } },
      { label: 'p95 ms', render: function (r) { return fmt((r.cancel_latency_ms || {}).p95, 3); } },
      { label: 'db statuses', render: function (r) { return Object.keys(r.db_status_counts || {}).map(function (s) { return esc(s) + '=' + r.db_status_counts[s]; }).join(' '); } },
      { label: 'verdict', render: function (r) { return r.passed ? '<span class="badge ok">PASS</span>' : '<span class="badge err">' + r.mismatches + ' mismatch</span>'; } }
    ], rows);

    $('chartPanel').style.display = 'block';
    fetch('/api/chart.svg?t=' + Date.now()).then(function (r) { return r.text(); })
      .then(function (svg) { $('chartWrap').innerHTML = svg; })
      .catch(function () {});
  }

  $('btnAcceptance').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    $('sweepSpin').innerHTML = '<span class="spinner"></span> <span class="small muted">running 606 trials…</span>';
    api('/api/acceptance', { runs_per_offset: 3 }).then(function (data) {
      renderSummary(data.summary);
    }).catch(function (e) {
      $('sweepVerdict').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    }).finally(function () { btn.disabled = false; $('sweepSpin').innerHTML = ''; });
  });

  $('btnQuickSweep').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    $('sweepSpin').innerHTML = '<span class="spinner"></span>';
    api('/api/sweep', { target: 'fenced', runs_per_offset: 1 }).then(function () {
      return api('/api/summary');
    }).then(renderSummary).catch(function (e) {
      $('sweepVerdict').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    }).finally(function () { btn.disabled = false; $('sweepSpin').innerHTML = ''; });
  });

  $('btnLoadSummary').addEventListener('click', function () {
    api('/api/summary').then(renderSummary).catch(function () {});
  });

  // ---------------------------------------------------------------- audit tab

  function loadDb() {
    api('/api/bookings?limit=100').then(function (d) {
      $('dbPathChip').textContent = d.db_path;
      var c = d.counts;
      $('dbStats').innerHTML = [
        ['Total rows', c.total, 'info', 'all variants'],
        ['Committed', c.committed, c.committed ? 'warn' : 'good', 'naive ' + c.naive_committed + ' · fenced ' + c.fenced_committed],
        ['Rolled back', c.rolled_back, 'good', 'phantom bookings prevented'],
        ['Still pending', c.pending, c.pending ? 'bad' : 'good', 'must be 0 after resolution']
      ].map(function (x) {
        return '<div class="stat"><div class="k">' + esc(x[0]) + '</div><div class="v ' + x[2] + '">' +
          esc(x[1]) + '</div><div class="s">' + esc(x[3]) + '</div></div>';
      }).join('');

      table($('tblBookings'), [
        { label: 'id', key: 'id' },
        { label: 'variant', render: function (r) { return '<span class="badge ' + (r.agent_variant === 'fenced' ? 'ok' : 'err') + '">' + esc(r.agent_variant) + '</span>'; } },
        { label: 'party', key: 'party_size' },
        { label: 'time', key: 'time_str' },
        { label: 'status', render: function (r) { return statusBadge(r.status); } }
      ], d.bookings, function (r) { return r.status === 'COMMITTED' && r.agent_variant === 'naive' ? 'mismatch' : ''; });

      table($('tblLog'), [
        { label: 'id', key: 'id' },
        { label: 'booking', key: 'booking_id' },
        { label: 'event', render: function (r) {
            var cls = r.event_type === 'ROLLED_BACK' ? 'err' : r.event_type === 'PENDING_CREATED' ? 'pend' : 'ok';
            return '<span class="badge ' + cls + '">' + esc(r.event_type) + '</span>';
          } },
        { label: 'timestamp', render: function (r) { return esc(String(r.timestamp).slice(11, 23)); } }
      ], d.transaction_log);
    }).catch(function (e) {
      $('dbStats').innerHTML = '<div class="note warn">' + esc(e.message) + '</div>';
    });
  }

  $('btnRefreshDb').addEventListener('click', loadDb);
  $('btnResetDb').addEventListener('click', function () {
    api('/api/reset', {}).then(function () { loadDb(); }).catch(function () {});
  });

  // ---------------------------------------------------------------- mobile tab

  var mobileReady = false;
  function setupMobile() {
    if (mobileReady) return;
    mobileReady = true;
    var url = window.location.origin + '/mobile';
    $('mobileUrl').value = url;
    $('btnOpenMobile').href = url;
    try {
      window.QRLite.render($('qrBox'), url, { scale: 4, quiet: 3 });
    } catch (e) {
      $('qrBox').innerHTML = '<div style="color:#000;font-family:monospace;font-size:11px;padding:8px;width:168px">' +
        'QR unavailable — use the link.</div>';
    }

    table($('tblMobileCauses'), [
      { label: '#', key: 'n' },
      { label: 'Cause', key: 'cause' },
      { label: 'Symptom', key: 'symptom' },
      { label: 'Fix', key: 'fix' }
    ], [
      { n: 1, cause: 'Page over http:// or a LAN IP', symptom: 'Mic permission silently denied; agent never hears you', fix: 'https:// page only' },
      { n: 2, cause: 'LIVEKIT_URL points at localhost/LAN', symptom: 'Phone cannot reach the server at all', fix: 'wss://<project>.livekit.cloud' },
      { n: 3, cause: 'No agent dispatch for the mobile participant', symptom: 'Room connects but is empty; total silence', fix: 'Start the worker before the phone joins' },
      { n: 4, cause: 'iOS Safari autoplay policy', symptom: 'Worker logs TTS but the phone plays nothing', fix: 'Tap-to-connect gesture must call room.startAudio()' },
      { n: 5, cause: 'Carrier blocks WebRTC UDP', symptom: 'Works on Wi-Fi, dies on LTE', fix: 'LiveKit Cloud TURN over TCP 443' },
      { n: 6, cause: 'Dependency drift between machines', symptom: 'Worker crashes or fence hooks never fire', fix: 'Pinned requirements.txt + --check' }
    ]);
  }

  $('btnCopyUrl').addEventListener('click', function () {
    var btn = this;
    var url = $('mobileUrl').value;
    var done = function () { btn.textContent = 'Copied ✓'; setTimeout(function () { btn.textContent = 'Copy link'; }, 1400); };
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done).catch(done);
    else { $('mobileUrl').select(); document.execCommand('copy'); done(); }
  });

  function loadFeed() {
    api('/api/calls?limit=25').then(function (d) {
      table($('tblFeed'), [
        { label: 'session', render: function (r) { return esc(r.session_id.slice(0, 8)); } },
        { label: 'variant', render: function (r) { return '<span class="badge ' + (r.variant === 'fenced' ? 'ok' : 'err') + '">' + esc(r.variant) + '</span>'; } },
        { label: 'row', key: 'booking_id' },
        { label: 'status', render: function (r) { return statusBadge(r.db_status); } },
        { label: 'mismatch', render: function (r) { return r.mismatch ? '<span class="badge err">YES</span>' : '<span class="badge mute">no</span>'; } },
        { label: 'heard', render: function (r) { return '<span class="small">' + esc((r.heard_text || '(nothing)').slice(0, 42)) + '</span>'; } }
      ], d.calls, function (r) { return r.mismatch ? 'mismatch' : ''; });
    }).catch(function () {});
  }

  $('btnFeedToggle').addEventListener('click', function () {
    if (feedTimer) {
      clearInterval(feedTimer); feedTimer = null;
      this.textContent = 'Start auto-refresh';
      $('feedChip').textContent = 'idle';
    } else {
      loadFeed();
      feedTimer = setInterval(loadFeed, 2000);
      this.textContent = 'Stop auto-refresh';
      $('feedChip').textContent = 'refreshing every 2s';
    }
  });

  // ---------------------------------------------------------------- how tab

  function renderHow() {
    api('/api/fence-states').then(function (d) {
      table($('tblFenceStates'), [
        { label: 'state', render: function (r) { return statusBadge(r.state === 'PENDING' ? 'PENDING_AUDIO' : r.state); } },
        { label: 'meaning', key: 'meaning' }
      ], d.states);
    }).catch(function () {});

    table($('tblProviders'), [
      { label: 'env var', render: function (r) { return '<code>' + esc(r.key) + '</code>'; } },
      { label: 'purpose', key: 'purpose' },
      { label: 'status', render: function (r) {
          return r.configured ? '<span class="badge ok">configured</span>' : '<span class="badge mute">not set · not needed</span>';
        } }
    ], (CFG && CFG.providers) || []);

    table($('tblWords'), [
      { label: '#', render: function (r) { return String(r.i); } },
      { label: 'word', render: function (r) { return r.is_gating ? '<strong style="color:var(--amber)">' + esc(r.text) + '</strong>' : esc(r.text); } },
      { label: 'start', render: function (r) { return fmt(r.start); } },
      { label: 'end', render: function (r) { return fmt(r.end); } },
      { label: '', render: function (r) { return r.is_gating ? '<span class="badge pend">gating word</span>' : ''; } }
    ], (TL ? TL.words.map(function (w, i) { var o = Object.assign({}, w); o.i = i + 1; return o; }) : []));

    var p = TL && TL.provenance ? TL.provenance : {};
    $('fixtureNote').innerHTML = '<strong>Fixture provenance: ' + esc(p.provenance || 'unknown') + '.</strong> ' + esc(p.note || '');

    table($('tblApi'), [
      { label: 'method', key: 'm' },
      { label: 'endpoint', render: function (r) { return '<code>' + esc(r.p) + '</code>'; } },
      { label: 'does', key: 'd' }
    ], [
      { m: 'POST', p: '/api/call/start', d: 'Run the tool call for a variant (naive commits now; fenced creates PENDING_AUDIO)' },
      { m: 'POST', p: '/api/call/advance', d: 'Feed playback position into the real AudioFence' },
      { m: 'POST', p: '/api/call/barge-in', d: 'Interrupt playback; the fence commits or rolls back' },
      { m: 'POST', p: '/api/call/finish', d: 'Playback completed without interruption' },
      { m: 'GET', p: '/api/heard?at=2.1', d: 'What the caller had actually heard at that instant' },
      { m: 'GET', p: '/api/plan?offset_ms=-200', d: 'Injector arithmetic: gating_word_end + offset' },
      { m: 'POST', p: '/api/sweep', d: 'Run a real chaos sweep against one variant' },
      { m: 'POST', p: '/api/acceptance', d: 'The full 606-trial acceptance test' },
      { m: 'GET', p: '/api/summary', d: 'reporting.dashboard.summarize over the trial logs' },
      { m: 'GET', p: '/api/chart.svg', d: 'Mismatch-vs-offset chart from reporting/chart.py' },
      { m: 'GET', p: '/api/bookings', d: 'Direct read of the SQLite store' }
    ]);
  }

  // ---------------------------------------------------------------- boot

  $('voiceSupport').textContent = speechSupported()
    ? 'Web Speech API available on this device.'
    : 'This browser has no Web Speech API — playback runs silently on the same timeline.';

  Promise.all([api('/api/timeline'), api('/api/config')]).then(function (res) {
    TL = res[0];
    CFG = res[1];
    drawTimeline();
    paintHeard(0, null);
    $('tlCursor').style.display = 'none';
    setVariant('fenced');
    updateComparePlan();
    renderHow();

    var anyKeys = CFG.any_configured;
    $('modeChip').textContent = 'browser voice + offline harness';

    api('/api/summary').then(function (s) {
      if ((s.totals || {}).trials) renderSummary(s);
    }).catch(function () {});
  }).catch(function (e) {
    document.querySelector('.wrap').insertAdjacentHTML('afterbegin',
      '<div class="note warn mt">Failed to load: ' + esc(e.message) + '</div>');
  });
})();
