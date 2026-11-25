/* Screensaver and burn-in protection for the lodge display. */
(function () {
    'use strict';
    const cfgEl = document.getElementById('bdh-saver-cfg');
    if (!cfgEl) return;
    const cfg = JSON.parse(cfgEl.textContent);
    const params = new URLSearchParams(location.search);

    /* ---------- stock scenes ---------- */
    const PALETTES = {
        winter: {sky: ['#1b2b4d', '#5a6f9a', '#e9a77a', '#f5cf9a'], sun: '#ffe3b0', m1: ['#f4f6fa', '#aab4cc', '#5d6a8a'],
            m2: ['#e8eef7', '#7d8aa8'], ground: '#eef2f8', cap: .62, cap2: .55, tree: '#1d2b3a', treeSnow: true, flakes: true},
        spring: {sky: ['#3d6fae', '#7fb0e0', '#cfe4f5', '#eaf4fb'], sun: '#fff6d8', m1: ['#f6f8fb', '#9fb2c8', '#5f7690'],
            m2: ['#dfe9f2', '#7f97a8'], ground: '#9fbf7a', cap: .42, cap2: .22, patches: true, tree: '#24433a'},
        summer: {sky: ['#2b6cc4', '#5fa2e6', '#a9d3f4', '#dff0fb'], sun: '#fff3c0', sunHigh: true, m1: ['#c7d3c9', '#7f9a86', '#4d6a58'],
            m2: ['#7fa36e', '#4f7552'], ground: '#7aa95a', cap: .12, cap2: 0, flowers: true, tree: '#1f3d2c'},
        fall: {sky: ['#33406b', '#8a7aa0', '#e8a06a', '#f6c98a'], sun: '#ffd28a', m1: ['#e9e6ea', '#9a8f9e', '#5c5470'],
            m2: ['#b58a4e', '#7a5a36'], ground: '#a8874e', cap: .22, cap2: .05, tree: '#2a3a2a', larch: true},
    };
    const R1 = [[0, 760], [240, 560], [380, 640], [620, 380], [820, 560], [980, 470], [1200, 640], [1420, 500], [1640, 620], [1860, 520], [2000, 590]];
    const R2 = [[0, 900], [300, 720], [520, 820], [760, 640], [1000, 830], [1260, 700], [1540, 860], [1800, 740], [2000, 820]];
    const TREES = [[1650, 990, 130, 34], [1710, 1000, 100, 26], [1760, 1010, 80, 22], [1880, 990, 120, 30], [1560, 1005, 90, 24]];

    const ridge = (pts, fill, op) =>
        `<path d="M${pts.map(p => p.join(' ')).join(' L')} L2000 1160 L0 1160Z" fill="${fill}" opacity="${op}"/>`;

    // Snow cap: walks down both flanks of each peak by fraction f, with a jagged lower edge.
    function caps(pts, f, tall) {
        if (f <= 0) return '';
        let d = '';
        for (let i = 1; i < pts.length - 1; i++) {
            const [px, py] = pts[i], [ax, ay] = pts[i - 1], [bx, by] = pts[i + 1];
            if (py > ay || py > by) continue;
            const ff = f * (tall ? 1 - (py - 380) / 400 : 1);
            if (ff <= 0.02) continue;
            const L = [px + (ax - px) * ff, py + (ay - py) * ff], R = [px + (bx - px) * ff, py + (by - py) * ff];
            const drop = Math.abs(R[1] - py);
            d += `M${px} ${py} L${R[0]} ${R[1]}`;
            for (let j = 1; j < 6; j++) {
                const t = j / 6;
                d += ` L${R[0] + (L[0] - R[0]) * t} ${R[1] + (L[1] - R[1]) * t + (j % 2 ? -drop * .28 : drop * .08)}`;
            }
            d += ` L${L[0]} ${L[1]}Z`;
        }
        return `<path d="${d}" fill="#fbfcfe" opacity=".93"/>`;
    }

    function scene(season) {
        const c = PALETTES[season] || PALETTES.winter;
        let h = `<svg viewBox="0 0 2000 1160" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg"><defs>`
            + `<linearGradient id="bdh-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c.sky[0]}"/>`
            + `<stop offset=".45" stop-color="${c.sky[1]}"/><stop offset=".72" stop-color="${c.sky[2]}"/><stop offset="1" stop-color="${c.sky[3]}"/></linearGradient>`
            + `<linearGradient id="bdh-m1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c.m1[0]}"/>`
            + `<stop offset=".5" stop-color="${c.m1[1]}"/><stop offset="1" stop-color="${c.m1[2]}"/></linearGradient>`
            + `<linearGradient id="bdh-m2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c.m2[0]}"/>`
            + `<stop offset="1" stop-color="${c.m2[1]}"/></linearGradient></defs>`
            + `<rect width="2000" height="1160" fill="url(#bdh-sky)"/>`
            + `<circle cx="1480" cy="${c.sunHigh ? 300 : 560}" r="70" fill="${c.sun}" opacity=".75"/>`;
        h += ridge(R1, 'url(#bdh-m1)', .92) + caps(R1, c.cap, true) + ridge(R2, 'url(#bdh-m2)', 1) + caps(R2, c.cap2, false);
        h += `<path d="M0 1000 Q500 900 1000 960 T2000 940 L2000 1160 L0 1160Z" fill="${c.ground}"/>`;
        if (c.patches) {
            h += '<g fill="#f2f6fa" opacity=".85"><ellipse cx="420" cy="1010" rx="160" ry="16"/>'
                + '<ellipse cx="1180" cy="985" rx="120" ry="12"/><ellipse cx="1500" cy="1030" rx="90" ry="10"/></g>';
        }
        if (c.flowers) {
            const colors = ['#f4d35e', '#e86a92', '#f7f7f7', '#b28dff'];
            for (let i = 0; i < 90; i++) h += `<circle cx="${(i * 137) % 2000}" cy="${975 + (i * 53) % 170}" r="4" fill="${colors[i % 4]}"/>`;
        }
        TREES.forEach(([x, y, ht, w], i) => {
            h += `<path d="M${x} ${y} l${w} ${-ht} l${w} ${ht}z" fill="${c.larch && i % 2 ? '#d89a2e' : c.tree}"/>`;
            if (c.treeSnow) {
                h += `<path d="M${x + w * .55} ${y - ht * .55} l${w * .45} ${-ht * .45} l${w * .45} ${ht * .45} `
                    + `q-${w * .45} -10 -${w * .9} 0z" fill="#f4f7fb" opacity=".9"/>`;
            }
        });
        if (c.flakes) {
            for (let i = 0; i < 140; i++) {
                h += `<circle cx="${(i * 263) % 2000}" cy="${(i * 149) % 1100}" r="${1.5 + i % 3}" fill="#fff" opacity="${.35 + (i % 4) * .12}"/>`;
            }
        }
        return h + '</svg>';
    }

    /* ---------- burn-in: shift every screen by a few pixels ---------- */
    const SHIFT = 6;
    function shift() {
        // Up/left only: a positive offset would push the page past the viewport and make it scroll.
        const r = () => -Math.round(Math.random() * SHIFT) + 'px';
        document.documentElement.style.setProperty('--bx', r());
        document.documentElement.style.setProperty('--by', r());
    }
    if (cfg.burn_in) {
        shift();
        setInterval(shift, 3 * 60 * 1000);
    }

    /* ---------- the overlay ---------- */
    let root = null, data = null, season = null, timers = [];
    let night = cfg.night || null;
    const $ = sel => root.querySelector(sel);
    const esc = s => String(s).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[ch]);
    const hubClock = window.bdhClock || {format: (d, locale, opts) => new Date(d == null ? Date.now() : d).toLocaleString(locale, opts)};
    const timeText = iso => hubClock.format(iso, 'en-US', {hour: 'numeric', minute: '2-digit'});

    function build() {
        root = document.createElement('div');
        root.className = 'bdh-saver';
        root.style.display = 'none';
        root.innerHTML = '<div class="layer stock"></div><div class="layer photos"></div><div class="shade"></div>'
            + '<div class="info"><div class="clock"></div><div class="date"></div><div class="row"></div></div>'
            + '<div class="cam-name"></div><div class="caption"></div><div class="hint">Tap anywhere</div>';
        // Swallow the tap that wakes the display so it doesn't land on what's underneath.
        ['pointerdown', 'touchstart', 'mousedown'].forEach(type => root.addEventListener(type, e => {
            e.preventDefault();
            e.stopPropagation();
        }, {passive: false}));
        root.addEventListener('pointerup', e => {
            e.preventDefault();
            e.stopPropagation();
            hide();
        });
        root.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); });
        document.body.appendChild(root);
        els.set('stock', $('.layer.stock'));
        els.set('photos', $('.layer.photos'));
    }

    // After the saver closes, eat the click that the same tap may still produce.
    function swallowNextClick() {
        const eat = e => { e.preventDefault(); e.stopPropagation(); };
        window.addEventListener('click', eat, {capture: true, once: true});
        setTimeout(() => window.removeEventListener('click', eat, {capture: true}), 500);
    }

    function tick() {
        const [hm, ampm] = hubClock.format(null, 'en-US', {hour: 'numeric', minute: '2-digit'}).split(' ');
        $('.clock').innerHTML = `${esc(hm)}<small>${esc(ampm || '')}</small>`;
        $('.date').textContent = hubClock.format(null, 'en-US', {weekday: 'long', month: 'long', day: 'numeric'});
    }

    // Burn-in: the clock and label creep slowly and continuously (each move is a 120s linear transition).
    function drift() {
        const r = n => Math.round(Math.random() * n * 2 - n) + 'px';
        $('.info').style.translate = `${r(40)} ${r(24)}`;
    }

    // Night mode, in lodge time (the display's own clock may be on another zone).
    function isNight() {
        if (params.get('saver_night') === '1') return true;
        if (!night || night.from === night.until) return false;
        const now = new Date();
        const m = ((now.getUTCHours() * 60 + now.getUTCMinutes() + (night.offset || 0)) % 1440 + 1440) % 1440;
        return night.from < night.until ? m >= night.from && m < night.until : m >= night.from || m < night.until;
    }
    function applyNight() { if (root) root.classList.toggle('night', isNight()); }
    // A UniFi Connect screen can also switch its backlight off; the hub turns it back on in the morning.
    let askedSleep = false;
    function sleepAtNight() {
        applyNight();
        if (cfg.sleep && !askedSleep && isNight()) {
            askedSleep = true;
            navigator.sendBeacon(cfg.sleep);
        }
    }

    // Nothing is shown until the settings arrive, so the wrong layer can't flash up first.
    let showing = null;
    // A layer per source on screen: the built-in photos and stock art, plus one per app source.
    const els = new Map();
    const el = id => els.get(id) || null;
    const appLayer = id => ((data && data.layers) || []).find(l => l.id === id) || null;
    const kindOf = id => id === 'stock' || id === 'photos' ? id : ((appLayer(id) || {}).kind || '');
    // Photos, stock art and app photo sources each rotate through their own shuffled deck.
    const isDeck = id => kindOf(id) === 'stock' || kindOf(id) === 'photos';
    const decks = {};
    const deckOf = id => decks[id] || (decks[id] = {order: [], key: '', pos: 0});
    // Images that failed to load are skipped for a while, so one bad file can't leave the screen black.
    const broken = new Map();
    const BROKEN_MS = 10 * 60 * 1000;
    const isBroken = url => broken.has(url) && Date.now() - broken.get(url) < BROKEN_MS;
    function photoList() { return ((data && data.photos) || []).filter(p => !isBroken(p.url)); }
    function stockList() { return ((data && data.stock) || []).filter(u => !isBroken(u)).map(url => ({url})); }
    function items(id) {
        if (id === 'stock') return stockList();
        if (id === 'photos') return photoList();
        const layer = appLayer(id);
        return layer ? layer.items.filter(i => !isBroken(i.url)) : [];
    }
    const rand = (lo, hi) => lo + Math.random() * (hi - lo);

    function shuffle(urls, avoidFirst) {
        const a = urls.slice();
        for (let i = a.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [a[i], a[j]] = [a[j], a[i]];
        }
        // Don't show the same photo twice in a row across a reshuffle.
        if (a.length > 1 && a[0] === avoidFirst) [a[0], a[1]] = [a[1], a[0]];
        return a;
    }

    // Reshuffles only when the set of images changes, so a refresh doesn't restart the cycle.
    function syncOrder(id) {
        const deck = deckOf(id);
        const urls = items(id).map(p => p.url);
        const key = urls.slice().sort().join('\n');
        if (key !== deck.key) {
            deck.key = key;
            deck.order = shuffle(urls);
            deck.pos = 0;
        }
    }

    function currentFrame(id) {
        const layer = el(id);
        const frames = layer ? layer.querySelectorAll('.frame') : [];
        return frames[frames.length - 1] || null;
    }

    function clearFrames(id, keep) {
        const layer = el(id);
        if (layer) layer.querySelectorAll('.frame').forEach(f => { if (f !== keep) f.remove(); });
    }

    // Library photos and app photos carry a caption (a date or the app's own), shown top right.
    function showCaption() {
        const captioned = !!showing && showing !== 'stock' && isDeck(showing);
        const frame = captioned ? currentFrame(showing) : null;
        const item = frame && items(showing).find(p => p.url === frame.dataset.src);
        const text = (item && item.caption) || '';
        $('.caption').textContent = text;
        $('.caption').classList.toggle('on', captioned && !!text);
    }

    function rotateSeconds() {
        return Math.min(90, Math.max(15, (data && data.rotate_seconds) || 30));
    }

    // Offset of a zoomed-in view pushed toward the edge along `angle`. With transform scale(s)
    // translate(t%), the visible shift is s*t, so |t| must stay under (s-1)/(2s) of the frame or
    // an edge shows; 85% of that leaves margin.
    function kenBurnsOffset(s, angle) {
        const lim = (s - 1) / (2 * s) * 100 * 0.85;
        return [Math.cos(angle) * lim, Math.sin(angle) * lim];
    }

    // Ken Burns: usually starts close in on one side of the photo and pulls back to it whole and
    // centred; sometimes the reverse. It lasts as long as the photo stays up.
    function kenBurns(frame) {
        const every = rotateSeconds();
        // Photos shown whole over a blurred copy (portraits) move less.
        const fill = frame.classList.contains('fill');
        const close = fill ? rand(1.45, 1.55) : rand(1.28, 1.34);
        const wide = fill ? rand(1.03, 1.06) : rand(1.0, 1.02);
        const [cx, cy] = kenBurnsOffset(close, Math.random() * 2 * Math.PI);
        const pullOut = Math.random() < 2 / 3;
        const [s0, s1] = pullOut ? [close, wide] : [wide, close];
        const [x0, y0, x1, y1] = pullOut ? [cx, cy, 0, 0] : [0, 0, cx, cy];
        frame.style.setProperty('--s0', s0.toFixed(3));
        frame.style.setProperty('--s1', s1.toFixed(3));
        [['--x0', x0], ['--y0', y0], ['--x1', x1], ['--y1', y1]]
            .forEach(([k, v]) => frame.style.setProperty(k, v.toFixed(2) + '%'));
        frame.style.animationDuration = (every + 4) + 's';
    }

    function restartKenBurns(frame) {
        if (!frame) return;
        kenBurns(frame);
        frame.style.animationName = 'none';
        void frame.offsetWidth;
        frame.style.animationName = '';
    }

    // Brings in the next picture of a deck, crossfading over the current one if it's on screen.
    // The stock layer keeps its drawn scene underneath, so it shows if no painting loads.
    function nextFrame(id, fade) {
        const layer = el(id);
        if (!layer) return;
        const list = items(id);
        if (!list.length) { clearFrames(id); showCaption(); return; }
        syncOrder(id);
        const deck = deckOf(id);
        const current = currentFrame(id);
        if (deck.pos >= deck.order.length) {
            deck.order = shuffle(deck.order, current && current.dataset.src);
            deck.pos = 0;
        }
        let url = deck.order[deck.pos++];
        if (current && current.dataset.src === url) {
            if (deck.order.length < 2) return;
            url = deck.order[deck.pos++ % deck.order.length];
        }
        const frame = document.createElement('div');
        frame.className = 'frame';
        frame.dataset.src = url;
        const bg = new Image(), fg = new Image();
        bg.alt = fg.alt = '';
        bg.className = 'bg';
        fg.className = 'fg';
        fg.onload = () => {
            if (el(id) !== layer) return;
            // Close to the screen's shape: fill it. Otherwise (portraits) show it whole over a blurred copy.
            const ratio = (fg.naturalWidth / fg.naturalHeight) / (window.innerWidth / window.innerHeight);
            frame.classList.toggle('fill', ratio > 0.8 && ratio < 1.25);
            kenBurns(frame);
            layer.append(frame);
            if (!fade) {
                frame.classList.add('in');
                clearFrames(id, frame);
            } else {
                requestAnimationFrame(() => requestAnimationFrame(() => frame.classList.add('in')));
                setTimeout(() => clearFrames(id, frame), 3200);
            }
            showCaption();
        };
        fg.onerror = () => {
            broken.set(url, Date.now());
            // Try another image, or fall back (another source, or the drawn scene) if none are left.
            if (data) render(data);
        };
        frame.append(bg, fg);
        bg.src = url;
        fg.src = url;
    }

    /* ---------- live pictures (an app's cameras) ---------- */
    // Per live source: which picture is up, its refresh timer, and whether it has never loaded.
    const live = {};
    const liveOf = id => live[id] || (live[id] = {pos: 0, timer: null, every: 0, broken: false});
    const liveItems = id => (appLayer(id) || {items: []}).items;
    function liveItem(id) {
        const list = liveItems(id);
        return list.length ? list[liveOf(id).pos % list.length] : null;
    }
    function dropLive(id) {
        if (live[id]) clearInterval(live[id].timer);
        delete live[id];
    }

    // The camera's name, from the picture actually on screen.
    function showCamName() {
        const layer = showing && kindOf(showing) === 'live' ? el(showing) : null;
        const img = layer && layer.querySelector('img');
        const label = (img && img.dataset.label) || '';
        $('.cam-name').textContent = label;
        $('.cam-name').classList.toggle('on', !!label);
    }

    // Live images change behind the same URL: refetch with a cache-buster, swap in once loaded.
    // A failed fetch keeps the last frame up.
    function loadLive(id, fresh) {
        const item = liveItem(id);
        const layer = el(id);
        if (!item || !layer) return;
        const st = liveOf(id);
        const img = new Image();
        img.alt = '';
        img.dataset.src = item.url;
        img.dataset.label = item.label || '';
        img.onload = () => {
            const now = liveItem(id);
            if (el(id) === layer && now && now.url === item.url) {
                layer.replaceChildren(img);
                showCamName();
            }
            st.broken = false;
        };
        img.onerror = () => {
            // Only fall back to another source if there's no earlier frame to keep showing.
            if (!layer.firstChild && !st.broken && data) { st.broken = true; render(data); }
        };
        img.src = fresh ? item.url + (item.url.includes('?') ? '&' : '?') + 't=' + Date.now() : item.url;
        refreshEvery(id, item);
    }

    // Only the picture on screen is refetched, every refresh_seconds (at least 10s).
    function refreshEvery(id, item) {
        const st = liveOf(id);
        const every = item.refresh_seconds ? Math.max(10, item.refresh_seconds) * 1000 : 0;
        if (every === st.every && (st.timer !== null || !every)) return;
        clearInterval(st.timer);
        st.timer = null;
        st.every = every;
        if (every) st.timer = setInterval(() => {
            if (showing === id && root && root.classList.contains('on')) loadLive(id, true);
        }, every);
    }

    // On to the source's next picture (unless one is picked); it swaps in once it has loaded.
    function advanceLive(id) {
        const layer = appLayer(id);
        if (!layer || layer.pick || layer.items.length < 2) return;
        const st = liveOf(id);
        st.pos = (st.pos + 1) % layer.items.length;
        loadLive(id, true);
    }

    // A layer per app source, under the shade and text; gone ones are dropped.
    function syncLayers(list) {
        const want = new Map(list.map(l => [l.id, l.kind]));
        els.forEach((node, id) => {
            if (id === 'stock' || id === 'photos' || want.get(id) === node.dataset.kind) return;
            node.remove();
            els.delete(id);
            dropLive(id);
            delete decks[id];
        });
        list.forEach(layer => {
            let node = el(layer.id);
            if (!node) {
                node = document.createElement('div');
                node.className = layer.kind === 'live' ? 'layer cam' : 'layer';
                node.dataset.id = layer.id;
                node.dataset.kind = layer.kind;
                $('.shade').before(node);
                els.set(layer.id, node);
                if (layer.id === showing) node.classList.add('on');
            }
            if (layer.kind !== 'live') return;
            const st = liveOf(layer.id);
            if (st.pos >= layer.items.length) st.pos = 0;
            const item = layer.items[st.pos];
            const shown = node.querySelector('img');
            if (!shown || shown.dataset.src !== item.url) loadLive(layer.id, false);
            else refreshEvery(layer.id, item);
        });
    }

    function showLayer(name) {
        const was = showing;
        showing = name;
        els.forEach((node, id) => node.classList.toggle('on', id === name));
        els.forEach((node, id) => {
            if (!isDeck(id)) return;
            // Swap in the next image once it's faded out, and restart the pan for the one coming back.
            if (was === id && name !== id) setTimeout(() => nextFrame(id, false), 3500);
            if (was !== id && name === id) restartKenBurns(currentFrame(id));
        });
        if (was && was !== name) {
            // A camera coming back gets a fresh picture; one going away moves on to its next.
            if (kindOf(name) === 'live') loadLive(name, true);
            if (kindOf(was) === 'live') setTimeout(() => advanceLive(was), 3500);
        }
        showCamName();
        showCaption();
    }

    function render(d) {
        data = d;
        if ('night' in d) night = d.night;
        applyNight();
        if (d.season !== season) {
            season = d.season;
            $('.layer.stock').innerHTML = scene(season);
        }
        $('.row').innerHTML = (d.stats || []).map(s =>
            `<div><b>${esc(s.time ? timeText(s.time) : s.value)}</b>${esc(s.label)}</div>`).join('');
        syncLayers(d.layers || []);
        const sources = d.sources || ['stock'];
        // A live picture still loading counts as there: its layer stays dark until the image arrives.
        const empty = id => kindOf(id) === 'live' ? !liveItems(id).length || liveOf(id).broken
            : id !== 'stock' && !items(id).length;
        const target = sources.includes(showing) && !empty(showing) ? showing : (sources.find(s => !empty(s)) || 'stock');
        els.forEach((node, id) => {
            if (!isDeck(id)) return;
            // Load only what can be shown, so e.g. a stock painting can't flash up before the photos.
            if (!sources.includes(id) && id !== target) { clearFrames(id); return; }
            const loaded = currentFrame(id);
            syncOrder(id);
            if (!items(id).length) clearFrames(id);
            else if (!loaded || !items(id).some(p => p.url === loaded.dataset.src)) nextFrame(id, false);
        });
        showCaption();
        if (target !== showing) showLayer(target);
    }

    function load() {
        // The settings page's Preview passes its unsaved values (the API only honours them for staff).
        const q = new URLSearchParams();
        [['saver_season', 'season'], ['saver_source', 'source'], ['saver_pick', 'pick'],
         ['saver_rotate', 'rotate'], ['saver_caption', 'caption']].forEach(([from, to]) => {
            if (params.has(from)) q.set(to, params.get(from));
        });
        const url = cfg.api + (q.toString() ? '?' + q : '');
        return fetch(url, {cache: 'no-store'}).then(r => r.ok ? r.json() : Promise.reject(r.status)).then(render)
            .catch(() => { if (!data) render({season: 'winter', label: 'Winter', sources: ['stock'], stats: []}); });
    }

    // Each "Change every": the next source in a Mix, else the next picture of the one on screen.
    function rotate() {
        const sources = (data && data.sources) || ['stock'];
        if (sources.length < 2) {
            if (isDeck(showing) && items(showing).length > 1) nextFrame(showing, true);
            else if (kindOf(showing) === 'live') advanceLive(showing);
            return;
        }
        // Skip sources with nothing loaded yet (or a camera that never loaded).
        const at = sources.indexOf(showing);
        for (let n = 1; n < sources.length; n++) {
            const next = sources[(at + n + sources.length) % sources.length];
            const node = el(next);
            if (node && node.firstChild && !(kindOf(next) === 'live' && liveOf(next).broken)) { showLayer(next); return; }
        }
    }

    // Has the layer about to be shown got its first image (or is it the drawn stock scene)?
    function ready() {
        if (!data) return false;
        if (kindOf(showing) === 'live') return !!(el(showing) && el(showing).firstChild);
        if (isDeck(showing)) return !!currentFrame(showing) || (showing === 'stock' && !items('stock').length);
        return true;
    }

    // Fetch the settings and the first image off screen, then fade in with them (or after a few seconds anyway).
    // Any touch meanwhile cancels it, like it would the saver itself.
    let preparing = 0;
    function show() {
        if (root && root.classList.contains('on')) return;
        if (preparing) return;
        if (!root) build();
        const token = preparing = Date.now();
        const started = Date.now();
        load().then(function wait() {
            if (preparing !== token) return;
            if (!ready() && Date.now() - started < 4000) { setTimeout(wait, 100); return; }
            preparing = 0;
            begin();
        });
    }

    function begin() {
        root.style.display = '';
        root.style.pointerEvents = '';
        sleepAtNight();
        tick();
        drift();
        if (showing) showLayer(showing);
        timers.push(setInterval(rotate, rotateSeconds() * 1000));
        timers.push(setInterval(tick, 10 * 1000), setInterval(drift, 120 * 1000), setInterval(load, 5 * 60 * 1000),
            setInterval(sleepAtNight, 30 * 1000));
        requestAnimationFrame(() => root.classList.add('on'));
    }

    function hide(swallow = true) {
        preparing = 0;
        askedSleep = false;
        if (!root) return;
        timers.forEach(clearInterval);
        timers = [];
        root.classList.remove('on');
        root.style.pointerEvents = 'none';
        if (swallow) swallowNextClick();
        setTimeout(() => { if (!root.classList.contains('on')) root.style.display = 'none'; }, 1300);
        arm();
    }

    /* ---------- idle detection ---------- */
    const idleMs = Math.max(1, cfg.idle_minutes || 10) * 60 * 1000;
    let idleTimer = null;
    let paused = false;
    // A page that must stay on screen (e.g. live cameras) opts out with
    // <meta name="bdh-screensaver" content="off">; alerts' resume() can't undo it.
    const held = !!document.querySelector('meta[name="bdh-screensaver"][content="off"]');
    const isDisplay = () => window.innerWidth >= 1280;
    function arm() {
        preparing = 0;
        clearTimeout(idleTimer);
        if (!paused && !held && isDisplay()) idleTimer = setTimeout(show, idleMs);
    }
    ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart'].forEach(type =>
        document.addEventListener(type, () => { if (!root || !root.classList.contains('on')) arm(); },
            {passive: true, capture: true}));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
    window.addEventListener('resize', arm);

    /* Something else (an alert) owns the screen: keep the saver away until resume(). */
    function pause() {
        paused = true;
        preparing = 0;
        clearTimeout(idleTimer);
    }
    function resume() {
        paused = false;
        arm();
    }
    const wake = () => hide(false);

    window.BDHSaver = {show, hide, wake, pause, resume, scene, isNight};
    if (params.get('saver') === '1') show(); else arm();
})();
