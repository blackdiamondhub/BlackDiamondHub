// Runs broker/static/broker/embed-host.js against a stub DOM and replays
// postMessage events at it (see test_security_boundary.py).
//
//   node embed_host_harness.js <embed-host.js> <bare 0|1>  < messages.json
//
// messages.json is a list of {origin, source: "frame"|"other", data}; prints a
// list with what each one did: {nav, title, style, appended, events, error}.
'use strict';
const fs = require('fs');
const vm = require('vm');

const [script, bare] = process.argv.slice(2);
const HUB = 'http://hub.local:8000';
let log;

function element(tag) {
    return {
        tagName: tag, style: {}, children: [], dataset: {}, textContent: '', id: '',
        appendChild(child) { this.children.push(child); if (log) log.appended.push(child); return child; },
        remove() {},
        classList: {add() {}},
    };
}

const frame = Object.assign(element('iframe'), {
    contentWindow: {name: 'app frame'},
    closest() { return null; },
    getBoundingClientRect() { return {top: 0}; },
});
frame.dataset = {appOrigin: 'http://hub.local:9100', bare, navUrl: '/apps/_nav/', appId: 'cams'};
const title = element('span');
const ids = {'bdh-app-frame': frame, 'bdh-app-title': title};
const document = {
    head: element('head'), body: element('body'),
    getElementById(id) { return ids[id] || null; },
    createElement(tag) {
        const el = element(tag);
        return new Proxy(el, {set(t, k, v) { t[k] = v; if (k === 'id' && v) ids[v] = t; return true; }});
    },
    dispatchEvent(ev) { log.events.push(ev.type); },
};
const listeners = {};
const location = {
    origin: HUB,
    get href() { return HUB + '/apps/cams/'; },
    set href(v) { log.nav.push(String(v)); },
};
const window = {
    location, scrollY: 0, innerHeight: 800,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
};
window.top = window;

vm.runInNewContext(fs.readFileSync(script, 'utf8'), {
    window, document, URL, JSON, Object, Array, String, Math, Proxy,
    Event: class { constructor(type) { this.type = type; } },
    localStorage: {setItem() {}, getItem() { return null; }},
    setTimeout() {},
});

const out = [];
for (const msg of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    log = {nav: [], events: [], appended: []};
    title.textContent = '';
    const before = ids['bdh-app-bg'] ? ids['bdh-app-bg'].textContent : '';
    let error = null;
    try {
        for (const fn of listeners.message || []) {
            fn({origin: msg.origin, source: msg.source === 'frame' ? frame.contentWindow : {}, data: msg.data});
        }
    } catch (err) {
        error = String(err);
    }
    const style = ids['bdh-app-bg'] ? ids['bdh-app-bg'].textContent : '';
    out.push({
        nav: log.nav, title: title.textContent, style: style === before ? '' : style,
        appended: log.appended.map((el) => el.tagName), events: log.events, error,
    });
}
process.stdout.write(JSON.stringify(out));
