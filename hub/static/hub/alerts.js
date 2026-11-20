/* Pop-up alerts pushed by apps through bdh_sdk.alerts (see hub/alerts.py).
 * One alert at a time: the newest replaces the current one, and a repeat of the
 * same key just restarts its countdown (unless "Keep open" was tapped). A new
 * alert plays its sound (chimes.js), then reads out `speak`, unless it's the
 * night window; a repeat with `resound` plays them again. A "dismiss" message
 * closes the alert wherever it is showing. Only the hub's own display listens.
 * The alert's own iframe can post {bdhAlert: 'hold'} (e.g. while the doorbell
 * mic is open) to pause the countdown, 'release' to resume with a short grace
 * period, and 'hangup' to close the alert. */
(function () {
    'use strict';
    const isDisplay = () => window.innerWidth >= 1280;
    let ws = null, backoff = 1000, retry = null, resizeTimer = null;
    const KEEP_MAX_S = 600, RELEASE_GRACE_S = 30;
    let root = null, current = null, closeTimer = null, closesAt = 0, kept = false, held = false, stopVoice = null;

    function el(tag, cls) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        return e;
    }

    function build() {
        root = el('div', 'hub-alert');
        root.setAttribute('role', 'dialog');
        root.setAttribute('aria-modal', 'true');
        root.innerHTML =
            '<div class="hub-alert-card">' +
            '<div class="hub-alert-head"><div class="hub-alert-text">' +
            '<div class="hub-alert-title"></div><div class="hub-alert-body"></div></div>' +
            '<button type="button" class="hub-alert-keep" aria-pressed="false">' +
            '<i class="fas fa-thumbtack"></i><span>Keep open</span></button>' +
            '<button type="button" class="hub-alert-close" aria-label="Close">' +
            '<i class="fas fa-times"></i></button></div>' +
            '<div class="hub-alert-frame"></div>' +
            '<div class="hub-alert-bar"><span></span></div></div>';
        root.addEventListener('click', e => {
            if (e.target.closest('.hub-alert-keep')) keepOpen();
            else if (e.target === root || e.target.closest('.hub-alert-close')) close();
        });
        document.body.appendChild(root);
    }

    function setKept(on) {
        kept = on;
        root.classList.toggle('kept', on);
        const btn = root.querySelector('.hub-alert-keep');
        btn.setAttribute('aria-pressed', String(on));
        btn.querySelector('span').textContent = on ? 'Kept open' : 'Keep open';
    }

    /* Stop the countdown; the alert stays until closed, up to a safety cap so a
     * forgotten live feed doesn't stream (and hold off the screensaver) forever. */
    function keepOpen() {
        if (!current || kept) return;
        setKept(true);
        clearTimeout(closeTimer);
        closeTimer = setTimeout(close, KEEP_MAX_S * 1000);
    }

    /* While held the alert behaves as kept open (with the same safety cap); on
     * release a not-kept alert gets a fresh short countdown. */
    function hold(on) {
        if (!current || held === on) return;
        held = on;
        root.classList.toggle('held', on);
        if (kept) return;
        clearTimeout(closeTimer);
        closeTimer = null;
        if (on) closeTimer = setTimeout(close, KEEP_MAX_S * 1000);
        else startTimer(RELEASE_GRACE_S, false);
    }

    function startTimer(ttl, repeat) {
        const ends = Date.now() + ttl * 1000;
        // A repeat that ends when the running countdown does (e.g. a timer's
        // second chime) keeps the bar going instead of refilling it.
        if (repeat && closeTimer && Math.abs(ends - closesAt) < 1500) return;
        closesAt = ends;
        clearTimeout(closeTimer);
        closeTimer = setTimeout(close, ttl * 1000);
        const bar = root.querySelector('.hub-alert-bar span');
        bar.style.transition = 'none';
        bar.style.transform = 'scaleX(1)';
        void bar.offsetWidth;
        bar.style.transition = 'transform ' + ttl + 's linear';
        bar.style.transform = 'scaleX(0)';
    }

    function hush() {
        if (stopVoice) stopVoice();
        stopVoice = null;
    }

    function sound(alert) {
        const chimes = window.BDHChimes;
        if (!chimes) return;
        hush();
        if (alert.speak && chimes.say) stopVoice = chimes.say(alert.speak, alert.sound);
        else chimes.play(alert.sound);
    }

    function show(alert) {
        if (!root) build();
        const saver = window.BDHSaver;
        if (saver) {
            if (alert.wake !== false && saver.wake) saver.wake();
            if (saver.pause) saver.pause();
        }
        const same = current && current.key === alert.key && current.embed_url === alert.embed_url;
        current = alert;
        // A new alert chimes; repeats (another ring) stay quiet unless they ask to
        // resound, and the night window is always quiet.
        if ((!same || alert.resound) && !(saver && saver.isNight && saver.isNight())) sound(alert);
        if (!same) {
            const frame = root.querySelector('.hub-alert-frame');
            frame.textContent = '';
            const iframe = el('iframe');
            iframe.setAttribute('allow', 'autoplay; fullscreen');
            iframe.setAttribute('title', alert.title || 'Alert');
            iframe.src = alert.embed_url;
            frame.appendChild(iframe);
        }
        root.querySelector('.hub-alert-title').textContent = alert.title || '';
        const body = root.querySelector('.hub-alert-body');
        body.textContent = alert.body || '';
        body.hidden = !alert.body;
        root.classList.add('on');
        if (!same) { setKept(false); held = false; root.classList.remove('held'); }
        // A repeat of a kept or held alert (e.g. another ring) leaves it open.
        if (!kept && !held) startTimer(Math.max(1, Math.min(600, alert.ttl || 60)), same);
    }

    function close() {
        if (!root || !current) return;
        clearTimeout(closeTimer);
        closeTimer = null;
        hush();
        current = null;
        setKept(false);
        held = false;
        root.classList.remove('on', 'held');
        // Drop the iframe so live video stops streaming.
        root.querySelector('.hub-alert-frame').textContent = '';
        const saver = window.BDHSaver;
        if (saver && saver.resume) saver.resume();
    }

    function connect() {
        clearTimeout(retry);
        if (ws || !isDisplay()) return;
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(proto + '//' + location.host + '/ws/hub/alerts/');
        ws.onopen = () => { backoff = 1000; };
        ws.onmessage = e => {
            let msg;
            try { msg = JSON.parse(e.data); } catch (err) { return; }
            if (msg && msg.type === 'alert' && typeof msg.embed_url === 'string'
                && msg.embed_url[0] === '/' && msg.embed_url[1] !== '/') show(msg);
            else if (msg && msg.type === 'dismiss' && current
                && current.app === msg.app && current.key === msg.key) close();
        };
        ws.onclose = () => {
            ws = null;
            if (!isDisplay()) return;
            retry = setTimeout(connect, backoff);
            backoff = Math.min(backoff * 2, 30000);
        };
    }

    function disconnect() {
        clearTimeout(retry);
        if (ws) { ws.onclose = null; ws.close(); ws = null; }
        close();
    }

    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => (isDisplay() ? connect() : disconnect()), 500);
    });
    window.addEventListener('message', e => {
        const iframe = root && current && root.querySelector('.hub-alert-frame iframe');
        if (!iframe || e.source !== iframe.contentWindow || !e.data) return;
        const cmd = e.data.bdhAlert;
        if (cmd === 'hold' || cmd === 'release') hold(cmd === 'hold');
        else if (cmd === 'hangup') close();
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    window.BDHAlerts = {show, close, keepOpen};
    connect();
})();
