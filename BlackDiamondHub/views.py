from bdh_sdk.units import units
from django.shortcuts import render
from social_django.utils import load_strategy
from social_core.backends.spotify import SpotifyOAuth2

from display.views import home_theme
from hub.modes import current_stay, resolve_mode, tiles_for_mode, welcome
from hub.plugins import GUEST
from hub.status import lodge_card
from hub.views import PAGE_TIMEOUT, home_data


def landing_page(request):
    mode = resolve_mode(request)
    card = lodge_card(mode)
    sections = tiles_for_mode(mode)

    def holding(kind):
        return next((key for key, _, tiles in sections if any(t['kind'] == kind for t in tiles)), None)

    context = {
        'hub_mode': mode,
        'hub_sections': sections,
        'hub_card': card,
        # Status lines and quick actions, so they show without waiting on a poll.
        'hub_home': home_data(mode, timeout=PAGE_TIMEOUT),
        # Without a card app, guests still get the plain welcome line.
        'hub_welcome': welcome(current_stay(request)) if mode == GUEST and not card else None,
        # It heads the pane with the lodge card, else the first pane.
        'hub_welcome_key': holding('lodge') or next((key for key, _, _ in sections), None),
        'hub_theme': home_theme(),
        'hub_imperial': units().imperial,
    }
    return render(request, 'index.html', context)


def refresh_spotify_token(user):
    social_auth = user.social_auth.get(provider='spotify')
    strategy = load_strategy()
    spotify_backend = SpotifyOAuth2(strategy=strategy)

    # Check if the token has expired
    if spotify_backend.access_token_expired(social_auth):
        new_token = spotify_backend.refresh_token(social_auth.extra_data['refresh_token'])
        social_auth.extra_data['access_token'] = new_token['access_token']
        social_auth.save()
        return new_token['access_token']
    return social_auth.extra_data['access_token']

