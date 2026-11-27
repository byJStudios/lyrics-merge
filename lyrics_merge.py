from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mutagen.flac import FLAC

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
except ImportError:
    Figure = None
    FigureCanvasTkAgg = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False


# ============================================================
# APP
# ============================================================

APP_NAME = "Qobuz + Amazon Lyrics Merger"
VERSION = "2.8.1 Final"
ANALYSIS_SCHEMA_VERSION = 2
SAMSUNG_EXPORT_SCHEMA_VERSION = 7
DEFAULT_MAX_DURATION_DIFF = 1.50
DEFAULT_WORKERS = min(8, max(2, os.cpu_count() or 4))

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "lyrics_merger_settings.json"
LOG_DIR = APP_DIR / "logs"
ANALYSIS_CACHE_FILE = APP_DIR / "audio_analysis_cache.json"
ANALYSIS_CACHE_DIR = APP_DIR / "audio_analysis_cache"
SAMSUNG_EXPORT_CACHE_FILE = APP_DIR / "samsung_export_cache.json"
REVIEW_DECISIONS_FILE = APP_DIR / "match_review_decisions.json"
REVIEW_DECISIONS_SCHEMA_VERSION = 1

SYNCED_LYRICS_RE = re.compile(r"(?m)^\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
LRC_TIMESTAMP_TOKEN_RE = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
LRC_METADATA_RE = re.compile(r"^\[(?:ar|ti|al|by|offset|re|ve|length):.*\]$", re.IGNORECASE)
MULTIDISC_TOTAL_TAGS = ("TRACKTOTAL", "TOTALTRACKS")
MULTIDISC_DISC_TOTAL_TAGS = ("DISCTOTAL", "TOTALDISCS")
LYRICS_FIELDS = ("LYRICS", "SYNCEDLYRICS", "UNSYNCEDLYRICS")
COVER_FILENAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
)

STATUS_ORDER = {
    "BEREIT": 0,
    "MANUELL FREIGEGEBEN": 1,
    "ÜBERSCHREIBEN": 2,
    "VORHANDEN": 3,
    "AMAZON-ONLY OK": 4,
    "AMAZON-ONLY": 5,
    "AMAZON-ONLY IGNORIERT": 6,
    "KEINE SYNC-LYRICS": 7,
    "KEINE LYRICS": 8,
    "VARIANTENPRÜFUNG": 9,
    "ZEITPRÜFUNG": 10,
    "VARIANTE ABGELEHNT": 11,
    "KEIN AMAZON-TITEL": 12,
}
MATCH_ORDER = {"ISRC": 0, "TITLE": 1, "ALBUM/TRACK": 2, "-": 3, "": 4}
SOURCE_ORDER = {"Qobuz + Amazon": 0, "Qobuz": 1, "Amazon-only": 2}


# ============================================================
# THEME
# ============================================================

COLORS = {
    "bg": "#15171a",
    "panel": "#1d2024",
    "panel2": "#24282d",
    "field": "#101215",
    "border": "#3a4148",
    "fg": "#f1f3f5",
    "muted": "#a6adb4",
    "accent": "#3d8bfd",
    "accent_hover": "#66a3ff",
    "selection": "#2b5d87",
    "progress": "#4b7898",
    "ready_bg": "#183b2b",
    "ready_fg": "#96f2bd",
    "existing_bg": "#3f381b",
    "existing_fg": "#ffe08a",
    "warning_bg": "#4b331c",
    "warning_fg": "#ffc078",
    "error_bg": "#4a2327",
    "error_fg": "#ff9b9b",
    "overwrite_bg": "#20384a",
    "overwrite_fg": "#91d5ff",
    "neutral_bg": "#2c3035",
    "neutral_fg": "#c7cdd3",
    "amazon_bg": "#20384a",
    "amazon_fg": "#91d5ff",
}


def enable_windows_dark_titlebar(window):
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
                break
            except Exception:
                continue
    except Exception:
        pass


# ============================================================
# DATA MODELS
# ============================================================

@dataclass(frozen=True)
class Track:
    path: Path
    title: str
    title_norm: str
    artist: str
    artist_norm: str
    album: str
    album_norm: str
    isrc: str
    track_number: Optional[int]
    disc_number: Optional[int]
    track_total: Optional[int]
    disc_total: Optional[int]
    duration: float
    lyrics: str
    lyrics_synced: bool
    codec: str
    sample_rate: int
    bits_per_sample: int
    channels: int
    file_size: int
    audio_payload_size: int
    audio_bitrate_kbps: int
    pcm_bitrate_kbps: int
    cover_present: bool
    cover_width: int
    cover_height: int
    cover_source: str
    thumbnail_png: Optional[bytes]


@dataclass
class Match:
    # target bleibt aus Kompatibilitätsgründen der primär angezeigte Track:
    # Qobuz bei Qobuz-Zeilen, Amazon bei AMAZON-ONLY-Zeilen.
    target: Track
    source: Optional[Track]
    status: str
    method: str
    duration_diff: Optional[float]
    message: str = ""
    target_kind: str = "Qobuz"


def qobuz_track(match: Match) -> Optional[Track]:
    return match.target if match.target_kind == "Qobuz" else None


def amazon_track(match: Match) -> Optional[Track]:
    if match.target_kind == "Amazon":
        return match.target
    return match.source


def display_track(match: Match) -> Track:
    return qobuz_track(match) or amazon_track(match) or match.target


def source_label(match: Match) -> str:
    q = qobuz_track(match)
    a = amazon_track(match)
    if q and a:
        return "Qobuz + Amazon"
    if q:
        return "Qobuz"
    return "Amazon-only"


# ============================================================
# BASIC HELPERS
# ============================================================

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def _path_token(path: Path) -> str:
    return str(path.resolve()).casefold()


def pair_decision_key(qobuz: Track, amazon: Track) -> str:
    raw = f"PAIR|{_path_token(qobuz.path)}|{_path_token(amazon.path)}"
    return "pair_" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def amazon_decision_key(amazon: Track) -> str:
    raw = f"AMAZON|{_path_token(amazon.path)}"
    return "amazon_" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def review_entries(review_data) -> dict:
    if isinstance(review_data, dict) and isinstance(review_data.get("entries"), dict):
        return review_data["entries"]
    if isinstance(review_data, dict):
        return review_data
    return {}


def get_pair_decision(review_data, qobuz: Optional[Track], amazon: Optional[Track]) -> str:
    if not qobuz or not amazon:
        return ""
    entry = review_entries(review_data).get(pair_decision_key(qobuz, amazon), {})
    action = entry.get("action", "") if isinstance(entry, dict) else ""
    return action if action in ("APPROVE", "REJECT") else ""


def get_amazon_decision(review_data, amazon: Optional[Track]) -> str:
    if not amazon:
        return ""
    entry = review_entries(review_data).get(amazon_decision_key(amazon), {})
    action = entry.get("action", "") if isinstance(entry, dict) else ""
    return action if action in ("INCLUDE", "IGNORE") else ""


def lyrics_censor_suspicion(track: Optional[Track]) -> str:
    if not track or not track.lyrics:
        return "keine Lyrics"
    text = track.lyrics
    markers = []
    if re.search(r"\*{2,}", text):
        markers.append("Sternchen-Zensur")
    if re.search(r"(?i)\b(?:x{2,}|[a-z]?\*+[a-z]?)\b", text):
        markers.append("maskierte Wörter")
    if re.search(r"(?i)\b(clean|censored|radio edit)\b", track.title + " " + track.album):
        markers.append("Clean/Radio-Hinweis")
    return ", ".join(dict.fromkeys(markers)) if markers else "keine offensichtlichen Zensurmarker"


def lyrics_preview(track: Optional[Track], max_lines: int = 4, max_chars: int = 420) -> str:
    if not track or not track.lyrics:
        return ""
    lines = [line.strip() for line in track.lyrics.splitlines() if line.strip()]
    text = "\n".join(lines[:max_lines])
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def get_tag(tags, key: str) -> str:
    if not tags:
        return ""
    for existing_key in tags.keys():
        if existing_key.upper() == key.upper():
            values = tags.get(existing_key)
            return str(values[0]).strip() if values else ""
    return ""


def parse_track_number(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value.split("/")[0].strip())
    except (ValueError, IndexError):
        return None


def extract_lyrics(tags) -> tuple[str, bool]:
    fallback = ""
    for field in LYRICS_FIELDS:
        value = get_tag(tags, field)
        if not value:
            continue
        if SYNCED_LYRICS_RE.search(value):
            return value, True
        if not fallback:
            fallback = value
    return fallback, False


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{sec:02}" if hours else f"{minutes}:{sec:02}"


def format_samplerate(rate: int) -> str:
    if not rate:
        return "-"
    if rate % 1000 == 0:
        return f"{rate // 1000} kHz"
    return f"{rate / 1000:.1f} kHz"


def format_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def channel_text(channels: int) -> str:
    if channels == 1:
        return "Mono"
    if channels == 2:
        return "Stereo"
    return f"{channels} Kanäle" if channels else "-"


def quality_text(track: Track) -> str:
    bitrate = f"{track.audio_bitrate_kbps} kbps" if track.audio_bitrate_kbps else "-"
    return f"{track.bits_per_sample}-Bit / {format_samplerate(track.sample_rate)} / {bitrate}"


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dbfs(value: float) -> float:
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value)


# ============================================================
# FLAC PAYLOAD / AUDIO INTEGRITY
# ============================================================

def get_audio_payload_offset(path: Path) -> int:
    with path.open("rb") as file:
        if file.read(4) != b"fLaC":
            raise RuntimeError(f"Keine standardkonforme FLAC-Datei: {path}")
        while True:
            header = file.read(4)
            if len(header) != 4:
                raise RuntimeError(f"Beschädigte FLAC-Metadaten: {path}")
            last_block = bool(header[0] & 0x80)
            block_length = int.from_bytes(header[1:4], byteorder="big")
            file.seek(block_length, os.SEEK_CUR)
            if last_block:
                return file.tell()


def audio_payload_sha256(path: Path) -> str:
    offset = get_audio_payload_offset(path)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        file.seek(offset)
        while True:
            chunk = file.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ============================================================
# COVER
# ============================================================

def get_cover_data(audio: FLAC, path: Path) -> tuple[Optional[bytes], int, int, str]:
    if audio.pictures:
        picture = next((p for p in audio.pictures if p.type == 3), audio.pictures[0])
        return picture.data, picture.width or 0, picture.height or 0, "Embedded"

    for filename in COVER_FILENAMES:
        candidate = path.parent / filename
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_bytes()
            width = height = 0
            if Image is not None:
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
            return raw, width, height, candidate.name
        except Exception:
            continue
    return None, 0, 0, ""


def create_thumbnail(cover_data: Optional[bytes], size: int = 44) -> Optional[bytes]:
    if not cover_data or Image is None:
        return None
    try:
        with Image.open(io.BytesIO(cover_data)) as image:
            image = image.convert("RGB")
            image.thumbnail((size, size), Image.LANCZOS)
            background = Image.new("RGB", (size, size), COLORS["panel2"])
            background.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
            output = io.BytesIO()
            background.save(output, format="PNG")
            return output.getvalue()
    except Exception:
        return None


def load_large_cover(path: Path, size: int = 220):
    if Image is None:
        return None
    try:
        audio = FLAC(path)
        raw, _, _, _ = get_cover_data(audio, path)
        if not raw:
            return None
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((size, size), Image.LANCZOS)
            background = Image.new("RGB", (size, size), COLORS["panel2"])
            background.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
            return ImageTk.PhotoImage(background)
    except Exception:
        return None


# ============================================================
# TRACK READING
# ============================================================

def read_track(path: Path, make_thumbnail: bool = False) -> Track:
    audio = FLAC(path)
    tags = audio.tags or {}
    title = get_tag(tags, "TITLE") or path.stem
    artist = get_tag(tags, "ARTIST") or get_tag(tags, "ALBUMARTIST") or get_tag(tags, "ALBUM_ARTIST")
    album = get_tag(tags, "ALBUM")
    isrc = get_tag(tags, "ISRC").upper()
    track_number = parse_track_number(get_tag(tags, "TRACKNUMBER") or get_tag(tags, "TRACK"))
    disc_number = parse_track_number(get_tag(tags, "DISCNUMBER") or get_tag(tags, "DISC"))
    track_total = parse_track_number(get_tag(tags, "TRACKTOTAL") or get_tag(tags, "TOTALTRACKS"))
    disc_total = parse_track_number(get_tag(tags, "DISCTOTAL") or get_tag(tags, "TOTALDISCS"))
    lyrics, lyrics_synced = extract_lyrics(tags)
    duration = float(audio.info.length)
    file_size = path.stat().st_size

    try:
        payload_offset = get_audio_payload_offset(path)
        audio_payload_size = max(0, file_size - payload_offset)
    except Exception:
        audio_payload_size = 0

    audio_bitrate_kbps = (
        round(audio_payload_size * 8 / duration / 1000)
        if duration > 0 and audio_payload_size > 0 else 0
    )
    pcm_bitrate_kbps = round(
        audio.info.sample_rate * audio.info.bits_per_sample * audio.info.channels / 1000
    )

    cover_data, cover_width, cover_height, cover_source = get_cover_data(audio, path)
    thumbnail_png = create_thumbnail(cover_data) if make_thumbnail else None

    return Track(
        path=path,
        title=title,
        title_norm=normalize(title),
        artist=artist,
        artist_norm=normalize(artist),
        album=album,
        album_norm=normalize(album),
        isrc=isrc,
        track_number=track_number,
        disc_number=disc_number,
        track_total=track_total,
        disc_total=disc_total,
        duration=duration,
        lyrics=lyrics,
        lyrics_synced=lyrics_synced,
        codec="FLAC",
        sample_rate=int(audio.info.sample_rate),
        bits_per_sample=int(audio.info.bits_per_sample),
        channels=int(audio.info.channels),
        file_size=file_size,
        audio_payload_size=audio_payload_size,
        audio_bitrate_kbps=audio_bitrate_kbps,
        pcm_bitrate_kbps=pcm_bitrate_kbps,
        cover_present=bool(cover_data),
        cover_width=cover_width,
        cover_height=cover_height,
        cover_source=cover_source,
        thumbnail_png=thumbnail_png,
    )


def find_flacs(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".flac"),
        key=lambda p: str(p).casefold(),
    )


def scan_tracks(paths, workers, cancel_event, make_thumbnails, progress_callback=None):
    tracks, errors = [], []
    if not paths:
        return tracks, errors
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(read_track, path, make_thumbnails): path for path in paths
        }
        completed = 0
        for future in as_completed(future_map):
            if cancel_event.is_set():
                break
            path = future_map[future]
            try:
                tracks.append(future.result())
            except Exception as exc:
                errors.append((path, str(exc)))
            completed += 1
            if progress_callback:
                progress_callback(completed, len(paths))
    return tracks, errors


# ============================================================
# MATCHING
# ============================================================

def candidate_sort_key(target: Track, source: Track):
    return (
        abs(target.duration - source.duration),
        0 if target.artist_norm and target.artist_norm == source.artist_norm else 1,
        0 if target.album_norm and target.album_norm == source.album_norm else 1,
        0 if target.track_number is not None and target.track_number == source.track_number else 1,
    )


def build_source_indexes(sources: list[Track]):
    by_isrc = defaultdict(list)
    by_title = defaultdict(list)
    by_album_track = defaultdict(list)
    for track in sources:
        if track.isrc:
            by_isrc[track.isrc].append(track)
        if track.title_norm:
            by_title[track.title_norm].append(track)
        if track.album_norm and track.track_number is not None:
            by_album_track[(track.album_norm, track.track_number)].append(track)
    return by_isrc, by_title, by_album_track


def find_source_candidate(
    target: Track,
    by_isrc,
    by_title,
    by_album_track,
    max_duration_diff: float,
):
    """
    Trennt Dateizuordnung bewusst von Lyrics-Verfügbarkeit.

    Rückgabe:
      source, method, classification, duration_diff, reason

    classification:
      SAFE     -> sichere automatische Zuordnung
      TIME     -> starke Zuordnung, aber Laufzeit außerhalb Toleranz
      VARIANT  -> wahrscheinlich derselbe Album-Slot, aber abweichende
                  Titel-/ISRC-Metadaten; niemals automatisch Lyrics übernehmen
      NONE     -> kein plausibler Amazon-Kandidat
    """
    # 1) ISRC ist der stärkste Schlüssel.
    if target.isrc and target.isrc in by_isrc:
        candidates = list(by_isrc[target.isrc])
        source = min(candidates, key=lambda c: candidate_sort_key(target, c))
        diff = abs(target.duration - source.duration)
        if diff > max_duration_diff:
            return source, "ISRC", "TIME", diff, "ISRC identisch, Laufzeit außerhalb der Toleranz."
        return source, "ISRC", "SAFE", diff, "ISRC identisch."

    # 2) Exakter normalisierter Titel; Artist wird nach Möglichkeit mitgeprüft.
    if target.title_norm and target.title_norm in by_title:
        candidates = list(by_title[target.title_norm])
        if target.artist_norm:
            same_artist = [c for c in candidates if c.artist_norm == target.artist_norm]
            if same_artist:
                candidates = same_artist
        source = min(candidates, key=lambda c: candidate_sort_key(target, c))
        diff = abs(target.duration - source.duration)

        # Sind auf beiden Seiten explizite, aber unterschiedliche ISRCs vorhanden,
        # darf ein bloßer Titelgleichstand NICHT automatisch gewinnen.
        if target.isrc and source.isrc and target.isrc != source.isrc:
            return source, "TITLE", "VARIANT", diff, "Titel passt, aber ISRC ist unterschiedlich."
        if diff > max_duration_diff:
            return source, "TITLE", "TIME", diff, "Titel passt, Laufzeit außerhalb der Toleranz."
        return source, "TITLE", "SAFE", diff, "Titel/Artist passen."

    # 3) Diagnose-Fallback: gleicher Album-Slot + möglichst gleicher Artist.
    # Das erkennt z. B. Clean/Explicit- oder Dialogue-Varianten, ohne sie jemals
    # automatisch als Lyrics-Quelle zu benutzen.
    key = (target.album_norm, target.track_number)
    if target.album_norm and target.track_number is not None and key in by_album_track:
        candidates = list(by_album_track[key])
        if target.artist_norm:
            same_artist = [c for c in candidates if c.artist_norm == target.artist_norm]
            if same_artist:
                candidates = same_artist
        source = min(candidates, key=lambda c: candidate_sort_key(target, c))
        diff = abs(target.duration - source.duration)
        # Etwas großzügiger als der echte Merge, weil dies nur eine Warn-/Diagnoseklasse ist.
        diagnostic_limit = max(3.0, max_duration_diff)
        if diff <= diagnostic_limit:
            reasons = ["Album und Tracknummer passen"]
            if target.artist_norm and source.artist_norm == target.artist_norm:
                reasons.append("Artist passt")
            if target.isrc and source.isrc and target.isrc != source.isrc:
                reasons.append("ISRC unterschiedlich")
            if target.title_norm != source.title_norm:
                reasons.append("Titel unterschiedlich")
            return source, "ALBUM/TRACK", "VARIANT", diff, ", ".join(reasons) + "."

    return None, "-", "NONE", None, "Keine passende Amazon-Datei gefunden."


def build_matches(sources, targets, max_duration_diff, overwrite, only_synced, review_data=None):
    """
    Vorschau 2.7:
    - Dateizuordnung und Lyrics-Verfügbarkeit bleiben getrennt.
    - VARIANT/TIME können bewusst manuell freigegeben oder abgelehnt werden.
    - Abgelehnte Varianten dürfen anschließend als eigener Amazon-only-Titel erscheinen.
    - Amazon-only Entscheidungen (einschließen/ignorieren) werden sichtbar dargestellt.
    """
    by_isrc, by_title, by_album_track = build_source_indexes(sources)
    matches: list[Match] = []
    accounted_amazon_paths: set[Path] = set()

    for target in sorted(targets, key=lambda item: str(item.path).casefold()):
        source, method, classification, duration_diff, reason = find_source_candidate(
            target, by_isrc, by_title, by_album_track, max_duration_diff
        )
        pair_decision = get_pair_decision(review_data, target, source)

        # Sichere Matches werden immer verbucht. Offene Varianten ebenfalls, damit
        # derselbe Kandidat nicht gleichzeitig verwirrend als AMAZON-ONLY auftaucht.
        # Eine explizit ABGELEHNTE Paarung wird dagegen freigegeben: Die Amazon-Datei
        # kann dann als eigenständiger Titel erscheinen und separat bewertet werden.
        if source is not None:
            if classification == "SAFE" or pair_decision != "REJECT":
                accounted_amazon_paths.add(source.path.resolve())

        # Bereits vorhandene Qobuz-Lyrics haben Vorrang; die Review-Entscheidung
        # bleibt trotzdem gespeichert und wird beim Samsung-Export berücksichtigt.
        if target.lyrics and not overwrite:
            msg = "Qobuz-Datei enthält bereits Lyrics; kein Schreibvorgang nötig."
            if pair_decision == "APPROVE":
                msg += " Variantenpaar wurde manuell freigegeben."
            elif pair_decision == "REJECT":
                msg += " Variantenpaar wurde manuell abgelehnt."
            matches.append(Match(
                target, source, "VORHANDEN", method if source else "-", duration_diff,
                msg, "Qobuz"
            ))
            continue

        if classification in ("TIME", "VARIANT") and source is not None:
            if pair_decision == "REJECT":
                matches.append(Match(
                    target, source, "VARIANTE ABGELEHNT", method, duration_diff,
                    reason + " Manuell abgelehnt; keine Lyrics-Übernahme.", "Qobuz"
                ))
                continue

            if pair_decision == "APPROVE":
                # Die menschliche Freigabe ersetzt nur die automatische Zuordnungs-
                # Sperre. Die Lyrics-Anforderungen werden weiterhin vollständig geprüft.
                if not source.lyrics:
                    matches.append(Match(
                        target, source, "KEINE LYRICS", method, duration_diff,
                        reason + " Manuell als Variante freigegeben, Amazon enthält aber keine Lyrics.", "Qobuz"
                    ))
                    continue
                if only_synced and not source.lyrics_synced:
                    matches.append(Match(
                        target, source, "KEINE SYNC-LYRICS", method, duration_diff,
                        reason + " Manuell freigegeben, aber Amazon enthält nur unsynchronisierte Lyrics.", "Qobuz"
                    ))
                    continue
                matches.append(Match(
                    target, source, "MANUELL FREIGEGEBEN", method, duration_diff,
                    reason + " Manuell freigegeben; Lyrics dürfen übernommen werden.", "Qobuz"
                ))
                continue

            status = "ZEITPRÜFUNG" if classification == "TIME" else "VARIANTENPRÜFUNG"
            detail = reason
            if source:
                detail += f" Amazon: {source.title}"
                if source.isrc:
                    detail += f" [{source.isrc}]"
            matches.append(Match(
                target, source, status, method, duration_diff,
                detail + " Persönliche Entscheidung erforderlich; kein Auto-Merge.", "Qobuz"
            ))
            continue

        if classification == "NONE" or source is None:
            matches.append(Match(
                target, None, "KEIN AMAZON-TITEL", "-", None,
                "Kein plausibles Amazon-Gegenstück gefunden.", "Qobuz"
            ))
            continue

        # Ab hier steht die Dateizuordnung automatisch sicher fest. Erst jetzt Lyrics bewerten.
        if not source.lyrics:
            matches.append(Match(
                target, source, "KEINE LYRICS", method, duration_diff,
                "Amazon-Datei wurde sicher zugeordnet, enthält aber keinen Lyrics-Tag.", "Qobuz"
            ))
            continue

        if only_synced and not source.lyrics_synced:
            matches.append(Match(
                target, source, "KEINE SYNC-LYRICS", method, duration_diff,
                "Amazon-Datei wurde sicher zugeordnet, enthält aber nur unsynchronisierte Lyrics.", "Qobuz"
            ))
            continue

        status = "ÜBERSCHREIBEN" if target.lyrics else "BEREIT"
        matches.append(Match(target, source, status, method, duration_diff, reason, "Qobuz"))

    # Amazon-Dateien ohne Qobuz-Gegenstück werden sichtbar gemacht. Das gilt auch,
    # wenn die Qobuz-Bibliothek komplett leer/nicht angegeben ist.
    for source in sorted(sources, key=lambda item: str(item.path).casefold()):
        if source.path.resolve() in accounted_amazon_paths:
            continue
        lyric_note = (
            "Sync-Lyrics vorhanden; Samsung-Export kann FLAC + LRC erzeugen."
            if source.lyrics_synced else
            ("Nur unsynchronisierte Lyrics vorhanden." if source.lyrics else "Keine Lyrics vorhanden.")
        )
        amazon_decision = get_amazon_decision(review_data, source)
        if amazon_decision == "IGNORE":
            status = "AMAZON-ONLY IGNORIERT"
            decision_note = " Persönlich für den Samsung-Export ignoriert."
        elif amazon_decision == "INCLUDE":
            status = "AMAZON-ONLY OK"
            decision_note = " Persönlich für den Samsung-Export bestätigt."
        else:
            status = "AMAZON-ONLY"
            decision_note = " Noch keine persönliche Entscheidung gespeichert."
        matches.append(Match(
            source, None, status, "-", None,
            "Kein Qobuz-Gegenstück erkannt. " + lyric_note + decision_note,
            "Amazon",
        ))

    return matches


# ============================================================
# SAFE METADATA WRITE
# ============================================================

def write_lyrics_atomic(target_path, lyrics, target_root, overwrite, verify_audio, backup_root):
    original_stat = target_path.stat()
    original_hash = audio_payload_sha256(target_path) if verify_audio else None

    fd, temp_name = tempfile.mkstemp(prefix=".lyricsmerge_", suffix=".flac", dir=target_path.parent)
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        shutil.copy2(target_path, temp_path)
        audio = FLAC(temp_path)
        existing, _ = extract_lyrics(audio.tags or {})
        if existing and not overwrite:
            return False, "Lyrics inzwischen vorhanden."

        if audio.tags:
            for key in list(audio.tags.keys()):
                if key.upper() in LYRICS_FIELDS:
                    del audio.tags[key]
        audio["LYRICS"] = [lyrics]
        audio.save()

        if verify_audio and audio_payload_sha256(temp_path) != original_hash:
            raise RuntimeError("AUDIO-INTEGRITÄTSPRÜFUNG FEHLGESCHLAGEN. Original wurde NICHT verändert.")

        if backup_root:
            relative = target_path.relative_to(target_root)
            backup_path = backup_root / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, backup_path)

        os.replace(temp_path, target_path)
        try:
            os.utime(target_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        except Exception:
            pass
        return True, "OK"
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


# ============================================================
# SAMSUNG MUSIC EXPORT
# ============================================================

def samsung_export_cache_key(source_path: Path, export_root: Path, relative: Optional[Path] = None) -> str:
    """Eindeutiger Cache-Key je Audioquelle, Exportziel und Zielpfad."""
    rel = str(relative) if relative is not None else ""
    return f"{export_root.resolve()}|{source_path.resolve()}|{rel}".casefold()


def samsung_source_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000)),
    }


def synced_lyrics_digest(lyrics: str) -> str:
    if not lyrics:
        return ""
    return hashlib.sha256(lyrics.encode("utf-8", errors="replace")).hexdigest()


def _lrc_timestamp_to_seconds(match: re.Match) -> float:
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    fraction_raw = match.group(3) or "0"
    fraction = int(fraction_raw) / (10 ** len(fraction_raw))
    return minutes * 60 + seconds + fraction


def _format_lrc_timestamp(seconds: float) -> str:
    # Samsung Music ist mit klassischem LRC im Hundertstel-Format am robustesten.
    centiseconds = max(0, int(round(seconds * 100)))
    minutes, rem = divmod(centiseconds, 6000)
    sec, cs = divmod(rem, 100)
    return f"[{minutes:02d}:{sec:02d}.{cs:02d}]"


def sanitize_lrc_text(lyrics: str) -> tuple[str, dict]:
    """
    Wandelt Amazon-LRC-artige Daten in ein bewusst konservatives Samsung-LRC um.

    Amazon liefert häufig abwechselnd START-Zeilen mit Text und nackte Zeitstempel,
    die offenbar das Ende einer Zeile markieren. Samsung Music interpretiert diese
    nackten Timestamps jedoch als eigene leere Events und kann bei überlappenden
    Zeiten hängen bleiben. Deshalb:
      - nackte Timestamp-Events entfernen
      - echte Text-Events chronologisch/stabil sortieren
      - exakte Duplikate entfernen
      - bekannte LRC-Metadaten erhalten
      - ungetimte Rohzeilen aus dem Samsung-Sidecar fernhalten
      - echte Zeitwerte NICHT erfinden oder verschieben
    """
    stats = {
        "source_lines": 0,
        "source_timestamp_events": 0,
        "text_events": 0,
        "empty_events_removed": 0,
        "metadata_lines": 0,
        "untimed_lines_removed": 0,
        "reordered_text_events": 0,
        "duplicates_removed": 0,
        "same_timestamp_conflicts": 0,
        "final_text_events": 0,
        "timeline_ok": True,
    }
    if not lyrics:
        return "", stats

    normalized = lyrics.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    metadata_lines: list[str] = []
    events: list[tuple[float, int, str]] = []
    seq = 0

    for raw in normalized.split("\n"):
        line = raw.rstrip()
        stats["source_lines"] += 1
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        if not line.strip():
            continue

        stripped = line.strip()
        if LRC_METADATA_RE.match(stripped):
            metadata_lines.append(stripped)
            stats["metadata_lines"] += 1
            continue

        # Nur unmittelbar am Zeilenanfang aufeinanderfolgende Timestamp-Tokens
        # gelten als LRC-Timestamps. So bleiben Texte mit [Klammern] unangetastet.
        pos = 0
        timestamp_matches = []
        while True:
            m = LRC_TIMESTAMP_TOKEN_RE.match(stripped, pos)
            if not m:
                break
            timestamp_matches.append(m)
            pos = m.end()

        if not timestamp_matches:
            stats["untimed_lines_removed"] += 1
            continue

        text_part = stripped[pos:].strip()
        stats["source_timestamp_events"] += len(timestamp_matches)
        if not text_part:
            stats["empty_events_removed"] += len(timestamp_matches)
            continue

        for m in timestamp_matches:
            events.append((_lrc_timestamp_to_seconds(m), seq, text_part))
            seq += 1
            stats["text_events"] += 1

    original_order = [(round(sec, 6), seq_no, txt) for sec, seq_no, txt in events]
    events.sort(key=lambda item: (item[0], item[1]))
    sorted_order = [(round(sec, 6), seq_no, txt) for sec, seq_no, txt in events]
    if sorted_order != original_order:
        stats["reordered_text_events"] = sum(
            1 for a, b in zip(original_order, sorted_order) if a != b
        )

    deduped: list[tuple[float, int, str]] = []
    seen_exact = set()
    same_ts_texts: dict[int, set[str]] = defaultdict(set)
    for sec, seq_no, txt in events:
        formatted = _format_lrc_timestamp(sec)
        key = (formatted, txt)
        if key in seen_exact:
            stats["duplicates_removed"] += 1
            continue
        seen_exact.add(key)
        ts_key = int(round(sec * 100))
        if same_ts_texts[ts_key] and txt not in same_ts_texts[ts_key]:
            stats["same_timestamp_conflicts"] += 1
        same_ts_texts[ts_key].add(txt)
        deduped.append((sec, seq_no, txt))

    # Nach der Sortierung muss die Text-Timeline monoton sein. Gleiche Zeiten
    # sind erlaubt; sie werden nicht künstlich auseinandergezogen.
    last = -1.0
    for sec, _, _ in deduped:
        if sec + 1e-9 < last:
            stats["timeline_ok"] = False
            break
        last = sec

    stats["final_text_events"] = len(deduped)
    out = []
    out.extend(dict.fromkeys(metadata_lines))
    out.extend(f"{_format_lrc_timestamp(sec)} {txt}" for sec, _, txt in deduped)
    return ("\r\n".join(out).rstrip() + "\r\n" if out else ""), stats


def normalize_lrc_text(lyrics: str) -> str:
    # Kompatibilitäts-Wrapper für ältere Aufrufer.
    return sanitize_lrc_text(lyrics)[0]


def samsung_metadata_digest(metadata_overrides: Optional[dict]) -> str:
    if not metadata_overrides:
        return ""
    payload = json.dumps(metadata_overrides, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_samsung_metadata_overrides(audio: FLAC, metadata_overrides: Optional[dict]) -> bool:
    if not metadata_overrides:
        return False
    if audio.tags is None:
        audio.add_tags()
    changed = False

    def set_tag(name: str, value: str):
        nonlocal changed
        current = get_tag(audio.tags or {}, name)
        if current != value:
            audio[name] = [value]
            changed = True

    if metadata_overrides.get("flatten_multidisc"):
        track_no = str(metadata_overrides["track_number"])
        total = str(metadata_overrides["track_total"])

        # Samsung soll genau EINE lineare Trackreihenfolge sehen. Neben den
        # üblichen Vorbis-Comment-Namen werden vorhandene Alias-Tags ebenfalls
        # konsistent gehalten, damit kein Player eine alte Disc-/Tracknummer liest.
        set_tag("TRACKNUMBER", track_no)
        if "TRACK" in (audio.tags or {}):
            set_tag("TRACK", track_no)

        set_tag("TRACKTOTAL", total)
        for tag in MULTIDISC_TOTAL_TAGS:
            if tag in (audio.tags or {}) and tag != "TRACKTOTAL":
                set_tag(tag, total)

        set_tag("DISCNUMBER", "1")
        if "DISC" in (audio.tags or {}):
            set_tag("DISC", "1")

        # DISCTOTAL wird bewusst immer gesetzt. In 2.8 wurde nur ein bereits
        # vorhandenes Disc-Total aktualisiert; damit konnte Samsung weiterhin
        # widersprüchliche Mehrdisc-Informationen aus anderen Tags cachen.
        set_tag("DISCTOTAL", "1")
        for tag in MULTIDISC_DISC_TOTAL_TAGS:
            if tag in (audio.tags or {}) and tag != "DISCTOTAL":
                set_tag(tag, "1")
    return changed


def _filename_track_prefix(path_or_name) -> Optional[int]:
    """Liest nur eindeutige Präfixe wie '01. Titel.flac' / '01 - Titel.flac'."""
    name = path_or_name.name if hasattr(path_or_name, "name") else str(path_or_name)
    m = re.match(r"^\s*(\d{1,3})\s*(?:[.\-_)]\s+|\s+-\s+)", name)
    if not m:
        return None
    try:
        value = int(m.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _filename_order_is_authoritative(group: list[dict], name_key: str) -> tuple[bool, dict[int, dict]]:
    """
    Sichere Fallback-Erkennung für Samsung: Alle FLACs eines Albumordners müssen
    eindeutige, lückenlose Dateipräfixe 1..N besitzen. Erst wenn die eingebetteten
    Track-Tags davon abweichen/dupliziert sind, darf die Dateireihenfolge gewinnen.
    """
    if len(group) < 2:
        return False, {}

    by_prefix = {}
    for item in group:
        prefix = _filename_track_prefix(item[name_key])
        if prefix is None or prefix in by_prefix:
            return False, {}
        by_prefix[prefix] = item

    expected = list(range(1, len(group) + 1))
    if sorted(by_prefix) != expected:
        return False, {}

    # Nicht unnötig jedes normale Album umtaggen. Der Fallback greift nur, wenn
    # die vorhandenen TRACKNUMBER-Tags die eindeutige Dateireihenfolge NICHT
    # bereits abbilden (z. B. The Fame Monster: Disc-Tracknummern 1,2,... erneut).
    tracks = [item.get("track_number") for item in group]
    known_tracks = [t for t in tracks if t is not None]
    duplicate_track_tags = len(known_tracks) != len(set(known_tracks))
    mismatch = any(
        item.get("track_number") is None or item.get("track_number") != prefix
        for prefix, item in by_prefix.items()
    )
    return bool(duplicate_track_tags or mismatch), by_prefix


def annotate_samsung_multidisc_jobs(jobs: list[dict]) -> dict:
    """
    Markiert Mehrdisc-/Trackreihenfolgen für Samsungs lineare Sortierung.

    Priorität:
    1) echte, vollständige DISCNUMBER+TRACKNUMBER-Struktur;
    2) sicherer Fallback auf lückenlose Dateipräfixe 01..N, falls die Tags die
       bereits kanonische Dateireihenfolge widersprechen.
    """
    groups = defaultdict(list)
    for job in jobs:
        groups[job["relative"].parent].append(job)

    summary = {"albums": 0, "tracks": 0}
    for _parent, group in groups.items():
        disc_values = {job.get("disc_number") for job in group if job.get("disc_number") is not None}
        ordered = None
        reason = None

        if len(disc_values) >= 2 and not any(
            job.get("track_number") is None or job.get("disc_number") is None for job in group
        ):
            ordered = sorted(
                group,
                key=lambda job: (
                    int(job.get("disc_number") or 1),
                    int(job.get("track_number") or 0),
                    str(job["relative"]).casefold(),
                ),
            )
            reason = "disc_tags"
        else:
            use_filename_order, by_prefix = _filename_order_is_authoritative(group, "relative")
            if use_filename_order:
                ordered = [by_prefix[i] for i in range(1, len(group) + 1)]
                reason = "filename_prefix"

        if not ordered:
            continue

        total = len(ordered)
        for new_track, job in enumerate(ordered, start=1):
            job["metadata_overrides"] = {
                "flatten_multidisc": True,
                "track_number": new_track,
                "track_total": total,
                "original_disc_number": job.get("disc_number"),
                "original_track_number": job.get("track_number"),
                "flatten_reason": reason,
            }
        summary["albums"] += 1
        summary["tracks"] += total
    return summary

def samsung_export_entry_valid(entry: dict, source_path: Path, export_flac: Path, lrc_path: Path, lyrics_digest: str, metadata_digest: str = "") -> bool:
    if not entry or entry.get("schema") != SAMSUNG_EXPORT_SCHEMA_VERSION:
        return False
    if not source_path.is_file() or not export_flac.is_file():
        return False

    fp = samsung_source_fingerprint(source_path)
    for key in ("size", "mtime_ns", "ctime_ns"):
        if entry.get(f"source_{key}") != fp[key]:
            return False

    if entry.get("lyrics_sha256", "") != lyrics_digest:
        return False
    if entry.get("metadata_sha256", "") != metadata_digest:
        return False

    try:
        export_stat = export_flac.stat()
    except OSError:
        return False

    if entry.get("export_size") != export_stat.st_size or entry.get("export_mtime_ns") != export_stat.st_mtime_ns:
        return False

    expects_lrc = bool(lyrics_digest)
    if entry.get("has_lrc") != expects_lrc:
        return False

    if expects_lrc:
        if not lrc_path.is_file():
            return False
        try:
            lrc_stat = lrc_path.stat()
        except OSError:
            return False
        if entry.get("lrc_size") != lrc_stat.st_size or entry.get("lrc_mtime_ns") != lrc_stat.st_mtime_ns:
            return False
    elif lrc_path.exists():
        # Keine synchronisierten Lyrics mehr: eine alte LRC darf nicht liegen bleiben.
        return False

    return True


def build_samsung_export_jobs(
    qobuz_root: Optional[Path],
    amazon_root: Optional[Path],
    max_duration_diff: float = DEFAULT_MAX_DURATION_DIFF,
    review_data=None,
    include_unreviewed_amazon_only: bool = True,
) -> list[dict]:
    """
    Baut die Samsung-Exportliste mit automatischer Quellenwahl.

    Priorität:
      1. Qobuz ist immer die bevorzugte AUDIOQUELLE, wenn ein Track dort existiert.
      2. Synchronisierte Lyrics kommen bevorzugt aus der Qobuz-Master-FLAC.
      3. Fehlen dort Sync-Lyrics, darf eine passende Amazon-FLAC die LRC liefern.
      4. Amazon-Tracks ohne Qobuz-Gegenstück werden direkt aus Amazon exportiert.
      5. Gehört ein Amazon-only Track zu einem bereits sicher zugeordneten Album,
         wird er in den entsprechenden Qobuz-Albumordner einsortiert.

    Matching Qobuz -> Amazon:
      - primär ISRC
      - Fallback Titel + nach Möglichkeit Artist
      - beim Titel-Fallback Laufzeitprüfung

    Album-Mapping für Amazon-only Tracks:
      - entsteht ausschließlich aus bereits erfolgreichen Track-Matches
      - bevorzugt belastbare ISRC-Matches; Titel-Matches zählen nur, wenn auch
        die normalisierten Album-Tags übereinstimmen
      - mindestens zwei unabhängige Track-Belege aus demselben Amazon-Ordner
      - mindestens 80 % der Belege müssen auf denselben Qobuz-Ordner zeigen
      - bei Unsicherheit bleibt der ursprüngliche Amazon-Ordner erhalten

    Rückgabe pro Job:
      audio_source       Pfad der FLAC, deren Audio/Metadaten kopiert werden
      audio_source_kind  "Qobuz" oder "Amazon"
      lyrics_source      Pfad der Lyrics-Quelle oder None
      lyrics_source_kind "Qobuz", "Amazon" oder "-"
      synced_lyrics      LRC-Text oder ""
      relative           relativer Zielpfad innerhalb des Samsung-Exports
      title              Anzeigename
    """

    qobuz_root = qobuz_root if qobuz_root and qobuz_root.is_dir() else None
    amazon_root = amazon_root if amazon_root and amazon_root.is_dir() else None

    if qobuz_root is None and amazon_root is None:
        return []

    qobuz_tracks: list[Track] = []
    amazon_tracks: list[Track] = []

    if qobuz_root:
        for path in find_flacs(qobuz_root):
            try:
                qobuz_tracks.append(read_track(path, False))
            except Exception:
                # Fehler werden beim eigentlichen Export erneut sauber gemeldet.
                continue

    if amazon_root:
        for path in find_flacs(amazon_root):
            try:
                amazon_tracks.append(read_track(path, False))
            except Exception:
                continue

    by_isrc = defaultdict(list)
    by_title = defaultdict(list)

    for track in amazon_tracks:
        if track.isrc:
            by_isrc[track.isrc].append(track)
        if track.title_norm:
            by_title[track.title_norm].append(track)

    used_amazon_paths: set[Path] = set()
    used_relative_keys: set[str] = set()
    jobs: list[dict] = []

    # Evidenz für die Zuordnung Amazon-Albumordner -> Qobuz-Albumordner.
    # Gezählt werden die konkret gematchten Amazon-Dateien, nicht bloß Namen.
    # Dadurch können leicht unterschiedliche Albumordner-Namen sauber
    # zusammengeführt werden, ohne die Verzeichnisstruktur zu erraten.
    album_mapping_evidence: dict[Path, dict[Path, set[Path]]] = defaultdict(
        lambda: defaultdict(set)
    )

    by_album_track = defaultdict(list)
    for track in amazon_tracks:
        if track.album_norm and track.track_number is not None:
            by_album_track[(track.album_norm, track.track_number)].append(track)

    def best_amazon_match(target: Track):
        return find_source_candidate(
            target, by_isrc, by_title, by_album_track, max_duration_diff
        )

    def add_album_mapping_evidence(qtrack: Track, atrack: Track, method: str) -> None:
        """Sammelt nur konservativ belastbare Album-Zuordnungen."""
        if not qobuz_root or not amazon_root:
            return

        trusted = method == "ISRC"
        if method == "TITLE":
            # Ein reines Titel-Match kann bei Singles/Compilations mehrdeutig sein.
            # Als Album-Beleg darf es deshalb nur zählen, wenn zusätzlich beide
            # Album-Tags vorhanden und identisch normalisiert sind.
            trusted = bool(
                qtrack.album_norm
                and atrack.album_norm
                and qtrack.album_norm == atrack.album_norm
            )

        if not trusted:
            return

        amazon_parent = atrack.path.parent.resolve()
        qobuz_parent = qtrack.path.parent.resolve()
        album_mapping_evidence[amazon_parent][qobuz_parent].add(atrack.path.resolve())

    # --------------------------------------------------------
    # Qobuz hat Priorität als Audioquelle.
    # --------------------------------------------------------
    for qtrack in sorted(qobuz_tracks, key=lambda t: str(t.path).casefold()):
        amazon_match, match_method, classification, _diff, _reason = best_amazon_match(qtrack)
        pair_decision = get_pair_decision(review_data, qtrack, amazon_match)
        manual_safe = bool(amazon_match and classification in ("VARIANT", "TIME") and pair_decision == "APPROVE")
        rejected_pair = bool(amazon_match and classification in ("VARIANT", "TIME") and pair_decision == "REJECT")

        if amazon_match and not rejected_pair:
            # Sichere, offene und manuell freigegebene Varianten zählen als dem
            # Qobuz-Slot zugeordnet. Explizit abgelehnte Varianten werden dagegen
            # freigegeben und können später als Amazon-only separat bewertet werden.
            used_amazon_paths.add(amazon_match.path.resolve())
            if classification == "SAFE" or manual_safe:
                add_album_mapping_evidence(qtrack, amazon_match, match_method)

        if qtrack.lyrics_synced:
            synced_lyrics = qtrack.lyrics
            lyrics_source = qtrack.path
            lyrics_source_kind = "Qobuz"
        elif (classification == "SAFE" or manual_safe) and amazon_match and amazon_match.lyrics_synced:
            synced_lyrics = amazon_match.lyrics
            lyrics_source = amazon_match.path
            lyrics_source_kind = "Amazon"
        else:
            # Offene/abgelehnte Varianten und Zeitprüfungen werden nie automatisch
            # als Lyrics-Quelle verwendet. Nur eine explizite APPROVE-Entscheidung
            # hebt diese Sperre auf.
            synced_lyrics = ""
            lyrics_source = None
            lyrics_source_kind = "-"

        relative = qtrack.path.relative_to(qobuz_root)
        used_relative_keys.add(str(relative).casefold())

        jobs.append({
            "audio_source": qtrack.path,
            "audio_source_kind": "Qobuz",
            "lyrics_source": lyrics_source,
            "lyrics_source_kind": lyrics_source_kind,
            "synced_lyrics": synced_lyrics,
            "relative": relative,
            "title": qtrack.title,
            "album": qtrack.album,
            "track_number": qtrack.track_number,
            "disc_number": qtrack.disc_number,
            "metadata_overrides": {},
        })

    # Aus der gesammelten Evidenz nur wirklich eindeutige Ordner-Mappings bauen.
    # Beispiel: 11 Tracks aus Amazon-Album A matchen auf Qobuz-Album B. Ein
    # fehlender zwölfter Amazon-Track darf dann sicher in B einsortiert werden.
    safe_album_mapping: dict[Path, Path] = {}
    for amazon_parent, targets in album_mapping_evidence.items():
        ranked = sorted(
            (
                (qobuz_parent, len(evidence_paths))
                for qobuz_parent, evidence_paths in targets.items()
            ),
            key=lambda item: (-item[1], str(item[0]).casefold()),
        )
        if not ranked:
            continue

        winner_parent, winner_count = ranked[0]
        total_count = sum(count for _, count in ranked)
        dominance = (winner_count / total_count) if total_count else 0.0

        if winner_count >= 2 and dominance >= 0.80:
            safe_album_mapping[amazon_parent] = winner_parent

    # --------------------------------------------------------
    # Amazon-Fallback: Nur Tracks ohne Qobuz-Gegenstück.
    # --------------------------------------------------------
    if amazon_root:
        for atrack in sorted(amazon_tracks, key=lambda t: str(t.path).casefold()):
            if atrack.path.resolve() in used_amazon_paths:
                continue

            amazon_decision = get_amazon_decision(review_data, atrack)
            if amazon_decision == "IGNORE":
                continue
            if not amazon_decision and not include_unreviewed_amazon_only:
                continue

            synced_lyrics = atrack.lyrics if atrack.lyrics_synced else ""

            mapped_qobuz_parent = safe_album_mapping.get(atrack.path.parent.resolve())
            if mapped_qobuz_parent is not None and qobuz_root is not None:
                try:
                    mapped_parent_relative = mapped_qobuz_parent.relative_to(qobuz_root.resolve())
                    relative = mapped_parent_relative / atrack.path.name
                except ValueError:
                    # Sollte bei intern erzeugten Mappings nicht vorkommen. Falls
                    # doch, niemals raten: sicher auf Amazon-Struktur zurückfallen.
                    relative = atrack.path.relative_to(amazon_root)
            else:
                relative = atrack.path.relative_to(amazon_root)

            # Falls ein Amazon-only Track zufällig denselben relativen Dateipfad
            # wie ein nicht gematchter Qobuz-Track besitzt, darf er diesen im
            # Export niemals überschreiben. Solche seltenen Kollisionen landen
            # deshalb sauber unter einem eigenen Fallback-Unterordner.
            if str(relative).casefold() in used_relative_keys:
                relative = Path("_Amazon Fallback") / relative

                candidate = relative
                counter = 2
                while str(candidate).casefold() in used_relative_keys:
                    candidate = relative.with_name(
                        f"{relative.stem} [Amazon {counter}]{relative.suffix}"
                    )
                    counter += 1
                relative = candidate

            used_relative_keys.add(str(relative).casefold())

            jobs.append({
                "audio_source": atrack.path,
                "audio_source_kind": "Amazon",
                "lyrics_source": atrack.path if synced_lyrics else None,
                "lyrics_source_kind": "Amazon" if synced_lyrics else "-",
                "synced_lyrics": synced_lyrics,
                "relative": relative,
                "title": atrack.title,
                "album": atrack.album,
                "track_number": atrack.track_number,
                "disc_number": atrack.disc_number,
                "metadata_overrides": {},
            })

    annotate_samsung_multidisc_jobs(jobs)
    return jobs

def write_samsung_export_copy(
    source_path: Path,
    export_flac: Path,
    synced_lyrics: str,
    verify_audio: bool = True,
    metadata_overrides: Optional[dict] = None,
) -> dict:
    """
    Erstellt eine Samsung-Music-Kopie einer Qobuz- oder Amazon-FLAC:
      - Quelle bleibt unangetastet.
      - Kein Audio-Encoding.
      - Embedded Lyrics werden nur aus der Export-Kopie entfernt.
      - LRC wird für Samsung sanitisiert (keine nackten End-Timestamps).
      - Mehrdisc-Alben können ausschließlich in der Export-Kopie linearisiert werden.
      - Komprimierte FLAC-Audioframes müssen vor/nach Tag-Write identisch bleiben.
    """
    export_flac.parent.mkdir(parents=True, exist_ok=True)
    lrc_path = export_flac.with_suffix(".lrc")
    source_audio_hash = audio_payload_sha256(source_path) if verify_audio else None

    fd, temp_name = tempfile.mkstemp(prefix=".samsung_export_", suffix=".flac", dir=export_flac.parent)
    os.close(fd)
    temp_flac = Path(temp_name)
    temp_lrc = lrc_path.with_name(lrc_path.name + ".tmp")

    lrc_written = False
    stale_lrc_removed = False
    metadata_changed = False
    lrc_stats = {}

    try:
        shutil.copy2(source_path, temp_flac)
        audio = FLAC(temp_flac)

        if audio.tags:
            for key in list(audio.tags.keys()):
                if key.upper() in LYRICS_FIELDS:
                    del audio.tags[key]
        metadata_changed = _apply_samsung_metadata_overrides(audio, metadata_overrides)
        audio.save()

        if verify_audio:
            exported_hash = audio_payload_sha256(temp_flac)
            if exported_hash != source_audio_hash:
                raise RuntimeError(
                    "AUDIO-INTEGRITÄTSPRÜFUNG FEHLGESCHLAGEN. "
                    "Samsung-Export wurde NICHT übernommen."
                )

        os.replace(temp_flac, export_flac)
        # Bei normalem Export bleibt das Quell-MTIME erhalten. Ein späterer
        # Reparaturmodus setzt bewusst ein neues MTIME, damit Android neu scannt.
        try:
            source_stat = source_path.stat()
            os.utime(export_flac, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        except Exception:
            pass

        lrc_text, lrc_stats = sanitize_lrc_text(synced_lyrics)
        if lrc_text:
            temp_lrc.write_text(lrc_text, encoding="utf-8", newline="")
            os.replace(temp_lrc, lrc_path)
            lrc_written = True
        else:
            if lrc_path.exists():
                lrc_path.unlink()
                stale_lrc_removed = True

        export_stat = export_flac.stat()
        result = {
            "export_flac": export_flac,
            "lrc_path": lrc_path,
            "lrc_written": lrc_written,
            "lrc_stats": lrc_stats,
            "stale_lrc_removed": stale_lrc_removed,
            "metadata_changed": metadata_changed,
            "audio_verified": bool(verify_audio),
            "export_size": export_stat.st_size,
            "export_mtime_ns": export_stat.st_mtime_ns,
        }
        if lrc_written:
            lrc_stat = lrc_path.stat()
            result.update({"lrc_size": lrc_stat.st_size, "lrc_mtime_ns": lrc_stat.st_mtime_ns})
        else:
            result.update({"lrc_size": 0, "lrc_mtime_ns": 0})
        return result

    finally:
        for temp in (temp_flac, temp_lrc):
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass


def repair_lrc_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    sanitized, stats = sanitize_lrc_text(raw)
    current_normalized = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    target_normalized = sanitized.replace("\r\n", "\n").replace("\r", "\n")
    changed = current_normalized != target_normalized
    if changed:
        temp = path.with_name(path.name + ".tmp")
        try:
            temp.write_text(sanitized, encoding="utf-8", newline="")
            os.replace(temp, path)
        finally:
            if temp.exists():
                try: temp.unlink()
                except OSError: pass
    return {"changed": changed, "stats": stats}


def repair_flac_metadata_atomic(path: Path, metadata_overrides: dict, verify_audio: bool = True) -> dict:
    source_hash = audio_payload_sha256(path) if verify_audio else None
    fd, temp_name = tempfile.mkstemp(prefix=".samsung_repair_", suffix=".flac", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(path, temp)
        audio = FLAC(temp)
        changed = _apply_samsung_metadata_overrides(audio, metadata_overrides)
        if not changed:
            return {"changed": False, "audio_verified": bool(verify_audio)}
        audio.save()
        if verify_audio and audio_payload_sha256(temp) != source_hash:
            raise RuntimeError("Audio-SHA-256 hat sich bei der Samsung-Metadatenreparatur geändert.")
        os.replace(temp, path)
        # Bewusst neues MTIME: Android/MediaStore soll die geänderten Tags neu einlesen.
        now = time.time_ns()
        try: os.utime(path, ns=(now, now))
        except OSError: pass
        return {"changed": True, "audio_verified": bool(verify_audio)}
    finally:
        if temp.exists():
            try: temp.unlink()
            except OSError: pass


def discover_multidisc_repair_actions(export_root: Path) -> tuple[list[dict], list[dict]]:
    """
    Ermittelt sichere Samsung-Trackreparaturen aus Export-Tags und – falls nötig –
    aus einer eindeutigen, lückenlosen Dateinummerierung 01..N.
    """
    groups = defaultdict(list)
    unreadable = []
    for path in find_flacs(export_root):
        try:
            audio = FLAC(path)
            tags = audio.tags or {}
            track_number = parse_track_number(get_tag(tags, "TRACKNUMBER") or get_tag(tags, "TRACK"))
            disc_number = parse_track_number(get_tag(tags, "DISCNUMBER") or get_tag(tags, "DISC"))
            groups[path.parent.resolve()].append({
                "path": path,
                "track_number": track_number,
                "disc_number": disc_number,
                "album": get_tag(tags, "ALBUM"),
            })
        except Exception as exc:
            unreadable.append({"path": path, "error": str(exc)})

    actions = []
    for parent, group in groups.items():
        # Zusätzliche Plausibilitätsbremse: Falls in EINEM Ordner mehrere
        # unterschiedliche nichtleere ALBUM-Tags liegen, wird nicht geflattet.
        album_keys = {normalize(item.get("album") or "") for item in group if (item.get("album") or "").strip()}
        if len(album_keys) > 1:
            continue

        discs = {item["disc_number"] for item in group if item["disc_number"] is not None}
        ordered = None
        reason = None

        if len(discs) >= 2 and not any(
            item["disc_number"] is None or item["track_number"] is None for item in group
        ):
            ordered = sorted(
                group,
                key=lambda item: (item["disc_number"], item["track_number"], str(item["path"]).casefold()),
            )
            reason = "disc_tags"
        else:
            use_filename_order, by_prefix = _filename_order_is_authoritative(group, "path")
            if use_filename_order:
                ordered = [by_prefix[i] for i in range(1, len(group) + 1)]
                reason = "filename_prefix"

        if not ordered:
            continue

        total = len(ordered)
        for new_track, item in enumerate(ordered, start=1):
            actions.append({
                "path": item["path"],
                "album_dir": parent,
                "album": item["album"],
                "metadata_overrides": {
                    "flatten_multidisc": True,
                    "track_number": new_track,
                    "track_total": total,
                    "original_disc_number": item["disc_number"],
                    "original_track_number": item["track_number"],
                    "flatten_reason": reason,
                },
            })
    return actions, unreadable


# ============================================================
# SETTINGS / CACHE
# ============================================================

def default_settings():
    return {
        "amazon_path": "",
        "qobuz_path": "",
        "samsung_export_path": "",
        "ffmpeg_path": "",
        "only_synced": True,
        "overwrite": False,
        "verify_audio": True,
        "backup": False,
        "include_unreviewed_amazon_only": True,
        "max_duration_diff": DEFAULT_MAX_DURATION_DIFF,
        "tutorial_seen": False,
        "geometry": "1500x900",
    }


def load_json_file(path: Path, default):
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json_file(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_settings():
    settings = default_settings()
    settings.update(load_json_file(SETTINGS_FILE, {}))
    return settings


def file_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def analysis_cache_key(path: Path) -> str:
    return str(path.resolve()).casefold()


def cache_entry_valid(entry: dict, path: Path) -> bool:
    if not entry or not path.is_file():
        return False
    # Analyse-Algorithmen dürfen sich zwischen Releases ändern. Alte Ergebnisse
    # werden deshalb automatisch ignoriert, selbst wenn die FLAC unverändert ist.
    if entry.get("analysis_schema") != ANALYSIS_SCHEMA_VERSION:
        return False
    fp = file_fingerprint(path)
    return entry.get("size") == fp["size"] and entry.get("mtime_ns") == fp["mtime_ns"]


# ============================================================
# FFMPEG / DEEP ANALYSIS
# ============================================================

def find_ffmpeg(saved_path: str = "") -> Optional[Path]:
    candidates = []
    if saved_path:
        candidates.append(Path(saved_path))
    candidates += [APP_DIR / "ffmpeg.exe", APP_DIR / "ffmpeg"]
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            continue
    return None


def run_ffmpeg_capture(ffmpeg: Path, args: list[str], text=False, timeout=None):
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run([str(ffmpeg), *args], **kwargs)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise RuntimeError(err[-1800:] or "FFmpeg-Fehler")
    if text:
        return proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    return proc.stdout, proc.stderr


def decode_native_float32(ffmpeg: Path, path: Path) -> tuple["np.ndarray", int, int]:
    """
    Dekodiert die FLAC kanalgetreu als float32.

    WICHTIG: Für Pegelmessungen wird *nicht* vorher auf Mono heruntergemischt.
    Ein Downmix kann Kanäle addieren und künstliche Werte > 0 dBFS erzeugen.
    Rückgabeform: (frames, channels), Sample-Rate, Kanalzahl.
    """
    if np is None:
        raise RuntimeError("NumPy fehlt.")

    audio = FLAC(path)
    sample_rate = int(audio.info.sample_rate)
    channels = max(1, int(audio.info.channels))

    raw, _ = run_ffmpeg_capture(
        ffmpeg,
        [
            "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vn", "-map_metadata", "-1", "-ar", str(sample_rate),
            "-f", "f32le", "-acodec", "pcm_f32le", "-",
        ],
    )

    interleaved = np.frombuffer(raw, dtype="<f4")
    if interleaved.size == 0:
        raise RuntimeError("FFmpeg lieferte keine Audiodaten.")

    usable = (interleaved.size // channels) * channels
    if usable == 0:
        raise RuntimeError("FFmpeg-Audiodaten konnten nicht in Kanäle aufgeteilt werden.")

    # float64 für robuste Statistik/FFT, ohne die Quelldatei anzufassen.
    frames = interleaved[:usable].reshape(-1, channels).astype(np.float64, copy=False)
    return frames, sample_rate, channels


def parse_ebur128(ffmpeg: Path, path: Path) -> dict:
    args = [
        "-hide_banner", "-nostats", "-i", str(path),
        "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
    ]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run([str(ffmpeg), *args], **kwargs)
    text = proc.stderr.decode("utf-8", "replace")
    summary_pos = text.rfind("Summary:")
    summary = text[summary_pos:] if summary_pos >= 0 else text

    def match(pattern):
        found = re.search(pattern, summary, re.IGNORECASE | re.MULTILINE)
        return safe_float(found.group(1)) if found else None

    return {
        "integrated_lufs": match(r"\bI:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS"),
        "lra_lu": match(r"\bLRA:\s*([-+]?\d+(?:\.\d+)?)\s*LU"),
        "true_peak_dbfs": match(r"\bPeak:\s*([-+]?\d+(?:\.\d+)?)\s*dBFS"),
    }


def build_waveform(samples, points=1200):
    if np is None:
        return [], []
    points = max(100, min(points, len(samples)))
    edges = np.linspace(0, len(samples), points + 1, dtype=np.int64)
    mins, maxs = np.empty(points), np.empty(points)
    for i in range(points):
        block = samples[edges[i]:edges[i + 1]]
        if block.size:
            mins[i], maxs[i] = float(block.min()), float(block.max())
        else:
            mins[i] = maxs[i] = 0.0
    return mins.tolist(), maxs.tolist()


def build_spectrum(samples, sr, nfft=16384, max_frames=900):
    if np is None:
        return [], []
    if len(samples) < nfft:
        nfft = 2 ** int(math.floor(math.log2(max(256, len(samples)))))
    hop = max(1, nfft // 2)
    total_frames = max(1, 1 + (len(samples) - nfft) // hop) if len(samples) >= nfft else 1
    frame_indices = np.unique(np.linspace(0, max(total_frames - 1, 0), min(total_frames, max_frames), dtype=np.int64))
    window = np.hanning(nfft)
    power_sum = np.zeros(nfft // 2 + 1, dtype=np.float64)
    used = 0
    for idx in frame_indices:
        start = int(idx * hop)
        frame = samples[start:start + nfft]
        if len(frame) < nfft:
            frame = np.pad(frame, (0, nfft - len(frame)))
        spec = np.fft.rfft(frame * window)
        power_sum += np.abs(spec) ** 2
        used += 1
    avg_power = power_sum / max(used, 1)
    db = 10.0 * np.log10(avg_power + 1e-20)
    db -= float(db.max()) if db.size else 0.0
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)

    # compact to <= 700 points for cache / GUI
    keep = min(700, len(freqs))
    indices = np.linspace(0, len(freqs) - 1, keep, dtype=np.int64)
    return freqs[indices].tolist(), db[indices].tolist()


def detect_lossy_cutoff(freqs, spectrum_db, sr):
    if np is None or not freqs or not spectrum_db:
        return {
            "plausibility": "UNKLAR",
            "confidence": "niedrig",
            "highest_active_hz": None,
            "cutoff_hz": None,
            "cutoff_drop_db": None,
            "note": "Zu wenig Spektraldaten.",
        }

    f = np.asarray(freqs, dtype=float)
    s = np.asarray(spectrum_db, dtype=float)
    nyquist = sr / 2.0
    if len(s) >= 7:
        kernel = np.ones(7) / 7.0
        s_sm = np.convolve(s, kernel, mode="same")
    else:
        s_sm = s

    active = f[s_sm > -60.0]
    highest_active = float(active.max()) if active.size else None

    max_candidate = min(nyquist - 800.0, 22000.0)
    candidate_freqs = np.arange(14000.0, max_candidate, 250.0)
    best = None
    for c in candidate_freqs:
        below = s_sm[(f >= c - 1000) & (f < c - 250)]
        above = s_sm[(f > c + 250) & (f <= c + 1800)]
        if below.size < 2 or above.size < 2:
            continue
        below_mean = float(np.mean(below))
        above_mean = float(np.mean(above))
        drop = below_mean - above_mean
        if drop > 14 and above_mean < -48:
            score = drop + max(0, -above_mean - 48) * 0.25
            if best is None or score > best[0]:
                best = (score, c, drop, above_mean)

    cutoff = float(best[1]) if best else None
    drop = float(best[2]) if best else None

    if cutoff is not None and cutoff <= 16500 and drop >= 18:
        plausibility, confidence = "VERDACHT AUF LOSSY-QUELLE", "hoch"
        note = "Deutlicher, dauerhaft energiearmer Hochfrequenzbereich nach einem frühen Cutoff."
    elif cutoff is not None and cutoff <= 19500 and drop >= 16:
        plausibility, confidence = "VERDACHT AUF LOSSY-QUELLE", "mittel"
        note = "Auffälliger Hochfrequenz-Cutoff. Das kann auch vom Master stammen."
    elif highest_active is not None and highest_active < min(15000.0, nyquist * 0.75):
        plausibility, confidence = "UNKLAR", "mittel"
        note = "Sehr wenig Hochfrequenzenergie; Ursache kann Quelle oder Master sein."
    else:
        plausibility, confidence = "UNAUFFÄLLIG", "mittel"
        note = "Kein klarer Lossy-Cutoff erkannt. Das beweist nicht, dass die Quelle lossless ist."

    return {
        "plausibility": plausibility,
        "confidence": confidence,
        "highest_active_hz": highest_active,
        "cutoff_hz": cutoff,
        "cutoff_drop_db": drop,
        "note": note,
    }


def save_spectrogram_png(samples, sr, output_path: Path):
    if Figure is None or np is None:
        return None
    # Downsample time dimension for fast plotting, but preserve original sample rate for frequency axis.
    nfft = 4096 if sr <= 48000 else 8192
    hop = nfft // 2
    total_frames = max(1, 1 + (len(samples) - nfft) // hop) if len(samples) >= nfft else 1
    frame_indices = np.unique(np.linspace(0, max(total_frames - 1, 0), min(total_frames, 550), dtype=np.int64))
    window = np.hanning(nfft)
    columns = []
    for idx in frame_indices:
        start = int(idx * hop)
        frame = samples[start:start + nfft]
        if len(frame) < nfft:
            frame = np.pad(frame, (0, nfft - len(frame)))
        mag = np.abs(np.fft.rfft(frame * window))
        db = 20 * np.log10(mag + 1e-9)
        columns.append(db)
    spec = np.stack(columns, axis=1)
    spec -= float(np.max(spec))
    spec = np.clip(spec, -90, 0)

    fig = Figure(figsize=(9.0, 3.5), dpi=120, facecolor=COLORS["panel"])
    ax = fig.add_subplot(111)
    ax.set_facecolor(COLORS["field"])
    extent = [0, len(samples) / sr, 0, sr / 2 / 1000]
    ax.imshow(spec, origin="lower", aspect="auto", extent=extent, cmap="magma", vmin=-90, vmax=0)
    ax.set_xlabel("Zeit (s)", color=COLORS["fg"])
    ax.set_ylabel("Frequenz (kHz)", color=COLORS["fg"])
    ax.tick_params(colors=COLORS["muted"])
    for spine in ax.spines.values():
        spine.set_color(COLORS["border"])
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    return output_path


def deep_analyze_track(ffmpeg: Path, path: Path, spectrogram_path: Path) -> dict:
    if np is None or Figure is None:
        raise RuntimeError("NumPy/Matplotlib fehlen.")

    # Pegelstatistik erfolgt kanalgetreu. Erst die rein visuellen/spektralen
    # Analysen verwenden einen sicheren arithmetischen Mono-Mittelwert.
    frames, sr, channels = decode_native_float32(ffmpeg, path)
    abs_frames = np.abs(frames)

    peak = float(abs_frames.max())
    # Integer-FLAC wird von FFmpeg auf [-1, 1] normalisiert. Kleine numerische
    # Rundungsreste oberhalb 1.0 werden nicht als echter >0-dBFS-Samplepeak
    # ausgewiesen. True Peak (Intersample) darf dagegen > 0 dBTP sein.
    peak_for_db = min(peak, 1.0)
    rms = float(np.sqrt(np.mean(frames * frames)))
    peak_db = dbfs(peak_for_db)
    rms_db = dbfs(rms)
    crest = peak_db - rms_db

    clipping_threshold = 0.999969
    near_zero_db_threshold = 10 ** (-0.1 / 20.0)
    clipping_samples = int(np.count_nonzero(abs_frames >= clipping_threshold))
    near_clip_samples = int(np.count_nonzero(abs_frames >= near_zero_db_threshold))
    total_sample_values = int(frames.size)

    channel_dc = np.mean(frames, axis=0)
    worst_dc_index = int(np.argmax(np.abs(channel_dc)))
    dc_offset = float(channel_dc[worst_dc_index])

    # Sicherer Visualisierungs-Mix: Mittelwert kann bei normalisierten
    # Eingangskanälen nicht durch bloße Kanaladdition über Full Scale steigen.
    mono_samples = np.mean(frames, axis=1) if channels > 1 else frames[:, 0]

    wave_min, wave_max = build_waveform(mono_samples)
    freqs, spectrum_db = build_spectrum(mono_samples, sr)
    lossy = detect_lossy_cutoff(freqs, spectrum_db, sr)
    loudness = parse_ebur128(ffmpeg, path)
    save_spectrogram_png(mono_samples, sr, spectrogram_path)

    return {
        "analysis_schema": ANALYSIS_SCHEMA_VERSION,
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_rate": sr,
        "channels": channels,
        "sample_peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
        "crest_factor_db": crest,
        "clipping_samples": clipping_samples,
        "near_clip_samples": near_clip_samples,
        "near_clip_percent": (near_clip_samples / total_sample_values * 100.0) if total_sample_values else 0.0,
        "dc_offset": dc_offset,
        "dc_offset_channels": [float(v) for v in channel_dc],
        "integrated_lufs": loudness.get("integrated_lufs"),
        "lra_lu": loudness.get("lra_lu"),
        "true_peak_dbfs": loudness.get("true_peak_dbfs"),
        "wave_min": wave_min,
        "wave_max": wave_max,
        "spectrum_freqs": freqs,
        "spectrum_db": spectrum_db,
        "spectrogram_path": str(spectrogram_path),
        **lossy,
    }


# ============================================================
# REPORT
# ============================================================

def write_report(matches, result_messages):
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    report_path = LOG_DIR / f"lyrics_merge_{timestamp}.csv"
    with report_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            "Status", "Quelle", "Titel", "Artist", "Album", "Track", "ISRC",
            "Codec", "Bit", "Samplerate_Hz", "FLAC_kbit_s", "PCM_kbit_s",
            "Kanäle", "Laufzeit_s", "Dateigröße_Bytes", "Lyrics",
            "Match", "Laufzeitdifferenz_s", "Qobuz", "Amazon", "Hinweis", "Ergebnis",
        ])
        for match in matches:
            t = display_track(match)
            q = qobuz_track(match)
            a = amazon_track(match)
            writer.writerow([
                match.status, source_label(match), t.title, t.artist, t.album,
                t.track_number, t.isrc, t.codec, t.bits_per_sample, t.sample_rate,
                t.audio_bitrate_kbps, t.pcm_bitrate_kbps, t.channels,
                f"{t.duration:.3f}", t.file_size,
                "SYNC" if t.lyrics_synced else ("JA" if t.lyrics else "NEIN"),
                match.method,
                f"{match.duration_diff:.3f}" if match.duration_diff is not None else "",
                str(q.path) if q else "", str(a.path) if a else "",
                match.message,
                result_messages.get(str(q.path), "") if q else "",
            ])
    return report_path


# ============================================================
# GUI
# ============================================================

class LyricsMergeGUI:
    def __init__(self):
        missing = []
        if Image is None:
            missing.append("pillow")
        if np is None:
            missing.append("numpy")
        if Figure is None:
            missing.append("matplotlib")
        if missing:
            root = tk.Tk(); root.withdraw()
            messagebox.showerror(APP_NAME, "Fehlende Pakete:\n\n" + " ".join(missing) + "\n\nBitte die Installationszeile aus der Anleitung ausführen.")
            root.destroy(); raise SystemExit

        self.settings = load_settings()
        self.analysis_cache = load_json_file(ANALYSIS_CACHE_FILE, {})
        self.samsung_export_cache = load_json_file(SAMSUNG_EXPORT_CACHE_FILE, {})
        self.review_decisions = load_json_file(
            REVIEW_DECISIONS_FILE,
            {"schema": REVIEW_DECISIONS_SCHEMA_VERSION, "entries": {}},
        )
        if not isinstance(self.review_decisions, dict) or not isinstance(self.review_decisions.get("entries"), dict):
            self.review_decisions = {"schema": REVIEW_DECISIONS_SCHEMA_VERSION, "entries": {}}
        self.cache_lock = threading.Lock()
        self.samsung_cache_lock = threading.Lock()

        self.root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry(self.settings.get("geometry", "1500x900"))
        self.root.minsize(1120, 700)
        self.style = ttk.Style()
        self.configure_dark_theme()

        self.source_var = tk.StringVar(value=self.settings.get("amazon_path", ""))
        self.target_var = tk.StringVar(value=self.settings.get("qobuz_path", ""))
        self.samsung_export_var = tk.StringVar(value=self.settings.get("samsung_export_path", ""))
        self.overwrite_var = tk.BooleanVar(value=bool(self.settings.get("overwrite", False)))
        self.synced_only_var = tk.BooleanVar(value=bool(self.settings.get("only_synced", True)))
        self.verify_var = tk.BooleanVar(value=bool(self.settings.get("verify_audio", True)))
        self.backup_var = tk.BooleanVar(value=bool(self.settings.get("backup", False)))
        self.include_unreviewed_amazon_only_var = tk.BooleanVar(
            value=bool(self.settings.get("include_unreviewed_amazon_only", True))
        )
        self.duration_var = tk.StringVar(value=str(self.settings.get("max_duration_diff", DEFAULT_MAX_DURATION_DIFF)))
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="ALLE")
        self.source_filter_var = tk.StringVar(value="ALLE")
        self.album_filter_var = tk.StringVar(value="ALLE")

        self.ffmpeg_path = find_ffmpeg(self.settings.get("ffmpeg_path", ""))
        if self.ffmpeg_path:
            self.settings["ffmpeg_path"] = str(self.ffmpeg_path)

        self.cancel_event = threading.Event()
        self.matches: list[Match] = []
        self.source_tracks: list[Track] = []
        self.target_tracks: list[Track] = []
        self.last_preview_max_diff = DEFAULT_MAX_DURATION_DIFF
        self.last_preview_source_count = 0
        self.last_preview_target_count = 0
        self.last_preview_error_count = 0
        self.report_path = None
        self.tree_item_matches = {}
        self.thumbnail_refs = {}
        self.detail_cover_ref = None
        self.tooltip_window = None
        self.tooltip_after_id = None
        self._hover_item = None
        self.current_sort_column = None
        self.current_sort_reverse = False
        self.selected_match: Optional[Match] = None
        self.current_deep_job = False
        self.deep_status_var = tk.StringVar(value="Noch nicht analysiert")
        self.spectrogram_ref = None

        self.create_gui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, lambda: enable_windows_dark_titlebar(self.root))
        if not self.settings.get("tutorial_seen", False):
            self.root.after(450, self.show_tutorial)

    # ---------------- THEME ----------------
    def configure_dark_theme(self):
        c = COLORS
        self.root.configure(bg=c["bg"])
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure(".", background=c["bg"], foreground=c["fg"], font=("Segoe UI", 10))
        self.style.configure("TFrame", background=c["bg"])
        self.style.configure("Panel.TFrame", background=c["panel"])
        self.style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        self.style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        self.style.configure("Panel.TLabel", background=c["panel"], foreground=c["fg"])
        self.style.configure("Title.TLabel", background=c["panel"], foreground=c["fg"], font=("Segoe UI", 16, "bold"))
        self.style.configure("TButton", background=c["panel2"], foreground=c["fg"], borderwidth=1, padding=(10, 6))
        self.style.map("TButton", background=[("active", c["border"]), ("pressed", c["selection"])], foreground=[("disabled", c["muted"])])
        self.style.configure("Accent.TButton", background=c["accent"], foreground="#ffffff")
        self.style.map("Accent.TButton", background=[("active", c["accent_hover"]), ("disabled", c["panel2"])])
        self.style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"], bordercolor=c["border"])
        self.style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        self.style.map("TCheckbutton", background=[("active", c["bg"])], foreground=[("active", c["fg"])])
        self.style.configure("TLabelframe", background=c["bg"], foreground=c["fg"], bordercolor=c["border"])
        self.style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        self.style.configure(
            "TCombobox",
            fieldbackground=c["field"],
            background=c["panel2"],
            foreground=c["fg"],
            arrowcolor=c["fg"],
            selectbackground=c["field"],
            selectforeground=c["fg"],
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["field"]), ("disabled", c["panel2"])],
            background=[("readonly", c["panel2"]), ("active", c["border"])],
            foreground=[("readonly", c["fg"]), ("disabled", c["muted"])],
            selectbackground=[("readonly", c["field"])],
            selectforeground=[("readonly", c["fg"])],
            arrowcolor=[("readonly", c["fg"])],
        )
        self.style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"], rowheight=52, bordercolor=c["border"])
        self.style.map("Treeview", background=[("selected", c["selection"])], foreground=[("selected", "#ffffff")])
        self.style.configure("Treeview.Heading", background=c["panel2"], foreground=c["fg"], relief="flat", padding=(5, 7))
        self.style.map("Treeview.Heading", background=[("active", c["border"])])
        self.style.configure("TNotebook", background=c["panel"], borderwidth=0)
        self.style.configure("TNotebook.Tab", background=c["panel2"], foreground=c["fg"], padding=(10, 6))
        self.style.map("TNotebook.Tab", background=[("selected", c["selection"]), ("active", c["border"])])

        # Scrollbars und Progressbar ins Dark Theme integrieren.
        self.style.configure(
            "Vertical.TScrollbar",
            background=c["panel2"], troughcolor=c["field"], bordercolor=c["border"],
            arrowcolor=c["fg"], lightcolor=c["panel2"], darkcolor=c["panel2"],
        )
        self.style.map("Vertical.TScrollbar", background=[("active", c["border"])])
        self.style.configure(
            "Horizontal.TScrollbar",
            background=c["panel2"], troughcolor=c["field"], bordercolor=c["border"],
            arrowcolor=c["fg"], lightcolor=c["panel2"], darkcolor=c["panel2"],
        )
        self.style.map("Horizontal.TScrollbar", background=[("active", c["border"])])
        self.style.configure(
            "JStudios.Horizontal.TProgressbar",
            troughcolor=c["field"], background=c["progress"], bordercolor=c["border"],
            lightcolor=c["progress"], darkcolor=c["progress"], thickness=8,
        )
        try:
            self.root.option_add("*TCombobox*Listbox.background", c["field"])
            self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", c["selection"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except Exception:
            pass

    # ---------------- GUI BUILD ----------------
    def create_gui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        header = ttk.Frame(main); header.grid(row=0, column=0, sticky="ew", pady=(0, 8)); header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        self.ffmpeg_status_label = ttk.Label(header, text="", style="Muted.TLabel")
        self.ffmpeg_status_label.grid(row=0, column=1, padx=10)
        ttk.Label(header, text="Drag & Drop aktiv" if HAS_DND else "Drag & Drop optional", style="Muted.TLabel").grid(row=0, column=2, padx=10)
        ttk.Button(header, text="?  Anleitung", command=self.show_tutorial).grid(row=0, column=3, sticky="e")
        self.update_ffmpeg_status()

        guide = ttk.Frame(main, style="Panel.TFrame", padding=10); guide.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(guide, style="Panel.TLabel", text="Amazon +/oder Qobuz analysieren  →  sichere Matches + Review-Fälle trennen  →  Varianten persönlich freigeben/ablehnen  →  KI-Review exportieren  →  Samsung Export = Qobuz bevorzugt + Samsung-LRC-Sanitizer + Multi-Disc-Fix     ① Analysieren  ② Lyrics übernehmen  ③ Samsung Music Export / Reparatur").pack(anchor="w")

        paths = ttk.Frame(main); paths.grid(row=2, column=0, sticky="ew"); paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="Amazon Music – Lyrics-Quelle / Fallback:").grid(row=0, column=0, sticky="w", pady=4)
        self.source_entry = ttk.Entry(paths, textvariable=self.source_var); self.source_entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="Ordner wählen", command=self.choose_source).grid(row=0, column=2)
        ttk.Label(paths, text="Qobuz – Master-Bibliothek (optional für Samsung):").grid(row=1, column=0, sticky="w", pady=4)
        self.target_entry = ttk.Entry(paths, textvariable=self.target_var); self.target_entry.grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="Ordner wählen", command=self.choose_target).grid(row=1, column=2)
        ttk.Label(paths, text="Samsung Music – Exportziel:").grid(row=2, column=0, sticky="w", pady=4)
        self.samsung_export_entry = ttk.Entry(paths, textvariable=self.samsung_export_var); self.samsung_export_entry.grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="Ordner wählen", command=self.choose_samsung_export).grid(row=2, column=2)
        self.setup_drop_target(self.source_entry, self.source_var)
        self.setup_drop_target(self.target_entry, self.target_var)
        self.setup_drop_target(self.samsung_export_entry, self.samsung_export_var)

        options = ttk.LabelFrame(main, text="Optionen", padding=8)
        options.grid(row=3, column=0, sticky="ew", pady=(10, 8))
        options.columnconfigure(0, weight=1)

        option_checks = ttk.Frame(options)
        option_checks.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(option_checks, text="Nur synchronisierte Lyrics", variable=self.synced_only_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(option_checks, text="Vorhandene Lyrics überschreiben", variable=self.overwrite_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(option_checks, text="Audioframes per SHA-256 verifizieren", variable=self.verify_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(option_checks, text="Vollbackup geänderter FLACs", variable=self.backup_var).pack(side="left")

        # Eigene zweite Zeile: bleibt auch bei kleineren Fensterbreiten sichtbar.
        duration_frame = ttk.Frame(options)
        duration_frame.grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Label(duration_frame, text="Max. Laufzeitabweichung:").pack(side="left", padx=(0, 5))
        ttk.Entry(duration_frame, textvariable=self.duration_var, width=7).pack(side="left")
        ttk.Label(duration_frame, text="Sek.").pack(side="left", padx=5)
        ttk.Checkbutton(
            duration_frame,
            text="Ungeprüfte Amazon-only beim Samsung-Export einschließen",
            variable=self.include_unreviewed_amazon_only_var,
        ).pack(side="left", padx=(18, 0))

        actions = ttk.Frame(main); actions.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        self.preview_button = ttk.Button(actions, text="1. Analysieren / Vorschau", command=self.start_preview); self.preview_button.pack(side="left", padx=(0, 6))
        self.apply_button = ttk.Button(actions, text="2. Lyrics übernehmen", style="Accent.TButton", command=self.start_apply, state="disabled"); self.apply_button.pack(side="left", padx=6)
        self.samsung_export_button = ttk.Button(actions, text="3. Samsung Music Export", command=self.start_samsung_export); self.samsung_export_button.pack(side="left", padx=6)
        self.samsung_repair_button = ttk.Button(actions, text="Samsung Export reparieren", command=self.start_samsung_repair); self.samsung_repair_button.pack(side="left", padx=6)
        self.review_export_button = ttk.Button(actions, text="KI-Review exportieren", command=self.export_ai_review_package); self.review_export_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(actions, text="Abbrechen", command=self.cancel, state="disabled"); self.cancel_button.pack(side="left", padx=6)

        content = ttk.Frame(main); content.grid(row=5, column=0, sticky="nsew"); content.columnconfigure(0, weight=4); content.columnconfigure(1, weight=2); content.rowconfigure(0, weight=1)
        self.create_table(content)
        self.create_details(content)

        self.progress = ttk.Progressbar(main, mode="determinate", style="JStudios.Horizontal.TProgressbar")
        self.progress.grid(row=6, column=0, sticky="ew", pady=(8, 3))
        self.status_label = ttk.Label(main, text="Bereit.", style="Muted.TLabel")
        self.status_label.grid(row=7, column=0, sticky="w")

        # Absolut übertrieben offizielles Branding – weil man's kann. :)
        footer = ttk.Frame(main)
        footer.grid(row=8, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        branding = ttk.Frame(footer)
        branding.grid(row=0, column=0, sticky="w")
        ttk.Label(branding, text="Made with ", style="Muted.TLabel").pack(side="left")
        tk.Label(branding, text="♥", fg="#ff4d6d", bg=COLORS["bg"], font=("Segoe UI Symbol", 11, "bold"), bd=0, padx=0, pady=0).pack(side="left")
        ttk.Label(branding, text=" by Julian Schmitz from JStudios", style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, text="© 2026 JStudios", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

    # ---------------- TABLE ----------------
    def create_table(self, parent):
        frame = ttk.Frame(parent); frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8)); frame.columnconfigure(0, weight=1); frame.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(frame); toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4)); toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Suche:").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(toolbar, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(toolbar, text="Status:").grid(row=0, column=2, padx=(12, 6))
        self.filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.filter_var,
            values=(
                "ALLE", "BEREIT", "MANUELL FREIGEGEBEN", "ÜBERSCHREIBEN", "VORHANDEN",
                "AMAZON-ONLY", "AMAZON-ONLY OK", "AMAZON-ONLY IGNORIERT",
                "KEINE LYRICS", "KEINE SYNC-LYRICS", "VARIANTENPRÜFUNG",
                "ZEITPRÜFUNG", "VARIANTE ABGELEHNT", "KEIN AMAZON-TITEL",
                "OFFENE ENTSCHEIDUNGEN", "NUR PROBLEME",
            ),
            state="readonly", width=25,
        )
        self.filter_box.grid(row=0, column=3)
        ttk.Button(toolbar, text="CSV exportieren", command=self.export_preview_csv).grid(row=0, column=4, padx=(12, 0))
        self.visible_count_label = ttk.Label(toolbar, text="0 Titel", style="Muted.TLabel"); self.visible_count_label.grid(row=0, column=5, padx=(12, 0))

        filterbar = ttk.Frame(frame); filterbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 7)); filterbar.columnconfigure(3, weight=1)
        ttk.Label(filterbar, text="Quelle:").grid(row=0, column=0, padx=(0, 6))
        self.source_filter_box = ttk.Combobox(
            filterbar, textvariable=self.source_filter_var,
            values=("ALLE", "QOBUZ", "AMAZON", "AMAZON-ONLY"),
            state="readonly", width=14,
        )
        self.source_filter_box.grid(row=0, column=1, sticky="w")
        ttk.Label(filterbar, text="Album:").grid(row=0, column=2, padx=(14, 6))
        self.album_filter_box = ttk.Combobox(
            filterbar, textvariable=self.album_filter_var,
            values=("ALLE",), state="readonly", width=42,
        )
        self.album_filter_box.grid(row=0, column=3, sticky="ew")
        ttk.Button(filterbar, text="Filter zurücksetzen", command=self.reset_filters).grid(row=0, column=4, padx=(12, 0))

        self.search_var.trace_add("write", lambda *_: self.apply_filters())
        self.filter_box.bind("<<ComboboxSelected>>", lambda _: self.apply_filters())
        self.source_filter_box.bind("<<ComboboxSelected>>", lambda _: self.apply_filters())
        self.album_filter_box.bind("<<ComboboxSelected>>", lambda _: self.apply_filters())

        columns = ("sourcekind", "album", "title", "quality", "match", "delta")
        self.tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        # #0 enthält Status + Thumbnail; Quelle und Album sind jetzt eigene sortierbare Spalten.
        self.tree.heading("#0", text="Status", command=lambda: self.sort_by("status"))
        self.tree.column("#0", width=232, minwidth=220, stretch=False)
        self.tree.heading("sourcekind", text="Quelle", command=lambda: self.sort_by("source"))
        self.tree.heading("album", text="Album", command=lambda: self.sort_by("album"))
        self.tree.heading("title", text="Titel", command=lambda: self.sort_by("title"))
        self.tree.heading("quality", text="Audioqualität", command=lambda: self.sort_by("quality"))
        self.tree.heading("match", text="Match", command=lambda: self.sort_by("match"))
        self.tree.heading("delta", text="Δ Zeit", command=lambda: self.sort_by("delta"))
        self.tree.column("sourcekind", width=120, minwidth=100, stretch=False)
        self.tree.column("album", width=190, minwidth=130)
        self.tree.column("title", width=235, minwidth=150)
        self.tree.column("quality", width=185, minwidth=155, stretch=False)
        self.tree.column("match", width=92, minwidth=72, stretch=False)
        self.tree.column("delta", width=68, minwidth=55, stretch=False)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hscrollbar = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=hscrollbar.set)
        self.tree.grid(row=2, column=0, sticky="nsew")
        scrollbar.grid(row=2, column=1, sticky="ns")
        hscrollbar.grid(row=3, column=0, sticky="ew")

        for tag, bg, fg in (
            ("BEREIT", COLORS["ready_bg"], COLORS["ready_fg"]),
            ("VORHANDEN", COLORS["existing_bg"], COLORS["existing_fg"]),
            ("ZEITPRÜFUNG", COLORS["warning_bg"], COLORS["warning_fg"]),
            ("VARIANTENPRÜFUNG", COLORS["warning_bg"], COLORS["warning_fg"]),
            ("MANUELL FREIGEGEBEN", COLORS["ready_bg"], COLORS["ready_fg"]),
            ("VARIANTE ABGELEHNT", COLORS["error_bg"], COLORS["error_fg"]),
            ("KEINE LYRICS", COLORS["neutral_bg"], COLORS["neutral_fg"]),
            ("KEINE SYNC-LYRICS", COLORS["existing_bg"], COLORS["existing_fg"]),
            ("KEIN AMAZON-TITEL", COLORS["error_bg"], COLORS["error_fg"]),
            ("AMAZON-ONLY", COLORS["amazon_bg"], COLORS["amazon_fg"]),
            ("AMAZON-ONLY OK", COLORS["ready_bg"], COLORS["ready_fg"]),
            ("AMAZON-ONLY IGNORIERT", COLORS["neutral_bg"], COLORS["neutral_fg"]),
            ("ÜBERSCHREIBEN", COLORS["overwrite_bg"], COLORS["overwrite_fg"]),
        ):
            self.tree.tag_configure(tag, background=bg, foreground=fg)

        self.tree.bind("<<TreeviewSelect>>", self.on_track_select)
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", lambda _: self.hide_tooltip())
        self.tree.bind("<Button-3>", self.show_context_menu)

    def reset_filters(self):
        self.search_var.set("")
        self.filter_var.set("ALLE")
        self.source_filter_var.set("ALLE")
        self.album_filter_var.set("ALLE")
        self.apply_filters()

    def refresh_album_filter_values(self):
        current = self.album_filter_var.get()
        albums = sorted({
            (display_track(m).album or "(Ohne Album)")
            for m in self.matches
        }, key=lambda value: normalize(value))
        values = ("ALLE", *albums)
        self.album_filter_box.configure(values=values)
        if current not in values:
            self.album_filter_var.set("ALLE")

    # ---------------- REVIEW / PERSONAL DECISIONS ----------------
    def save_review_decisions(self):
        self.review_decisions["schema"] = REVIEW_DECISIONS_SCHEMA_VERSION
        self.review_decisions.setdefault("entries", {})
        save_json_file(REVIEW_DECISIONS_FILE, self.review_decisions)

    def decision_text_for_match(self, match: Optional[Match]) -> str:
        if not match:
            return "-"
        q, a = qobuz_track(match), amazon_track(match)
        pair = get_pair_decision(self.review_decisions, q, a)
        if pair == "APPROVE":
            return "Variante manuell freigegeben"
        if pair == "REJECT":
            return "Variante manuell abgelehnt"
        amazon_dec = get_amazon_decision(self.review_decisions, a) if match.target_kind == "Amazon" else ""
        if amazon_dec == "INCLUDE":
            return "Amazon-only für Samsung bestätigt"
        if amazon_dec == "IGNORE":
            return "Amazon-only für Samsung ignoriert"
        if match.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG", "AMAZON-ONLY"):
            return "Offen"
        return "Keine persönliche Entscheidung nötig"

    def update_review_buttons(self, match: Optional[Match]):
        pair_state = "disabled"
        amazon_state = "disabled"
        reset_state = "disabled"
        if match:
            q, a = qobuz_track(match), amazon_track(match)
            if q and a and match.status in (
                "VARIANTENPRÜFUNG", "ZEITPRÜFUNG", "MANUELL FREIGEGEBEN",
                "VARIANTE ABGELEHNT", "KEINE LYRICS", "KEINE SYNC-LYRICS", "VORHANDEN",
            ):
                # Nur echte Paarentscheidungen aktivieren, wenn bereits eine Review-
                # Entscheidung existiert oder der aktuelle Status eine unsichere Paarung ist.
                existing = get_pair_decision(self.review_decisions, q, a)
                if match.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG") or existing:
                    pair_state = "normal"
                if existing:
                    reset_state = "normal"
            if match.target_kind == "Amazon" and a:
                amazon_state = "normal"
                if get_amazon_decision(self.review_decisions, a):
                    reset_state = "normal"
        for button in (self.approve_variant_button, self.reject_variant_button):
            button.config(state=pair_state)
        for button in (self.amazon_include_button, self.amazon_ignore_button):
            button.config(state=amazon_state)
        self.reset_review_button.config(state=reset_state)

    def rebuild_matches_from_review(self, preserve_path: Optional[Path] = None):
        if self.source_tracks is None or self.target_tracks is None:
            return
        self.matches = build_matches(
            self.source_tracks,
            self.target_tracks,
            self.last_preview_max_diff,
            self.overwrite_var.get(),
            self.synced_only_var.get(),
            self.review_decisions,
        )
        self.refresh_album_filter_values()
        self.apply_filters()
        self.update_preview_summary()
        self.restore_apply_state()
        if preserve_path:
            wanted = preserve_path.resolve()
            for item, match in self.tree_item_matches.items():
                try:
                    if display_track(match).path.resolve() == wanted:
                        self.tree.selection_set(item); self.tree.focus(item); self.tree.see(item); self.on_track_select()
                        break
                except Exception:
                    continue

    def set_pair_review_decision(self, action: str):
        match = self.selected_match
        if not match:
            return
        q, a = qobuz_track(match), amazon_track(match)
        if not q or not a:
            messagebox.showinfo(APP_NAME, "Für diesen Titel gibt es kein Qobuz/Amazon-Paar zu bewerten.")
            return
        if action not in ("APPROVE", "REJECT"):
            return

        if action == "APPROVE":
            warning = (
                "Diese Variantenpaarung wird MANUELL freigegeben. Danach darf die Amazon-Datei als Lyrics-Quelle "
                "für die Qobuz-Datei verwendet werden, auch wenn automatische Sicherheitsmerkmale widersprechen.\n\n"
                f"Qobuz: {q.title}\nISRC: {q.isrc or '-'}\nLaufzeit: {q.duration:.3f} s\n\n"
                f"Amazon: {a.title}\nISRC: {a.isrc or '-'}\nLaufzeit: {a.duration:.3f} s\n\n"
                f"Δ: {match.duration_diff:.3f} s\n\n"
                "Nur freigeben, wenn du die Fassungen bewusst geprüft hast."
                if match.duration_diff is not None else
                "Diese Variantenpaarung wird manuell freigegeben. Fortfahren?"
            )
            if not messagebox.askyesno("Variante freigeben", warning):
                return
        else:
            if not messagebox.askyesno(
                "Variante ablehnen",
                "Diese Qobuz/Amazon-Paarung wird als nicht passend gespeichert. "
                "Die Amazon-Datei kann danach als eigener Amazon-only-Titel erscheinen.\n\nFortfahren?",
            ):
                return

        key = pair_decision_key(q, a)
        self.review_decisions.setdefault("entries", {})[key] = {
            "kind": "pair",
            "action": action,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "qobuz_path": str(q.path),
            "amazon_path": str(a.path),
            "qobuz_title": q.title,
            "amazon_title": a.title,
            "qobuz_isrc": q.isrc,
            "amazon_isrc": a.isrc,
            "qobuz_duration": round(q.duration, 6),
            "amazon_duration": round(a.duration, 6),
        }
        self.save_review_decisions()
        self.rebuild_matches_from_review(q.path)

    def set_amazon_review_decision(self, action: str):
        match = self.selected_match
        if not match:
            return
        a = amazon_track(match)
        if match.target_kind != "Amazon" or not a:
            messagebox.showinfo(APP_NAME, "Diese Entscheidung gilt nur für Amazon-only-Titel.")
            return
        if action not in ("INCLUDE", "IGNORE"):
            return
        label = "einschließen" if action == "INCLUDE" else "ignorieren"
        if not messagebox.askyesno(
            "Amazon-only Entscheidung",
            f"'{a.title}' künftig beim Samsung-Export {label}?\n\n"
            "Die Amazon-Quelldatei selbst wird nicht verändert.",
        ):
            return
        key = amazon_decision_key(a)
        self.review_decisions.setdefault("entries", {})[key] = {
            "kind": "amazon_only",
            "action": action,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "amazon_path": str(a.path),
            "title": a.title,
            "artist": a.artist,
            "album": a.album,
            "isrc": a.isrc,
        }
        self.save_review_decisions()
        self.rebuild_matches_from_review(a.path)

    def reset_selected_review_decision(self):
        match = self.selected_match
        if not match:
            return
        q, a = qobuz_track(match), amazon_track(match)
        entries = self.review_decisions.setdefault("entries", {})
        keys = []
        if q and a:
            keys.append(pair_decision_key(q, a))
        if match.target_kind == "Amazon" and a:
            keys.append(amazon_decision_key(a))
        existing = [key for key in keys if key in entries]
        if not existing:
            return
        if not messagebox.askyesno("Entscheidung zurücksetzen", "Gespeicherte persönliche Entscheidung für diesen Eintrag entfernen?"):
            return
        for key in existing:
            entries.pop(key, None)
        self.save_review_decisions()
        self.rebuild_matches_from_review(display_track(match).path)

    # ---------------- DETAILS ----------------
    def create_details(self, parent):
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.columnconfigure(0, weight=1); panel.rowconfigure(3, weight=1)

        cover_frame = tk.Frame(panel, bg=COLORS["panel"], width=210, height=210)
        cover_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8)); cover_frame.grid_propagate(False)
        cover_frame.columnconfigure(0, weight=1); cover_frame.rowconfigure(0, weight=1)
        self.detail_cover_canvas = tk.Canvas(cover_frame, width=190, height=190, bg=COLORS["panel"], highlightthickness=0, borderwidth=0)
        self.detail_cover_canvas.grid(row=0, column=0)

        self.detail_title = ttk.Label(panel, text="Kein Titel ausgewählt", style="Title.TLabel", wraplength=390)
        self.detail_title.grid(row=1, column=0, sticky="w")
        self.detail_artist = ttk.Label(panel, text="", style="Panel.TLabel")
        self.detail_artist.grid(row=2, column=0, sticky="w", pady=(2, 8))

        self.notebook = ttk.Notebook(panel)
        self.notebook.grid(row=3, column=0, sticky="nsew")
        self.overview_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.audio_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.spectrum_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.match_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.notebook.add(self.overview_tab, text="Übersicht")
        self.notebook.add(self.audio_tab, text="Audio")
        self.notebook.add(self.spectrum_tab, text="Spektrum")
        self.notebook.add(self.match_tab, text="Match")
        self.create_overview_tab()
        self.create_audio_tab()
        self.create_spectrum_tab()
        self.create_match_tab()

    def create_overview_tab(self):
        self.overview_tab.columnconfigure(1, weight=1)
        self.detail_vars = {}
        fields = [
            ("Album", "album"), ("Track", "track"), ("Codec", "codec"), ("Bit-Tiefe", "bits"),
            ("Samplerate", "samplerate"), ("FLAC-Bitrate", "bitrate"), ("PCM-Referenz", "pcm"),
            ("Kanäle", "channels"), ("Laufzeit", "duration"), ("Dateigröße", "size"),
            ("ISRC", "isrc"), ("Lyrics", "lyrics"), ("Cover", "cover"), ("Match", "match"), ("Entscheidung", "decision"), ("Δ Laufzeit", "delta"),
        ]
        for row, (label, key) in enumerate(fields):
            ttk.Label(self.overview_tab, text=f"{label}:", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            var = tk.StringVar(value="-"); self.detail_vars[key] = var
            ttk.Label(self.overview_tab, textvariable=var, style="Panel.TLabel", wraplength=300).grid(row=row, column=1, sticky="nw", pady=2)
        row = len(fields)
        ttk.Separator(self.overview_tab, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10); row += 1
        ttk.Label(self.overview_tab, text="Qobuz-Datei:", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.detail_qobuz_path = ttk.Label(self.overview_tab, text="-", style="Panel.TLabel", wraplength=400); self.detail_qobuz_path.grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 8)); row += 1
        ttk.Label(self.overview_tab, text="Amazon-Match:", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.detail_amazon_path = ttk.Label(self.overview_tab, text="-", style="Panel.TLabel", wraplength=400); self.detail_amazon_path.grid(row=row, column=0, columnspan=2, sticky="w")

    def create_audio_tab(self):
        self.audio_tab.columnconfigure(1, weight=1)
        buttons = ttk.Frame(self.audio_tab, style="Panel.TFrame")
        buttons.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.deep_button = ttk.Button(buttons, text="🔬 Audio tief analysieren", command=self.deep_analyze_selected)
        self.deep_button.pack(side="left", padx=(0, 6))
        self.deep_both_button = ttk.Button(buttons, text="↔ Qobuz + Amazon analysieren", command=self.deep_analyze_selected_both)
        self.deep_both_button.pack(side="left", padx=6)
        self.deep_visible_button = ttk.Button(buttons, text="Alle sichtbaren analysieren", command=self.deep_analyze_visible)
        self.deep_visible_button.pack(side="left", padx=6)
        self.ffmpeg_button = ttk.Button(buttons, text="FFmpeg auswählen", command=self.choose_ffmpeg)
        self.ffmpeg_button.pack(side="right")

        ttk.Label(self.audio_tab, textvariable=self.deep_status_var, style="Muted.TLabel", wraplength=390).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.deep_vars = {}
        fields = [
            ("Integrated Loudness", "lufs"), ("Loudness Range", "lra"), ("True Peak (dBTP)", "true_peak"),
            ("Sample Peak", "sample_peak"), ("RMS", "rms"), ("Crest Factor", "crest"),
            ("Full-Scale-Samples", "clipping"), ("Samples ≥ -0.1 dBFS", "near_clip"),
            ("DC Offset", "dc"), ("Lossless-Plausibilität", "plausibility"), ("Confidence", "confidence"),
            ("Höchste Aktivität", "highest"), ("Cutoff-Verdacht", "cutoff"),
        ]
        for row, (label, key) in enumerate(fields, start=2):
            ttk.Label(self.audio_tab, text=f"{label}:", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
            var = tk.StringVar(value="-"); self.deep_vars[key] = var
            ttk.Label(self.audio_tab, textvariable=var, style="Panel.TLabel", wraplength=270).grid(row=row, column=1, sticky="nw", pady=2)
        row = 2 + len(fields)
        ttk.Label(self.audio_tab, text="Hinweis:", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="nw", pady=(8, 2))
        self.lossy_note_var = tk.StringVar(value="Spektralbasierte Lossy-Erkennung ist eine Heuristik, kein Beweis.")
        ttk.Label(self.audio_tab, textvariable=self.lossy_note_var, style="Panel.TLabel", wraplength=300).grid(row=row, column=1, sticky="nw", pady=(8, 2))

    def create_spectrum_tab(self):
        self.spectrum_tab.columnconfigure(0, weight=1); self.spectrum_tab.rowconfigure(0, weight=1)
        self.spectrum_container = ttk.Frame(self.spectrum_tab, style="Panel.TFrame")
        self.spectrum_container.grid(row=0, column=0, sticky="nsew")
        self.spectrum_placeholder = ttk.Label(self.spectrum_container, text="Noch keine Tiefenanalyse.\n\nIm Tab „Audio“ auf „Audio tief analysieren“ klicken.", style="Panel.TLabel", justify="center")
        self.spectrum_placeholder.pack(fill="both", expand=True, pady=30)
        self.chart_canvas = None

    def create_match_tab(self):
        self.match_tab.columnconfigure(0, weight=1)
        self.match_tab.rowconfigure(1, weight=1)
        self.match_summary_var = tk.StringVar(value="Kein Track ausgewählt.")
        ttk.Label(self.match_tab, textvariable=self.match_summary_var, style="Panel.TLabel", wraplength=520).grid(row=0, column=0, sticky="w", pady=(0, 8))
        columns = ("field", "qobuz", "amazon")
        self.compare_tree = ttk.Treeview(self.match_tab, columns=columns, show="headings", height=14)
        self.compare_tree.heading("field", text="Eigenschaft"); self.compare_tree.heading("qobuz", text="Qobuz"); self.compare_tree.heading("amazon", text="Amazon")
        self.compare_tree.column("field", width=120, stretch=False); self.compare_tree.column("qobuz", width=120); self.compare_tree.column("amazon", width=120)
        self.compare_tree.grid(row=1, column=0, sticky="nsew")

        review = ttk.Frame(self.match_tab, style="Panel.TFrame")
        review.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        review.columnconfigure(2, weight=1)
        self.approve_variant_button = ttk.Button(review, text="Variante freigeben", command=lambda: self.set_pair_review_decision("APPROVE"), state="disabled")
        self.approve_variant_button.grid(row=0, column=0, sticky="w", padx=(0, 5), pady=(0, 5))
        self.reject_variant_button = ttk.Button(review, text="Variante ablehnen", command=lambda: self.set_pair_review_decision("REJECT"), state="disabled")
        self.reject_variant_button.grid(row=0, column=1, sticky="w", padx=5, pady=(0, 5))
        self.reset_review_button = ttk.Button(review, text="Entscheidung zurücksetzen", command=self.reset_selected_review_decision, state="disabled")
        self.reset_review_button.grid(row=0, column=3, sticky="e", padx=(8, 0), pady=(0, 5))
        self.amazon_include_button = ttk.Button(review, text="Amazon-only einschließen", command=lambda: self.set_amazon_review_decision("INCLUDE"), state="disabled")
        self.amazon_include_button.grid(row=1, column=0, sticky="w", padx=(0, 5))
        self.amazon_ignore_button = ttk.Button(review, text="Amazon-only ignorieren", command=lambda: self.set_amazon_review_decision("IGNORE"), state="disabled")
        self.amazon_ignore_button.grid(row=1, column=1, sticky="w", padx=5)

    # ---------------- SORTING ----------------
    def sort_by(self, column):
        if self.current_sort_column == column:
            self.current_sort_reverse = not self.current_sort_reverse
        else:
            self.current_sort_column = column
            self.current_sort_reverse = False
        self.update_heading_arrows()
        self.apply_filters()

    def update_heading_arrows(self):
        labels = {
            "status": "Status", "source": "Quelle", "album": "Album", "title": "Titel",
            "quality": "Audioqualität", "match": "Match", "delta": "Δ Zeit",
        }
        for key, label in labels.items():
            arrow = ""
            if self.current_sort_column == key:
                arrow = " ↓" if self.current_sort_reverse else " ↑"
            target = "#0" if key == "status" else ("sourcekind" if key == "source" else key)
            self.tree.heading(target, text=label + arrow)

    def sort_key(self, match: Match):
        col = self.current_sort_column
        t = display_track(match)
        if col == "status":
            return (STATUS_ORDER.get(match.status, 99), t.album_norm, t.track_number or 9999, t.title_norm)
        if col == "source":
            label = source_label(match)
            return (SOURCE_ORDER.get(label, 99), t.album_norm, t.track_number or 9999, t.title_norm)
        if col == "album":
            return (t.album_norm, t.track_number or 9999, t.title_norm)
        if col == "title":
            return (t.title_norm, t.album_norm, t.track_number or 9999)
        if col == "quality":
            return (t.bits_per_sample, t.sample_rate, t.audio_bitrate_kbps, t.title_norm)
        if col == "match":
            return (MATCH_ORDER.get(match.method, 99), t.title_norm)
        if col == "delta":
            return (match.duration_diff if match.duration_diff is not None else float("inf"), t.title_norm)
        return str(t.path).casefold()

    # ---------------- DND / PATHS / SETTINGS ----------------
    def setup_drop_target(self, widget, variable):
        if not HAS_DND:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda event: self.handle_folder_drop(event, variable))
        except Exception:
            pass

    def handle_folder_drop(self, event, variable):
        try:
            paths = self.root.tk.splitlist(event.data)
            if not paths:
                return
            path = Path(paths[0]); path = path.parent if path.is_file() else path
            if not path.is_dir():
                messagebox.showerror("Ungültiger Ordner", "Bitte einen Ordner hineinziehen."); return
            variable.set(str(path)); self.save_settings()
        except Exception as exc:
            messagebox.showerror("Drag & Drop", str(exc))

    def choose_source(self):
        folder = filedialog.askdirectory(title="Amazon-Music-Ordner", initialdir=self.source_var.get() or str(APP_DIR))
        if folder:
            self.source_var.set(folder); self.save_settings()

    def choose_target(self):
        folder = filedialog.askdirectory(title="Qobuz-Ordner", initialdir=self.target_var.get() or str(APP_DIR))
        if folder:
            self.target_var.set(folder); self.save_settings()

    def choose_samsung_export(self):
        initial = self.samsung_export_var.get() or self.target_var.get() or str(APP_DIR)
        folder = filedialog.askdirectory(title="Samsung-Music-Exportziel", initialdir=initial)
        if folder:
            self.samsung_export_var.set(folder); self.save_settings()

    def choose_ffmpeg(self):
        filename = filedialog.askopenfilename(
            title="ffmpeg.exe auswählen",
            filetypes=[("FFmpeg", "ffmpeg.exe"), ("Programme", "*.exe"), ("Alle Dateien", "*.*")],
        )
        if filename:
            self.ffmpeg_path = Path(filename).resolve()
            self.settings["ffmpeg_path"] = str(self.ffmpeg_path)
            self.save_settings(); self.update_ffmpeg_status()

    def update_ffmpeg_status(self):
        if self.ffmpeg_path and self.ffmpeg_path.is_file():
            self.ffmpeg_status_label.config(text=f"FFmpeg: ✓ {self.ffmpeg_path.name}")
        else:
            self.ffmpeg_status_label.config(text="FFmpeg: nicht gefunden – Tiefenanalyse benötigt Auswahl")

    def save_settings(self):
        try:
            diff = float(self.duration_var.get().replace(",", "."))
        except Exception:
            diff = DEFAULT_MAX_DURATION_DIFF
        self.settings.update({
            "amazon_path": self.source_var.get(), "qobuz_path": self.target_var.get(),
            "samsung_export_path": self.samsung_export_var.get(),
            "ffmpeg_path": str(self.ffmpeg_path) if self.ffmpeg_path else "",
            "only_synced": self.synced_only_var.get(), "overwrite": self.overwrite_var.get(),
            "verify_audio": self.verify_var.get(), "backup": self.backup_var.get(),
            "include_unreviewed_amazon_only": self.include_unreviewed_amazon_only_var.get(),
            "max_duration_diff": diff, "geometry": self.root.geometry(),
        })
        save_json_file(SETTINGS_FILE, self.settings)

    # ---------------- TUTORIAL ----------------
    def show_tutorial(self):
        window = tk.Toplevel(self.root); window.title("Kurzanleitung"); window.geometry("800x760"); window.minsize(680, 600); window.configure(bg=COLORS["bg"]); window.transient(self.root); window.grab_set()
        window.after(50, lambda: enable_windows_dark_titlebar(window))
        frame = ttk.Frame(window, padding=22); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Qobuz als Master behalten. Amazon-Lyrics übernehmen. Samsung-kompatible LRC-Bibliothek separat exportieren.", style="Muted.TLabel").pack(anchor="w", pady=(3, 18))
        tutorial = (
            "① AMAZON / QOBUZ\nFür die Vorschau reicht bereits EINE der beiden Bibliotheken. Mit beiden zusammen werden sichere Matches, Varianten, fehlende Lyrics und Amazon-only-Titel getrennt erkannt.\n\n"
            "② ANALYSIEREN / VORSCHAU\nISRC ist der Primärschlüssel. Exakter Titel/Artist ist der sichere Fallback. Album + Tracknummer dient nur zur Diagnose möglicher Varianten. Die Lyrics-Verfügbarkeit wird erst NACH der Dateizuordnung bewertet.\n\n"
            "③ STATUSLOGIK\nBEREIT/ÜBERSCHREIBEN = automatisch sicher. MANUELL FREIGEGEBEN = eine Varianten-/Zeitprüfung wurde von dir ausdrücklich bestätigt. KEINE LYRICS bzw. KEINE SYNC-LYRICS = Datei wurde gefunden, aber Amazon liefert nicht die gewünschte Lyrics-Art. VARIANTENPRÜFUNG/ZEITPRÜFUNG = persönliche Entscheidung offen. VARIANTE ABGELEHNT = bewusst nicht gekoppelt. KEIN AMAZON-TITEL = Qobuz ohne plausibles Amazon-Gegenstück. AMAZON-ONLY = kein Qobuz-Gegenstück; OK/IGNORIERT zeigen deine gespeicherte Samsung-Entscheidung.\n\n"
            "④ SORTIEREN / FILTERN\nStatus, Quelle, Album, Titel, Audioqualität, Match und Δ Zeit sind per Spaltenkopf sortierbar. Zusätzlich gibt es Filter für Status, Quelle und Album sowie OFFENE ENTSCHEIDUNGEN. Die Statusspalte ist für lange Review-Status verbreitert.\n\n"
            "⑤ PERSÖNLICHE REVIEW-ENTSCHEIDUNGEN\nIm Match-Tab oder per Rechtsklick kannst du VARIANTENPRÜFUNG/ZEITPRÜFUNG freigeben oder ablehnen. Amazon-only-Titel lassen sich für Samsung einschließen oder ignorieren. Entscheidungen werden dauerhaft in match_review_decisions.json gespeichert und bei neuen Scans wieder angewendet.\n\n"
            "⑥ KI-REVIEW EXPORT\nDer Button 'KI-Review exportieren' erstellt eine Markdown-Liste plus JSON mit allen offenen Varianten/Zeitfällen und ungeprüften Amazon-only-Titeln. Enthalten sind Qobuz/Amazon-Metadaten, ISRC, Laufzeiten, Audioformat, Lyrics-Status, kurze Lyrics-Probe und Zensur-Indizien. Das Paket enthält direkt einen konservativen Prüfauftrag für eine KI.\n\n"
            "⑦ LYRICS ÜBERNEHMEN\nAutomatisch sichere BEREIT/ÜBERSCHREIBEN-Titel und ausdrücklich MANUELL FREIGEGEBENE Varianten können geschrieben werden. Kein Audio-Encoding. Bei aktivierter SHA-256-Prüfung müssen die komprimierten FLAC-Audioframes vor/nach dem Tag-Write byte-identisch sein.\n\n"
            "⑧ SAMSUNG MUSIC EXPORT\nQobuz wird als Audioquelle bevorzugt. Sichere oder manuell freigegebene Amazon-Matches dürfen die LRC liefern. Amazon-only-Entscheidungen werden berücksichtigt. Embedded Lyrics werden nur aus der Export-Kopie entfernt. Die LRC wird für Samsung bereinigt: nackte End-Timestamps und exakte Duplikate werden entfernt, Text-Events chronologisch geschrieben. Echte Mehrdisc-Alben erhalten ausschließlich in der Samsung-Kopie fortlaufende Tracknummern; die Master bleiben unverändert.\n\n⑨ SAMSUNG EXPORT REPARIEREN\nFür bereits exportierte/aufs Handy kopierte Bibliotheken. Der vorhandene Samsung-Export wird in-place geprüft: LRC-Dateien werden sanitisiert und Mehrdisc-Metadaten sicher geflattet. Geänderte Dateien werden zusätzlich mit Original-Ordnerstruktur in einen kleinen Samsung_Repair_Patch-Ordner kopiert. FLAC-Audioframes werden bei Tag-Reparaturen per SHA-256 verifiziert.\n\n"
            "⑩ AUDIO TIEF ANALYSIEREN\nBenötigt FFmpeg. LUFS, LRA, True Peak, RMS, Crest Factor, Full-Scale-Samples, Waveform, Spektrum und ein vorsichtiger Lossy-Cutoff-Hinweis. Die Lossy-Erkennung ist eine Heuristik, kein Beweis.\n\n"
            "⑪ CACHE / REPORTS\nTiefenanalysen und Samsung-Exporte werden inkrementell gecacht. CSV-Analyse und Reports enthalten Quelle, beide Dateipfade und Diagnosehinweise; persönliche Review-Entscheidungen liegen separat und werden nicht stillschweigend überschrieben."
        )
        text = tk.Text(frame, bg=COLORS["field"], fg=COLORS["fg"], insertbackground=COLORS["fg"], relief="flat", borderwidth=0, wrap="word", font=("Segoe UI", 10), padx=14, pady=14)
        text.pack(fill="both", expand=True); text.insert("1.0", tutorial); text.configure(state="disabled")
        def close():
            self.settings["tutorial_seen"] = True; self.save_settings(); window.destroy()
        ttk.Button(frame, text="Verstanden", style="Accent.TButton", command=close).pack(anchor="e", pady=(14, 0))
        window.protocol("WM_DELETE_WINDOW", close)

    # ---------------- VALIDATION / STATUS ----------------
    def validate_paths(self):
        """Vorschau darf Amazon-only, Qobuz-only oder beide Bibliotheken analysieren."""
        amazon_text = self.source_var.get().strip()
        qobuz_text = self.target_var.get().strip()

        source = Path(amazon_text).expanduser().resolve() if amazon_text else None
        target = Path(qobuz_text).expanduser().resolve() if qobuz_text else None

        if source is not None and not source.is_dir():
            raise ValueError("Amazon-Ordner existiert nicht.")
        if target is not None and not target.is_dir():
            raise ValueError("Qobuz-Ordner existiert nicht.")
        if source is None and target is None:
            raise ValueError("Bitte mindestens einen gültigen Amazon- oder Qobuz-Ordner auswählen.")
        if source is not None and target is not None:
            if source == target or source in target.parents or target in source.parents:
                raise ValueError("Amazon- und Qobuz-Bibliothek dürfen nicht identisch oder ineinander verschachtelt sein.")

        try:
            max_diff = float(self.duration_var.get().replace(",", "."))
        except ValueError:
            raise ValueError("Ungültige Laufzeitabweichung.")
        if max_diff < 0:
            raise ValueError("Die Laufzeitabweichung darf nicht negativ sein.")
        return source, target, max_diff

    def validate_samsung_export_paths(self):
        qobuz_text = self.target_var.get().strip()
        amazon_text = self.source_var.get().strip()
        export_text = self.samsung_export_var.get().strip()

        qobuz_root = Path(qobuz_text).expanduser().resolve() if qobuz_text else None
        amazon_root = Path(amazon_text).expanduser().resolve() if amazon_text else None

        if qobuz_root is not None and not qobuz_root.is_dir():
            qobuz_root = None
        if amazon_root is not None and not amazon_root.is_dir():
            amazon_root = None

        if qobuz_root is None and amazon_root is None:
            raise ValueError(
                "Für den Samsung-Export muss mindestens ein gültiger Qobuz- oder Amazon-Ordner vorhanden sein."
            )

        if not export_text:
            raise ValueError("Bitte zuerst ein Samsung-Music-Exportziel auswählen.")

        export_root = Path(export_text).expanduser().resolve()
        export_root.mkdir(parents=True, exist_ok=True)

        for label, root in (("Qobuz", qobuz_root), ("Amazon", amazon_root)):
            if root is None:
                continue
            if export_root == root or export_root in root.parents or root in export_root.parents:
                raise ValueError(
                    f"Samsung-Export und {label}-Bibliothek dürfen nicht identisch oder ineinander verschachtelt sein."
                )

        try:
            max_diff = float(self.duration_var.get().replace(",", "."))
        except ValueError:
            max_diff = DEFAULT_MAX_DURATION_DIFF

        return qobuz_root, amazon_root, export_root, max_diff

    def set_busy(self, busy: bool):
        self.preview_button.config(state="disabled" if busy else "normal")
        self.cancel_button.config(state="normal" if busy else "disabled")
        self.deep_button.config(state="disabled" if busy else "normal")
        self.deep_both_button.config(state="disabled" if busy else "normal")
        self.deep_visible_button.config(state="disabled" if busy else "normal")
        self.samsung_export_button.config(state="disabled" if busy else "normal")
        if hasattr(self, "samsung_repair_button"):
            self.samsung_repair_button.config(state="disabled" if busy else "normal")
        if hasattr(self, "review_export_button"):
            self.review_export_button.config(state="disabled" if busy else "normal")

    def restore_apply_state(self):
        ready = any(m.target_kind == "Qobuz" and m.status in ("BEREIT", "ÜBERSCHREIBEN", "MANUELL FREIGEGEBEN") and m.source for m in self.matches)
        self.apply_button.config(state="normal" if ready else "disabled")

    def set_progress(self, current, total, text=""):
        def update():
            self.progress["maximum"] = max(total, 1); self.progress["value"] = current
            if text:
                self.status_label.config(text=text)
        self.root.after(0, update)

    # ---------------- PREVIEW ----------------
    def start_preview(self):
        try:
            source, target, max_diff = self.validate_paths()
        except Exception as exc:
            messagebox.showerror("Fehler", str(exc)); return
        self.save_settings(); self.hide_tooltip(); self.cancel_event.clear(); self.set_busy(True); self.apply_button.config(state="disabled"); self.clear_table()
        threading.Thread(target=self.preview_worker, args=(source, target, max_diff, self.overwrite_var.get(), self.synced_only_var.get()), daemon=True).start()

    def preview_worker(self, source, target, max_diff, overwrite, synced_only):
        try:
            source_files = find_flacs(source) if source else []
            target_files = find_flacs(target) if target else []
            total = len(source_files) + len(target_files); counter = [0]
            def progress(_, __):
                counter[0] += 1; self.set_progress(counter[0], max(total, 1), "FLAC-Dateien werden analysiert...")

            # Amazon bekommt ebenfalls Thumbnails, damit AMAZON-ONLY-Zeilen vollständig aussehen.
            source_tracks, source_errors = scan_tracks(source_files, DEFAULT_WORKERS, self.cancel_event, True, progress)
            if self.cancel_event.is_set(): return
            target_tracks, target_errors = scan_tracks(target_files, DEFAULT_WORKERS, self.cancel_event, True, progress)
            if self.cancel_event.is_set(): return

            self.source_tracks = source_tracks
            self.target_tracks = target_tracks
            self.last_preview_max_diff = max_diff
            self.matches = build_matches(
                source_tracks, target_tracks, max_diff, overwrite, synced_only, self.review_decisions
            )
            self.root.after(0, lambda: self.show_preview(source_files, target_files, source_errors, target_errors))
        except Exception as exc:
            self.root.after(0, lambda exc=exc: messagebox.showerror("Fehler", str(exc)))
        finally:
            self.root.after(0, lambda: (self.set_busy(False), self.restore_apply_state()))

    # ---------------- FILTER / RENDER ----------------
    def match_visible(self, match: Match):
        selected = self.filter_var.get()
        problem_statuses = {
            "KEINE LYRICS", "KEINE SYNC-LYRICS", "VARIANTENPRÜFUNG",
            "ZEITPRÜFUNG", "VARIANTE ABGELEHNT", "KEIN AMAZON-TITEL",
            "AMAZON-ONLY", "AMAZON-ONLY IGNORIERT",
        }
        open_decision_statuses = {"VARIANTENPRÜFUNG", "ZEITPRÜFUNG", "AMAZON-ONLY"}
        if selected == "NUR PROBLEME" and match.status not in problem_statuses:
            return False
        if selected == "OFFENE ENTSCHEIDUNGEN" and match.status not in open_decision_statuses:
            return False
        if selected not in ("", "ALLE", "NUR PROBLEME", "OFFENE ENTSCHEIDUNGEN") and match.status != selected:
            return False

        source_filter = self.source_filter_var.get()
        q = qobuz_track(match)
        a = amazon_track(match)
        if source_filter == "QOBUZ" and q is None:
            return False
        if source_filter == "AMAZON" and a is None:
            return False
        if source_filter == "AMAZON-ONLY" and match.target_kind != "Amazon":
            return False

        t = display_track(match)
        album_filter = self.album_filter_var.get()
        if album_filter not in ("", "ALLE"):
            shown_album = t.album or "(Ohne Album)"
            if shown_album != album_filter:
                return False

        query = normalize(self.search_var.get())
        if not query:
            return True
        parts = [
            t.title_norm, t.artist_norm, t.album_norm, normalize(t.isrc), normalize(str(t.path)),
            normalize(match.status), normalize(match.method), normalize(match.message), normalize(source_label(match)),
        ]
        if q and q is not t:
            parts += [q.title_norm, q.artist_norm, q.album_norm, normalize(q.isrc), normalize(str(q.path))]
        if a and a is not t:
            parts += [a.title_norm, a.artist_norm, a.album_norm, normalize(a.isrc), normalize(str(a.path))]
        return query in " ".join(parts)

    def clear_table(self):
        self.hide_tooltip(); self.tree.delete(*self.tree.get_children()); self.tree_item_matches.clear(); self.thumbnail_refs.clear(); self.visible_count_label.config(text="0 Titel"); self.clear_details()

    def visible_matches(self):
        items = [m for m in self.matches if self.match_visible(m)]
        if self.current_sort_column:
            items.sort(key=self.sort_key, reverse=self.current_sort_reverse)
        return items

    def apply_filters(self):
        if not hasattr(self, "tree"):
            return
        self.hide_tooltip(); self.tree.delete(*self.tree.get_children()); self.tree_item_matches.clear(); self.thumbnail_refs.clear()
        items = self.visible_matches()
        for match in items:
            t = display_track(match)
            delta = f"{match.duration_diff:.2f}s" if match.duration_diff is not None else "-"
            photo = None
            if t.thumbnail_png:
                try:
                    image = Image.open(io.BytesIO(t.thumbnail_png)); photo = ImageTk.PhotoImage(image)
                except Exception:
                    pass
            item = self.tree.insert(
                "", "end", text=f"    {match.status}", image=photo,
                values=(source_label(match), t.album or "-", t.title, quality_text(t), match.method, delta),
                tags=(match.status,),
            )
            self.tree_item_matches[item] = match
            if photo is not None:
                self.thumbnail_refs[item] = photo
        self.visible_count_label.config(text=f"{len(items)} / {len(self.matches)} Titel")
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0]); self.tree.focus(children[0]); self.on_track_select()
        else:
            self.clear_details()

    def update_preview_summary(self):
        ready = sum(1 for m in self.matches if m.status in ("BEREIT", "ÜBERSCHREIBEN", "MANUELL FREIGEGEBEN"))
        no_lyrics = sum(1 for m in self.matches if m.status in ("KEINE LYRICS", "KEINE SYNC-LYRICS"))
        variants = sum(1 for m in self.matches if m.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG"))
        decided = sum(1 for m in self.matches if m.status in ("MANUELL FREIGEGEBEN", "VARIANTE ABGELEHNT", "AMAZON-ONLY OK", "AMAZON-ONLY IGNORIERT"))
        amazon_only = sum(1 for m in self.matches if m.status in ("AMAZON-ONLY", "AMAZON-ONLY OK", "AMAZON-ONLY IGNORIERT"))
        missing = sum(1 for m in self.matches if m.status == "KEIN AMAZON-TITEL")
        open_decisions = sum(1 for m in self.matches if m.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG", "AMAZON-ONLY"))
        self.status_label.config(text=(
            f"Amazon: {self.last_preview_source_count} FLACs   |   Qobuz: {self.last_preview_target_count} FLACs   |   "
            f"Bereit: {ready}   |   Ohne passende Lyrics: {no_lyrics}   |   Varianten/Zeit offen: {variants}   |   "
            f"Amazon-only: {amazon_only}   |   Entscheidungen offen: {open_decisions}   |   Entschieden: {decided}   |   "
            f"Ohne Amazon: {missing}   |   Lesefehler: {self.last_preview_error_count}"
        ))

    def show_preview(self, source_files, target_files, source_errors, target_errors):
        self.last_preview_source_count = len(source_files)
        self.last_preview_target_count = len(target_files)
        self.last_preview_error_count = len(source_errors) + len(target_errors)
        self.refresh_album_filter_values()
        self.apply_filters()
        self.update_preview_summary()
        self.progress["maximum"] = max(len(source_files) + len(target_files), 1); self.progress["value"] = self.progress["maximum"]
        self.restore_apply_state()

    # ---------------- DETAILS ----------------
    def clear_details(self):
        self.selected_match = None
        self.detail_title.config(text="Kein Titel ausgewählt"); self.detail_artist.config(text=""); self.detail_cover_canvas.delete("all"); self.detail_cover_ref = None
        for var in self.detail_vars.values(): var.set("-")
        for var in self.deep_vars.values(): var.set("-")
        self.deep_status_var.set("Noch nicht analysiert")
        self.lossy_note_var.set("Spektralbasierte Lossy-Erkennung ist eine Heuristik, kein Beweis.")
        self.detail_qobuz_path.config(text="-"); self.detail_amazon_path.config(text="-")
        self.match_summary_var.set("Kein Track ausgewählt."); self.compare_tree.delete(*self.compare_tree.get_children())
        self.update_review_buttons(None)
        self.render_spectrum(None)

    def on_track_select(self, _event=None):
        selection = self.tree.selection()
        if not selection: return
        match = self.tree_item_matches.get(selection[0])
        if not match: return
        self.selected_match = match
        t = display_track(match)
        q = qobuz_track(match)
        a = amazon_track(match)

        self.detail_title.config(text=t.title); self.detail_artist.config(text=t.artist or "Unbekannter Interpret")
        self.detail_cover_canvas.delete("all"); cover = load_large_cover(t.path, 190); self.detail_cover_ref = cover
        if cover:
            self.detail_cover_canvas.create_image(95, 95, image=cover, anchor="center")
        else:
            self.detail_cover_canvas.create_text(95, 95, text="Kein Cover", fill=COLORS["muted"], font=("Segoe UI", 10))

        vals = {
            "album": t.album or "-", "track": str(t.track_number) if t.track_number is not None else "-", "codec": t.codec,
            "bits": f"{t.bits_per_sample} Bit", "samplerate": format_samplerate(t.sample_rate), "bitrate": f"{t.audio_bitrate_kbps} kbit/s",
            "pcm": f"{t.pcm_bitrate_kbps} kbit/s", "channels": channel_text(t.channels), "duration": format_duration(t.duration),
            "size": format_size(t.file_size), "isrc": t.isrc or "-",
            "lyrics": "Synchronisiert" if t.lyrics_synced else ("Vorhanden, nicht synchronisiert" if t.lyrics else "Keine"),
            "cover": (t.cover_source + (f" ({t.cover_width}×{t.cover_height})" if t.cover_width and t.cover_height else "")) if t.cover_present else "Kein Cover",
            "match": f"{match.status} ({match.method})",
            "decision": self.decision_text_for_match(match),
            "delta": f"{match.duration_diff:.3f} s" if match.duration_diff is not None else "-",
        }
        for k, v in vals.items(): self.detail_vars[k].set(v)

        self.detail_qobuz_path.config(text=str(q.path) if q else "-")
        self.detail_amazon_path.config(text=(
            str(a.path) + "\n\n" + quality_text(a) + "\nLyrics: " +
            ("synchronisiert" if a.lyrics_synced else ("vorhanden" if a.lyrics else "keine"))
        ) if a else "-")
        self.populate_match_tab(match)
        self.update_review_buttons(match)
        self.load_cached_analysis_for_selected()

    # ---------------- HOVER / CONTEXT ----------------
    def on_tree_motion(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            self.hide_tooltip(); return
        if self._hover_item == item:
            return
        self._hover_item = item; self.hide_tooltip(clear_hover=False)
        self.tooltip_after_id = self.root.after(450, lambda: self.show_track_tooltip(item))

    def hide_tooltip(self, clear_hover=True):
        if self.tooltip_after_id is not None:
            try: self.root.after_cancel(self.tooltip_after_id)
            except Exception: pass
            self.tooltip_after_id = None
        if self.tooltip_window is not None:
            try: self.tooltip_window.destroy()
            except Exception: pass
            self.tooltip_window = None
        if clear_hover: self._hover_item = None

    def show_track_tooltip(self, item):
        match = self.tree_item_matches.get(item)
        if not match: return
        t = display_track(match)
        tip = tk.Toplevel(self.root); self.tooltip_window = tip; tip.overrideredirect(True); tip.attributes("-topmost", True); tip.configure(bg=COLORS["border"])
        c = tk.Frame(tip, bg=COLORS["panel"], padx=12, pady=12); c.pack(padx=1, pady=1)
        cover = load_large_cover(t.path, 120); tip._cover_ref = cover
        if cover:
            tk.Label(c, image=cover, bg=COLORS["panel"]).grid(row=0, column=0, rowspan=10, sticky="n", padx=(0, 12))
        tk.Label(c, text=t.title, bg=COLORS["panel"], fg=COLORS["fg"], font=("Segoe UI", 11, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
        tk.Label(c, text=f"{t.artist or '-'}\n{t.album or '-'}", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9), justify="left", anchor="w").grid(row=1, column=1, sticky="w", pady=(2, 8))
        cache = self.get_cached_analysis(t.path)
        extra = ""
        if cache:
            extra = f"\nLUFS: {self.fmt_num(cache.get('integrated_lufs'), 1)}\nTrue Peak: {self.fmt_dbtp(cache.get('true_peak_dbfs'))}\nPlausibilität: {cache.get('plausibility', '-')}"
        info = (
            f"{t.codec}\n{t.bits_per_sample} Bit / {format_samplerate(t.sample_rate)}\nFLAC: {t.audio_bitrate_kbps} kbit/s\nPCM: {t.pcm_bitrate_kbps} kbit/s\n"
            f"{channel_text(t.channels)} · {format_duration(t.duration)}\n{format_size(t.file_size)}\nISRC: {t.isrc or '-'}\nLyrics: {'synchronisiert' if t.lyrics_synced else ('vorhanden' if t.lyrics else 'keine')}\nStatus: {match.status}{extra}"
        )
        tk.Label(c, text=info, bg=COLORS["panel"], fg=COLORS["fg"], justify="left", font=("Segoe UI", 9), anchor="w").grid(row=2, column=1, sticky="w")
        tip.update_idletasks(); w, h = tip.winfo_reqwidth(), tip.winfo_reqheight(); sx, sy = tip.winfo_screenwidth(), tip.winfo_screenheight(); px, py = self.root.winfo_pointerx(), self.root.winfo_pointery(); x, y = px + 18, py + 18
        if x + w > sx: x = px - w - 18
        if y + h > sy: y = py - h - 18
        tip.geometry(f"+{max(0, x)}+{max(0, y)}")

    def show_context_menu(self, event):
        self.hide_tooltip(); item = self.tree.identify_row(event.y)
        if not item: return
        self.tree.selection_set(item); match = self.tree_item_matches.get(item)
        if not match: return
        self.selected_match = match
        menu = tk.Menu(self.root, tearoff=0, bg=COLORS["panel2"], fg=COLORS["fg"], activebackground=COLORS["selection"], activeforeground="#ffffff", relief="flat")
        menu.add_command(label="Im Explorer anzeigen", command=lambda: self.open_in_explorer(display_track(match).path))
        q = qobuz_track(match); a = amazon_track(match)
        if q: menu.add_command(label="Qobuz-Pfad kopieren", command=lambda: self.copy_to_clipboard(str(q.path)))
        if a: menu.add_command(label="Amazon-Pfad kopieren", command=lambda: self.copy_to_clipboard(str(a.path)))
        menu.add_separator()
        pair_existing = get_pair_decision(self.review_decisions, q, a)
        if q and a and (match.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG") or pair_existing):
            menu.add_command(label="Variante freigeben", command=lambda: self.set_pair_review_decision("APPROVE"))
            menu.add_command(label="Variante ablehnen", command=lambda: self.set_pair_review_decision("REJECT"))
            if pair_existing:
                menu.add_command(label="Varianten-Entscheidung zurücksetzen", command=self.reset_selected_review_decision)
            menu.add_separator()
        if match.target_kind == "Amazon" and a:
            menu.add_command(label="Amazon-only für Samsung einschließen", command=lambda: self.set_amazon_review_decision("INCLUDE"))
            menu.add_command(label="Amazon-only bei Samsung ignorieren", command=lambda: self.set_amazon_review_decision("IGNORE"))
            if get_amazon_decision(self.review_decisions, a):
                menu.add_command(label="Amazon-only Entscheidung zurücksetzen", command=self.reset_selected_review_decision)
            menu.add_separator()
        menu.add_command(label="Review-Daten kopieren", command=lambda: self.copy_review_entry(match))
        menu.add_command(label="Technische Daten kopieren", command=lambda: self.copy_track_info(display_track(match)))
        menu.add_command(label="Audio tief analysieren", command=lambda: self.deep_analyze_paths([display_track(match).path]))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def open_in_explorer(self, path: Path):
        try:
            if os.name == "nt": subprocess.Popen(["explorer", "/select,", str(path)])
            else: subprocess.Popen(["xdg-open", str(path.parent)])
        except Exception as exc: messagebox.showerror("Explorer", str(exc))

    def copy_to_clipboard(self, text):
        self.root.clipboard_clear(); self.root.clipboard_append(text); self.root.update()

    def copy_track_info(self, t: Track):
        text = (
            f"Titel: {t.title}\nArtist: {t.artist}\nAlbum: {t.album}\nTrack: {t.track_number or '-'}\nISRC: {t.isrc or '-'}\n\n"
            f"Codec: {t.codec}\nAuflösung: {t.bits_per_sample} Bit / {format_samplerate(t.sample_rate)}\nKanäle: {channel_text(t.channels)}\n"
            f"FLAC-Audiobitrate: {t.audio_bitrate_kbps} kbit/s\nPCM-Referenz: {t.pcm_bitrate_kbps} kbit/s\nLaufzeit: {format_duration(t.duration)}\nDateigröße: {format_size(t.file_size)}\n\nDatei:\n{t.path}"
        )
        cache = self.get_cached_analysis(t.path)
        if cache:
            text += (
                f"\n\nTiefenanalyse:\nIntegrated Loudness: {self.fmt_num(cache.get('integrated_lufs'), 1)} LUFS\n"
                f"LRA: {self.fmt_num(cache.get('lra_lu'), 1)} LU\nTrue Peak: {self.fmt_dbtp(cache.get('true_peak_dbfs'))}\n"
                f"Sample Peak: {self.fmt_db(cache.get('sample_peak_dbfs'))}\nRMS: {self.fmt_db(cache.get('rms_dbfs'))}\n"
                f"Lossless-Plausibilität: {cache.get('plausibility', '-')} ({cache.get('confidence', '-')})"
            )
        self.copy_to_clipboard(text)

    # ---------------- AI REVIEW EXPORT ----------------
    def review_payload(self, match: Match, review_id: str = "") -> dict:
        q, a = qobuz_track(match), amazon_track(match)
        payload = {
            "review_id": review_id,
            "status": match.status,
            "source": source_label(match),
            "match_method": match.method,
            "duration_delta_s": round(match.duration_diff, 6) if match.duration_diff is not None else None,
            "automatic_note": match.message,
            "saved_decision": self.decision_text_for_match(match),
            "qobuz": None,
            "amazon": None,
            "signals": {},
        }

        def track_payload(t: Optional[Track]):
            if not t:
                return None
            return {
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "track": t.track_number,
                "isrc": t.isrc,
                "duration_s": round(t.duration, 6),
                "bit_depth": t.bits_per_sample,
                "sample_rate_hz": t.sample_rate,
                "flac_kbit_s": t.audio_bitrate_kbps,
                "lyrics": "SYNC" if t.lyrics_synced else ("UNSYNC" if t.lyrics else "NONE"),
                "lyrics_censor_suspicion": lyrics_censor_suspicion(t),
                "lyrics_preview": lyrics_preview(t),
                "path": str(t.path),
            }

        payload["qobuz"] = track_payload(q)
        payload["amazon"] = track_payload(a)
        if q and a:
            payload["signals"] = {
                "title_equal_normalized": q.title_norm == a.title_norm,
                "artist_equal_normalized": bool(q.artist_norm and q.artist_norm == a.artist_norm),
                "album_equal_normalized": bool(q.album_norm and q.album_norm == a.album_norm),
                "track_number_equal": q.track_number is not None and q.track_number == a.track_number,
                "isrc_equal": bool(q.isrc and a.isrc and q.isrc == a.isrc),
                "isrc_conflict": bool(q.isrc and a.isrc and q.isrc != a.isrc),
            }
        return payload

    def review_payload_markdown(self, payload: dict) -> str:
        q = payload.get("qobuz") or {}
        a = payload.get("amazon") or {}
        sig = payload.get("signals") or {}
        rid = payload.get("review_id") or "REVIEW"
        lines = [
            f"## {rid} — {payload.get('status', '-')}",
            "",
            f"- **Automatische Methode:** {payload.get('match_method', '-')}",
            f"- **Δ Laufzeit:** {payload.get('duration_delta_s') if payload.get('duration_delta_s') is not None else '-'} s",
            f"- **Hinweis des Programms:** {payload.get('automatic_note', '-')}",
            f"- **Bisherige persönliche Entscheidung:** {payload.get('saved_decision', '-')}",
            "",
        ]
        if q:
            lines += [
                "### Qobuz",
                f"- Titel: {q.get('title')}",
                f"- Artist: {q.get('artist')}",
                f"- Album: {q.get('album')}",
                f"- Track: {q.get('track')}",
                f"- ISRC: {q.get('isrc') or '-'}",
                f"- Laufzeit: {q.get('duration_s')} s",
                f"- Audio: {q.get('bit_depth')} Bit / {q.get('sample_rate_hz')} Hz / {q.get('flac_kbit_s')} kbit/s FLAC",
                f"- Lyrics: {q.get('lyrics')}",
                f"- Datei: `{q.get('path')}`",
                "",
            ]
        if a:
            lines += [
                "### Amazon",
                f"- Titel: {a.get('title')}",
                f"- Artist: {a.get('artist')}",
                f"- Album: {a.get('album')}",
                f"- Track: {a.get('track')}",
                f"- ISRC: {a.get('isrc') or '-'}",
                f"- Laufzeit: {a.get('duration_s')} s",
                f"- Audio: {a.get('bit_depth')} Bit / {a.get('sample_rate_hz')} Hz / {a.get('flac_kbit_s')} kbit/s FLAC",
                f"- Lyrics: {a.get('lyrics')}",
                f"- Zensur-Indiz: {a.get('lyrics_censor_suspicion')}",
            ]
            if a.get("lyrics_preview"):
                lines += ["- Kurzer Lyrics-Ausschnitt:", "```text", a.get("lyrics_preview"), "```"]
            lines += [f"- Datei: `{a.get('path')}`", ""]
        if sig:
            lines += [
                "### Automatische Vergleichssignale",
                f"- Titel normalisiert gleich: {sig.get('title_equal_normalized')}",
                f"- Artist normalisiert gleich: {sig.get('artist_equal_normalized')}",
                f"- Album normalisiert gleich: {sig.get('album_equal_normalized')}",
                f"- Tracknummer gleich: {sig.get('track_number_equal')}",
                f"- ISRC gleich: {sig.get('isrc_equal')}",
                f"- ISRC-Konflikt: {sig.get('isrc_conflict')}",
                "",
                "**Gewünschte Entscheidung:** `APPROVE_PAIR`, `REJECT_PAIR` oder `NEEDS_REVIEW`.",
            ]
        else:
            lines += [
                "**Gewünschte Entscheidung:** `INCLUDE_AMAZON`, `IGNORE_AMAZON` oder `ASK_USER`.",
                "Bei Amazon-only ist die Auswahl teilweise eine persönliche Bibliotheksentscheidung; nicht so tun, als könne sie allein aus Metadaten objektiv entschieden werden.",
            ]
        return "\n".join(lines)

    def copy_review_entry(self, match: Match):
        payload = self.review_payload(match, "AUSGEWÄHLT")
        self.copy_to_clipboard(self.review_payload_markdown(payload))
        self.status_label.config(text="Review-Daten des ausgewählten Titels wurden in die Zwischenablage kopiert.")

    def export_ai_review_package(self):
        if not self.matches:
            messagebox.showinfo(APP_NAME, "Bitte zuerst eine Analyse durchführen.")
            return
        review_matches = [
            m for m in self.matches
            if m.status in ("VARIANTENPRÜFUNG", "ZEITPRÜFUNG", "AMAZON-ONLY")
        ]
        if not review_matches:
            messagebox.showinfo(APP_NAME, "Es gibt aktuell keine offenen persönlichen Entscheidungen.")
            return

        filename = filedialog.asksaveasfilename(
            title="KI-Review-Paket speichern",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md")],
            initialfile="lyrics_merge_KI_review.md",
        )
        if not filename:
            return
        md_path = Path(filename)
        json_path = md_path.with_suffix(".json")

        payloads = [self.review_payload(m, f"R{i:03d}") for i, m in enumerate(review_matches, start=1)]
        header = (
            f"# {APP_NAME} {VERSION} — KI-Review-Paket\n\n"
            f"Erstellt: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "## Aufgabe für die prüfende KI\n\n"
            "Bewerte ausschließlich die unten aufgeführten offenen Fälle. Sei konservativ. "
            "Bei Qobuz/Amazon-Paaren darf `APPROVE_PAIR` nur empfohlen werden, wenn die Daten stark dafür sprechen, "
            "dass es dieselbe konkrete Audiofassung ist und synchronisierte Lyrics zeitlich plausibel übertragbar sind. "
            "Unterschiedliche ISRC, Clean/Explicit-, Radio-, Dialogue-, Film-/Extended- oder andere Versionshinweise sind ernst zu nehmen. "
            "Ein fast identischer Titel allein reicht nicht. Wenn die Daten nicht reichen: `NEEDS_REVIEW`.\n\n"
            "Bei Amazon-only-Fällen ist die Frage `INCLUDE_AMAZON` vs. `IGNORE_AMAZON` teilweise eine persönliche Bibliotheksentscheidung. "
            "Wenn sich das nicht objektiv aus den Daten ergibt, antworte `ASK_USER`.\n\n"
            "Bitte gib am Ende zusätzlich eine kompakte Tabelle `ID | Empfehlung | Begründung | Sicherheit` aus.\n\n"
            "---\n\n"
        )
        md_text = header + "\n\n---\n\n".join(self.review_payload_markdown(p) for p in payloads) + "\n"
        json_data = {
            "schema": 1,
            "app": APP_NAME,
            "version": VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "open_decisions": len(payloads),
            "items": payloads,
        }
        try:
            md_path.write_text(md_text, encoding="utf-8")
            json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo(
                "KI-Review exportiert",
                f"{len(payloads)} offene Entscheidungen exportiert.\n\nMarkdown für Chat/KI:\n{md_path}\n\nMaschinenlesbares JSON:\n{json_path}",
            )
        except Exception as exc:
            messagebox.showerror("KI-Review Export", str(exc))

    # ---------------- CSV ----------------
    def export_preview_csv(self):
        if not self.matches:
            messagebox.showinfo(APP_NAME, "Bitte zuerst eine Analyse durchführen."); return
        filename = filedialog.asksaveasfilename(title="Analyse exportieren", defaultextension=".csv", filetypes=[("CSV-Datei", "*.csv")], initialfile="lyrics_merge_analysis.csv")
        if not filename: return
        try:
            with open(filename, "w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow([
                    "Status", "Quelle", "Titel", "Artist", "Album", "Track", "ISRC", "Codec", "Bit",
                    "Samplerate_Hz", "FLAC_kbit_s", "PCM_kbit_s", "Kanäle", "Laufzeit_s",
                    "Dateigröße_Bytes", "Lyrics", "Match", "Delta_s", "Hinweis", "Entscheidung", "Qobuz", "Amazon",
                    "LUFS", "LRA", "TruePeak_dBTP", "Plausibilität",
                ])
                for m in self.matches:
                    t = display_track(m); q = qobuz_track(m); am = amazon_track(m)
                    a = self.get_cached_analysis(t.path) or {}
                    writer.writerow([
                        m.status, source_label(m), t.title, t.artist, t.album, t.track_number, t.isrc,
                        t.codec, t.bits_per_sample, t.sample_rate, t.audio_bitrate_kbps, t.pcm_bitrate_kbps,
                        t.channels, f"{t.duration:.3f}", t.file_size,
                        "SYNC" if t.lyrics_synced else ("JA" if t.lyrics else "NEIN"), m.method,
                        f"{m.duration_diff:.3f}" if m.duration_diff is not None else "", m.message,
                        self.decision_text_for_match(m),
                        str(q.path) if q else "", str(am.path) if am else "",
                        a.get("integrated_lufs", ""), a.get("lra_lu", ""), a.get("true_peak_dbfs", ""), a.get("plausibility", ""),
                    ])
            messagebox.showinfo(APP_NAME, f"Analyse exportiert:\n\n{filename}")
        except Exception as exc: messagebox.showerror("CSV-Export", str(exc))

    # ---------------- DEEP ANALYSIS CACHE ----------------
    def get_cached_analysis(self, path: Path):
        entry = self.analysis_cache.get(analysis_cache_key(path))
        if entry and cache_entry_valid(entry, path):
            return entry.get("analysis")
        return None

    def store_cached_analysis(self, path: Path, analysis: dict):
        fp = file_fingerprint(path)
        with self.cache_lock:
            self.analysis_cache[analysis_cache_key(path)] = {
                **fp,
                "analysis_schema": ANALYSIS_SCHEMA_VERSION,
                "analysis": analysis,
            }
            save_json_file(ANALYSIS_CACHE_FILE, self.analysis_cache)

    def spectrogram_cache_path(self, path: Path):
        fp = file_fingerprint(path)
        token = hashlib.sha1(f"{path.resolve()}|{fp['size']}|{fp['mtime_ns']}".encode("utf-8", "replace")).hexdigest()
        return ANALYSIS_CACHE_DIR / f"{token}.png"

    def ensure_ffmpeg(self):
        if self.ffmpeg_path and self.ffmpeg_path.is_file():
            return True
        self.ffmpeg_path = find_ffmpeg(self.settings.get("ffmpeg_path", ""))
        if self.ffmpeg_path:
            self.update_ffmpeg_status(); return True
        if messagebox.askyesno("FFmpeg benötigt", "Für die Tiefenanalyse wurde FFmpeg nicht gefunden.\n\nJetzt ffmpeg.exe auswählen?"):
            self.choose_ffmpeg()
        return bool(self.ffmpeg_path and self.ffmpeg_path.is_file())

    def deep_analyze_selected(self):
        if not self.selected_match: return
        self.deep_analyze_paths([display_track(self.selected_match).path])

    def deep_analyze_selected_both(self):
        if not self.selected_match: return
        q = qobuz_track(self.selected_match); a = amazon_track(self.selected_match)
        paths = []
        for t in (q, a):
            if t and t.path not in paths:
                paths.append(t.path)
        if paths:
            self.deep_analyze_paths(paths)

    def deep_analyze_visible(self):
        paths = []
        seen = set()
        for m in self.visible_matches():
            p = display_track(m).path
            if p not in seen:
                paths.append(p); seen.add(p)
        if not paths:
            messagebox.showinfo(APP_NAME, "Keine sichtbaren Titel."); return
        if not messagebox.askyesno("Alle sichtbaren analysieren", f"{len(paths)} Titel tief analysieren?\n\nDas kann je nach Anzahl einige Zeit dauern. Bereits gültig gecachte Titel werden übersprungen."):
            return
        self.deep_analyze_paths(paths)

    def deep_analyze_paths(self, paths):
        if self.current_deep_job:
            messagebox.showinfo(APP_NAME, "Es läuft bereits eine Tiefenanalyse."); return
        if not self.ensure_ffmpeg(): return
        self.cancel_event.clear(); self.current_deep_job = True; self.set_busy(True); self.apply_button.config(state="disabled")
        threading.Thread(target=self.deep_worker, args=(list(paths),), daemon=True).start()

    def deep_worker(self, paths):
        try:
            total = len(paths); done = 0
            for path in paths:
                if self.cancel_event.is_set(): break
                cached = self.get_cached_analysis(path)
                if cached:
                    done += 1; self.set_progress(done, total, f"Gecacht: {path.name}"); continue
                self.set_progress(done, total, f"Tiefenanalyse: {path.name}")
                analysis = deep_analyze_track(self.ffmpeg_path, path, self.spectrogram_cache_path(path))
                self.store_cached_analysis(path, analysis)
                done += 1; self.set_progress(done, total, f"Analysiert: {path.name}")
            self.root.after(0, self.deep_finished)
        except Exception as exc:
            self.root.after(0, lambda exc=exc: messagebox.showerror("Tiefenanalyse", str(exc)))
        finally:
            self.current_deep_job = False
            self.root.after(0, lambda: (self.set_busy(False), self.restore_apply_state()))

    def deep_finished(self):
        self.status_label.config(text="Tiefenanalyse abgeschlossen.")
        self.load_cached_analysis_for_selected()
        self.populate_match_tab(self.selected_match) if self.selected_match else None

    def load_cached_analysis_for_selected(self):
        if not self.selected_match:
            return
        a = self.get_cached_analysis(display_track(self.selected_match).path)
        if not a:
            for var in self.deep_vars.values(): var.set("-")
            self.deep_status_var.set("Noch nicht analysiert")
            self.lossy_note_var.set("Spektralbasierte Lossy-Erkennung ist eine Heuristik, kein Beweis.")
            self.render_spectrum(None); return
        self.deep_status_var.set(f"Analyse vom {a.get('analyzed_at', '-')} · Cache gültig")
        self.deep_vars["lufs"].set(f"{self.fmt_num(a.get('integrated_lufs'), 1)} LUFS")
        self.deep_vars["lra"].set(f"{self.fmt_num(a.get('lra_lu'), 1)} LU")
        self.deep_vars["true_peak"].set(self.fmt_dbtp(a.get("true_peak_dbfs")))
        self.deep_vars["sample_peak"].set(self.fmt_db(a.get("sample_peak_dbfs")))
        self.deep_vars["rms"].set(self.fmt_db(a.get("rms_dbfs")))
        self.deep_vars["crest"].set(f"{self.fmt_num(a.get('crest_factor_db'), 2)} dB")
        self.deep_vars["clipping"].set(str(a.get("clipping_samples", "-")))
        self.deep_vars["near_clip"].set(f"{a.get('near_clip_samples', '-')} ({self.fmt_num(a.get('near_clip_percent'), 4)} %)")
        self.deep_vars["dc"].set(self.fmt_num(a.get("dc_offset"), 8))
        self.deep_vars["plausibility"].set(a.get("plausibility", "-"))
        self.deep_vars["confidence"].set(a.get("confidence", "-"))
        highest = a.get("highest_active_hz"); cutoff = a.get("cutoff_hz")
        self.deep_vars["highest"].set(f"{highest / 1000:.2f} kHz" if highest is not None else "-")
        self.deep_vars["cutoff"].set(f"~{cutoff / 1000:.2f} kHz" if cutoff is not None else "Kein klarer Cutoff")
        self.lossy_note_var.set(a.get("note", "-"))
        self.render_spectrum(a)

    @staticmethod
    def fmt_num(value, digits=2):
        return "-" if value is None else f"{float(value):.{digits}f}"

    @staticmethod
    def fmt_db(value):
        return "-" if value is None else f"{float(value):.2f} dBFS"

    @staticmethod
    def fmt_dbtp(value):
        return "-" if value is None else f"{float(value):.2f} dBTP"

    # ---------------- CHARTS ----------------
    def render_spectrum(self, analysis):
        if self.chart_canvas is not None:
            try:
                self.chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.chart_canvas = None

        for child in self.spectrum_container.winfo_children():
            child.destroy()

        if not analysis:
            ttk.Label(
                self.spectrum_container,
                text="Noch keine Tiefenanalyse.\n\nIm Tab „Audio“ auf „Audio tief analysieren“ klicken.",
                style="Panel.TLabel",
                justify="center",
            ).pack(fill="both", expand=True, pady=30)
            return

        # Waveform + Spektrum nutzen den Hauptbereich. Das Spektrogramm bekommt
        # darunter bewusst deutlich mehr Platz als in 2.4.
        self.spectrum_container.columnconfigure(0, weight=1)
        self.spectrum_container.rowconfigure(0, weight=3)
        self.spectrum_container.rowconfigure(1, weight=2)

        plots_frame = ttk.Frame(self.spectrum_container, style="Panel.TFrame")
        plots_frame.grid(row=0, column=0, sticky="nsew")

        fig = Figure(figsize=(7.2, 4.4), dpi=95, facecolor=COLORS["panel"])
        ax1 = fig.add_subplot(211)
        ax2 = fig.add_subplot(212)

        for ax in (ax1, ax2):
            ax.set_facecolor(COLORS["field"])
            ax.tick_params(colors=COLORS["muted"], labelsize=8)
            for spine in ax.spines.values():
                spine.set_color(COLORS["border"])

        wmin = np.asarray(analysis.get("wave_min", []), dtype=float)
        wmax = np.asarray(analysis.get("wave_max", []), dtype=float)
        if wmin.size and wmax.size:
            x = np.linspace(0, 1, len(wmin))
            ax1.fill_between(x, wmin, wmax, alpha=0.8)
            ax1.set_ylim(-1.05, 1.05)
        ax1.set_title("Waveform", color=COLORS["fg"], fontsize=10)
        ax1.set_xlabel("Track-Verlauf", color=COLORS["fg"], fontsize=8)
        ax1.set_ylabel("Amplitude", color=COLORS["fg"], fontsize=8)

        freqs = np.asarray(analysis.get("spectrum_freqs", []), dtype=float)
        db = np.asarray(analysis.get("spectrum_db", []), dtype=float)
        if freqs.size and db.size:
            ax2.plot(freqs / 1000.0, db, linewidth=1.0)
            ax2.set_ylim(-90, 5)
        ax2.set_title("Mittleres Frequenzspektrum", color=COLORS["fg"], fontsize=10)
        ax2.set_xlabel("kHz", color=COLORS["fg"], fontsize=8)
        ax2.set_ylabel("dB relativ", color=COLORS["fg"], fontsize=8)

        fig.tight_layout(pad=1.2)
        self.chart_canvas = FigureCanvasTkAgg(fig, master=plots_frame)
        self.chart_canvas.draw()
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)

        spec_frame = ttk.Frame(self.spectrum_container, style="Panel.TFrame")
        spec_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        spec_path = analysis.get("spectrogram_path")
        if spec_path and Path(spec_path).is_file():
            try:
                with Image.open(spec_path) as img:
                    img = img.convert("RGB")
                    # Genug Details, aber nicht größer als der übliche Sidebar-Bereich.
                    img.thumbnail((920, 300), Image.LANCZOS)
                    self.spectrogram_ref = ImageTk.PhotoImage(img.copy())
                spec_label = ttk.Label(spec_frame, image=self.spectrogram_ref, style="Panel.TLabel", anchor="center")
                spec_label.pack(fill="both", expand=True)
            except Exception:
                ttk.Label(spec_frame, text="Spektrogramm konnte nicht geladen werden.", style="Muted.TLabel").pack(pady=12)

    # ---------------- MATCH TAB ----------------
    def populate_match_tab(self, match: Optional[Match]):
        self.compare_tree.delete(*self.compare_tree.get_children())
        if not match:
            self.match_summary_var.set("Kein Track ausgewählt."); return
        q, a = qobuz_track(match), amazon_track(match)
        if match.duration_diff is not None:
            summary = f"{match.status} · {match.method} · Δ {match.duration_diff:.3f} s"
        else:
            summary = f"{match.status} · {source_label(match)}"
        if match.message:
            summary += f"\n{match.message}"
        self.match_summary_var.set(summary)
        missing = "—"

        def val(track, fn):
            return fn(track) if track else missing

        rows = [
            ("Titel", q.title if q else missing, a.title if a else missing),
            ("Artist", q.artist if q else missing, a.artist if a else missing),
            ("Album", q.album if q else missing, a.album if a else missing),
            ("Track", str(q.track_number) if q and q.track_number is not None else missing, str(a.track_number) if a and a.track_number is not None else missing),
            ("Bit-Tiefe", val(q, lambda x: f"{x.bits_per_sample} Bit"), val(a, lambda x: f"{x.bits_per_sample} Bit")),
            ("Samplerate", val(q, lambda x: format_samplerate(x.sample_rate)), val(a, lambda x: format_samplerate(x.sample_rate))),
            ("FLAC-Bitrate", val(q, lambda x: f"{x.audio_bitrate_kbps} kbps"), val(a, lambda x: f"{x.audio_bitrate_kbps} kbps")),
            ("PCM", val(q, lambda x: f"{x.pcm_bitrate_kbps} kbps"), val(a, lambda x: f"{x.pcm_bitrate_kbps} kbps")),
            ("Laufzeit", val(q, lambda x: f"{x.duration:.3f} s"), val(a, lambda x: f"{x.duration:.3f} s")),
            ("ISRC", (q.isrc or "-") if q else missing, (a.isrc or "-") if a else missing),
            (
                "Lyrics",
                ("Sync" if q.lyrics_synced else ("Ja" if q.lyrics else "Nein")) if q else missing,
                ("Sync" if a.lyrics_synced else ("Ja" if a.lyrics else "Nein")) if a else missing,
            ),
        ]
        qa = self.get_cached_analysis(q.path) if q else None
        aa = self.get_cached_analysis(a.path) if a else None
        if qa or aa:
            rows += [
                ("LUFS", f"{self.fmt_num(qa.get('integrated_lufs'), 1)}" if qa else missing, f"{self.fmt_num(aa.get('integrated_lufs'), 1)}" if aa else missing),
                ("True Peak", self.fmt_dbtp(qa.get("true_peak_dbfs")) if qa else missing, self.fmt_dbtp(aa.get("true_peak_dbfs")) if aa else missing),
                ("Cutoff", self.cutoff_text(qa) if qa else missing, self.cutoff_text(aa) if aa else missing),
                ("Plausibilität", qa.get("plausibility", "-") if qa else missing, aa.get("plausibility", "-") if aa else missing),
            ]
        for row in rows:
            self.compare_tree.insert("", "end", values=row)

    @staticmethod
    def cutoff_text(a):
        if not a: return "-"
        c = a.get("cutoff_hz")
        return f"~{c / 1000:.2f} kHz" if c is not None else "kein klarer"

    # ---------------- SAMSUNG MUSIC EXPORT ----------------
    def start_samsung_export(self):
        try:
            qobuz_root, amazon_root, export_root, max_diff = self.validate_samsung_export_paths()
        except Exception as exc:
            messagebox.showerror("Samsung Music Export", str(exc))
            return

        self.set_progress(0, 1, "Samsung Export: Quellen werden vorbereitet...")

        try:
            jobs = build_samsung_export_jobs(
                qobuz_root,
                amazon_root,
                max_duration_diff=max_diff,
                review_data=self.review_decisions,
                include_unreviewed_amazon_only=self.include_unreviewed_amazon_only_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("Samsung Music Export", f"Exportliste konnte nicht erstellt werden:\n\n{exc}")
            return

        if not jobs:
            messagebox.showinfo(
                APP_NAME,
                "In den verfügbaren Qobuz-/Amazon-Ordnern wurden keine exportierbaren FLAC-Dateien gefunden.",
            )
            return

        qobuz_jobs = sum(1 for job in jobs if job["audio_source_kind"] == "Qobuz")
        amazon_jobs = sum(1 for job in jobs if job["audio_source_kind"] == "Amazon")
        amazon_lyrics_for_qobuz = sum(
            1
            for job in jobs
            if job["audio_source_kind"] == "Qobuz" and job["lyrics_source_kind"] == "Amazon"
        )
        lrc_jobs = sum(1 for job in jobs if job["synced_lyrics"])
        multidisc_jobs = sum(1 for job in jobs if job.get("metadata_overrides", {}).get("flatten_multidisc"))
        multidisc_albums = len({job["relative"].parent for job in jobs if job.get("metadata_overrides", {}).get("flatten_multidisc")})

        source_summary = []
        if qobuz_root:
            source_summary.append(f"Qobuz: {qobuz_root}")
        if amazon_root:
            source_summary.append(f"Amazon: {amazon_root}")

        if not messagebox.askyesno(
            "Samsung Music Export",
            (
                f"{len(jobs)} FLACs werden für Samsung Music geprüft.\n\n"
                "Automatische Quellenwahl:\n"
                "• Qobuz wird als Audioquelle IMMER bevorzugt\n"
                "• Fehlen Qobuz-Sync-Lyrics, darf Amazon die LRC liefern\n"
                "• Amazon-only Tracks werden direkt aus Amazon exportiert\n"
                "• Embedded Lyrics werden NUR aus der Export-Kopie entfernt\n"
                "• LRC-Sanitizer entfernt problematische nackte End-Timestamps\n"
                "• Mehrdisc-Alben werden nur in der Samsung-Kopie fortlaufend nummeriert\n"
                "• Kein Audio-Encoding\n"
                "• Audioframes werden per SHA-256 verifiziert\n"
                "• Unveränderte Exporte werden übersprungen\n\n"
                f"Audio aus Qobuz: {qobuz_jobs}\n"
                f"Audio aus Amazon-Fallback: {amazon_jobs}\n"
                f"Qobuz-Audio + Amazon-LRC: {amazon_lyrics_for_qobuz}\n"
                f"Tracks mit LRC: {lrc_jobs}\n"
                f"Mehrdisc-Alben erkannt: {multidisc_albums} ({multidisc_jobs} Tracks)\n\n"
                + "\n".join(source_summary)
                + f"\n\nExportziel:\n{export_root}\n\nFortfahren?"
            ),
        ):
            return

        self.save_settings()
        self.cancel_event.clear()
        self.set_busy(True)
        self.apply_button.config(state="disabled")
        threading.Thread(
            target=self.samsung_export_worker,
            args=(export_root, jobs),
            daemon=True,
        ).start()

    def samsung_export_worker(self, export_root: Path, jobs: list[dict]):
        written = 0
        lrc_written = 0
        no_sync = 0
        skipped = 0
        errors = 0
        integrity_ok = 0
        stale_removed = 0
        qobuz_audio = 0
        amazon_audio = 0
        amazon_lyrics_for_qobuz = 0
        lrc_empty_removed = 0
        lrc_reordered = 0
        lrc_duplicates_removed = 0
        multidisc_metadata_changed = 0
        report_rows = []

        try:
            for index, job in enumerate(jobs, start=1):
                if self.cancel_event.is_set():
                    break

                source_path: Path = job["audio_source"]
                relative: Path = job["relative"]
                synced_lyrics: str = job["synced_lyrics"]
                audio_source_kind: str = job["audio_source_kind"]
                lyrics_source_kind: str = job["lyrics_source_kind"]
                lyrics_source: Optional[Path] = job["lyrics_source"]
                metadata_overrides: dict = job.get("metadata_overrides") or {}

                export_flac = export_root / relative
                lrc_path = export_flac.with_suffix(".lrc")

                self.set_progress(
                    index - 1,
                    len(jobs),
                    f"Samsung Export [{audio_source_kind}]: {relative}",
                )

                try:
                    lrc_preview, _lrc_preview_stats = sanitize_lrc_text(synced_lyrics)
                    lyrics_digest = hashlib.sha256(lrc_preview.encode("utf-8")).hexdigest() if lrc_preview else ""
                    metadata_digest = samsung_metadata_digest(metadata_overrides)
                    cache_key = samsung_export_cache_key(source_path, export_root, relative)

                    with self.samsung_cache_lock:
                        cache_entry = self.samsung_export_cache.get(cache_key)

                    if samsung_export_entry_valid(
                        cache_entry,
                        source_path,
                        export_flac,
                        lrc_path,
                        lyrics_digest,
                        metadata_digest,
                    ):
                        skipped += 1
                        if not synced_lyrics:
                            no_sync += 1

                        report_rows.append((
                            "SKIP",
                            audio_source_kind,
                            str(source_path),
                            lyrics_source_kind,
                            str(lyrics_source) if lyrics_source else "",
                            str(export_flac),
                            str(lrc_path) if synced_lyrics else "",
                            "Unverändert",
                            "", "", "", "",
                            (f"{metadata_overrides.get('track_number')}/{metadata_overrides.get('track_total')}" if metadata_overrides else ""),
                        ))
                        self.set_progress(index, len(jobs))
                        continue

                    result = write_samsung_export_copy(
                        source_path,
                        export_flac,
                        synced_lyrics,
                        verify_audio=True,
                        metadata_overrides=metadata_overrides,
                    )

                    written += 1
                    integrity_ok += 1

                    if audio_source_kind == "Qobuz":
                        qobuz_audio += 1
                    else:
                        amazon_audio += 1

                    if audio_source_kind == "Qobuz" and lyrics_source_kind == "Amazon":
                        amazon_lyrics_for_qobuz += 1

                    if result["lrc_written"]:
                        lrc_written += 1
                        lstats = result.get("lrc_stats") or {}
                        lrc_empty_removed += int(lstats.get("empty_events_removed", 0))
                        lrc_reordered += int(lstats.get("reordered_text_events", 0))
                        lrc_duplicates_removed += int(lstats.get("duplicates_removed", 0))
                    else:
                        no_sync += 1

                    if result.get("metadata_changed"):
                        multidisc_metadata_changed += 1

                    if result["stale_lrc_removed"]:
                        stale_removed += 1

                    source_fp = samsung_source_fingerprint(source_path)
                    new_entry = {
                        "schema": SAMSUNG_EXPORT_SCHEMA_VERSION,
                        "source_size": source_fp["size"],
                        "source_mtime_ns": source_fp["mtime_ns"],
                        "source_ctime_ns": source_fp["ctime_ns"],
                        "lyrics_sha256": lyrics_digest,
                        "metadata_sha256": metadata_digest,
                        "has_lrc": bool(lrc_preview),
                        "audio_source_kind": audio_source_kind,
                        "lyrics_source_kind": lyrics_source_kind,
                        "export_size": result["export_size"],
                        "export_mtime_ns": result["export_mtime_ns"],
                        "lrc_size": result["lrc_size"],
                        "lrc_mtime_ns": result["lrc_mtime_ns"],
                    }

                    with self.samsung_cache_lock:
                        self.samsung_export_cache[cache_key] = new_entry

                    if synced_lyrics:
                        status = f"FLAC + LRC | Audio: {audio_source_kind} | Lyrics: {lyrics_source_kind}"
                    else:
                        status = f"FLAC ohne Sync-Lyrics | Audio: {audio_source_kind}"

                    report_rows.append((
                        "OK",
                        audio_source_kind,
                        str(source_path),
                        lyrics_source_kind,
                        str(lyrics_source) if lyrics_source else "",
                        str(export_flac),
                        str(lrc_path) if synced_lyrics else "",
                        status,
                        (result.get("lrc_stats") or {}).get("final_text_events", ""),
                        (result.get("lrc_stats") or {}).get("empty_events_removed", ""),
                        (result.get("lrc_stats") or {}).get("reordered_text_events", ""),
                        (result.get("lrc_stats") or {}).get("duplicates_removed", ""),
                        (f"{metadata_overrides.get('track_number')}/{metadata_overrides.get('track_total')}" if metadata_overrides else ""),
                    ))

                except Exception as exc:
                    errors += 1
                    report_rows.append((
                        "FEHLER",
                        audio_source_kind,
                        str(source_path),
                        lyrics_source_kind,
                        str(lyrics_source) if lyrics_source else "",
                        str(export_flac),
                        "",
                        str(exc),
                        "", "", "", "", "",
                    ))

                self.set_progress(index, len(jobs))

            with self.samsung_cache_lock:
                save_json_file(SAMSUNG_EXPORT_CACHE_FILE, self.samsung_export_cache)

            LOG_DIR.mkdir(exist_ok=True)
            report_path = LOG_DIR / f"samsung_export_{time.strftime('%Y-%m-%d_%H-%M-%S')}.csv"

            with report_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Status",
                    "Audioquelle",
                    "Audio-Quelldatei",
                    "Lyrics-Quelle",
                    "Lyrics-Quelldatei",
                    "Samsung-FLAC",
                    "LRC",
                    "Hinweis",
                    "LRC-Text-Events",
                    "Leere LRC-Events entfernt",
                    "LRC-Reihenfolge korrigiert",
                    "LRC-Dubletten entfernt",
                    "Samsung-Tracknummer (Multi-Disc)",
                ])
                writer.writerows(report_rows)

            self.root.after(
                0,
                lambda: self.finish_samsung_export(
                    export_root,
                    written,
                    lrc_written,
                    no_sync,
                    skipped,
                    errors,
                    integrity_ok,
                    stale_removed,
                    qobuz_audio,
                    amazon_audio,
                    amazon_lyrics_for_qobuz,
                    lrc_empty_removed,
                    lrc_reordered,
                    lrc_duplicates_removed,
                    multidisc_metadata_changed,
                    report_path,
                ),
            )

        finally:
            self.root.after(0, lambda: (self.set_busy(False), self.restore_apply_state()))

    def finish_samsung_export(
        self,
        export_root,
        written,
        lrc_written,
        no_sync,
        skipped,
        errors,
        integrity_ok,
        stale_removed,
        qobuz_audio,
        amazon_audio,
        amazon_lyrics_for_qobuz,
        lrc_empty_removed,
        lrc_reordered,
        lrc_duplicates_removed,
        multidisc_metadata_changed,
        report_path,
    ):
        text = (
            "Samsung Music Export abgeschlossen.\n\n"
            f"FLACs neu/aktualisiert: {written}\n"
            f"LRC-Dateien geschrieben: {lrc_written}\n"
            f"Tracks ohne Sync-Lyrics: {no_sync}\n"
            f"Unverändert übersprungen: {skipped}\n"
            f"Audiointegrität bestätigt: {integrity_ok}\n"
            f"Veraltete LRC entfernt: {stale_removed}\n"
            f"Fehler: {errors}\n\n"
            f"Neu/aktualisiert mit Qobuz-Audio: {qobuz_audio}\n"
            f"Neu/aktualisiert mit Amazon-Audio: {amazon_audio}\n"
            f"Qobuz-Audio + Amazon-Lyrics: {amazon_lyrics_for_qobuz}\n\n"
            f"LRC-Sanitizer: {lrc_empty_removed} leere Events entfernt, "
            f"{lrc_reordered} Text-Events umsortiert, {lrc_duplicates_removed} Dubletten entfernt\n"
            f"Multi-Disc-Metadaten angepasst: {multidisc_metadata_changed} FLACs\n\n"
            f"Exportziel:\n{export_root}\n\n"
            f"Report:\n{report_path}"
        )
        messagebox.showinfo("Samsung Music Export", text)
        self.status_label.config(
            text=(
                f"Samsung Export fertig: {written} aktualisiert, "
                f"{skipped} übersprungen, {errors} Fehler."
            )
        )

    # ---------------- SAMSUNG EXPORT REPARIEREN ----------------
    def start_samsung_repair(self):
        export_text = self.samsung_export_var.get().strip()
        if not export_text:
            messagebox.showerror("Samsung Export reparieren", "Bitte zuerst den vorhandenen Samsung-Music-Exportordner auswählen.")
            return
        export_root = Path(export_text).expanduser().resolve()
        if not export_root.is_dir():
            messagebox.showerror("Samsung Export reparieren", f"Exportordner nicht gefunden:\n\n{export_root}")
            return

        lrc_count = sum(1 for p in export_root.rglob("*.lrc") if p.is_file())
        flac_count = sum(1 for p in export_root.rglob("*.flac") if p.is_file())
        if not messagebox.askyesno(
            "Samsung Export reparieren",
            (
                f"Vorhandenen Samsung-Export prüfen und reparieren?\n\n"
                f"FLACs: {flac_count}\nLRCs: {lrc_count}\n\n"
                "Automatisch und nur im Samsung-Export:\n"
                "• nackte/leer getimte LRC-Events entfernen\n"
                "• echte Lyrics chronologisch und Samsung-kompatibel schreiben\n"
                "• exakte LRC-Dubletten entfernen\n"
                "• Mehrdisc-/widersprüchliche Tracknummern sicher auf 01..N flatten\n"
                "• FLAC-Audioframes bei Tag-Änderungen per SHA-256 verifizieren\n"
                "• alle geänderten Dateien zusätzlich als kleinen Repair-Patch sammeln\n\n"
                "Qobuz-/Amazon-Master werden NICHT angefasst."
            ),
        ):
            return

        self.cancel_event.clear()
        self.set_busy(True)
        self.apply_button.config(state="disabled")
        threading.Thread(target=self.samsung_repair_worker, args=(export_root,), daemon=True).start()

    def samsung_repair_worker(self, export_root: Path):
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        patch_root = APP_DIR / "Exports" / f"Samsung_Repair_Patch_{timestamp}"
        report_path = LOG_DIR / f"samsung_repair_{timestamp}.csv"
        LOG_DIR.mkdir(exist_ok=True)
        patch_root.mkdir(parents=True, exist_ok=True)

        lrc_files = sorted((p for p in export_root.rglob("*.lrc") if p.is_file()), key=lambda p: str(p).casefold())
        actions, unreadable = discover_multidisc_repair_actions(export_root)
        total_work = len(lrc_files) + len(actions)
        done = 0
        report_rows = []
        changed_lrc = 0
        empty_removed = 0
        reordered = 0
        duplicates_removed = 0
        changed_flac = 0
        integrity_ok = 0
        errors = len(unreadable)
        multidisc_albums = set()

        for item in unreadable:
            report_rows.append(("FEHLER", "FLAC", str(item["path"]), "", "", "", "", item["error"]))

        try:
            for lrc in lrc_files:
                if self.cancel_event.is_set(): break
                self.set_progress(done, max(total_work, 1), f"Samsung Repair – LRC: {lrc.relative_to(export_root)}")
                try:
                    result = repair_lrc_file(lrc)
                    stats = result["stats"]
                    empty_removed += int(stats.get("empty_events_removed", 0))
                    reordered += int(stats.get("reordered_text_events", 0))
                    duplicates_removed += int(stats.get("duplicates_removed", 0))
                    if result["changed"]:
                        changed_lrc += 1
                        rel = lrc.relative_to(export_root)
                        patch = patch_root / rel
                        patch.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(lrc, patch)
                    report_rows.append((
                        "GEÄNDERT" if result["changed"] else "OK",
                        "LRC",
                        str(lrc),
                        stats.get("final_text_events", 0),
                        stats.get("empty_events_removed", 0),
                        stats.get("reordered_text_events", 0),
                        stats.get("duplicates_removed", 0),
                        ("Timeline OK" if stats.get("timeline_ok") else "Timeline prüfen"),
                    ))
                except Exception as exc:
                    errors += 1
                    report_rows.append(("FEHLER", "LRC", str(lrc), "", "", "", "", str(exc)))
                done += 1
                self.set_progress(done, max(total_work, 1))

            for action in actions:
                if self.cancel_event.is_set(): break
                flac = action["path"]
                self.set_progress(done, max(total_work, 1), f"Samsung Repair – Multi-Disc: {flac.relative_to(export_root)}")
                try:
                    result = repair_flac_metadata_atomic(flac, action["metadata_overrides"], verify_audio=True)
                    if result["changed"]:
                        changed_flac += 1
                        integrity_ok += 1
                        multidisc_albums.add(str(action["album_dir"]))
                        rel = flac.relative_to(export_root)
                        patch = patch_root / rel
                        patch.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(flac, patch)
                    report_rows.append((
                        "GEÄNDERT" if result["changed"] else "OK",
                        "FLAC-METADATEN",
                        str(flac),
                        "", "", "", "",
                        (
                            f"{('Dateipräfix' if action['metadata_overrides'].get('flatten_reason') == 'filename_prefix' else 'Disc-Tags')} | "
                            f"Disc {action['metadata_overrides'].get('original_disc_number') if action['metadata_overrides'].get('original_disc_number') is not None else '-'} / "
                            f"Track {action['metadata_overrides'].get('original_track_number') if action['metadata_overrides'].get('original_track_number') is not None else '-'} "
                            f"-> Samsung Track {action['metadata_overrides']['track_number']}/{action['metadata_overrides']['track_total']}"
                        ),
                    ))
                except Exception as exc:
                    errors += 1
                    report_rows.append(("FEHLER", "FLAC-METADATEN", str(flac), "", "", "", "", str(exc)))
                done += 1
                self.set_progress(done, max(total_work, 1))

            with report_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Status", "Typ", "Datei", "Text-Events", "Leere Events entfernt", "Umsortiert", "Dubletten entfernt", "Hinweis"])
                writer.writerows(report_rows)

            # Kein leerer Patch-Ordner zurücklassen.
            patch_has_files = any(p.is_file() for p in patch_root.rglob("*"))
            if not patch_has_files:
                try: shutil.rmtree(patch_root)
                except OSError: pass
                patch_root_value = None
            else:
                patch_root_value = patch_root

            self.root.after(0, lambda: self.finish_samsung_repair(
                export_root, patch_root_value, report_path, len(lrc_files), changed_lrc,
                empty_removed, reordered, duplicates_removed, len(multidisc_albums),
                changed_flac, integrity_ok, errors,
            ))
        finally:
            self.root.after(0, lambda: (self.set_busy(False), self.restore_apply_state()))

    def finish_samsung_repair(
        self, export_root, patch_root, report_path, lrc_scanned, changed_lrc,
        empty_removed, reordered, duplicates_removed, multidisc_albums,
        changed_flac, integrity_ok, errors,
    ):
        patch_text = str(patch_root) if patch_root else "Keine geänderten Dateien – kein Patch nötig."
        messagebox.showinfo(
            "Samsung Export reparieren",
            (
                "Samsung-Reparatur abgeschlossen.\n\n"
                f"LRCs geprüft: {lrc_scanned}\n"
                f"LRCs geändert: {changed_lrc}\n"
                f"Leere Timestamp-Events entfernt: {empty_removed}\n"
                f"Text-Events umsortiert: {reordered}\n"
                f"Exakte Dubletten entfernt: {duplicates_removed}\n\n"
                f"Album-Reihenfolgen repariert: {multidisc_albums}\n"
                f"FLAC-Metadaten geändert: {changed_flac}\n"
                f"Audiointegrität bestätigt: {integrity_ok}\n"
                f"Fehler: {errors}\n\n"
                f"Reparierter Export:\n{export_root}\n\n"
                f"Nur geänderte Dateien für das Handy:\n{patch_text}\n\n"
                f"Report:\n{report_path}"
            ),
        )
        self.status_label.config(text=f"Samsung Repair fertig: {changed_lrc} LRCs + {changed_flac} FLAC-Tags geändert, {errors} Fehler.")

    # ---------------- APPLY ----------------
    def start_apply(self):
        if not self.matches: return
        try: _, target, _ = self.validate_paths()
        except Exception as exc: messagebox.showerror("Fehler", str(exc)); return
        if target is None:
            messagebox.showinfo(APP_NAME, "Für 'Lyrics übernehmen' wird eine Qobuz-Master-Bibliothek benötigt."); return
        jobs = [m for m in self.matches if m.target_kind == "Qobuz" and m.status in ("BEREIT", "ÜBERSCHREIBEN", "MANUELL FREIGEGEBEN") and m.source]
        if not jobs: messagebox.showinfo(APP_NAME, "Keine Dateien zum Bearbeiten."); return
        verify_text = "AKTIV" if self.verify_var.get() else "DEAKTIVIERT"
        manual_count = sum(1 for m in jobs if m.status == "MANUELL FREIGEGEBEN")
        manual_note = (
            f"\nManuell freigegebene Varianten: {manual_count}\n"
            "Diese Paarungen wurden bewusst von dir freigegeben und umgehen nur die automatische Varianten-Sperre.\n"
            if manual_count else ""
        )
        if not messagebox.askyesno(
            "Lyrics übernehmen",
            f"{len(jobs)} Qobuz-FLACs werden bearbeitet.\n\n"
            f"Audio-Encoding: KEINES\nAudio-SHA-256: {verify_text}\n"
            f"{manual_note}\nFortfahren?",
        ):
            return
        self.save_settings(); self.cancel_event.clear(); self.set_busy(True); self.apply_button.config(state="disabled")
        threading.Thread(target=self.apply_worker, args=(target, jobs, self.overwrite_var.get(), self.verify_var.get(), self.backup_var.get()), daemon=True).start()

    def apply_worker(self, target_root, jobs, overwrite, verify_audio, backup):
        results = {}; backup_root = None
        if backup:
            backup_root = target_root.parent / (target_root.name + "__BACKUP_" + time.strftime("%Y-%m-%d_%H-%M-%S"))
        success = failed = 0
        try:
            for index, match in enumerate(jobs, start=1):
                if self.cancel_event.is_set(): break
                self.set_progress(index - 1, len(jobs), f"Bearbeite: {match.target.title}")
                try:
                    ok, msg = write_lyrics_atomic(match.target.path, match.source.lyrics, target_root, overwrite, verify_audio, backup_root)
                    success += int(ok); failed += int(not ok); results[str(match.target.path)] = msg
                except Exception as exc:
                    failed += 1; results[str(match.target.path)] = f"FEHLER: {exc}"
                self.set_progress(index, len(jobs))
            self.report_path = write_report(self.matches, results)
            self.root.after(0, lambda: self.finish_apply(success, failed, backup_root))
        finally:
            self.root.after(0, lambda: (self.set_busy(False), self.restore_apply_state()))

    def finish_apply(self, success, failed, backup_root):
        text = f"Fertig.\n\nErfolgreich: {success}\nFehler/übersprungen: {failed}\n"
        if backup_root: text += f"\nBackup:\n{backup_root}\n"
        if self.report_path: text += f"\nReport:\n{self.report_path}"
        messagebox.showinfo(APP_NAME, text); self.status_label.config(text=f"Fertig: {success} erfolgreich, {failed} Fehler."); self.start_preview()

    # ---------------- CANCEL / CLOSE ----------------
    def cancel(self):
        self.cancel_event.set(); self.status_label.config(text="Abbruch angefordert...")

    def on_close(self):
        self.cancel_event.set(); self.hide_tooltip(); self.save_settings(); self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = LyricsMergeGUI()
    app.run()
