"""
Scraper dla shinden.pl: wyciaga liste odcinkow danej serii oraz - dla
wybranego odcinka - link do strony cda.pl z polskimi napisami.

Dziala to tak: na stronie odcinka kazdy wpis serwisu (Gdrive, Cda, Sibnet...)
ma swojego "Pokaz" w postaci <a class="change-video-player" data-episode='{...}'>,
gdzie atrybut `data-episode` to JSON z dokladnymi danymi wpisu, np.:

    {"online_id":"1490275","player":"Cda","lang_audio":"jp","lang_subs":"pl",
     "max_res":"1080p","added":"2023-10-25 15:08:53", ...}

Dzieki temu nie trzeba zgadywac z tekstu komorek tabeli - wystarczy
sprawdzic `player == "Cda"` i `lang_subs == "pl"` w tym JSON-ie (zweryfikowane
na realnym HTML strony odcinka podanym przez uzytkownika).

Po kliknieciu "Pokaz" strona (JS) wstawia odtwarzacz (iframe z cda.pl) do
div#player-block - czasem najpierw pokazuje sie reklama (krotkie
opoznienie, czasem z licznikiem odliczajacym), a w rzadszych przypadkach
moze pojawic sie hCaptcha ("Albo reklamy, albo reCaptcha") zamiast playera.

WAZNE: ten kod NIGDY nie probuje rozwiazywac captchy automatycznie - jesli
ja wykryje, zglasza to jako blad dla danego odcinka i konczy dzialanie.
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

SHINDEN_HOST_SUFFIX = "shinden.pl"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Katalog do zrzucania pelnego HTML strony odcinka, gdy nie znajdziemy wpisu
# Cda+PL - przydatne do debugowania, czy strona pokazuje scraperowi inna
# (uboga) liste serwisow niz w normalnej przegladarce.
DEBUG_DIR = os.path.join(PROJECT_ROOT, "downloads", "shinden_debug")

# Wiersz odcinka na liscie: <tr data-episode-no="3">...<td class="ep-title">Tytul</td>...
# ...<td class="button-group"><a href="/episode/.../view/139010" class="... detail">Szczegoly</a></td></tr>
EPISODE_ROW_SELECTOR = "table.data-view-table-episodes tbody tr[data-episode-no]"

# Selektor playera wybierania serwisu - kluczowe dane sa w atrybucie data-episode (JSON)
PLAYER_ANCHOR_SELECTOR = "a.change-video-player[data-episode]"

# hCaptcha pojawiajaca sie czasem zamiast playera po kliknieciu "Pokaz"
CAPTCHA_SELECTOR = "#hcaptcha-container, .h-captcha"

# Licznik odliczajacy reklame ("za chwile zacznie sie..."), ktory bywa
# wstawiany do #player-block PRZED prawdziwym playerem - sam tekst licznika
# to zwykla cyfra (np. "5", "1"), wiec sprawdzamy obecnosc kontenera, nie
# jego tresci. Jesli ten licznik jest widoczny, dajemy stronie wiecej czasu.
AD_COUNTDOWN_SELECTOR = "#player-block #circle-counter"

# Wbudowany w JS strony komunikat, gdy zapytanie AJAX o dane playera
# (do api4.shinden.pl/xhr) nie powiedzie sie - widziane w warunkach duzego
# rownoleglego obciazenia (kilka przegladarek naraz odpytujacych z tego
# samego IP). Nie jest to captcha ani nakladka - po prostu nieudane
# doladowanie, ktore zwykle udaje sie po ponownym kliknieciu "Pokaz".
LOAD_ERROR_TEXT = "Problem z załadowaniem danych"

# Maksymalna liczba pelnych prob (klik 'Pokaz' + czekanie na player) dla
# JEDNEGO znalezionego wpisu Cda+PL. Jesli znalezlismy poprawny wpis, ale
# kliknieciem/doladowanie zawiedzie (nakladka GDPR, zawiesza sie licznik
# reklamy, chwilowy blad sieci API) - probujemy od nowa, zamiast poddawac
# sie po pierwszej nieudanej probie.
MAX_PLAYER_ATTEMPTS = 5


# Banner GDPR/CMP (tri-table CMP), ktory pokazuje sie na shinden.pl przy
# pierwszym wejsciu i moze blokowac doladowanie pelnej listy serwisow
# (skrypty trzeciej strony czekaja na zgode). Klikamy w nim "Zaakceptuj
# wszystko", zanim odczytujemy cokolwiek ze strony.
GDPR_ACCEPT_SELECTORS = [
    "button:has-text('Zaakceptuj wszystko')",
    ".e1sXLPUy",
]


# ---------------------------------------------------------------------------
# "Stealth" - shinden serwuje headless Chromium ubozsza wersje strony (np.
# tylko Crunchyroll, bez Cda/Gdrive/Sibnet), mimo ze te same dane SA w pelnej
# wersji widocznej w normalnej przegladarce, nawet bez logowania
# (zweryfikowane na realnym view-source od uzytkownika - 8 wpisow serwisow,
# w tym 3x Cda). Najbardziej prawdopodobna przyczyna: wykrycie automatyzacji
# po Sec-CH-UA/User-Agent ujawniajacym "HeadlessChrome" i/lub navigator.webdriver.
# Ponizsze maskuje te najbardziej oczywiste sygnaly.
# ---------------------------------------------------------------------------

STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# Wykonywany w kazdej nowej stronie PRZED jakimkolwiek skryptem strony -
# usuwa/maskuje najbardziej standardowe sygnatury automatyzacji.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['pl-PL', 'pl', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


def _new_stealth_browser_and_page(p):
    """Uruchamia Chromium i tworzy strone udajaca normalna, zwykla
    desktopowa przegladarke (a nie headless bota) - realny User-Agent,
    maskowanie navigator.webdriver itp. Zwraca (browser, page)."""
    browser = p.chromium.launch(headless=True, args=STEALTH_LAUNCH_ARGS)
    context = browser.new_context(
        user_agent=STEALTH_USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="pl-PL",
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)
    page = context.new_page()
    return browser, page


def _noop(_text, _percent=None) -> None:
    pass


def _accept_gdpr(page, progress_cb=None, wait_ms: int = 15000, find_timeout_ms: int = 8000) -> bool:
    """Czeka (odpytujac co ok. 300ms, do find_timeout_ms) na pojawienie sie
    przycisku "Zaakceptuj wszystko" w bannerze GDPR/CMP i go klika. Skrypt
    CMP (cmp.spolecznosci.net) wstrzykuje baner ASYNCHRONICZNIE - pod
    obciazeniem (kilka przegladarek naraz) bywa, ze zajmuje to wiecej niz
    sekunde-dwie, dlatego nie poprzestajemy na jednej krotkiej probie.

    Po kliknieciu dajemy stronie dodatkowe ~15s (w kawalkach, z logiem co
    kilka sekund), zeby doczytala reszte - skrypty trzecich stron
    czekajace na zgode czesto blokuja doczytanie pelnej listy serwisow.

    Zwraca True, jesli baner zostal znaleziony i klikniety; False, jesli
    nie pojawil sie w ogole w wyznaczonym czasie (bezpieczne - nic wtedy
    nie robimy)."""
    if progress_cb is None:
        progress_cb = _noop

    deadline = time.time() + (find_timeout_ms / 1000)
    clicked = False
    while time.time() < deadline and not clicked:
        for selector in GDPR_ACCEPT_SELECTORS:
            try:
                page.click(selector, timeout=400)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            page.wait_for_timeout(300)

    if not clicked:
        return False

    progress_cb("Zaakceptowano banner GDPR, daje stronie chwile na doczytanie...")
    remaining = wait_ms
    step = 3000
    elapsed = 0
    while remaining > 0:
        chunk = min(step, remaining)
        page.wait_for_timeout(chunk)
        remaining -= chunk
        elapsed += chunk
        if remaining > 0:
            progress_cb(f"Wciaz czekam na doczytanie strony po GDPR... ({elapsed // 1000}s)")
    return True


def is_shinden_series_url(url: str) -> bool:
    """Rozpoznaje link do listy odcinkow serii na shinden.pl (np. .../series/123-tytul/episodes)."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return False
    if not host.endswith(SHINDEN_HOST_SUFFIX):
        return False
    return "/series/" in url or "/episodes" in url


def get_episode_list(series_url: str, progress_cb=None) -> list[dict]:
    """
    Wchodzi na strone z lista odcinkow serii i wyciaga linki do
    poszczegolnych odcinkow na podstawie REALNEJ struktury tabeli (HTML
    zweryfikowany przez uzytkownika):

        <tr data-episode-no="3">
            <td>3</td>
            <td class="ep-title">Ofiarowanie modlitwy</td>
            ...
            <td class="button-group">
                <a href="/episode/45178-koutetsujou-no-kabaneri/view/139010" class="button active detail">Szczegoly</a>
            </td>
        </tr>

    Zwraca liste dict: {"label": "Odcinek 3: Ofiarowanie modlitwy", "url": "...", "episode_no": "3"},
    posortowana wg numeru odcinka ROSNACO (strona shinden listuje odcinki
    malejaco - najnowszy na gorze).
    """
    if progress_cb is None:
        progress_cb = _noop

    progress_cb(f"Otwieram liste odcinkow: {series_url}")
    episodes = []

    with sync_playwright() as p:
        browser, page = _new_stealth_browser_and_page(p)
        try:
            try:
                page.goto(series_url, wait_until="domcontentloaded", timeout=20000)
                if not _accept_gdpr(page, progress_cb=progress_cb):
                    page.wait_for_timeout(1000)

                rows = page.query_selector_all(EPISODE_ROW_SELECTOR)
                for row in rows:
                    try:
                        ep_no = row.get_attribute("data-episode-no")
                        title_el = row.query_selector("td.ep-title")
                        title = title_el.inner_text().strip() if title_el else ""
                        link_el = row.query_selector("td.button-group a")
                        href = link_el.get_attribute("href") if link_el else None
                    except Exception:
                        continue
                    if not href:
                        continue
                    full_url = urljoin(series_url, href)
                    label = f"Odcinek {ep_no}" + (f": {title}" if title else "")
                    episodes.append({"label": label, "url": full_url, "episode_no": ep_no})

                def _sort_key(ep):
                    try:
                        return int(ep["episode_no"])
                    except Exception:
                        return 0

                episodes.sort(key=_sort_key)
            finally:
                page.close()
        finally:
            browser.close()

    progress_cb(f"Znaleziono {len(episodes)} odcinek(ow) na liscie.")
    return episodes


def _get_player_entries(page) -> list[dict]:
    """
    Zwraca liste dictow z danymi WSZYSTKICH wpisow serwisow na stronie
    odcinka (z atrybutu data-episode), wyciagnietych JEDNYM atomowym
    wywolaniem JS (page.evaluate).

    Dlaczego atomowo: iterowanie po ElementHandle'ach z Pythona (osobne
    zadanie get_attribute() na kazdy element) jest podatne na rozjazd, jesli
    strona w miedzyczasie mutuje DOM (np. skrypty reklamowe/anti-adblock) -
    czesc uchwytow moze "obumrzec" w trakcie iteracji i zostac pomieta.
    Jedno wywolanie JS w przegladarce dostaje konsystentny zrzut na raz.
    """
    try:
        raw_list = page.evaluate(
            """() => Array.from(document.querySelectorAll('a.change-video-player[data-episode]'))
                .map(el => el.getAttribute('data-episode'))"""
        )
    except Exception:
        raw_list = []

    entries = []
    for raw in raw_list or []:
        try:
            entries.append(json.loads(raw))
        except Exception:
            continue
    return entries


def _find_cda_polish_entry(entries: list[dict]):
    for data in entries:
        player = (data.get("player") or "").strip()
        lang_subs = (data.get("lang_subs") or "").strip()
        if player.lower() == "cda" and lang_subs.lower() == "pl":
            return data
    return None


def _save_debug_html(page, episode_url: str) -> str | None:
    """Zrzuca pelny aktualny HTML strony (page.content(), czyli po wykonaniu
    JS) do pliku w DEBUG_DIR. Zwraca nazwe pliku (do wyswietlenia/serwowania)
    albo None, jesli zapis sie nie powiodl."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        episode_id = episode_url.rstrip("/").rsplit("/", 1)[-1]
        episode_id = re.sub(r"[^A-Za-z0-9_-]", "_", episode_id) or "unknown"
        filename = f"episode_{episode_id}_{int(time.time())}.html"
        path = os.path.join(DEBUG_DIR, filename)
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return filename
    except Exception:
        return None


def _dismiss_legacy_cookie_bar(page) -> None:
    """Zamyka starszy, niezalezny od glownego CMP baner "#cookie-bar"
    (link 'Akceptuje' klasy .cb-enable) - bywa, ze zostaje na wierchu i
    przechwytuje kliknicia w przyciski nizej na stronie. Bezpieczne, jesli
    bannera nie ma."""
    try:
        page.click("#cookie-bar .cb-enable", timeout=1000)
    except Exception:
        pass


def resolve_cda_link(page, episode_url: str, progress_cb=None) -> dict:
    """
    Wchodzi (na PRZEKAZANEJ stronie/page, zeby dalo sie reuzyc te sama
    instancje przegladarki co przy dalszej ekstrakcji z cda.pl) na strone
    odcinka, znajduje wpis serwisu Cda z polskimi napisami (po danych
    data-episode), klika "Pokaz" i czeka na pojawienie sie odtwarzacza
    cda.pl w div#player-block (z mozliwa krotka reklama po drodze).

    Jesli zamiast playera pojawi sie hCaptcha - NIE jest ona rozwiazywana
    automatycznie; zglaszamy to jako blad.

    Zwraca dict: {"ok": bool, "cda_url": str|None, "error": str|None}.
    """
    if progress_cb is None:
        progress_cb = _noop

    result = {"ok": False, "cda_url": None, "error": None, "debug_file": None}

    progress_cb(f"Otwieram odcinek: {episode_url}")
    try:
        page.goto(episode_url, wait_until="domcontentloaded", timeout=25000)
    except Exception:
        try:
            progress_cb("Strona nie odpowiedziala na czas, probuje ponownie...")
            page.goto(episode_url, wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:
            result["error"] = f"Nie udalo sie otworzyc strony odcinka: {exc}"
            return result

    if not _accept_gdpr(page, progress_cb=progress_cb):
        page.wait_for_timeout(600)

    _dismiss_legacy_cookie_bar(page)

    # Zbieramy wpisy serwisow z retry'ami: konczymy wczesniej, jesli znajdziemy
    # dopasowanie, albo gdy liczba wpisow przestanie sie zmieniac (DOM sie
    # "ustabilizowal") - maks. ok. 5 sekund prob.
    entries: list[dict] = []
    match = None
    stable_count = 0
    last_len = -1
    for _ in range(6):
        entries = _get_player_entries(page)
        match = _find_cda_polish_entry(entries)
        if match is not None:
            break
        if len(entries) == last_len:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0
        last_len = len(entries)
        page.wait_for_timeout(800)

    if match is None:
        debug_file = _save_debug_html(page, episode_url)
        result["debug_file"] = debug_file
        debug_note = f" Pelny HTML strony zapisany do podgladu: {debug_file}." if debug_file else ""
        if entries:
            summary = ", ".join(
                f"{(e.get('player') or '?')}/{(e.get('lang_subs') or '-')}" for e in entries
            )
            result["error"] = (
                f"Ten odcinek nie ma wersji 'Cda' z polskimi napisami "
                f"(znaleziono {len(entries)} wpis(ow) na liscie serwisow: {summary})."
                f"{debug_note}"
            )
        else:
            page_title = None
            try:
                page_title = page.title()
            except Exception:
                pass
            result["error"] = (
                "Nie znaleziono zadnych wpisow serwisow na tej stronie (0 elementow "
                "'a.change-video-player[data-episode]'). Tytul strony w tym momencie: "
                f"'{page_title}'. Strona mogla sie nie wczytac poprawnie, wymagac "
                "dodatkowego kroku (np. zaakceptowania ostrzezenia) albo zwracac inna "
                f"tresc dla automatyzacji niz w przegladarce.{debug_note}"
            )
        return result

    progress_cb(
        f"Znaleziono wpis Cda (jakosc {match.get('max_res', '?')}, dodano {match.get('added', '?')}), "
        "klikam 'Pokaz'..."
    )

    captured = {}

    def handle_response(response):
        if "cda_url" in captured:
            return
        url = response.url
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        if host.endswith("cda.pl") and "/video/" in url:
            captured["cda_url"] = url

    page.on("response", handle_response)

    online_id = match.get("online_id")
    click_selector = f"#player_data_{online_id}" if online_id else None
    if not click_selector:
        result["error"] = "Brak online_id w danych wpisu Cda - nie wiadomo czego kliknac."
        return result

    def _click_player_button() -> None:
        """Klika przycisk 'Pokaz'. Najpierw normalny klik Playwrighta; jesli
        sie nie uda (lub nawet jesli sie 'udal', ale realnie trafil w cos
        innego - np. nakladke GDPR lezaca wizualnie na wierchu), dodatkowo
        wywolujemy kliknicie bezposrednio przez JS (element.click()).
        To drugie podejscie NIE jest symulacja myszki w konkretnym punkcie
        ekranu (ktory moglby trafic w element lezacy wyzej) - wywoluje
        metode .click() na konkretnym elemencie DOM, wiec dziala niezaleznie
        od jakichkolwiek nakladek wizualnie zaslaniajacych przycisk."""
        try:
            page.click(click_selector, timeout=2000)
        except Exception:
            pass
        try:
            page.evaluate(
                "(sel) => { const el = document.querySelector(sel); if (el) el.click(); }",
                click_selector,
            )
        except Exception:
            pass

    captcha_seen = False
    last_attempt_note = ""

    for attempt in range(1, MAX_PLAYER_ATTEMPTS + 1):
        if attempt == 1:
            progress_cb("Klikam 'Pokaz'...")
        else:
            progress_cb(
                f"Proba {attempt}/{MAX_PLAYER_ATTEMPTS} - klikam 'Pokaz' ponownie "
                f"({last_attempt_note})..."
            )

        _accept_gdpr(page, progress_cb=progress_cb, wait_ms=2000, find_timeout_ms=3000)
        _dismiss_legacy_cookie_bar(page)
        _click_player_button()

        progress_cb("Czekam na zaladowanie playera (moze pojawic sie reklama)...")

        ad_countdown_reported = False
        zero_countdown_seen_for = 0
        found_this_attempt = False

        for _ in range(10):  # do ok. 10s na PROBE (maks. 5 prob = ok. 50s calosciowo)
            page.wait_for_timeout(1000)

            if "cda_url" in captured:
                found_this_attempt = True
                break

            try:
                iframe = page.query_selector(
                    "#player-block iframe[src*='cda.pl'], iframe[src*='cda.pl']"
                )
                if iframe:
                    src = iframe.get_attribute("src")
                    if src:
                        captured["cda_url"] = src
                        found_this_attempt = True
                        break
            except Exception:
                pass

            try:
                captcha_el = page.query_selector(CAPTCHA_SELECTOR)
                if captcha_el and captcha_el.is_visible():
                    captcha_seen = True
                    break
            except Exception:
                pass

            countdown_present = False
            countdown_text = None
            try:
                countdown_el = page.query_selector(AD_COUNTDOWN_SELECTOR)
                if countdown_el:
                    countdown_present = True
                    digit_el = page.query_selector(f"{AD_COUNTDOWN_SELECTOR} #countdown")
                    if digit_el:
                        countdown_text = (digit_el.inner_text() or "").strip()
            except Exception:
                pass

            if countdown_present and not ad_countdown_reported:
                ad_countdown_reported = True
                progress_cb("Trwa odliczanie reklamy przed playerem, czekam...")

            # Zawiecha: licznik reklamowy doszedl do "0" i nic dalej sie
            # nie dzieje - przerywamy wewnetrzne czekanie wczesniej, zeby
            # szybciej przejsc do kolejnej proby (ponownego kliknicia).
            if countdown_present and countdown_text == "0":
                zero_countdown_seen_for += 1
                if zero_countdown_seen_for >= 4:
                    last_attempt_note = "licznik reklamy doszedl do 0, ale player sie nie wstawil"
                    break
            else:
                zero_countdown_seen_for = 0

            try:
                player_block_text = page.inner_text("#player-block")
            except Exception:
                player_block_text = ""
            if LOAD_ERROR_TEXT in player_block_text:
                last_attempt_note = "strona zglosila 'Problem z zaladowaniem danych'"
                break

        if found_this_attempt or captcha_seen:
            break

        if not last_attempt_note:
            last_attempt_note = "player sie nie pojawil w wyznaczonym czasie"

    if "cda_url" in captured:
        result["ok"] = True
        result["cda_url"] = captured["cda_url"]
        progress_cb(f"Znaleziono link CDA: {captured['cda_url']}")
    elif captcha_seen:
        result["debug_file"] = _save_debug_html(page, episode_url)
        debug_note = f" Pelny HTML strony zapisany do podgladu: {result['debug_file']}." if result["debug_file"] else ""
        result["error"] = (
            "Shinden pokazal captche (hCaptcha) zamiast playera - nie rozwiazuje jej "
            f"automatycznie. Ten odcinek trzeba sprawdzic recznie w przegladarce.{debug_note}"
        )
    else:
        result["debug_file"] = _save_debug_html(page, episode_url)
        debug_note = f" Pelny HTML strony zapisany do podgladu: {result['debug_file']}." if result["debug_file"] else ""
        result["error"] = (
            f"Klikniento 'Pokaz', ale nie udalo sie przechwycic adresu cda.pl w odpowiednim czasie.{debug_note}"
        )

    return result


def build_tasks_from_lines(lines: list[str], progress_cb=None) -> list[dict]:
    """
    Zamienia liste linkow shinden.pl (serii) na liste "zadan" do
    rozwiazania - jedno zadanie na odcinek, z `episode_url` ustawionym
    (cda_url=None, do rozwiazania przez `resolve_cda_link`).

    Uzywane przez dedykowana strone /shinden (NIE przez glowny downloader
    cda.pl, ktory przyjmuje tylko zwykle linki do filmow).

    Zwraca liste dict: {"display": str, "episode_url": str, "error": str|None}.
    """
    if progress_cb is None:
        progress_cb = _noop

    tasks = []
    for line in lines:
        if not is_shinden_series_url(line):
            tasks.append(
                {
                    "display": line,
                    "episode_url": None,
                    "error": "To nie wyglada na link do listy odcinkow serii na shinden.pl (.../series/.../episodes).",
                }
            )
            continue

        episodes = get_episode_list(line, progress_cb=progress_cb)
        if not episodes:
            tasks.append(
                {
                    "display": f"{line} (nie znaleziono odcinkow)",
                    "episode_url": None,
                    "error": "Nie znaleziono zadnych odcinkow na liscie (sprawdz selektor w shinden.py).",
                }
            )
            continue

        for ep in episodes:
            tasks.append({"display": ep["label"], "episode_url": ep["url"], "error": None})

    return tasks


def resolve_many(tasks: list[dict], progress_cb=None, max_workers: int = 3) -> list[dict]:
    """
    Rozwiazuje rownolegle (domyslnie maks. 3 naraz - shinden zwraca
    sporadyczne bledy doladowania playera przy wiekszym obciazeniu z
    jednego IP) link cda.pl dla kazdego zadania z `build_tasks_from_lines`.
    Kazde zadanie dostaje wlasna, niezalezna instancje przegladarki.

    `progress_cb(idx, text)` - idx liczony od 1, odpowiadajacy pozycji w `tasks`.

    Zwraca liste dict (w TEJ SAMEJ kolejnosci co `tasks`):
        {"display": str, "episode_url": str|None, "ok": bool, "cda_url": str|None, "error": str|None}
    """
    if progress_cb is None:
        progress_cb = lambda idx, text: None

    total = len(tasks)
    results = [None] * total
    workers = max(1, min(max_workers, total))

    def _run_one(idx, task):
        def cb(text):
            progress_cb(idx, text)

        out = {
            "display": task.get("display"),
            "episode_url": task.get("episode_url"),
            "ok": False,
            "cda_url": None,
            "error": task.get("error"),
            "debug_file": None,
        }

        if out["error"] or not out["episode_url"]:
            cb(out["error"] or "Brak adresu odcinka do sprawdzenia.")
            return out

        cb("Uruchamiam przegladarke (Chromium)...")
        try:
            with sync_playwright() as p:
                browser, page = _new_stealth_browser_and_page(p)
                try:
                    try:
                        resolved = resolve_cda_link(page, out["episode_url"], progress_cb=cb)
                        out["ok"] = resolved.get("ok", False)
                        out["cda_url"] = resolved.get("cda_url")
                        out["error"] = resolved.get("error")
                        out["debug_file"] = resolved.get("debug_file")
                    finally:
                        page.close()
                finally:
                    browser.close()
        except Exception as exc:
            out["error"] = f"Blad krytyczny: {exc}"
            cb(out["error"])

        return out

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_run_one, idx + 1, task): idx for idx, task in enumerate(tasks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = {
                    "display": tasks[idx].get("display"),
                    "episode_url": tasks[idx].get("episode_url"),
                    "ok": False,
                    "cda_url": None,
                    "error": f"Blad krytyczny: {exc}",
                    "debug_file": None,
                }

    return results
