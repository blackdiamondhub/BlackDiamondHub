/* The hub side of the iframe contract with container apps (see bdh_sdk's embed.js).
 *
 * Messages are only taken from the app's frame on the app's origin, and only
 * as {bdh: 1, type, ...}: title, resize, activity, navigate, toast, theme, open-overlay.
 */
(function () {
    'use strict';
    var frame = document.getElementById('bdh-app-frame');
    if (!frame) return;
    var appOrigin = frame.dataset.appOrigin;
    var bare = frame.dataset.bare === '1';
    var navUrl = frame.dataset.navUrl;
    var target = bare ? window.top : window;
    // Same pattern as _theme.html: hex or rgb()/rgba() only, nothing that could
    // close the CSS rule.
    var THEME_COLOR = /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,\s%]+\))$/;

    var wrapper = frame.closest('.content-wrapper');
    if (wrapper) wrapper.classList.add('bdh-app-host');

    function fit() {
        if (bare) return;
        var top = frame.getBoundingClientRect().top + window.scrollY;
        frame.style.height = Math.max(200, window.innerHeight - top) + 'px';
    }
    fit();
    window.addEventListener('resize', fit);

    // A same-origin hub URL for ``path``, or null. Resolved first so tricks the URL
    // parser strips (tabs, newlines, backslashes) can't point the hub elsewhere.
    function hubUrl(path) {
        if (typeof path !== 'string' || path.charAt(0) !== '/' || /[\u0000-\u001f\\]/.test(path)) return null;
        var u;
        try { u = new URL(path, target.location.href); } catch (err) { return null; }
        if (u.origin !== target.location.origin || /^\/_broker(\/|$)/.test(u.pathname)) return null;
        return u.pathname + u.search + u.hash;
    }

    var toastBox = null;
    function toast(message, level) {
        if (!toastBox) {
            toastBox = document.createElement('div');
            toastBox.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
                'z-index:3000;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none';
            document.body.appendChild(toastBox);
        }
        var colors = {success: '#1f7a3a', warning: '#8a6d00', error: '#9b2226', info: '#1d3557'};
        var el = document.createElement('div');
        el.textContent = String(message).slice(0, 300);
        el.style.cssText = 'padding:10px 18px;border-radius:10px;color:#fff;font-size:15px;' +
            'box-shadow:0 4px 16px rgba(0,0,0,.4);background:' + (colors[level] || colors.info);
        toastBox.appendChild(el);
        setTimeout(function () { el.remove(); }, 4000);
    }

    var handlers = {
        title: function (d) {
            var t = document.getElementById('bdh-app-title');
            if (t && typeof d.title === 'string' && d.title) t.textContent = d.title.slice(0, 80);
        },
        resize: function () { /* the frame fills the page; the app scrolls itself */ },
        activity: function () {
            // The screensaver and idle timers listen for pointer activity on the document.
            document.dispatchEvent(new Event('pointermove'));
        },
        navigate: function (d) {
            if (typeof d.name === 'string' && /^[\w:.-]{1,100}$/.test(d.name)) {
                var args = Array.isArray(d.args) ? d.args.filter(function (a) {
                    return typeof a === 'string' || typeof a === 'number';
                }) : [];
                target.location.href = navUrl + '?name=' + encodeURIComponent(d.name) +
                    '&args=' + encodeURIComponent(JSON.stringify(args));
            } else {
                var url = hubUrl(d.path);
                if (url) target.location.href = url;
            }
        },
        toast: function (d) {
            if (typeof d.message === 'string' && d.message) toast(d.message, d.level);
        },
        theme: function (d) {
            // Paint the hub page behind the frame in the app's background, and
            // remember it so the next visit starts in it (see _theme.html).
            var c = d.background;
            if (bare) return;  // popup pages stay transparent over the overlay backdrop
            if (typeof c !== 'string' || !THEME_COLOR.test(c) || /^rgba\(.*,\s*0(\.0*)?\s*\)$/.test(c)) return;
            var s = document.getElementById('bdh-app-bg');
            if (!s) {
                s = document.createElement('style');
                s.id = 'bdh-app-bg';
                document.head.appendChild(s);
            }
            s.textContent = 'html,body,.content-wrapper,#bdh-app-frame{background:' + c + ' !important}';
            try { localStorage.setItem('bdh-app-bg:' + frame.dataset.appId, c); } catch (err) { /* storage blocked */ }
        },
        'open-overlay': function (d) {
            if (d.kind === 'feedback') {
                var modal = document.getElementById('feedback-modal');
                if (modal && window.bootstrap) window.bootstrap.Modal.getOrCreateInstance(modal).show();
            }
        },
    };

    window.addEventListener('message', function (e) {
        if (e.origin !== appOrigin || e.source !== frame.contentWindow) return;
        var d = e.data;
        if (!d || typeof d !== 'object' || d.bdh !== 1 || typeof d.type !== 'string') return;
        var fn = Object.prototype.hasOwnProperty.call(handlers, d.type) ? handlers[d.type] : null;
        if (fn) fn(d);
    });
})();
