/* RAX YT Downloader: website logic.
   Talks to server.py on the same address. No build step, no libraries. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const el = {
    form: $('#grab'),
    input: $('#link'),
    paste: $('#paste-btn'),
    go: $('#go-btn'),
    msg: $('#form-msg'),
    notices: $('#notices'),
    result: $('#result'),
    tray: $('#tray'),
    trayToggle: $('#tray-toggle'),
    trayCount: $('#tray-count'),
    trayList: $('#tray-list'),
    trayTip: $('#tray-tip'),
    trayClear: $('#tray-clear'),
    drop: $('#drop'),
    toast: $('#toast'),
    bookmarklet: $('#bookmarklet'),
    phoneUrl: $('#phone-url'),
    phoneLede: $('#phone-lede'),
    engine: $('#engine'),
  };

  const ACTIVE = ['queued', 'downloading', 'processing'];
  const state = {
    status: null,      // what /api/status said
    current: null,     // the video or list on screen
    infoCtrl: null,    // lookup in flight
    jobs: new Map(),   // job id -> { id, params, meta, data, el, saved }
    pollTimer: null,
    pollFails: 0,
    internalDrag: false,
    baseTitle: document.title,
  };

  /* ------------------------------------------------------------ formatting */

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtBytes(n) {
    if (!n || n < 0) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
  }

  function fmtClock(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  }

  function fmtEta(sec) {
    if (sec == null || !Number.isFinite(sec)) return '';
    if (sec < 60) return `${Math.max(1, Math.round(sec))} s left`;
    if (sec < 3600) return `${Math.round(sec / 60)} min left`;
    return `${(sec / 3600).toFixed(1)} h left`;
  }

  const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
  const fmtViews = (n) => (n == null ? '' : `${compact.format(n)} views`);

  function fmtDate(d) {
    if (!/^\d{8}$/.test(d || '')) return '';
    const date = new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8));
    return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  const plural = (n, one, many = `${one}s`) => `${n.toLocaleString('en')} ${n === 1 ? one : many}`;
  const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------ links */

  const LINK_RE = /(?:https?:\/\/)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)\/[^\s<>"'`]*/gi;

  function findLinks(text) {
    const raw = String(text || '').replace(/https?:\/\//gi, ' $&'); // links pasted with no gap between them
    const found = (raw.match(LINK_RE) || [])
      .map((u) => (/^https?:/i.test(u) ? u : `https://${u}`).replace(/[).,;\]}>]+$/, ''));
    const bare = raw.trim();
    if (!found.length && /^[\w-]{11}$/.test(bare)) found.push(`https://www.youtube.com/watch?v=${bare}`);
    return [...new Set(found)];
  }

  function videoIdOf(url) {
    try {
      const u = new URL(url);
      if (/(^|\.)youtu\.be$/i.test(u.hostname)) {
        const first = u.pathname.split('/')[1] || '';
        return /^[\w-]{11}$/.test(first) ? first : null;
      }
      const v = u.searchParams.get('v');
      if (v && /^[\w-]{11}$/.test(v)) return v;
      const m = u.pathname.match(/^\/(?:shorts|live|embed|v|e)\/([\w-]{11})/);
      return m ? m[1] : null;
    } catch (_) {
      return null;
    }
  }

  const thumbOf = (v) => (v.id ? `https://i.ytimg.com/vi/${v.id}/mqdefault.jpg` : v.thumbnail || '');

  /* ------------------------------------------------------------ talking to the server */

  async function api(path, options = {}) {
    let res;
    try {
      res = await fetch(path, {
        ...options,
        headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      });
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new Error("Can't reach the downloader. Check that the black START.bat window is still open.");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || `The downloader answered with error ${res.status}.`);
    return data;
  }

  /* ------------------------------------------------------------ messages */

  function setMsg(text, isError = false) {
    el.msg.textContent = text || '';
    el.msg.classList.toggle('is-error', Boolean(text) && isError);
  }

  let toastTimer = null;
  function toast(text, isError = false) {
    el.toast.textContent = text;
    el.toast.classList.toggle('is-error', isError);
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, isError ? 6500 : 3800);
  }

  // The main button says what it will fetch: one video, a playlist, or several videos.
  function goLabel() {
    const links = findLinks(el.input.value);
    if (links.length > 1) return 'Get videos';
    if (links.length === 1 && !videoIdOf(links[0])) {
      if (/[?&]list=/.test(links[0])) return 'Get playlist';
      if (/\/(?:@|channel\/|c\/|user\/)/.test(links[0])) return 'Get videos';
    }
    return 'Get video';
  }

  function setBusy(on) {
    el.go.setAttribute('aria-busy', on ? 'true' : 'false');
    el.go.textContent = on ? 'Loading…' : goLabel();
  }

  // Bring the result up to the top of the screen, unless it's already there.
  function revealResult() {
    requestAnimationFrame(() => {
      const top = el.result.getBoundingClientRect().top;
      if (top < 0 || top > window.innerHeight * 0.35) {
        window.scrollTo({ top: Math.max(0, window.scrollY + top - 16), behavior: reduceMotion() ? 'auto' : 'smooth' });
      }
    });
  }

  /* ------------------------------------------------------------ start-up */

  async function loadStatus() {
    try {
      const s = await api('/api/status');
      state.status = s;
      const notes = [];
      if (!s.ffmpeg) {
        notes.push("HD video and MP3 need FFmpeg, which isn't installed yet. Close the black window and run START.bat again to add it. Until then, fewer video qualities show up and audio saves as M4A.");
      }
      if (!s.js_runtime) {
        notes.push("Deno isn't installed, so some videos may fail or download slowly. Close the black window and run START.bat again to add it.");
      }
      el.notices.innerHTML = notes.map((n) => `<p class="notice">${esc(n)}</p>`).join('');

      if (s.lan_url) {
        el.phoneUrl.textContent = s.lan_url.replace(/^https?:\/\//, '');
      } else if (!s.phones_allowed) {
        el.phoneLede.textContent = 'Phone access is turned off. To turn it on, open server.py in Notepad and set ALLOW_PHONES_ON_WIFI = True.';
        el.phoneUrl.hidden = true;
      } else {
        el.phoneLede.textContent = "This computer isn't connected to a Wi-Fi or home network right now. Connect it, then reload this page.";
        el.phoneUrl.hidden = true;
      }
      el.engine.textContent = `Download engine: yt-dlp ${s.ytdlp}`;
    } catch (err) {
      setMsg(err.message, true);
    }
  }

  function setupShortcuts() {
    $$('.js-host').forEach((node) => { node.textContent = location.host; });

    // The one-click button: a bookmark that sends the YouTube page you're on to this site.
    el.bookmarklet.href = `javascript:(function(){window.open('${location.origin}/?url='+encodeURIComponent(location.href),'_blank');})();`;
    el.bookmarklet.addEventListener('click', (e) => {
      e.preventDefault();
      toast('Drag this button onto your bookmarks bar, then click it while you watch a YouTube video.');
    });
    el.bookmarklet.addEventListener('dragstart', () => { state.internalDrag = true; });
    el.bookmarklet.addEventListener('dragend', () => { state.internalDrag = false; });
  }

  // Links that arrive in the address bar: the one-click button (?url=) and the address swap (/watch?v=...).
  function incomingLink() {
    const here = new URL(location.href);
    const param = here.searchParams.get('url') || here.searchParams.get('link') || here.searchParams.get('text');
    if (param) return param;
    let path = here.pathname;
    try { path = decodeURIComponent(path); } catch (_) { /* keep it raw */ }
    if (!path || path === '/' || path === '/index.html') return null;
    const at = path.search(/(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)(?:\/|$)/i);
    if (at >= 0) return `https://${path.slice(at)}${here.search}`;
    if (/^\/(?:watch|shorts\/|playlist|live\/|embed\/|@|channel\/|c\/|user\/)/i.test(path)) {
      return `https://www.youtube.com${path}${here.search}`;
    }
    const id = path.match(/^\/([\w-]{11})\/?$/);
    return id ? `https://youtu.be/${id[1]}${here.search}` : null;
  }

  /* ------------------------------------------------------------ getting a link in */

  function handleText(text) {
    const links = findLinks(text);
    if (!links.length) {
      setMsg(String(text || '').trim()
        ? "That isn't a YouTube link. Copy the link from the address bar or the Share button, then paste it here."
        : 'Paste a YouTube link first.', true);
      el.input.focus();
      return;
    }
    if (links.length === 1) {
      el.input.value = links[0];
      loadInfo(links[0]);
    } else {
      el.input.value = links.join(' ');
      el.go.textContent = goLabel();
      showLinkList(links);
    }
  }

  async function pasteFromClipboard() {
    if (navigator.clipboard && navigator.clipboard.readText) {
      try {
        const text = await navigator.clipboard.readText();
        if (text && text.trim()) { handleText(text); return; }
        setMsg('Your clipboard is empty. Copy a YouTube link first.', true);
        return;
      } catch (_) { /* not allowed here, fall back to the keyboard */ }
    }
    el.input.focus();
    el.input.select();
    setMsg(window.matchMedia('(pointer: coarse)').matches
      ? 'Press and hold the box, then tap Paste.'
      : 'Press Ctrl+V to paste your link.');
  }

  el.form.addEventListener('submit', (e) => {
    e.preventDefault();
    handleText(el.input.value);
  });

  el.input.addEventListener('paste', (e) => {
    const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
    if (findLinks(text).length) {
      e.preventDefault();
      handleText(text);
    }
  });

  el.paste.addEventListener('click', pasteFromClipboard);

  el.input.addEventListener('input', () => {
    if (el.go.getAttribute('aria-busy') !== 'true') el.go.textContent = goLabel();
    if (el.msg.classList.contains('is-error')) setMsg('');
  });

  // Drop a link anywhere on the page
  const dragHasText = (e) => {
    const types = e.dataTransfer ? Array.from(e.dataTransfer.types || []) : [];
    return types.includes('text/uri-list') || types.includes('text/plain');
  };
  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => {
    if (state.internalDrag || !dragHasText(e)) return;
    dragDepth += 1;
    el.drop.hidden = false;
  });
  window.addEventListener('dragleave', () => {
    if (state.internalDrag) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.drop.hidden = true;
  });
  window.addEventListener('dragover', (e) => {
    if (!state.internalDrag && dragHasText(e)) e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    if (state.internalDrag) return;
    e.preventDefault();
    dragDepth = 0;
    el.drop.hidden = true;
    const text = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
    if (text) handleText(text);
  });

  /* ------------------------------------------------------------ looking up a link */

  function showLoading(isList) {
    el.result.hidden = false;
    el.result.innerHTML = `
      <div class="loading" aria-busy="true">
        <div class="loading-thumb"></div>
        <div>
          <div class="loading-lines"><span></span><span></span><span></span></div>
          <p class="loading-text" id="loading-text">${isList ? 'Reading the playlist…' : 'Looking up the video…'}</p>
        </div>
      </div>`;
    revealResult();
  }

  async function loadInfo(url, wantList = false) {
    setMsg('');
    if (state.infoCtrl) state.infoCtrl.abort();
    const ctrl = new AbortController();
    state.infoCtrl = ctrl;
    const looksLikeList = wantList
      || (/[?&]list=/.test(url) && !/[?&]v=|youtu\.be\/|\/shorts\//.test(url))
      || /\/(?:@|channel\/|c\/|user\/)/.test(url);
    showLoading(looksLikeList);
    setBusy(true);
    const slow = setTimeout(() => {
      const t = $('#loading-text');
      if (t) t.textContent = 'Still working. Long playlists and very long videos take a little longer.';
    }, 8000);
    try {
      const query = new URLSearchParams({ url });
      if (wantList) query.set('playlist', '1');
      const data = await api(`/api/info?${query}`, { signal: ctrl.signal });
      if (data.type === 'playlist') renderList(data, false);
      else renderVideo(data);
    } catch (err) {
      if (err.name === 'AbortError') return;
      el.result.hidden = true;
      el.result.innerHTML = '';
      state.current = null;
      setMsg(err.message, true);
    } finally {
      clearTimeout(slow);
      if (state.infoCtrl === ctrl) {
        state.infoCtrl = null;
        setBusy(false);
      }
    }
  }

  /* ------------------------------------------------------------ one video */

  function optionRow(name, detail, size, act, index, label) {
    return `
      <li class="opt">
        <span class="opt-q">${esc(name)}</span>
        <span class="opt-tag">${esc(detail)}</span>
        <span class="opt-size">${size ? `about ${fmtBytes(size)}` : ''}</span>
        <button type="button" class="btn btn-row" data-act="${act}" data-i="${index}" aria-label="Download ${esc(label)}">Download</button>
      </li>`;
  }

  function panelsHtml(v) {
    const tabs = [];
    if (v.video_options.length) {
      tabs.push(['video', 'Video', `<ul class="opts">${v.video_options.map((o, i) => optionRow(
        `${o.q}p`,
        [o.tag, o.fps > 30 ? `${o.fps} fps` : '', o.hdr ? 'HDR' : ''].filter(Boolean).join(', '),
        o.size, 'video', i, `${o.q}p video`)).join('')}</ul>`]);
    }
    if (v.audio_options.length) {
      tabs.push(['audio', 'Audio', `<ul class="opts">${v.audio_options.map((o, i) => optionRow(
        o.name, o.detail, o.size, 'audio', i, `${o.name} audio, ${o.detail}`)).join('')}</ul>`]);
    }
    const subs = v.subtitles || [];
    const subsBlock = subs.length
      ? `<p>Saved as an SRT file that works in VLC and most video players.</p>
         <div class="extra-row">
           <label class="sr-only" for="sub-lang">Subtitle language</label>
           <select class="select" id="sub-lang">${subs.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('')}</select>
           <button type="button" class="btn btn-row" data-act="subs">Download subtitles</button>
         </div>`
      : '<p>This video has no subtitles.</p>';
    const thumbLink = `/api/thumbnail?id=${encodeURIComponent(v.id)}&name=${encodeURIComponent(v.title)}`;
    tabs.push(['extras', 'Extras', `
      <div class="extras">
        <div class="extra">
          <h3 class="extra-title">Thumbnail</h3>
          <p>The cover image, in the best quality YouTube has.</p>
          <a class="btn btn-row" href="${esc(thumbLink)}" download>Download thumbnail</a>
        </div>
        <div class="extra">
          <h3 class="extra-title">Subtitles</h3>
          ${subsBlock}
        </div>
      </div>`]);

    const tabButtons = tabs.map(([key, label], i) => `
      <button type="button" role="tab" class="seg-btn" id="tab-${key}" aria-controls="panel-${key}"
              aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">${label}</button>`).join('');
    const panels = tabs.map(([key, , html], i) => `
      <div class="panel" role="tabpanel" id="panel-${key}" aria-labelledby="tab-${key}"${i ? ' hidden' : ''}>${html}</div>`).join('');
    return `<div class="seg" role="tablist" aria-label="What to save">${tabButtons}</div>${panels}`;
  }

  function renderVideo(v) {
    state.current = v;
    const meta = [v.channel, fmtViews(v.views), fmtDate(v.upload_date)]
      .filter(Boolean).map((t) => `<span>${esc(t)}</span>`).join('');
    let body;
    if (v.live) body = `<p class="video-live">${esc(v.live)}</p>`;
    else if (!v.video_options.length && !v.audio_options.length) body = '<p class="video-live">This video has no files that can be downloaded.</p>';
    else body = panelsHtml(v);

    el.result.hidden = false;
    el.result.innerHTML = `
      <article class="video result-enter" aria-labelledby="video-title">
        <div class="video-side">
          <div class="video-side-in">
            <div class="video-media">
              ${v.thumbnail ? `<img class="video-thumb" src="${esc(v.thumbnail)}" alt="" width="640" height="360">` : '<div class="video-thumb"></div>'}
              ${v.duration ? `<span class="video-time">${fmtClock(v.duration)}</span>` : ''}
            </div>
            <div class="video-info">
              <h2 class="video-title" id="video-title">${esc(v.title)}</h2>
              ${meta ? `<p class="video-meta">${meta}</p>` : ''}
              ${v.playlist_hint ? '<p class="video-hint">This video is part of a playlist. <button type="button" class="text-btn" data-act="whole-list">Show the whole playlist</button></p>' : ''}
            </div>
          </div>
        </div>
        <div class="video-body">${body}</div>
      </article>`;
    wireTabs(el.result);
    revealResult();
  }

  function wireTabs(root) {
    const tabs = $$('[role="tab"]', root);
    const select = (index, focus) => {
      tabs.forEach((tab, i) => {
        const on = i === index;
        tab.setAttribute('aria-selected', String(on));
        tab.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(tab.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
      });
      if (focus) tabs[index].focus();
    };
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => select(i, false));
      tab.addEventListener('keydown', (e) => {
        const keys = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1 };
        if (e.key in keys) {
          e.preventDefault();
          select(keys[e.key], true);
        }
      });
    });
  }

  /* ------------------------------------------------------------ playlists and lists of links */

  function formatChoices() {
    if (state.status && !state.status.ffmpeg) {
      return `<option value="video:best" selected>Video, best without FFmpeg</option>
              <option value="audio:m4a">Audio only, M4A</option>`;
    }
    return `
      <optgroup label="Video, MP4">
        <option value="video:best">Best quality</option>
        <option value="video:1080" selected>1080p Full HD</option>
        <option value="video:720">720p HD</option>
        <option value="video:480">480p</option>
        <option value="video:360">360p</option>
      </optgroup>
      <optgroup label="Audio only">
        <option value="audio:mp3-192">MP3, 192 kbps</option>
        <option value="audio:mp3-320">MP3, 320 kbps</option>
        <option value="audio:mp3-128">MP3, 128 kbps</option>
        <option value="audio:m4a">M4A, original quality</option>
      </optgroup>`;
  }

  function renderList(list, fromLinks) {
    state.current = list;
    const meta = fromLinks ? [plural(list.entries.length, 'video')] : [list.channel, plural(list.count, 'video')];
    const notes = [];
    if (!fromLinks && list.total && list.total > list.count) {
      notes.push(`Showing the first ${list.count.toLocaleString('en')} of ${list.total.toLocaleString('en')} videos.`);
    }
    if (fromLinks && list.skipped) {
      notes.push(`${plural(list.skipped, 'playlist link')} left out. Paste playlist links on their own to open them.`);
    }
    const rows = list.entries.map((it, i) => `
      <li class="item${it.unavailable ? ' is-off' : ''}" data-i="${i}">
        <label class="item-check"><input type="checkbox"${it.unavailable ? ' disabled' : ' checked'} aria-label="Include ${esc(it.title)}"></label>
        <span class="item-num" aria-hidden="true"></span>
        <img class="item-thumb" src="${esc(it.thumb)}" alt="" loading="lazy" width="120" height="68">
        <div class="item-text">
          <p class="item-title">${esc(it.title)}</p>
          <p class="item-sub">${it.unavailable ? 'Not available' : (it.duration ? fmtClock(it.duration) : '')}</p>
        </div>
        <span class="item-state"></span>
      </li>`).join('');

    el.result.hidden = false;
    el.result.innerHTML = `
      <section class="list result-enter" aria-labelledby="list-title">
        <header class="list-head">
          <h2 class="list-title" id="list-title">${esc(list.title)}</h2>
          <p class="list-meta">${meta.filter(Boolean).map((t) => `<span>${esc(t)}</span>`).join('')}</p>
          ${notes.map((n) => `<p class="list-note">${esc(n)}</p>`).join('')}
        </header>
        <div class="list-tools">
          <label class="check"><input type="checkbox" id="pl-all" checked> Select all</label>
          <label class="field">Save as <select class="select" id="pl-format">${formatChoices()}</select></label>
          <button type="button" class="btn btn-signal" data-act="list-go" id="pl-go">Download</button>
        </div>
        <ol class="list-items" id="pl-items">${rows}</ol>
      </section>`;
    updateListButton();
    revealResult();
  }

  function selectedItems() {
    const list = state.current;
    if (!list || !list.entries) return [];
    return $$('#pl-items .item')
      .filter((li) => { const box = $('input', li); return box && box.checked && !box.disabled; })
      .map((li) => ({ li, entry: list.entries[+li.dataset.i] }));
  }

  function updateListButton() {
    const btn = $('#pl-go');
    if (!btn) return;
    const n = selectedItems().length;
    const boxes = $$('#pl-items .item-check input:not(:disabled)');
    btn.textContent = n ? `Download ${plural(n, 'video')}` : 'Tick some videos first';
    btn.disabled = !n;
    const all = $('#pl-all');
    if (all) {
      all.checked = n > 0 && n === boxes.length;
      all.indeterminate = n > 0 && n < boxes.length;
    }
  }

  async function downloadList(btn) {
    const picks = selectedItems();
    if (!picks.length) return;
    const select = $('#pl-format');
    const [kind, value] = select.value.split(':');
    const choice = select.selectedOptions[0].textContent.trim();
    btn.disabled = true;
    btn.textContent = 'Adding…';
    if (picks.length > 1) el.trayTip.hidden = false;
    let added = 0;
    for (const { li, entry } of picks) {
      const params = { url: entry.url, kind, title: entry.title };
      if (kind === 'video') params.q = value === 'best' ? 'best' : Number(value);
      else params.audio = value;
      const label = kind === 'video' ? `${value === 'best' ? 'Best quality' : `${value}p`} video, MP4` : choice;
      const id = await startJob(params, { title: entry.title, thumb: entry.thumb, label, row: li, entry }, null, true);
      if (id) added += 1;
    }
    if (added) toast(`${plural(added, 'video')} added to your downloads.`);
    else toast('Nothing could be added. Check that the black window is still open, then try again.', true);
    updateListButton();
  }

  function showLinkList(links) {
    const entries = [];
    let skipped = 0;
    for (const url of links) {
      const id = videoIdOf(url);
      if (!id) { skipped += 1; continue; }
      if (entries.some((e) => e.id === id)) continue;
      entries.push({
        id,
        url: `https://www.youtube.com/watch?v=${id}`,
        title: 'Loading title…',
        thumb: `https://i.ytimg.com/vi/${id}/mqdefault.jpg`,
        duration: null,
      });
    }
    if (!entries.length) { loadInfo(links[0]); return; }
    if (entries.length === 1 && !skipped) { loadInfo(entries[0].url); return; }
    renderList({ type: 'links', title: 'Your links', entries, count: entries.length, skipped }, true);
    fillTitles(entries);
  }

  async function fillTitles(entries) {
    const queue = entries.slice();
    const worker = async () => {
      while (queue.length) {
        const entry = queue.shift();
        try {
          const d = await api(`/api/quick?id=${encodeURIComponent(entry.id)}`);
          entry.title = d.title || `Video ${entry.id}`;
          entry.channel = d.channel || '';
        } catch (_) {
          entry.title = `Video ${entry.id}`;
        }
        if (state.current && state.current.entries === entries) {
          const li = $(`#pl-items .item[data-i="${entries.indexOf(entry)}"]`);
          if (li) {
            $('.item-title', li).textContent = entry.title;
            $('.item-sub', li).textContent = entry.channel;
            $('input', li).setAttribute('aria-label', `Include ${entry.title}`);
          }
        }
        for (const job of state.jobs.values()) {
          if (job.meta.entry === entry) $('.job-title', job.el).textContent = entry.title;
        }
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
  }

  /* ------------------------------------------------------------ result clicks */

  el.result.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    const v = state.current;
    if (!btn || !v) return;
    const act = btn.dataset.act;
    if (act === 'whole-list') {
      loadInfo(v.source_url, true);
    } else if (act === 'video') {
      const o = v.video_options[+btn.dataset.i];
      startJob(
        { url: v.url, kind: 'video', q: o.q, fid: o.fid, progressive: o.progressive, vsize: o.vsize, asize: o.asize, title: v.title },
        { title: v.title, thumb: thumbOf(v), label: `${o.q}p video, MP4` }, btn);
    } else if (act === 'audio') {
      const o = v.audio_options[+btn.dataset.i];
      startJob(
        { url: v.url, kind: 'audio', audio: o.id, title: v.title },
        { title: v.title, thumb: thumbOf(v), label: o.id.startsWith('mp3') ? `MP3 audio, ${o.detail}` : `${o.name} audio, original quality` }, btn);
    } else if (act === 'subs') {
      const s = v.subtitles[+$('#sub-lang').value];
      startJob(
        { url: v.url, kind: 'subs', lang: s.code, title: v.title },
        { title: v.title, thumb: thumbOf(v), label: `Subtitles, ${s.name}` }, btn);
    } else if (act === 'list-go') {
      downloadList(btn);
    }
  });

  el.result.addEventListener('change', (e) => {
    if (e.target.id === 'pl-all') {
      $$('#pl-items .item-check input:not(:disabled)').forEach((box) => { box.checked = e.target.checked; });
      updateListButton();
    } else if (e.target.closest('.item-check')) {
      updateListButton();
    }
  });

  /* ------------------------------------------------------------ downloads */

  const ACTION_LABELS = { cancel: 'Cancel', save: 'Save again', retry: 'Try again', remove: 'Remove' };

  function actionsFor(s) {
    if (s === 'queued' || s === 'downloading') return ['cancel'];
    if (s === 'processing') return [];
    if (s === 'done') return ['save', 'remove'];
    return ['retry', 'remove'];
  }

  function statusHtml(d) {
    switch (d.state) {
      case 'queued': return 'Waiting in line';
      case 'downloading': {
        const nums = [];
        if (d.done_bytes && d.total_bytes) nums.push(`${fmtBytes(d.done_bytes)} of ${fmtBytes(d.total_bytes)}`);
        else if (d.done_bytes) nums.push(fmtBytes(d.done_bytes));
        if (d.speed) nums.push(`${fmtBytes(d.speed)}/s`);
        const eta = fmtEta(d.eta);
        if (eta) nums.push(eta);
        const pct = d.percent > 0.5 ? ` ${Math.floor(d.percent)}%` : '';
        return `<b>${esc(d.stage || 'Downloading')}${pct}</b>${nums.length ? `<br>${esc(nums.join(', '))}` : ''}`;
      }
      case 'processing': return `<b>${esc(d.stage || 'Finishing up')}</b>`;
      case 'done': return `<b>Done${d.size ? `, ${fmtBytes(d.size)}` : ''}.</b> Check your Downloads folder.`;
      case 'error': return esc(d.error || 'The download failed.');
      case 'cancelled': return 'Cancelled.';
      case 'gone': return 'This download expired. Start it again.';
      default: return '';
    }
  }

  function rowText(d) {
    return {
      queued: 'Waiting',
      downloading: d.percent > 0.5 ? `${Math.floor(d.percent)}%` : 'Starting',
      processing: 'Finishing',
      done: 'Done',
      error: 'Failed',
      cancelled: 'Cancelled',
      gone: 'Expired',
    }[d.state] || '';
  }

  function renderJob(job) {
    const d = job.data;
    const li = job.el;
    li.dataset.state = d.state;
    if (d.state === 'downloading' && !(d.percent > 0.5)) li.dataset.stalled = '';
    else delete li.dataset.stalled;
    const pct = d.state === 'done' || d.state === 'processing' ? 100 : Math.max(0, Math.min(100, d.percent || 0));
    $('.bar > span', li).style.width = `${pct}%`;
    $('.bar', li).setAttribute('aria-valuenow', String(Math.round(pct)));
    $('.job-status', li).innerHTML = statusHtml(d);
    const acts = $('.job-acts', li);
    const wanted = actionsFor(d.state);
    if (acts.dataset.set !== wanted.join()) {
      acts.dataset.set = wanted.join();
      acts.innerHTML = wanted.map((a) => `<button type="button" class="text-btn" data-job-act="${a}">${ACTION_LABELS[a]}</button>`).join('');
    }
    if (job.meta.row) {
      job.meta.row.dataset.state = d.state;
      const cell = $('.item-state', job.meta.row);
      if (cell) cell.textContent = rowText(d);
    }
  }

  function addJob(id, params, meta) {
    const li = document.createElement('li');
    li.className = 'job';
    li.innerHTML = `
      <img class="job-thumb" src="${esc(meta.thumb || '')}" alt="">
      <div class="job-main">
        <p class="job-title"></p>
        <p class="job-kind">${esc(meta.label || '')}</p>
        <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span></span></div>
        <p class="job-status"></p>
        <div class="job-acts"></div>
      </div>`;
    const title = meta.entry ? meta.entry.title : meta.title;
    $('.job-title', li).textContent = title;
    $('.bar', li).setAttribute('aria-label', `Progress: ${title}`);
    el.trayList.append(li);
    const job = { id, params, meta, data: { state: 'queued', percent: 0 }, el: li, saved: false };
    state.jobs.set(id, job);
    li.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-job-act]');
      if (btn) onJobAction(btn.dataset.jobAct, job);
    });
    renderJob(job);
    openTray();
    if (!meta.row) el.trayList.scrollTop = el.trayList.scrollHeight;
    refreshTray();
    ensurePolling();
  }

  async function startJob(params, meta, btn, quiet = false) {
    if (btn) btn.disabled = true;
    try {
      const { id } = await api('/api/download', { method: 'POST', body: JSON.stringify(params) });
      addJob(id, params, meta);
      if (btn) {
        const original = btn.textContent;
        btn.textContent = 'Added';
        btn.classList.add('is-added');
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove('is-added');
          btn.disabled = false;
        }, 1400);
      }
      return id;
    } catch (err) {
      if (!quiet) toast(err.message, true);
      if (meta.row) {
        meta.row.dataset.state = 'error';
        const cell = $('.item-state', meta.row);
        if (cell) cell.textContent = 'Failed';
      }
      if (btn) btn.disabled = false;
      return null;
    }
  }

  function onJobAction(act, job) {
    if (act === 'cancel') cancelJob(job);
    else if (act === 'save') saveFile(job.id);
    else if (act === 'retry') { removeJob(job); startJob(job.params, job.meta, null); }
    else if (act === 'remove') removeJob(job);
  }

  async function cancelJob(job) {
    try {
      job.data = await api(`/api/cancel/${encodeURIComponent(job.id)}`, { method: 'POST', body: '{}' });
      renderJob(job);
    } catch (err) {
      toast(err.message, true);
    }
    refreshTray();
    ensurePolling();
  }

  function removeJob(job) {
    job.el.remove();
    state.jobs.delete(job.id);
    refreshTray();
  }

  function saveFile(id) {
    const a = document.createElement('a');
    a.href = `/api/file/${encodeURIComponent(id)}`;
    a.download = '';
    a.hidden = true;
    document.body.append(a);
    a.click();
    a.remove();
  }

  function ensurePolling() {
    if (!state.pollTimer) state.pollTimer = setTimeout(poll, 500);
  }

  async function poll() {
    state.pollTimer = null;
    const active = [...state.jobs.values()].filter((j) => ACTIVE.includes(j.data.state));
    if (!active.length) { refreshTray(); return; }
    try {
      const { jobs } = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ ids: active.map((j) => j.id) }) });
      state.pollFails = 0;
      for (const d of jobs) {
        const job = state.jobs.get(d.id);
        if (!job) continue;
        job.data = d;
        renderJob(job);
        if (d.state === 'done' && !job.saved) {
          job.saved = true;
          saveFile(job.id);
        }
      }
    } catch (_) {
      state.pollFails += 1;
    }
    refreshTray();
    state.pollTimer = setTimeout(poll, state.pollFails ? Math.min(5000, 1000 * state.pollFails) : 700);
  }

  function openTray() {
    el.tray.hidden = false;
    el.tray.classList.remove('is-collapsed');
    el.trayToggle.setAttribute('aria-expanded', 'true');
  }

  function refreshTray() {
    const jobs = [...state.jobs.values()];
    el.tray.hidden = !jobs.length;
    document.body.classList.toggle('has-tray', jobs.length > 0);
    if (!jobs.length) {
      document.title = state.baseTitle;
      el.trayTip.hidden = true;
      return;
    }
    const active = jobs.filter((j) => ACTIVE.includes(j.data.state));
    const done = jobs.filter((j) => j.data.state === 'done').length;
    const failed = jobs.filter((j) => j.data.state === 'error').length;
    const avg = active.length
      ? Math.floor(active.reduce((sum, j) => sum + (j.data.state === 'processing' ? 99 : (j.data.percent || 0)), 0) / active.length)
      : 100;
    let text;
    if (state.pollFails > 2) text = 'Lost the connection. Is the black window still open?';
    else if (active.length) text = `${active.length} in progress, ${avg}%`;
    else if (done) text = `${plural(done, 'file')} ready${failed ? `, ${failed} failed` : ''}`;
    else if (failed) text = `${plural(failed, 'download')} failed`;
    else text = 'Nothing in progress';
    el.trayCount.textContent = text;
    document.title = active.length ? `(${avg}%) ${state.baseTitle}` : state.baseTitle;
    el.trayClear.hidden = jobs.length === active.length;
    if (!active.length) el.trayTip.hidden = true;
  }

  el.trayToggle.addEventListener('click', () => {
    const collapsed = el.tray.classList.toggle('is-collapsed');
    el.trayToggle.setAttribute('aria-expanded', String(!collapsed));
  });

  el.trayClear.addEventListener('click', () => {
    for (const job of [...state.jobs.values()]) {
      if (!ACTIVE.includes(job.data.state)) {
        job.el.remove();
        state.jobs.delete(job.id);
      }
    }
    refreshTray();
  });

  window.addEventListener('beforeunload', (e) => {
    if ([...state.jobs.values()].some((j) => ACTIVE.includes(j.data.state))) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  /* ------------------------------------------------------------ go */

  setupShortcuts();
  loadStatus();
  const link = incomingLink();
  if (link) {
    history.replaceState(null, '', '/');
    handleText(link);
  }
})();
