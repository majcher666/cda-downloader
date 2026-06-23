"""
Logika wyciagania bezposrednich linkow do plikow wideo/audio ze strony CDA.pl,
pobierania ich na serwer i laczenia w jeden plik mp4.

CDA w wielu przypadkach NIE wstawia linku do pliku mp4/m3u8 do statycznego
HTML strony - adres jest ladowany dynamicznie przez JavaScript dopiero po
kliknięciu przycisku odtwarzania. Dodatkowo przed filmem czesto leci
reklama, ktorej plik wideo tez przechodzi przez siec i latwo go pomylic
z prawdziwym filmem.

Z obserwacji (na realnym przykladzie):
- prawdziwe pliki CDA sa hostowane na subdomenie "*.cda.pl", a nazwa pliku
  jest dlugim, hashowym ciagiem znakow (~30+ znakow); audio ma te sama
  nazwe z prefiksem "a_".
- REKLAMY rowniez bywaja hostowane na subdomenie "*.cda.pl", wiec samo
  sprawdzanie domeny NIE wystarcza. Roznica jest w nazwie pliku: reklamy
  maja krotkie, "ludzkie" nazwy (np. "postapo.mp4"), a prawdziwe pliki
  wideo - dlugie hashe.
- Surowe linki do plikow CDA daja 403 przy kliknięciu z innej strony (CDA
  sprawdza naglowek Referer) - wiec serwer sam pobiera pliki z prawidlowym
  Refererem i laczy je przez ffmpeg w jeden plik mp4 (bez przekodowania).

Przetwarzanie wielu linkow odbywa sie rownolegle (domyslnie maks. 5 naraz),
kazdy link w swojej wlasnej instancji przegladarki (watek).

Postep zglaszany jest przez `progress_cb(text, percent=None)`:
- `text`  - linia do dopisania w logu tego konkretnego linku,
- `percent` - opcjonalny ORIENTACYJNY postep calego pipeline'u (0-100),
  liczony na podstawie etapu (otwarcie strony, szukanie play, reklama,
  pobieranie wideo, pobieranie audio, laczenie ffmpeg). Gdy `percent` jest
  `None`, front aktualizuje log, ale nie przesuwa paska postepu.

`_process_single_url` opakowuje to w `tagged_cb`, ktory dodaje numer linku,
tak zeby wywolujacy (Flask) wiedzial, do ktorego panelu w UI przypisac dany
komunikat.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

from cda.shinden import resolve_cda_link

try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = None

DOWNLOAD_HEADERS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_MAX_WORKERS = 5

PLAY_SELECTORS = [
    ".pb-play-ico",
    "[class*='pb-play']",
    "text=Odtworz",
    "button[aria-label*='play' i]",
    ".vjs-big-play-button",
    ".player-play",
    ".cda-player",
    "video",
]

SKIP_AD_SELECTORS = [
    "text=Pomin reklame",
    "text=Pomin reklame",
    "text=Skip Ad",
    "text=Skip ad",
    ".skip-ad",
    "button:has-text('Pomin')",
    "[class*='skip']",
]

VIDEO_URL_HINTS = (".mp4", ".m3u8")
VIDEO_CONTENT_TYPE_PREFIX = "video/"
CDA_HOST_SUFFIX = "cda.pl"

MIN_REAL_FILENAME_LENGTH = 20

EXT_RE = re.compile(r"\.(mp4|m3u8)(\?.*)?$", re.IGNORECASE)
TITLE_SUFFIX_RE = re.compile(r"\s*[-|]\s*(www\.)?cda\.pl\s*$", re.IGNORECASE)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

FILENAME_LOCK = threading.Lock()

# Orientacyjne progi procentowe poszczegolnych etapow pipeline'u.
PCT_START = 2
PCT_PAGE_LOADED = 10
PCT_SEARCHING_PLAY = 15
PCT_CLICKED_PLAY = 20
PCT_AD_HANDLED = 25
PCT_FILES_FOUND = 30
PCT_VIDEO_END_WITH_AUDIO = 70
PCT_VIDEO_END_NO_AUDIO = 95
PCT_AUDIO_END = 90
PCT_MERGE = 95
PCT_DONE = 100


def _noop(_text: str, _percent=None) -> None:
    pass


def _looks_like_video(url: str, content_type: str) -> bool:
    if content_type and content_type.startswith(VIDEO_CONTENT_TYPE_PREFIX):
        return True
    return any(hint in url for hint in VIDEO_URL_HINTS)


def _host_is_cda(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return host.endswith(CDA_HOST_SUFFIX)


def _filename_stem(url: str) -> str:
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]
    stem = EXT_RE.sub("", filename)
    if stem.startswith("a_"):
        stem = stem[2:]
    return stem


def _is_likely_real_file(url: str) -> bool:
    stem = _filename_stem(url)
    return len(stem) >= MIN_REAL_FILENAME_LENGTH


def _candidate_kind(url: str) -> str:
    path = urlparse(url).path
    filename = path.rsplit("/", 1)[-1]
    return "audio" if filename.startswith("a_") else "video"


def _try_skip_ad(page) -> bool:
    targets = [page] + list(page.frames)
    for target in targets:
        for selector in SKIP_AD_SELECTORS:
            try:
                target.click(selector, timeout=800)
                return True
            except Exception:
                continue
    return False


def _try_capture_files(page, progress_cb, ad_wait_ms: int = 2500, final_wait_ms: int = 7000):
    by_kind = {}
    rejected_ads = []

    def handle_response(response):
        url = response.url
        if not _host_is_cda(url):
            return
        try:
            ctype = response.headers.get("content-type", "")
        except Exception:
            ctype = ""
        if not _looks_like_video(url, ctype):
            return

        if not _is_likely_real_file(url):
            rejected_ads.append(url)
            return

        content_length = None
        try:
            cl = response.headers.get("content-length")
            if cl is not None:
                content_length = int(cl)
        except Exception:
            content_length = None

        kind = _candidate_kind(url)
        existing = by_kind.get(kind)
        if existing is None or (
            content_length is not None
            and (existing.get("content_length") or -1) <= content_length
        ):
            by_kind[kind] = {"url": url, "content_length": content_length}
        elif existing.get("content_length") is None and content_length is None:
            by_kind[kind] = {"url": url, "content_length": content_length}

    page.on("response", handle_response)

    progress_cb("Szukam przycisku odtwarzania...", PCT_SEARCHING_PLAY)
    targets = [page] + list(page.frames)
    clicked = False
    for target in targets:
        for selector in PLAY_SELECTORS:
            try:
                target.click(selector, timeout=1200)
                clicked = True
                break
            except Exception:
                continue
        if clicked:
            break

    if clicked:
        progress_cb("Klikniento play, czekam (moze leciec reklama)...", PCT_CLICKED_PLAY)
    else:
        progress_cb("Nie znalazlem przycisku play, czekam na automatyczne odtworzenie...", PCT_CLICKED_PLAY - 2)

    page.wait_for_timeout(ad_wait_ms)

    if rejected_ads:
        progress_cb(f"Wykryto i zignorowano {len(rejected_ads)} plik(ow) wygladajacych na reklame.")

    if _try_skip_ad(page):
        progress_cb("Pominieto reklame, czekam na prawdziwy plik...", PCT_AD_HANDLED)
    else:
        progress_cb("Czekam na pojawienie sie pliku wideo w ruchu sieciowym...", PCT_AD_HANDLED - 3)

    page.wait_for_timeout(final_wait_ms)

    if rejected_ads and not by_kind:
        progress_cb(
            f"Uwaga: zlapano tylko podejrzane (krotkie nazwy) pliki: {rejected_ads}. "
            "Prawdziwy film moze jeszcze nie wystartowal."
        )

    return by_kind


def fetch_direct_link(page, cda_url: str, progress_cb=None) -> dict:
    if progress_cb is None:
        progress_cb = _noop

    result = {
        "input": cda_url,
        "ok": False,
        "title": None,
        "files": [],
        "error": None,
    }

    progress_cb(f"Otwieram strone: {cda_url}", PCT_START)
    try:
        page.goto(cda_url, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:
        result["error"] = f"Nie udalo sie otworzyc strony: {exc}"
        return result

    try:
        raw_title = page.title()
        if raw_title:
            result["title"] = TITLE_SUFFIX_RE.sub("", raw_title).strip()
            progress_cb(f"Strona wczytana: {result['title']}", PCT_PAGE_LOADED)
    except Exception:
        pass

    try:
        by_kind = _try_capture_files(page, progress_cb)
    except Exception as exc:
        result["error"] = f"Blad podczas analizy ruchu sieciowego: {exc}"
        return result

    if by_kind:
        for kind in ("video", "audio"):
            if kind in by_kind:
                result["files"].append({"kind": kind, "url": by_kind[kind]["url"]})
        result["ok"] = True
        progress_cb(f"Znaleziono {len(result['files'])} plik(ow).", PCT_FILES_FOUND)
    else:
        result["error"] = (
            "Nie udalo sie przechwycic adresu pliku wideo. Mozliwe, ze "
            "trzeba kliknac inny element odtwarzacza, film wymaga logowania, "
            "albo CDA zmienilo sposob ladowania wideo."
        )
        progress_cb("Nie znaleziono pliku wideo.")

    return result


def _sanitize_filename(name: str) -> str:
    if not name:
        return ""
    name = INVALID_FILENAME_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:150]


def _reserve_output_path(downloads_dir: str, base_name: str) -> str:
    with FILENAME_LOCK:
        candidate_name = f"{base_name}.mp4"
        path = os.path.join(downloads_dir, candidate_name)
        n = 1
        while os.path.exists(path):
            candidate_name = f"{base_name} ({n}).mp4"
            path = os.path.join(downloads_dir, candidate_name)
            n += 1
        open(path, "ab").close()
    return path


def _format_mb(num_bytes: int) -> str:
    return f"{num_bytes / 1_048_576:.1f} MB"


def _download_to_file(
    url: str,
    referer: str,
    dest_path: str,
    progress_cb,
    label: str,
    pct_start: int,
    pct_end: int,
) -> None:
    """Pobiera plik z odpowiednim naglowkiem Referer (CDA blokuje hotlinking
    bez wlasciwego Referera). Mapuje postep pobierania na zakres [pct_start, pct_end]
    calego pipeline'u, wiec pasek postepu w UI plynnie rosnie podczas sciagania."""
    headers = {
        "User-Agent": DOWNLOAD_HEADERS_USER_AGENT,
        "Referer": referer,
    }
    with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total_header = resp.headers.get("content-length")
        total = int(total_header) if total_header is not None else None

        downloaded = 0
        last_reported_pct_int = None
        last_reported_mb_step = 0

        size_note = f" ({_format_mb(total)})" if total else " (rozmiar nieznany)"
        progress_cb(f"[{label}] Start pobierania{size_note}", pct_start)

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    frac = downloaded / total
                    pct = pct_start + frac * (pct_end - pct_start)
                    pct_int = int(pct)
                    if last_reported_pct_int is None or pct_int >= last_reported_pct_int + 2:
                        last_reported_pct_int = pct_int
                        progress_cb(
                            f"[{label}] {_format_mb(downloaded)} / {_format_mb(total)} ({int(frac * 100)}%)",
                            pct_int,
                        )
                else:
                    mb_step = downloaded // (2 * 1_048_576)
                    if mb_step > last_reported_mb_step:
                        last_reported_mb_step = mb_step
                        progress_cb(f"[{label}] pobrano {_format_mb(downloaded)}...")

        progress_cb(f"[{label}] zakonczono pobieranie ({_format_mb(downloaded)}).", pct_end)


def download_and_merge(result: dict, downloads_dir: str, progress_cb=None) -> None:
    if progress_cb is None:
        progress_cb = _noop

    if not result.get("ok") or not result.get("files"):
        return

    referer = result["input"]
    video_entry = next((f for f in result["files"] if f["kind"] == "video"), None)
    audio_entry = next((f for f in result["files"] if f["kind"] == "audio"), None)

    if video_entry is None:
        result["merge_error"] = "Brak pliku wideo do pobrania."
        return

    os.makedirs(downloads_dir, exist_ok=True)

    base_name = _sanitize_filename(result.get("title") or "")
    if not base_name:
        base_name = f"cda_{uuid_module.uuid4().hex[:10]}"

    output_path = _reserve_output_path(downloads_dir, base_name)
    output_filename = os.path.basename(output_path)

    video_pct_end = PCT_VIDEO_END_WITH_AUDIO if audio_entry else PCT_VIDEO_END_NO_AUDIO

    with tempfile.TemporaryDirectory() as tmp_dir:
        video_tmp = os.path.join(tmp_dir, "video.mp4")

        try:
            _download_to_file(
                video_entry["url"], referer, video_tmp, progress_cb, "wideo",
                PCT_FILES_FOUND, video_pct_end,
            )
        except Exception as exc:
            result["merge_error"] = f"Nie udalo sie pobrac pliku wideo: {exc}"
            return

        if audio_entry is None:
            try:
                shutil.move(video_tmp, output_path)
            except Exception as exc:
                result["merge_error"] = f"Nie udalo sie zapisac pliku: {exc}"
                return
            result["download_filename"] = output_filename
            progress_cb("Pobrano plik wideo (zawiera juz dzwiek).", PCT_DONE)
            return

        audio_tmp = os.path.join(tmp_dir, "audio.mp4")
        try:
            _download_to_file(
                audio_entry["url"], referer, audio_tmp, progress_cb, "audio",
                PCT_VIDEO_END_WITH_AUDIO, PCT_AUDIO_END,
            )
        except Exception as exc:
            result["merge_error"] = f"Nie udalo sie pobrac pliku audio: {exc}"
            return

        if FFMPEG_EXE is None:
            result["merge_error"] = (
                "Brak ffmpeg (pakiet imageio-ffmpeg) - nie mozna polaczyc wideo i audio."
            )
            return

        progress_cb("Lacze wideo i audio w jeden plik (ffmpeg)...", PCT_MERGE)
        cmd = [
            FFMPEG_EXE, "-y",
            "-i", video_tmp,
            "-i", audio_tmp,
            "-c", "copy",
            "-shortest",
            output_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                stderr_tail = proc.stderr.decode(errors="ignore")[-400:]
                result["merge_error"] = f"ffmpeg zwrocil blad: {stderr_tail}"
                return
        except Exception as exc:
            result["merge_error"] = f"Blad podczas laczenia plikow: {exc}"
            return

        result["download_filename"] = output_filename
        progress_cb("Polaczono wideo i audio w jeden plik. Gotowe do pobrania.", PCT_DONE)


def _process_single_url(idx: int, task: dict, downloads_dir: str, progress_cb) -> dict:
    """
    Pelny pipeline dla jednego zadania. `task` to dict:
        {"display": str, "cda_url": str|None, "episode_url": str|None, "error": str|None}

    Jesli `cda_url` jest None, najpierw rozwiazujemy go ze strony odcinka
    na shinden.pl (klikajac "Pokaz" w wierszu Cda/Polski), uzywajac TEJ
    SAMEJ instancji przegladarki/karty, co dalsza ekstrakcja z cda.pl.

    progress_cb tutaj ma sygnature (idx, text, percent) - numer linku jest
    doklejany przez `tagged_cb` przed wywolaniem dalszych funkcji.
    """

    def tagged_cb(text: str, percent=None) -> None:
        progress_cb(idx, text, percent)

    result = {
        "input": task.get("episode_url") or task.get("cda_url") or task.get("display"),
        "ok": False,
        "title": None,
        "files": [],
        "error": None,
    }

    if task.get("error"):
        result["error"] = task["error"]
        tagged_cb(task["error"])
        tagged_cb("Zakonczono z bledem.")
        return result

    tagged_cb("Uruchamiam przegladarke (Chromium)...", PCT_START)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                try:
                    cda_url = task.get("cda_url")

                    if cda_url is None:
                        episode_url = task.get("episode_url")
                        tagged_cb("Szukam linku CDA z polskimi napisami na Shinden...", PCT_START)
                        resolved = resolve_cda_link(page, episode_url, progress_cb=tagged_cb)
                        if not resolved.get("ok"):
                            result["error"] = resolved.get("error") or "Nie udalo sie znalezc linku CDA na Shinden."
                            tagged_cb("Zakonczono z bledem.")
                            return result
                        cda_url = resolved["cda_url"]
                        result["input"] = cda_url

                    result = fetch_direct_link(page, cda_url, progress_cb=tagged_cb)
                finally:
                    page.close()
            finally:
                browser.close()
    except Exception as exc:
        result["error"] = f"Blad przegladarki: {exc}"
        tagged_cb(f"Blad krytyczny: {exc}")
        return result

    if result.get("ok"):
        download_and_merge(result, downloads_dir, progress_cb=tagged_cb)

    if result.get("download_filename"):
        tagged_cb("Gotowe.", PCT_DONE)
    else:
        tagged_cb("Zakonczono z bledem.")

    return result


def process_tasks(
    tasks: list[dict],
    downloads_dir: str,
    progress_cb=None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[dict]:
    """
    Przetwarza liste zadan ROWNOLEGLE (domyslnie maks. 5 naraz). Zwraca
    liste wynikow w TEJ SAMEJ kolejnosci co `tasks`.

    `progress_cb(idx, text, percent=None)` - idx to numer zadania liczony
    od 1, odpowiadajacy pozycji w `tasks`.
    """
    if progress_cb is None:
        progress_cb = lambda idx, text, percent=None: None

    total = len(tasks)
    results = [None] * total
    workers = max(1, min(max_workers, total))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_process_single_url, idx + 1, task, downloads_dir, progress_cb): idx
            for idx, task in enumerate(tasks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = {
                    "input": tasks[idx].get("display"),
                    "ok": False,
                    "title": None,
                    "files": [],
                    "error": f"Blad krytyczny: {exc}",
                }
                progress_cb(idx + 1, f"Blad krytyczny: {exc}")

    return results
