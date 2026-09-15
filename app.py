from flask import Flask, request, redirect, jsonify
import os
import json
import time
import requests
from flask_cors import CORS
from traceback import print_exc
import jiosaavn
import helper

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET", "novamusic")
CORS(app)

JIO_SEARCH_URL = "https://www.jiosaavn.com/api.php"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0 Safari/537.36",
    "Referer": "https://www.jiosaavn.com/",
    "Origin": "https://www.jiosaavn.com",
    "Accept": "application/json,text/plain,*/*",
})

CACHE = {}
CACHE_TTL = 300


def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    if time.time() - item["time"] > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return item["data"]


def cache_set(key, data):
    CACHE[key] = {"time": time.time(), "data": data}


def parse_response(resp):
    try:
        return resp.json()
    except Exception:
        text = resp.text
        try:
            return json.loads(text.encode().decode("unicode-escape"))
        except Exception:
            return json.loads(text)


def normalize_fast(row):
    if not isinstance(row, dict):
        return None

    sid = str(row.get("id") or row.get("songid") or row.get("e_songid") or "")
    title = row.get("title") or row.get("song") or row.get("name") or ""
    artist = row.get("primary_artists") or row.get("singers") or row.get("artist") or ""
    album = row.get("album") or row.get("album_name") or "JioSaavn"
    cover = row.get("image") or row.get("image_url") or row.get("artworkUrl") or ""
    duration = row.get("duration") or row.get("length") or 0
    year = row.get("year") or row.get("release_date") or ""
    language = row.get("language") or ""
    perma = row.get("perma_url") or row.get("url") or ""

    media = row.get("media_url") or row.get("mediaUrl") or ""

    # search.getResults already commonly contains encrypted_media_url.
    # Decrypt locally — no song.getDetails request is needed.
    if not media:
        enc = row.get("encrypted_media_url") or row.get("encryptedMediaUrl")
        if enc:
            try:
                media = helper.decrypt_url(enc)
                if str(row.get("320kbps", "true")).lower() != "true":
                    media = media.replace("_320.mp4", "_160.mp4")
            except Exception:
                media = ""

    if not sid or not title or not media:
        return None

    try:
        duration = int(float(duration))
    except Exception:
        duration = 0

    return {
        "id": sid,
        "song": str(title),
        "title": str(title),
        "primary_artists": str(artist),
        "singers": str(artist),
        "artist": str(artist),
        "album": str(album),
        "image": str(cover).replace("150x150", "500x500"),
        "image_url": str(cover).replace("150x150", "500x500"),
        "media_url": str(media),
        "duration": duration,
        "year": str(year),
        "language": str(language),
        "perma_url": str(perma),
    }


def fast_search(query, page=1, limit=20):
    query = str(query or "").strip()
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 20), 20))

    if not query:
        return []

    key = f"{query.lower()}:{page}:{limit}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    params = {
        "__call": "search.getResults",
        "q": query,
        "n": limit,
        "p": page,
        "cc": "in",
        "_format": "json",
        "_marker": "0",
        "ctx": "web6dot0",
        "api_version": "4",
    }

    r = SESSION.get(JIO_SEARCH_URL, params=params, timeout=5)
    r.raise_for_status()
    data = parse_response(r)

    rows = (
        data.get("results")
        or data.get("songs", {}).get("data")
        or data.get("data")
        or []
    )

    result = []
    seen = set()

    for row in rows:
        song = normalize_fast(row)
        if not song or song["id"] in seen:
            continue
        seen.add(song["id"])
        result.append(song)
        if len(result) >= limit:
            break

    cache_set(key, result)
    return result


@app.route("/")
def home():
    return redirect("https://cyberboysumanjay.github.io/JioSaavnAPI/")


@app.route("/health")
def health():
    return jsonify({"status": True, "service": "NovaMusicAPI", "fast_search": True})


@app.route("/search/")
def search_fast():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"status": False, "error": "Query is required"}), 400
    try:
        p = max(1, int(request.args.get("p", 1)))
        n = min(20, max(1, int(request.args.get("n", 20))))
        return jsonify(fast_search(q, p, n))
    except Exception as e:
        print_exc()
        return jsonify({"status": False, "error": str(e)}), 500


# Keep the old API compatible.
@app.route("/song/")
def song_search():
    query = request.args.get("query", "").strip()
    lyrics_ = request.args.get("lyrics")
    songdata_ = request.args.get("songdata")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")
    songdata = not (songdata_ and songdata_.lower() != "true")

    if not query:
        return jsonify({"status": False, "error": "Query is required to search songs!"})

    try:
        if query.startswith("http") and "saavn.com" in query:
            return jsonify(jiosaavn.search_for_song(query, lyrics, songdata))
        if lyrics or not songdata:
            return jsonify(jiosaavn.search_for_song(query, lyrics, songdata))
        p = max(1, int(request.args.get("p", 1)))
        n = min(20, max(1, int(request.args.get("n", 20))))
        return jsonify(fast_search(query, p, n))
    except Exception as e:
        print_exc()
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/song/get/")
def get_song():
    song_id = request.args.get("id")
    lyrics_ = request.args.get("lyrics")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")
    if not song_id:
        return jsonify({"status": False, "error": "Song ID is required to get a song!"})
    try:
        resp = jiosaavn.get_song(song_id, lyrics)
        if not resp:
            return jsonify({"status": False, "error": "Invalid Song ID received!"})
        return jsonify(resp)
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/playlist/")
def playlist():
    query = request.args.get("query")
    lyrics_ = request.args.get("lyrics")
    lyrics = bool(lyrics_ and lyrics_.lower() != "false")
    if not query:
        return jsonify({"status": False, "error": "Query is required to search playlists!"})
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
        return jsonify({"status": False, "error": "Query is required to search albums!"})
    try:
        album_id = jiosaavn.get_album_id(query)
        return jsonify(jiosaavn.get_album(album_id, lyrics))
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 500


@app.route("/lyrics/")
def lyrics():
    query = request.args.get("query")
    if not query:
        return jsonify({"status": False, "error": "Query containing song link or id is required to fetch lyrics!"})
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
    query = request.args.get("query", "").strip()
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
