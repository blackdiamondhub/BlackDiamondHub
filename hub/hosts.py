"""Host names the hub answers to besides the ALLOWED_HOSTS env list.

ALLOWED_HOSTS stops DNS rebinding: a public site pointing its own name at the
hub's LAN IP. Names in the LAN's own DNS domains (the search domains the router
hands out, e.g. ``bdl``) and private-use suffixes can't be registered publicly,
so a local record like ``bdh.bdl`` just works without editing the server.
"""
from pathlib import Path

LOCAL_HOSTS = ['localhost', '.localhost', '127.0.0.1', '[::1]']
PRIVATE_SUFFIXES = ['.local', '.lan', '.home.arpa', '.internal']


def search_domains(resolv_conf='/etc/resolv.conf'):
    """The resolver's search domains, e.g. ['bdl', 'mi']."""
    try:
        text = Path(resolv_conf).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    domains = []
    for line in text.splitlines():
        words = line.split()
        if words and words[0] in ('search', 'domain'):
            domains += [w.strip('.').lower() for w in words[1:] if w.strip('.')]
    return list(dict.fromkeys(domains))


def allowed_hosts(configured, resolv_conf='/etc/resolv.conf'):
    """The configured hosts plus local names. '.bdl' matches bdl and *.bdl."""
    hosts = [h.strip() for h in configured if h.strip()]
    hosts += LOCAL_HOSTS + PRIVATE_SUFFIXES
    hosts += ['.' + d for d in search_domains(resolv_conf)]
    return list(dict.fromkeys(hosts))
