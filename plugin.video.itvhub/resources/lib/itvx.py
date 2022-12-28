
# ---------------------------------------------------------------------------------------------------------------------
#  Copyright (c) 2022 Dimitri Kroon.
#
#  SPDX-License-Identifier: GPL-2.0-or-later
#  This file is part of plugin.video.itvx
# ---------------------------------------------------------------------------------------------------------------------

import os
import string
import time
import logging

from datetime import datetime
import pytz


from codequick.support import logger_id

from . import fetch
from . import parsex
from . import utils

from .itv import get_live_schedule


logger = logging.getLogger(logger_id + '.itv')

FEATURE_SET = 'hd,progressive,single-track,mpeg-dash,widevine,widevine-download,inband-ttml,hls,aes,inband-webvtt,outband-webvtt,inband-audio-description'
PLATFORM_TAG = 'mobile'


def get_page_data(url):
    if not url.startswith('https://'):
        url = 'https://www.itv.com' + url
    html_doc = fetch.get_document(url)
    return parsex.scrape_json(html_doc)


def get_live_channels():
    from tzlocal import get_localzone
    local_tz = get_localzone()
    utc_tz = pytz.utc

    live_data = fetch.get_json(
        'https://nownext.oasvc.itv.com/channels',
        params={
            'broadcaster': 'itv',
            'featureSet': FEATURE_SET,
            'platformTag': PLATFORM_TAG})

    fanart_url = live_data['images']['backdrop']

    main_schedule = get_live_schedule()

    channels = live_data['channels']

    for channel in channels:
        channel['backdrop'] = fanart_url
        slots = channel.pop('slots')

        # The itv main live channels get their schedule from the full live schedule
        if channel['channelType'] == 'simulcast':
            chan_id = channel['id']
            for main_chan in main_schedule:
                # Caution, might get broken when ITV becomes ITV1 everywhere
                if main_chan['channel']['name'] == chan_id:
                    channel['slot'] = main_chan['slot']
                    break
            if channel.get('slot'):
                # On to the next channel if adding full schedule succeeded
                continue

        programs_list = []
        for prog in (slots['now'], slots['next']):
            if prog['detailedDisplayTitle']:
                details = ': '.join((prog['displayTitle'], prog['detailedDisplayTitle']))
            else:
                details = prog['displayTitle']

            start_t = prog['start'][:19]
            # TODO: check this in DST period
            utc_start = datetime(*(time.strptime(start_t, '%Y-%m-%dT%H:%M:%S')[0:6])).replace(tzinfo=utc_tz)

            programs_list.append({
                'programme_details': details,
                'programmeTitle': prog['displayTitle'],
                'orig_start': None,          # fast channels do not support play from start
                'startTime': utc_start.astimezone(local_tz).strftime('%H:%M')
            })
        channel['slot'] = programs_list
    return channels


def main_page_items():
    main_data = get_page_data('https://www.itv.com')
    for hero_data in main_data['heroContent']:
        yield parsex.parse_hero_content(hero_data)


def episodes(url):
    """Get a listing of series and their episodes

    Return a list containing only relevant info in a format that can easily be
    used by codequick Listitem.from_dict.

    """
    brand_data = get_page_data(url)['title']['brand']
    brand_title = brand_data['title']
    brand_thumb = brand_data['title'].format(parsex.IMG_PROPS_THUMB)
    brand_fanart = brand_data['title'].format(parsex.IMG_PROPS_FANART)
    brand_description = brand_data['synopses'].get('ninety', '')
    series_data = brand_data['series']

    if not series_data:
        return []

    series_list = []
    for series in series_data:
        title = series['title']
        series_idx = series['seriesNumber']
        series_obj = {
            'label': title,
            'art': {'thumb': brand_thumb, 'fanart': brand_fanart},
            'info': {'title': '[B]{} - {}[/B]'.format(brand_title, series['title']),
                     'plot': '{}\n\n{} - {} episodes'.format(
                         brand_description, title, series['seriesAvailableEpisodeCount'])},

            'params': {'url': url, 'series_idx': series_idx},
            'episodes': [parsex.parse_title(episode, brand_fanart) for episode in series['episodes']]
        }
        series_list.append(series_obj)
    return series_list


def categories():
    """Return all available categorie names."""
    data = get_page_data('https://www.itv.com/watch/categories')
    cat_list = data['subnav']['items']
    return ({'label': cat['name'], 'params': {'path': cat['url']}} for cat in cat_list)


def category_content(url: str, hide_payed=False):
    """Return all programmes in a category"""
    cat_data = get_page_data(url)
    category = cat_data['category']['pathSegment']
    progr_list = cat_data.get('programmes')

    for prog in progr_list:
        content_info = prog['contentInfo']
        is_playable = not content_info.lower().startswith('series')
        title = prog['title']

        if 'FREE' in prog['tier']:
            plot = prog['description']
        else:
            if hide_payed:
                continue
            plot = '\n\n'.join((prog['description'], '[COLOR yellow]X premium[/COLOR]'))

        sort_title = title.lower()

        programme_item = {
            'label': title,
            'art': {'thumb': prog['imageTemplate'].format(**parsex.IMG_PROPS_THUMB),
                    'fanart': prog['imageTemplate'].format(**parsex.IMG_PROPS_FANART)},
            'info': {'title': title if is_playable else '[B]{}[/B] {}'.format(title, content_info),
                     'plot': plot,
                     'sorttitle': sort_title[4:] if sort_title.startswith('the ') else sort_title},
        }

        if category == 'films':
            programme_item['art']['poster'] = prog['imageTemplate'].format(**parsex.IMG_PROPS_POSTER)

        if is_playable:
            programme_item['info']['duration'] = utils.duration_2_seconds(content_info)
            programme_item['params'] = {'url': parsex.build_url(title, prog['encodedProgrammeId']['letterA'])}
        else:
            programme_item['params'] = {'url': parsex.build_url(title,
                                                                prog['encodedProgrammeId']['letterA'],
                                                                prog['encodedEpisodeId']['letterA'])}
        yield {'playable': is_playable,
               'show': programme_item}


cached_programs = {}
CACHE_TIME = 600


def get_playlist_url_from_episode_page(page_url):
    """Obtain the url to the episode's playlist from the episode's HTML page.
    """
    import re

    logger.info("Get playlist from episode page - url=%s", page_url)
    html_doc = fetch.get_document(page_url)
    logger.debug("successfully retrieved page %s", page_url)

    # New version - might a bit overdone as just a regex to obtain the playlist url should suffice.
    # doc_data = parse.get__next__data_from_page(html_doc)
    # player_data = doc_data['props']['pageProps']['episodeHeroWrapperProps']['playerProps']
    # name = player_data['programmeTitle']
    # play_list_url = player_data['playlistUrl']

    # Only this will fail on itvX, but is the name actually used anywhere?
    # name = re.compile('data-video-title="(.+?)"').search(html_doc)[1]
    name = ''
    play_list_url = re.compile('data-video-id="(.+?)"').search(html_doc)[1]
    return play_list_url, name



def search(search_term):
    url = 'https://textsearch.prd.oasvc.itv.com/search'
    query_params = {
        'broadcaster': 'itv',
        'featureSet': 'clearkey,outband-webvtt,hls,aes,playready,widevine,fairplay,bbts,progressive,hd,rtmpe',
        # We can handle only free items because of the way we list production right now.
        'onlyFree': 'true',
        'platform': 'dotcom',
        'query': search_term
    }
    data = fetch.get_json(url, params=query_params)
    if data is None:
        return

    results = data.get('results')

    def parse_programme(prg_data):
        prog_name = prg_data['programmeTitle']
        img_url = prg_data['latestAvailableEpisode']['imageHref']

        return {
            'entity_type': 'programme',
            'label': prog_name,
            'art': {'thumb': img_url.format(width=960, height=540, quality=80, blur=0, bg='false')},
            'info': {'plot': prg_data.get('synopsis'),
                     'title': '[B]{}[/B] - {} episodes'.format(prog_name, prg_data.get('totalAvailableEpisodes', ''))},
            'params': {
                'url': 'https://discovery.hubsvc.itv.com/platform/itvonline/dotcom/productions?programmeId={}&'
                       'features=aes,clearkey,fairplay,hls,mpeg-dash,outband-webvtt,playready,'
                       'widevine&broadcaster=itv'.format(prg_data['legacyId']['apiEncoded']),
                'name': prog_name}
        }

    def parse_special(prg_data):
        # AFAICT special is always a production, which might be a production of a programme, but
        # presented as a single episode in the search results.
        prog_name = program_data['specialTitle']
        img_url = program_data['imageHref']
        # convert productionId to a format used in the url
        api_prod_id = prg_data['productionId'].replace('/', '_').replace('#', '.')

        return {
            'entity_type': 'special',
            'label': prog_name,
            'art': {'thumb': img_url.format(width=960, height=540, quality=80, blur=0, bg='false')},
            'info': {'plot': prg_data.get('synopsis'),
                     'title': prog_name},
            'params': {'url': 'https://magni.itv.com/playlist/itvonline/ITV/' + api_prod_id,
                       'name': prog_name}
        }

    for result in results:
        program_data = result['data']
        entity_type = result['entityType']

        if entity_type == 'programme':
            yield parse_programme(program_data)
        elif entity_type == 'special':
            yield parse_special(program_data)
        else:
            logger.warning("Unknown search result item entityType %s on search term %s", entity_type, search_term)
            continue
