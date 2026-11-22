"""The page shown while a sleeping or updating app starts: the hub's monogram
inside a spinning ring. It reloads itself until the app answers.

One page for both the app proxy (outside Django's templates and static files,
so everything is inline) and the hub's ``/apps/<app>/`` shell.
"""
import html
from functools import lru_cache

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{retry}"><title>{name}</title>
<style>
html,body{{margin:0;height:100%;background:#0b1622;color:#d7e0ea;font-family:system-ui,sans-serif}}
body{{display:flex;flex-direction:column;align-items:center;justify-content:center}}
.bdh-loader{{position:relative;width:150px;height:150px;display:flex;align-items:center;justify-content:center}}
.bdh-ring{{position:absolute;inset:0;border-radius:50%;border:3px solid rgba(255,255,255,.08);
border-top-color:#8ab4f8;animation:bdh-spin 1.1s linear infinite}}
.bdh-mark{{width:90px;height:90px}}
.bdh-mark svg{{width:100%;height:100%;display:block}}
p{{margin:26px 0 0;font-size:17px;letter-spacing:.02em}}
@keyframes bdh-spin{{to{{transform:rotate(360deg)}}}}
@media (prefers-reduced-motion:reduce){{.bdh-ring{{animation-duration:4s}}}}
</style></head>
<body><div class="bdh-loader" role="progressbar" aria-label="Loading"><div class="bdh-ring"></div>
<div class="bdh-mark">{mark}</div></div><p>{name} is waking up…</p></body></html>"""


@lru_cache(maxsize=1)
def _mark():
    from bdh_sdk.branding import monogram_svg

    return monogram_svg(light=True)


def page(name, retry):
    """The starting page for the app guests call ``name``, reloading every ``retry`` s."""
    return PAGE.format(name=html.escape(name), retry=int(retry), mark=_mark())
