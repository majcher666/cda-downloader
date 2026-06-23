# cda-downloader

Dwie powiązane, ale **oddzielone** funkcje:

1. **Główny downloader (`/`)** — wklejasz linki do filmów na cda.pl, dostajesz gotowe pliki mp4 (wideo+audio połączone).
2. **Shinden → CDA (`/shinden`)** — osobna strona: wklejasz link do listy odcinków serii na shinden.pl, program znajduje dla każdego odcinka wersję **Cda + polskie napisy** i wyciąga link do strony cda.pl. Wynik możesz skopiować albo jednym kliknięciem wysłać do głównego downloadera.

Strony nie są ze sobą zlinkowane funkcjonalnie poza przyciskami nawigacji i opcjonalnym przekazaniem znalezionych linków przez `sessionStorage` — `/shinden` niczego nie pobiera, tylko wyszukuje linki.

## `/` — downloader cda.pl

Wklejasz linki do filmów na cda.pl (każdy w nowej linii), każdy dostaje własny panel z paskiem postępu i logiem, program sam wchodzi na stronę, znajduje plik wideo (+ audio jeśli rozdzielone, odsiewając reklamy po długości nazwy pliku), pobiera i łączy przez ffmpeg, serwuje gotowy plik nazwany tytułem filmu.

## `/shinden` — scraper Shinden

1. Wklejasz link do listy odcinków serii (`.../series/.../episodes`), ewentualnie kilka, każdy w nowej linii.
2. **Faza wykrywania**: program wchodzi na listę i wyciąga odcinki na podstawie struktury tabeli (`table.data-view-table-episodes`, wiersze `<tr data-episode-no="N">`).
3. Dla każdego odcinka osobno (równolegle, maks. 3 naraz): wchodzi na stronę odcinka, znajduje wpis serwisu **Cda** z **polskimi napisami** po danych w atrybucie `data-episode` (JSON), klika „Pokaż” i czeka na pojawienie się odtwarzacza cda.pl w `div#player-block` (z możliwą krótką reklamą/licznikiem po drodze). Klikanie odbywa się zarówno przez normalny klik Playwrighta, jak i bezpośrednio przez JS (`element.click()`), co omija nakładki (np. baner GDPR) wizualnie leżące na przycisku. Jeśli próba się nie powiedzie (zawiesza się licznik reklamy, błąd sieciowy API, captcha) — ponawiamy do 5 razy.
4. Wynik: panel na każdy odcinek z linkiem cda.pl (i przyciskiem kopiowania) albo błędem (z przyciskiem do podglądu zapisanego HTML strony — debug). Na dole podsumowanie + przyciski „Skopiuj wszystkie” i „Wyślij do downloadera”.

### ⚠️ Ograniczenie: hCaptcha

Strona ma szablon `hcaptcha-tmpl` („Albo reklamy, albo reCaptcha”) — czasem zamiast reklamy może wyskoczyć captcha blokująca dostęp do playera. **Kod nigdy nie próbuje jej rozwiązać automatycznie** — wykrywa jej obecność i zgłasza błąd dla tego konkretnego odcinka. Taki odcinek trzeba sprawdzić ręcznie w przeglądarce.

## Struktura projektu

```
cda-downloader/
│
├── app.py                  # Flask: "/" (downloader cda) + "/shinden" (scraper)
├── cda/
│   ├── __init__.py
│   ├── shinden.py           # get_episode_list + resolve_cda_link/resolve_many
│   └── extractor.py         # ekstrakcja z cda.pl (równolegle) + pobieranie i łączenie (ffmpeg)
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

(downloads/ i venv/ nie są commitowane - patrz .gitignore)

## Instalacja / uruchomienie

`install.bat` (raz), potem `run.bat`. Strona główna: `http://localhost:5000/`, scraper Shinden: `http://localhost:5000/shinden`.

## Wymagania

- Python 3.10+, Flask, requests
- playwright (+ Chromium, doinstalowane przez `install.bat`)
- imageio-ffmpeg (ffmpeg do łączenia wideo+audio w głównym downloaderze)

## Uwagi / ograniczenia

- 3–5 równoległych zadań = tyle samo niezależnych instancji headless Chromium na raz — przy słabszym komputerze zmniejsz `max_workers` w odpowiednich miejscach.
- Strony cda.pl / shinden.pl mogą zmieniać strukturę — w razie zmian logika w `cda/extractor.py` / `cda/shinden.py` może wymagać aktualizacji.
- Pobieranie treści powinno być zgodne z regulaminem serwisów i prawami autorskimi — projekt edukacyjny / na własne potrzeby.
