"""Sort your Spotify Liked Songs into playlists by genre.

Spotify has no per-track genres, and Development Mode apps get an empty
`genres` list for artists. So each track inherits its artist's top tags from
Last.fm, which are then mapped into broad buckets (e.g. "indie rock" and
"alternative rock" both become "Rock").
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

ROOT = Path(__file__).parent
GENRE_CACHE = ROOT / "genre_cache.json"
SONGS_CACHE = ROOT / "liked_songs.json"
LASTFM_DELAY = 0.25  # Last.fm asks for at most 5 requests per second

# Each artist tag is matched against these keywords (as whole words). When an artist's genres
# map to different buckets, the most common bucket wins; ties go to the bucket
# listed first, so specific buckets come before broad ones like "Pop".
BUCKETS = [
    ("Sertanejo", ["sertanejo", "arrocha", "modao"]),
    ("Brazilian Funk", ["funk carioca", "funk paulista", "funk mtg", "brazilian funk", "funk ostentacao", "baile funk", "funk bh", "rave funk", "hyperfunk"]),
    ("Pagode & Samba", ["pagode", "samba"]),
    ("Forró & Piseiro", ["forro", "forró", "piseiro", "brega", "tecnobrega", "axe", "axé"]),
    ("MPB & Bossa Nova", ["mpb", "bossa nova", "musica popular brasileira", "tropicalia", "nova mpb"]),
    ("Gospel", ["gospel", "worship", "christian", "louvor"]),
    ("Rap & Hip Hop", ["rap", "hip hop", "hip-hop", "hiphop", "trap", "drill", "grime", "phonk"]),
    ("R&B & Soul", ["r&b", "rnb", "soul", "neo soul", "motown"]),
    ("Funk & Disco", ["funk", "disco", "boogie"]),
    ("Reggaeton & Latin", ["reggaeton", "latin", "urbano latino", "bachata", "salsa", "cumbia", "dembow"]),
    ("K-pop & J-pop", ["k-pop", "kpop", "j-pop", "jpop", "anime", "j-rock"]),
    ("Metal", ["metal", "metalcore", "djent", "deathcore"]),
    ("Punk & Emo", ["punk", "emo", "hardcore", "screamo"]),
    ("Rock", ["rock", "grunge", "shoegaze", "post-punk"]),
    ("Electronic", ["house", "techno", "edm", "electro", "trance", "dubstep", "drum and bass", "dnb", "eletronica", "electronic", "electronica", "trip-hop", "garage", "synthwave", "hyperpop"]),
    ("Ambient & Chillout", ["ambient", "chillout", "new age", "meditation", "relaxing"]),
    ("Indie & Alternative", ["indie", "alternative", "alternativo", "bedroom", "lo-fi", "lofi", "dream pop"]),
    ("Reggae", ["reggae", "dancehall", "ska", "dub"]),
    ("Jazz & Blues", ["jazz", "blues", "swing"]),
    ("Classical & Soundtracks", ["classical", "classica", "orchestra", "soundtrack", "score", "piano", "opera", "composer"]),
    ("Country & Folk", ["country", "folk", "americana", "bluegrass", "singer-songwriter"]),
    ("Pop", ["pop"]),
]
UNKNOWN = "Unknown genre"


def connect() -> spotipy.Spotify:
    load_dotenv(ROOT / ".env")
    auth = SpotifyOAuth(
        scope="user-library-read playlist-modify-private playlist-modify-public",
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        cache_path=str(ROOT / ".spotify_token"),
    )
    # 429 is left out of the automatic retries so call() can decide how long to wait
    return spotipy.Spotify(auth_manager=auth, retries=5, status_forcelist=(500, 502, 503, 504))


def call(fn, *args, **kwargs):
    """Run a Spotify request, waiting out short rate limits and stopping on the daily quota."""
    while True:
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status != 429:
                raise
            wait = int(e.headers.get("Retry-After", 5))
            if wait > 60:
                sys.exit(f"\nSpotify API quota reached. Try again in about {wait // 3600}h{wait % 3600 // 60:02d}m.")
            time.sleep(wait)


def fetch_liked_songs(sp: spotipy.Spotify) -> list[dict]:
    tracks = []
    page = call(sp.current_user_saved_tracks, limit=50)
    while page:
        for item in page["items"]:
            t = item.get("track")
            if not t or not t.get("id"):
                continue  # local files or unavailable tracks
            tracks.append({
                "id": t["id"],
                "uri": t["uri"],
                "name": t["name"],
                "artists": [a["name"] for a in t["artists"]],
                "artist_ids": [a["id"] for a in t["artists"] if a.get("id")],
            })
        print(f"\r  {len(tracks)} songs loaded...", end="", flush=True)
        page = call(sp.next, page) if page.get("next") else None
    print()
    SONGS_CACHE.write_text(json.dumps(tracks, ensure_ascii=False))
    return tracks


def lastfm_tags(api_key: str, **params) -> list[str] | None:
    """Top Last.fm tags, most popular first. None means the request failed and should be retried later."""
    try:
        r = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={**params, "autocorrect": 1, "api_key": api_key, "format": "json"},
            timeout=10,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if "error" in data:
        return [] if data["error"] == 6 else None  # 6 = artist or track not found
    tags = data.get("toptags", {}).get("tag", [])
    if isinstance(tags, dict):  # Last.fm returns a single tag as an object, not a list
        tags = [tags]
    return [t["name"].lower() for t in tags[:5] if int(t.get("count", 0)) >= 10]


def strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def save_cache(cache: dict) -> None:
    GENRE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))


def is_useful(tags: list[str]) -> bool:
    return pick_bucket(tags) != UNKNOWN


def artists_to_look_up(tracks: list[dict], cache: dict, failed: set) -> dict[str, str]:
    """For each track, the next artist to look up, unless an earlier artist already has useful tags."""
    needed = {}
    for t in tracks:
        for artist_id, name in zip(t["artist_ids"], t["artists"]):
            if artist_id in failed:
                continue
            if artist_id not in cache:
                needed[artist_id] = name
                break
            if is_useful(cache[artist_id]):
                break
    return needed


def fetch_artist_genres(tracks: list[dict], api_key: str) -> dict[str, list[str]]:
    cache = json.loads(GENRE_CACHE.read_text()) if GENRE_CACHE.exists() else {}
    failed = set()
    # Featured artists are only looked up when the main artist has no useful tags,
    # so this loops until every track has useful tags or ran out of artists.
    while needed := artists_to_look_up(tracks, cache, failed):
        print(f"  Looking up {len(needed)} artists on Last.fm...")
        for i, (artist_id, name) in enumerate(needed.items(), 1):
            tags = lastfm_tags(api_key, method="artist.gettoptags", artist=name)
            if tags == [] and (plain := strip_accents(name)) != name:
                # Last.fm sometimes knows "Rufus Du Sol" but not "RÜFÜS DU SOL"
                tags = lastfm_tags(api_key, method="artist.gettoptags", artist=plain)
            if tags is None:
                failed.add(artist_id)  # not cached, so the next run tries again
            else:
                cache[artist_id] = tags
            time.sleep(LASTFM_DELAY)
            if i % 50 == 0 or i == len(needed):  # save progress in case the run is interrupted
                save_cache(cache)
                print(f"\r  {i}/{len(needed)} artists", end="", flush=True)
        print()

    # Last resort for songs whose artists have no useful tags: the song's own tags
    songs = [t for t in tracks if not is_useful(tags_for(t, cache)) and f"track:{t['id']}" not in cache]
    if songs:
        print(f"  Looking up {len(songs)} songs without a genre on Last.fm...")
        for i, t in enumerate(songs, 1):
            tags = lastfm_tags(api_key, method="track.gettoptags", artist=t["artists"][0], track=t["name"])
            if tags is not None:
                cache[f"track:{t['id']}"] = tags
            time.sleep(LASTFM_DELAY)
            if i % 50 == 0 or i == len(songs):
                save_cache(cache)
                print(f"\r  {i}/{len(songs)} songs", end="", flush=True)
        print()
    return cache


def bucket_for_genre(genre: str) -> str | None:
    for bucket, keywords in BUCKETS:
        if any(re.search(rf"\b{re.escape(k)}\b", genre.lower()) for k in keywords):
            return bucket
    return None


def pick_bucket(genres: list[str]) -> str:
    votes = defaultdict(int)
    for g in genres:
        bucket = bucket_for_genre(g)
        if bucket:
            votes[bucket] += 1
    if not votes:
        return UNKNOWN
    order = [b for b, _ in BUCKETS]
    return max(votes, key=lambda b: (votes[b], -order.index(b)))


def tags_for(track: dict, cache: dict) -> list[str]:
    """Tags of the first artist whose tags map to a bucket, then the song's own tags, then any tags found."""
    artist_tags = [cache.get(a) or [] for a in track["artist_ids"]]
    for tags in artist_tags + [cache.get(f"track:{track['id']}") or []]:
        if is_useful(tags):
            return tags
    return next((tags for tags in artist_tags if tags), [])


def classify(tracks: list[dict], cache: dict) -> dict[str, list[dict]]:
    by_bucket = defaultdict(list)
    for t in tracks:
        genres = tags_for(t, cache)
        t["genres"] = genres
        t["bucket"] = pick_bucket(genres)
        by_bucket[t["bucket"]].append(t)
    return dict(sorted(by_bucket.items(), key=lambda kv: -len(kv[1])))


def save_csv(by_bucket: dict[str, list[dict]]) -> Path:
    path = ROOT / "songs_by_genre.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "song", "artists", "artist_tags"])
        for bucket, tracks in by_bucket.items():
            for t in tracks:
                w.writerow([bucket, t["name"], ", ".join(t["artists"]), ", ".join(t["genres"])])
    return path


def create_playlists(sp: spotipy.Spotify, by_bucket: dict[str, list[dict]], min_songs: int, prefix: str) -> None:
    for bucket, tracks in by_bucket.items():
        if bucket == UNKNOWN or len(tracks) < min_songs:
            continue
        name = f"{prefix}{bucket}"
        playlist = call(sp.current_user_playlist_create, name, public=False,
                        description=f"{len(tracks)} liked songs tagged as {bucket}")
        uris = [t["uri"] for t in tracks]
        for i in range(0, len(uris), 100):  # the API accepts up to 100 items per request
            call(sp.playlist_add_items, playlist["id"], uris[i:i + 100])
        print(f"  ✓ {name} ({len(tracks)} songs)")


def main() -> None:
    p = argparse.ArgumentParser(description="Sort your Spotify Liked Songs by genre.")
    p.add_argument("--create-playlists", action="store_true", help="create one private playlist per genre")
    p.add_argument("--min-songs", type=int, default=5, help="minimum songs needed to create a playlist (default: 5)")
    p.add_argument("--prefix", default="Liked · ", help='playlist name prefix (default: "Liked · ")')
    p.add_argument("--refresh", action="store_true", help="download Liked Songs again instead of using the saved copy")
    args = p.parse_args()

    sp = connect()
    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        sys.exit("Missing LASTFM_API_KEY in .env (see README > Setup).")
    if SONGS_CACHE.exists() and not args.refresh:
        tracks = json.loads(SONGS_CACHE.read_text())
        print(f"Using {len(tracks)} saved Liked Songs (run with --refresh to download them again)")
    else:
        print("Loading Liked Songs...")
        tracks = fetch_liked_songs(sp)
    print("Fetching artist genres...")
    cache = fetch_artist_genres(tracks, api_key)
    by_bucket = classify(tracks, cache)

    print("\nSummary:")
    for bucket, ts in by_bucket.items():
        print(f"  {len(ts):>5}  {bucket}")
    print(f"\nFull list saved to {save_csv(by_bucket)}")

    if args.create_playlists:
        print("\nCreating playlists...")
        create_playlists(sp, by_bucket, args.min_songs, args.prefix)
    else:
        print("Run again with --create-playlists to create the playlists.")


if __name__ == "__main__":
    main()
