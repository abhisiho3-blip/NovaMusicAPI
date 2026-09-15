from flask import Flask, request, redirect, jsonify
import os
import json
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from traceback import print_exc
from flask_cors import CORS

import jiosaavn

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET", "novamusic")
CORS(app)

JIO_BASE = "https://www.jiosaavn.com/api.php"
JIO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0 Safari/537.36",
    "Referer": "https://www.jiosaavn.com/",
    "Origin": "https://www.jiosaavn.com",
    "Accept": "application/json,text/plain,*/*",
}

# Reuse HTTP connections.
SESSION = requests.Session()
SESSION.headers.update(JIO_HEADERS)

# 8 parallel detail requests is a good balance for Render's small instances.
DETAIL_WORKERS = 8

# Small in-memory cache. This makes repeated searches nearly instant while
# the Render instance remains warm.
CACHE = {}
CACHE_TTL = 300


def _cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    if __import__("time").time() - item["time"] > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return item["data"]


def _cache_set(key, data):
    CACHE[key] = {"time": __import__("time").time(), "data": data}


def _clean_json_text(text):
    text = text or ""
    # JioSaavn sometimes prefixes JSON with a few characters.
    pos = text.find("{")
    if pos > 0:
        text = text[pos:]
    return text.encode().decode("unicode-escape")


def _fast_search_ids(query, page, limit):
    """One JioSaavn search request. Returns song IDs."""
    params = {
        "__call": "search.getResults",
        "q": query,
        "n": limit,
        "p": page,
        "_format": "json",
        "_marker": "0",
        "ctx": "web6dot0",
        "api_version": "4",
    }

    r = SESSION.get(JIO_BASE, params=params, timeout=6)
    r.raise_for_status()

    raw = _clean_json_text(r.text)
    data = json.loads(raw)

    rows = (
        data.get("results")
        or data.get("songs", {}).get("data")
        or data.get("data")
        or []
    )

    ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id") or row.get("songid")
        if sid and str(sid) not in ids:
            ids.append(str(sid))

    return ids[:limit]


def _fast_song(song_id):
    """Fetch and format one complete playable song."""
    try:
        return jiosaavn.get_song(song_id, False)
    except Exception:
        return None


def fast_search(query, page=1, limit=20):
    """
    Fast NovaMusic search:
    1 browser request -> 1 JioSaavn search request
    -> parallel song detail requests.
    """
    query = str(query or "").strip()
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 20), 20))

    if not query:
        return []

    cache_key = f"search:{query.lower()}:{page}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    ids = _fast_search_ids(query, page, limit)

    songs = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(_fast_song, sid) for sid in ids]
        for future in as_completed(futures):
            try:
                song = future.result()
                if song and song.get("media_url"):
                    songs.append(song)
            except Exception:
                pass

    # Keep JioSaavn search order rather than completion order.
    by_id = {str(s.get("id")): s for s in songs if s}
    ordered = [by_id[sid] for sid in ids if sid in by_id]

    _cache_set(cache_key, ordered)
    return ordered


@app.route("/")
def home():
    return redirect("https://cyberboysumanjay.github.io/JioSaavnAPI/")


@app.route("/health")
def health():
    return jsonify({"status": True, "service": "NovaMusicAPI"})


# NEW FAST ENDPOINT
# Example:
# /search/?q=Arijit%20Singh&n=20&p=1
@app.route("/search/")
def search_fast():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"status": False, "error": "Query is required"}), 400

    try:
        page = max(1, int(request.args.get("p", 1)))
        limit = min(20, max(1, int(request.args.get("n", 20))))
        return jsonify(fast_search(query, page, limit))
    except Exception as e:
        print_exc()
        return jsonify({"status": False, "error": str(e)}), 500


# Existing endpoint kept for compatibility.
# It is also made faster by using the new parallel implementation for normal
# text searches.
@app.route("/song/")
def song_search():
    query = request.args.get("query", "").strip()
    lyrics_ = request.args.get("lyrics")
    songdata_ = request.args.get("songdata")

    lyrics = bool(lyrics_ and lyrics_.lower() != "false")
    songdata = not (songdata_ and songdata_.lower() != "true")

    if not query:
        return jsonify({
            "status": False,
            "error": "Query is required to search songs!"
        })

    try:
        # URL searches must continue through the original implementation.
        if query.startswith("http") and "saavn.com" in query:
            return jsonify(jiosaavn.search_for_song(query, lyrics, songdata))

        # songdata=false is intentionally left as the original lightweight API.
        if not songdata or lyrics:
            return jsonify(jiosaavn.search_for_song(query, lyrics, songdata))

        page = max(1, int(request.args.get("p", 1)))
        limit = min(20, max(1, int(request.args.get("n", 20))))
        return jsonify(fast_search(query, page, limit))

    except Exception as e:
        print_exc()
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/song/get/")
def get_song():
    lyrics = False
    song_id = request.args.get("id")
    lyrics_ = request.args.get("lyrics")
    if lyrics_ and lyrics_.lower() != "false":
        lyrics = True

    if not song_id:
        return jsonify({
            "status": False,
            "error": "Song ID is required to get a song!"
        })

    try:
        resp = jiosaavn.get_song(song_id, lyrics)
        if not resp:
            return jsonify({
                "status": False,
                "error": "Invalid Song ID received!"
            })
        return jsonify(resp)
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/playlist/")
def playlist():
    query = request.args.get("query")
    lyrics_ = request.args.get("lyrics")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")

    if not query:
        return jsonify({
            "status": False,
            "error": "Query is required to search playlists!"
        })

    try:
        playlist_id = jiosaavn.get_playlist_id(query)
        return jsonify(jiosaavn.get_playlist(playlist_id, lyrics))
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/album/")
def album():
    query = request.args.get("query")
    lyrics_ = request.args.get("lyrics")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")

    if not query:
        return jsonify({
            "status": False,
            "error": "Query is required to search albums!"
        })

    try:
        album_id = jiosaavn.get_album_id(query)
        return jsonify(jiosaavn.get_album(album_id, lyrics))
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/lyrics/")
def lyrics():
    query = request.args.get("query")
    if not query:
        return jsonify({
            "status": False,
            "error": "Query containing song link or id is required to fetch lyrics!"
        })

    try:
        if "http" in query and "saavn" in query:
            song_id = jiosaavn.get_song_id(query)
            text = jiosaavn.get_lyrics(song_id)
        else:
            text = jiosaavn.get_lyrics(query)

        return jsonify({"status": True, "lyrics": text})
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/result/")
def result():
    query = request.args.get("query", "")
    lyrics_ = request.args.get("lyrics")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")

    if not query:
        return jsonify({"status": False, "error": "Query is required"}), 400

    try:
        if "saavn" not in query:
            if not lyrics:
                return jsonify(fast_search(query, 1, 20))
            return jsonify(jiosaavn.search_for_song(query, lyrics, True))

        if "/song/" in query:
            song_id = jiosaavn.get_song_id(query)
            return jsonify(jiosaavn.get_song(song_id, lyrics))

        if "/album/" in query:
            album_id = jiosaavn.get_album_id(query)
            return jsonify(jiosaavn.get_album(album_id, lyrics))

        if "/playlist/" in query or "/featured/" in query:
            playlist_id = jiosaavn.get_playlist_id(query)
            return jsonify(jiosaavn.get_playlist(playlist_id, lyrics))

        return jsonify({"status": False, "error": "Unsupported Saavn URL"}), 400

    except Exception as e:
        print_exc()
        return jsonify({"status": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    app.run(host="0.0.0.0", port=port, threaded=True)
