from django import template
from django.templatetags.static import static

register = template.Library()


@register.filter
def tile_icon(icon):
    """A tile icon's URL: container apps' icons are already URLs on the hub,
    in-process apps' are static file paths."""
    icon = str(icon or "")
    if not icon:
        return ""
    if icon.startswith(("/", "http://", "https://")):
        return icon
    return static(icon)
