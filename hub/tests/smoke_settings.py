"""Settings for HubSmokeCommandTests: adds a plugin with a tile on a built-in
page, so the smoke test doesn't depend on which apps a site installs
in-process."""
from dataclasses import replace

from BlackDiamondHub.settings_test import *  # noqa: F401,F403
from BlackDiamondHub.settings_test import HUB_PLUGINS
from hub.plugins import Tile

HUB_PLUGINS = tuple(
    replace(spec, tiles=(Tile(title="Location", url_name="hub_settings_location",
                              icon="favicon-32x32.png", section="lodge"),))
    if spec.id == "settings" else spec
    for spec in HUB_PLUGINS
)
