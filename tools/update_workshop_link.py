"""Record a real Workshop URL in the current clean checkout; never publish."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlparse, parse_qs

PENDING = 'Steam Workshop: pending first publication; no item ID has been assigned.'


def validate_url(value):
    url = urlparse(value)
    query = parse_qs(url.query, strict_parsing=True)
    if (url.scheme != 'https' or url.netloc != 'steamcommunity.com'
            or url.path not in ('/sharedfiles/filedetails/', '/workshop/filedetails/')
            or set(query) != {'id'} or len(query['id']) != 1
            or not re.fullmatch(r'[1-9][0-9]*', query['id'][0]) or url.fragment):
        raise ValueError('real_steam_workshop_url_required')
    return 'https://steamcommunity.com/sharedfiles/filedetails/?id='+query['id'][0]


def update(root, url):
    url = validate_url(url)
    metadata = root/'PUBLICATION.json'
    state = json.loads(metadata.read_text(encoding='utf-8'))
    if state.get('github_url') != 'https://github.com/andreykli/hoi4-eyes-public':
        raise ValueError('not_the_clean_public_export')
    old = state.get('workshop_url')
    for name in ('NOTICE', 'CREDITS.md', 'ATTRIBUTION.md'):
        path = root/name
        text = path.read_text(encoding='utf-8')
        text = text.replace('Steam Workshop: '+old, 'Steam Workshop: '+url) if old else text.replace(PENDING, 'Steam Workshop: '+url)
        path.write_bytes(text.encode('utf-8'))
    state['workshop_url'] = url
    metadata.write_bytes((json.dumps(state, indent=2)+'\n').encode('utf-8'))
    # This is a local metadata edit. Do not move the existing release tag or
    # pretend archives/checksums still match. The owner rebuilds after review.
    return {'url':url, 'published_by_tool':False, 'rebuild_archives_required':True,
            'refresh_source_manifest_required':True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    print(json.dumps(update(Path(__file__).resolve().parents[1], args.url), indent=2))
