# cda-downloader

Dwie powiązane, ale **oddzielone** funkcje:

1. **Główny downloader (`/`)** — wklejasz linki do filmów na cda.pl, dostajesz gotowe pliki mp4 (wideo+audio połączone).
2. **Shinden → CDA (`/shinden`)** — osobna strona: wklejasz link do listy odcinków serii na shinden.pl, program znajduje dla każdego odcinka wersję **Cda + polskie napisy** i wyciąga link do strony cda.pl. Wynik możesz skopiować albo jednym kliknięciem wysłać do głównego downloadera.

Strony nie są ze sobą zlinkowane funkcjonalnie poza przyciskami nawigacji i opcjonalnym przekazaniem znalezionych linków przez `sessionStorage` — `/shinden` niczego nie pobiera, tylko wyszukuje linki.

## `/` — downloader cda.pl

Wklejasz linki do filmów na cda.pl (każdy w nowej linii), każdy dostaje własny panel z paskiem postępu i logiem, program sam wchodzi na stronę, znajduje plik wideo (+ audio jeśli rozdzielone, odsiewając reklamy po długości nazwy pliku), pobiera i łączy przez ffmpeg, serwuje gotowy plik nazwany tytułem filmu.

## `/shinden` — scraper Shinden

1. Wklejasz link do listy odcinków serii (`.../series/.../episodes`), ewentualnie kilka, każdy w nowej linii.
2. **Faza wykrywania**: program wchodzi na listę i wyciąga odcinki na podstawie zweryfikowanej struktury tabeli (`<tr data-episode-no="N">`, `td.ep-title`, link w `td.button-group`).
3. Dla każdego odcinka osobno (równolegle, domyślnie maks. 3 naraz): wchodzi na stronę odcinka, znajduje wpis serwisu **Cda** z **polskimi napisami** po danych w atrybucie `data-episode` (JSON), klika „Pokaż” (z retry do 5 razy, bo strona bywa kapryśna - banery GDPR, reklamy z licznikiem, chwilowe błędy API) i czeka na pojawienie się odtwarzacza cda.pl w `div#player-block`.
4. Wynik: panel na każdy odcinek z linkiem cda.pl (i przyciskiem kopiowania) albo błędem (z przyciskiem doładowania pełnego HTML strony - debug). Na dole podsumowanie + przyciski „Skopiuj wszystkie” i „Wyślij do downloadera”.

### Co robi scraper, a czego nie

- **Nigdy nie rozwiązuje hCaptchy automatycznie** — jak się pojawi, zgłasza błąd dla tego odcinka, trzeba sprawdzić ręcznie.
- Używa „stealth” kontekstu Playwrighta (realny User-Agent, maskowanie `navigator.webdriver` itp.) - shinden.pl serwował uboższą listę serwisów (bez Cda) wykrytym botom headless Chromium.
- Klika przyciski "Pokaż" przez bezpośrednie wywołanie JS (`element.click()`), nie symulację myszki - omija nakładki (GDPR/cookies) wizualnie leżące na wierchu.
- `/shinden` **tylko wyszukuje linki** — nigdy nie pobiera plików ani nie odwiedza cda.pl w celu ekstrakcji wideo.

## Struktura projektu

```
cda-downloader/
│
├── app.py                  # Flask: "/" (downloader cda) + "/shinden" (scraper)
├── cda/
│   ├── __init__.py
│   ├── shinden.py           # get_episode_list + resolve_cda_link/resolve_many
│   └── extractor.py         # ekstrakcja z cda.pl + pobieranie i łączenie (ffmpeg)
├── templates/
│   ├── index.html           # downloader cda.pl
│   └── shinden.html         # scraper Shinden -> linki cda.pl
├── static/
│   └── style.css
├── requirements.txt
├── install.bat
├── run.bat
└── README.md
```

## Instalacja / uruchomienie (Windows)

`install.bat` (raz) tworzy venv, instaluje zależności i Chromium dla Playwrighta, potem `run.bat` startuje appkę na `http://localhost:5000/`.

## Wymagania

- Python 3.10+, Flask, requests
- playwright (+ Chromium, doinstalowane przez `install.bat`)
- imageio-ffmpeg (ffmpeg do łączenia wideo+audio w głównym downloaderze)

## Uwagi / ograniczenia

- 3-5 równoległych zadań = tyle samo niezależnych instancji headless Chromium na raz — przy słabszym komputerze zmniejsz `max_workers` w odpowiednich miejscach `app.py`.
- Strony cda.pl / shinden.pl mogą zmieniać strukturę — w razie zmian logika w `cda/extractor.py` / `cda/shinden.py` może wymagać aktualizacji.
- Pobieranie treści powinno być zgodne z regulaminem serwisów i prawami autorskimi — projekt edukacyjny / na własne potrzeby.
