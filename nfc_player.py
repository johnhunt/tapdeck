#!/usr/bin/env python3
"""NFC album player (proof of concept).

Tap a tag -> read its JSON -> {"id": N, ...} -> play album folder N from MUSIC_DIR
on the Cast speaker, in track order (ID3/tag track number, else filename).
Lift the tag -> pause. Put the same tag back -> resume. A different tag -> that album.

Settings come from environment variables (see player.env).
"""
import json
import logging
import mimetypes
import os
import queue
import re
import signal
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

os.environ.setdefault("BLINKA_SKIP_DEPENDENCY_CHECK", "1")

import board  # noqa: E402
import busio  # noqa: E402
import pychromecast  # noqa: E402
from mutagen import File as AudioFile  # noqa: E402

import nfc_reader as nfc  # noqa: E402

BASE = Path(__file__).resolve().parent
MUSIC = Path(os.environ.get("MUSIC_DIR", "/home/pi/music")).resolve()
CAST_NAME = os.environ.get("CAST_NAME", "Kitchen speaker")
CAST_HOST = os.environ.get("CAST_HOST", "").strip()  # optional known IP, helps across subnets
PORT = int(os.environ.get("HTTP_PORT", "8765"))
# Keep-alive: playback only pauses once the tag has gone unseen for this long. Any single
# detection restarts the clock, so a flaky read can't interrupt the music.
REMOVE_GRACE = float(os.environ.get("REMOVE_GRACE", "10"))
REMOVE_MISSES = int(os.environ.get("REMOVE_MISSES", "3"))  # ...and at least this many empty polls
VOLUME = os.environ.get("CAST_VOLUME", "").strip()  # e.g. 0.3; blank = leave speaker volume alone
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}

STOP = threading.Event()
log = logging.getLogger("player")


# ------------------------------------------------------------------ library

def first_int(value):
    """'3/10' -> 3, ['07'] -> 7, None -> None."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    m = re.match(r"\s*(\d+)", str(value)) if value is not None else None
    return int(m.group(1)) if m else None


def natural_key(name):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def find_album(album_id):
    """Folder whose name starts with the number, e.g. id 1 -> '001 - joni mitchell - blue'."""
    if not MUSIC.is_dir():
        return None
    matches = [d for d in MUSIC.iterdir()
               if d.is_dir() and first_int(re.match(r"\d*", d.name).group() or None) == album_id]
    return sorted(matches)[0] if matches else None


def album_tracks(folder):
    """Audio files in play order: (disc, track number from tags) then filename."""
    tracks = []
    for f in folder.iterdir():
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXT or f.name.startswith("."):
            continue
        disc = num = None
        title = artist = album = None
        try:
            tags = AudioFile(f, easy=True)
            if tags is not None and tags.tags is not None:
                num = first_int(tags.get("tracknumber"))
                disc = first_int(tags.get("discnumber"))
                title = (tags.get("title") or [None])[0]
                artist = (tags.get("artist") or tags.get("albumartist") or [None])[0]
                album = (tags.get("album") or [None])[0]
        except Exception as exc:  # unreadable tags shouldn't stop playback
            log.warning("tag read failed for %s: %s", f.name, exc)
        if num is None:
            num = first_int(re.match(r"\d*", f.name).group() or None)  # '03 Little Green.mp3'
        tracks.append({
            "path": f, "title": title or f.stem, "artist": artist, "album": album or folder.name,
            "sort": (disc or 1, num if num is not None else 10**6, natural_key(f.name)),
        })
    tracks.sort(key=lambda t: t["sort"])
    return tracks


# ------------------------------------------------------------------ HTTP server

class MusicHandler(BaseHTTPRequestHandler):
    """Serves files under MUSIC_DIR at /music/<path>, with Range support (for seeking)."""

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

    def serve(self, head):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if not rel.startswith("/music/"):
            return self.send_error(404)
        path = (MUSIC / rel[len("/music/"):]).resolve()
        if MUSIC not in path.parents or not path.is_file():
            return self.send_error(404)
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if head:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # speaker closed the connection (seek / stop) - normal

    def log_message(self, fmt, *args):
        log.debug("http %s " + fmt, self.client_address[0], *args)


def local_ip_for(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 8009))
        return s.getsockname()[0]


# ------------------------------------------------------------------ casting

class Speaker:
    def __init__(self):
        self.cast = None
        self.browser = None

    def connected(self):
        c = self.cast
        return c is not None and c.socket_client.is_connected and c.status is not None

    def connect(self):
        if self.connected():
            return self.cast
        self.close()
        log.info("looking for speaker %r%s", CAST_NAME, f" (known host {CAST_HOST})" if CAST_HOST else "")
        casts, self.browser = pychromecast.get_listed_chromecasts(
            friendly_names=[CAST_NAME], known_hosts=[CAST_HOST] if CAST_HOST else None,
            discovery_timeout=8, tries=1, timeout=10)
        if not casts:
            seen = [ci.friendly_name for ci in self.browser.devices.values()]
            self.close()
            raise RuntimeError(f"speaker {CAST_NAME!r} not found; visible: {seen}")
        self.cast = casts[0]
        self.cast.wait(timeout=15)
        log.info("connected to %r at %s", self.cast.cast_info.friendly_name, self.cast.cast_info.host)
        if VOLUME:
            self.cast.set_volume(float(VOLUME))
        return self.cast

    def close(self):
        if self.cast:
            try:
                self.cast.disconnect(timeout=5)
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.stop_discovery()
            except Exception:
                pass
        self.cast = self.browser = None


class Player:
    """Tracks what should be playing; all calls come from the main loop thread."""

    def __init__(self, speaker):
        self.speaker = speaker
        self.album = None
        self.tracks = []
        self.index = 0
        self.url = None
        self.paused_at = 0.0
        self.state = "idle"  # idle | playing | paused | finished
        self.load_time = 0.0
        self.pending_album = None  # last album asked for (so a failed start can be retried)

    def url_for(self, track):
        cast = self.speaker.cast
        rel = track["path"].relative_to(MUSIC).as_posix()
        return f"http://{local_ip_for(cast.cast_info.host)}:{PORT}/music/{urllib.parse.quote(rel)}"

    def start_album(self, album_id):
        self.pending_album = album_id
        self.state = "idle"
        folder = find_album(album_id)
        if folder is None:
            log.warning("no album folder for id %s in %s", album_id, MUSIC)
            self.state = "idle"
            return
        tracks = album_tracks(folder)
        if not tracks:
            log.warning("album %s has no audio files", folder.name)
            self.state = "idle"
            return
        self.album, self.tracks, self.index = folder.name, tracks, 0
        log.info("album %r: %s", folder.name,
                 ", ".join(f"{i + 1}.{t['path'].name}" for i, t in enumerate(tracks)))
        self.load(0, 0.0)

    def load(self, index, position):
        cast = self.speaker.connect()
        t = self.tracks[index]
        self.index, self.url = index, self.url_for(t)
        meta = {"metadataType": 3, "title": t["title"], "albumName": t["album"]}
        if t["artist"]:
            meta["artist"] = t["artist"]
        log.info("playing %d/%d %s%s", index + 1, len(self.tracks), t["path"].name,
                 f" from {position:.0f}s" if position else "")
        mc = cast.media_controller
        mc.play_media(self.url, mimetypes.guess_type(t["path"].name)[0] or "audio/mpeg",
                      title=t["title"], metadata=meta, stream_type="BUFFERED",
                      current_time=position or None)
        mc.block_until_active(timeout=15)
        self.state, self.load_time = "playing", time.monotonic()

    def pause(self):
        if self.state != "playing":
            return
        try:
            mc = self.speaker.cast.media_controller
            mc.update_status()
            time.sleep(0.3)
            self.paused_at = mc.status.adjusted_current_time or 0.0
            mc.pause()
            log.info("paused %s at %.0fs", self.tracks[self.index]["path"].name, self.paused_at)
        except Exception as exc:
            log.warning("pause failed (%s); will reload on resume", exc)
        self.state = "paused"

    def resume(self):
        if self.state == "idle" and self.pending_album is not None:
            return self.start_album(self.pending_album)  # earlier start never got going
        if self.state == "finished":
            log.info("album had finished - starting again")
            return self.load(0, 0.0)
        if self.state != "paused":
            return
        try:
            mc = self.speaker.cast.media_controller if self.speaker.connected() else None
            if mc and mc.status.content_id == self.url and mc.status.player_state == "PAUSED":
                mc.play()
                self.state = "playing"
                log.info("resumed")
                return
        except Exception as exc:
            log.warning("resume failed (%s); reloading", exc)
        # speaker forgot the session (timeout, someone else cast, reconnect) - reload where we were
        self.load(self.index, self.paused_at)

    def tick(self):
        """Advance to the next track when the current one finishes."""
        if self.state != "playing" or time.monotonic() - self.load_time < 3:
            return
        if not self.speaker.connected():
            return
        st = self.speaker.cast.media_controller.status
        if st.content_id == self.url and st.player_state == "IDLE" and st.idle_reason == "FINISHED":
            if self.index + 1 < len(self.tracks):
                self.load(self.index + 1, 0.0)
            else:
                log.info("album finished")
                self.state = "finished"


# ------------------------------------------------------------------ main loop

def album_id_from(text):
    data = json.loads(text)
    value = data.get("id") if isinstance(data, dict) else None
    if value is None and isinstance(data, dict) and data:
        value = next(iter(data.values()))  # "first field" fallback
    return int(value)


class CastWorker(threading.Thread):
    """Does all the speaker work (discovery, connect, load, pause, track advance) so a
    slow or missing speaker can never stall NFC polling. Commands run in order."""

    def __init__(self, player):
        super().__init__(daemon=True)
        self.player = player
        self.queue = queue.Queue()
        self.tag_present = threading.Event()

    def send(self, *cmd):
        self.queue.put(cmd)

    def run(self):
        retry, retry_at = None, 0.0
        while not STOP.is_set():
            try:
                cmd = self.queue.get(timeout=1)
                retry = None  # a newer command supersedes any pending retry
            except queue.Empty:
                cmd = None
                if retry and time.monotonic() >= retry_at and self.tag_present.is_set():
                    cmd, retry = retry, None
            if cmd:
                try:
                    if cmd[0] == "start":
                        self.player.start_album(cmd[1])
                    elif cmd[0] == "resume":
                        self.player.resume()
                    elif cmd[0] == "pause":
                        self.player.pause()
                except Exception as exc:
                    self.player.speaker.close()
                    if cmd[0] in ("start", "resume"):
                        log.error("playback failed: %s - retrying in 15s while the tag is on", exc)
                        retry, retry_at = ("resume",), time.monotonic() + 15
                    else:
                        log.warning("%s failed: %s", cmd[0], exc)
            try:
                self.player.tick()
            except Exception as exc:
                log.warning("track advance failed: %s", exc)
                self.player.speaker.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(BASE / "player.log", maxBytes=512000, backupCount=2)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    for noisy in ("pychromecast", "zeroconf", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MusicHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("serving %s on port %d; speaker %r", MUSIC, PORT, CAST_NAME)

    i2c = busio.I2C(board.SCL, board.SDA)
    pn532 = nfc.open_reader(i2c, log=log.info)
    speaker = Speaker()
    player = Player(speaker)
    worker = CastWorker(player)
    worker.start()

    present_uid = None    # tag currently on the reader
    playing_uid = None    # tag whose album is loaded (playing, paused or retrying)
    last_seen = 0.0
    misses = 0            # consecutive empty polls while a tag is believed present
    known = {}            # uid -> album id, so a known tag never needs re-reading
    log.info("ready - tap a tag (pauses after %.0fs without seeing the tag)", REMOVE_GRACE)

    while not STOP.is_set():
        try:
            uid = nfc.poll(pn532)
            now = time.monotonic()

            if uid is not None:
                last_seen, misses = now, 0
                if uid != present_uid:
                    if uid == playing_uid:
                        log.info("tag %s back on", uid.hex())
                        present_uid = uid
                        worker.tag_present.set()
                        worker.send("resume")
                        continue
                    if uid in known:
                        album_id = known[uid]
                        log.info("tag %s -> album id %s (remembered)", uid.hex(), album_id)
                    else:
                        t_read = time.monotonic()
                        try:
                            msg = nfc.read_tag(pn532, uid)
                        except nfc.ReadError as exc:
                            log.warning("tag read failed after %.1fs: %s",
                                        time.monotonic() - t_read, exc)
                            continue  # present_uid unchanged, so the next poll tries again
                        present_uid = uid
                        try:
                            text = nfc.tag_text(msg) if msg else None
                            if text is None:
                                raise ValueError("tag has no text/JSON record")
                            album_id = album_id_from(text)
                        except (ValueError, TypeError, IndexError) as exc:
                            log.warning("tag %s not understood: %s", uid.hex(), exc)
                            continue
                        known[uid] = album_id
                        log.info("tag %s -> album id %s (read in %.1fs)",
                                 uid.hex(), album_id, time.monotonic() - t_read)
                    present_uid = uid
                    playing_uid = uid
                    worker.tag_present.set()
                    worker.send("start", album_id)

            elif present_uid is not None:
                misses += 1
                if misses >= REMOVE_MISSES and now - last_seen > REMOVE_GRACE:
                    log.info("tag removed (%d missed polls over %.1fs)", misses, now - last_seen)
                    present_uid = None
                    worker.tag_present.clear()
                    worker.send("pause")

        except (RuntimeError, OSError) as exc:
            # PN532 stopped answering (I2C hiccup): re-initialise rather than die
            log.warning("reader error (%s); re-initialising", exc)
            time.sleep(0.5)
            pn532 = nfc.open_reader(i2c, log=log.info)
            last_seen = time.monotonic()  # don't count the outage as the tag going away

    log.info("stopping")
    try:
        if player.state == "playing":
            speaker.cast.media_controller.pause()
    except Exception:
        pass
    speaker.close()
    server.shutdown()


if __name__ == "__main__":
    main()
