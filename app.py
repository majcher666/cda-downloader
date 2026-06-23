import os
import threading
import uuid

from flask import Flask, jsonify, render_template, request, send_from_directory

from cda.extractor import process_tasks
from cda.shinden import build_tasks_from_lines, resolve_many

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
SHINDEN_DEBUG_DIR = os.path.join(DOWNLOADS_DIR, "shinden_debug")

# ---------------------------------------------------------------------------
# Główny downloader cda.pl ("/") - przyjmuje TYLKO zwykłe linki do filmów.
# ---------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()


def _run_job(job_id: str, lines: list[str]) -> None:
    tasks = [
        {"display": line, "cda_url": line, "episode_url": None, "error": None}
        for line in lines
    ]

    with JOBS_LOCK:
        JOBS[job_id]["links"] = [
            {
                "index": i + 1,
                "input": task["display"],
                "log": ["W kolejce..."],
                "percent": 0,
                "done": False,
                "result": None,
            }
            for i, task in enumerate(tasks)
        ]

    def cb(idx: int, text: str, percent=None) -> None:
        with JOBS_LOCK:
            link = JOBS[job_id]["links"][idx - 1]
            link["log"].append(text)
            if percent is not None:
                link["percent"] = percent

    try:
        results = process_tasks(tasks, DOWNLOADS_DIR, progress_cb=cb, max_workers=5)
        with JOBS_LOCK:
            for i, res in enumerate(results):
                link = JOBS[job_id]["links"][i]
                link["result"] = res
                link["done"] = True
                if res and res.get("download_filename"):
                    link["percent"] = 100
            JOBS[job_id]["done"] = True
    except Exception as exc:
        with JOBS_LOCK:
            for link in JOBS[job_id]["links"]:
                if not link["done"]:
                    link["done"] = True
                    link["result"] = {"ok": False, "error": f"Błąd krytyczny: {exc}", "files": []}
            JOBS[job_id]["done"] = True


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    raw_input = request.form.get("links", "")
    lines = [line.strip() for line in raw_input.splitlines() if line.strip()]

    if not lines:
        return jsonify({"error": "Nie podano żadnego linku."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"links": [], "done": False}

    thread = threading.Thread(target=_run_job, args=(job_id, lines), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Nieznane zadanie."}), 404
        payload = {"links": [dict(link) for link in job["links"]], "done": job["done"]}
    return jsonify(payload)


@app.route("/download/<filename>", methods=["GET"])
def download_file(filename):
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Strona "/shinden" - osobne narzędzie: link do listy odcinków serii
# na shinden.pl -> lista znalezionych linków cda.pl (z polskimi napisami).
# Nie pobiera plików - tylko wyszukuje linki, które można wkleić w
# głównym downloaderze ("/").
# ---------------------------------------------------------------------------

SHINDEN_JOBS = {}
SHINDEN_JOBS_LOCK = threading.Lock()


def _run_shinden_job(job_id: str, lines: list[str]) -> None:
    with SHINDEN_JOBS_LOCK:
        SHINDEN_JOBS[job_id]["phase"] = "discovering"

    def discovery_cb(text: str) -> None:
        with SHINDEN_JOBS_LOCK:
            SHINDEN_JOBS[job_id]["discovery_log"].append(text)

    try:
        tasks = build_tasks_from_lines(lines, progress_cb=discovery_cb)
    except Exception as exc:
        with SHINDEN_JOBS_LOCK:
            SHINDEN_JOBS[job_id]["discovery_log"].append(f"Błąd krytyczny: {exc}")
            SHINDEN_JOBS[job_id]["phase"] = "done"
            SHINDEN_JOBS[job_id]["done"] = True
        return

    with SHINDEN_JOBS_LOCK:
        SHINDEN_JOBS[job_id]["episodes"] = [
            {
                "index": i + 1,
                "label": task["display"],
                "log": ["W kolejce..."],
                "done": False,
                "ok": False,
                "cda_url": None,
                "error": None,
                "debug_file": None,
            }
            for i, task in enumerate(tasks)
        ]
        SHINDEN_JOBS[job_id]["phase"] = "processing"

    def cb(idx: int, text: str) -> None:
        with SHINDEN_JOBS_LOCK:
            SHINDEN_JOBS[job_id]["episodes"][idx - 1]["log"].append(text)

    try:
        results = resolve_many(tasks, progress_cb=cb, max_workers=5)
        with SHINDEN_JOBS_LOCK:
            for i, res in enumerate(results):
                ep = SHINDEN_JOBS[job_id]["episodes"][i]
                ep["done"] = True
                ep["ok"] = res.get("ok", False)
                ep["cda_url"] = res.get("cda_url")
                ep["error"] = res.get("error")
                ep["debug_file"] = res.get("debug_file")
            SHINDEN_JOBS[job_id]["phase"] = "done"
            SHINDEN_JOBS[job_id]["done"] = True
    except Exception as exc:
        with SHINDEN_JOBS_LOCK:
            for ep in SHINDEN_JOBS[job_id]["episodes"]:
                if not ep["done"]:
                    ep["done"] = True
                    ep["error"] = f"Błąd krytyczny: {exc}"
            SHINDEN_JOBS[job_id]["phase"] = "done"
            SHINDEN_JOBS[job_id]["done"] = True


@app.route("/shinden", methods=["GET"])
def shinden_page():
    return render_template("shinden.html")


@app.route("/shinden/start", methods=["POST"])
def shinden_start():
    raw_input = request.form.get("series_links", "")
    lines = [line.strip() for line in raw_input.splitlines() if line.strip()]

    if not lines:
        return jsonify({"error": "Nie podano żadnego linku do serii."}), 400

    job_id = uuid.uuid4().hex
    with SHINDEN_JOBS_LOCK:
        SHINDEN_JOBS[job_id] = {
            "phase": "discovering",
            "discovery_log": ["Sprawdzam podane linki..."],
            "episodes": [],
            "done": False,
        }

    thread = threading.Thread(target=_run_shinden_job, args=(job_id, lines), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/shinden/status/<job_id>", methods=["GET"])
def shinden_status(job_id):
    with SHINDEN_JOBS_LOCK:
        job = SHINDEN_JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Nieznane zadanie."}), 404
        payload = {
            "phase": job["phase"],
            "discovery_log": list(job["discovery_log"]),
            "episodes": [dict(ep) for ep in job["episodes"]],
            "done": job["done"],
        }
    return jsonify(payload)


@app.route("/shinden/debug/<filename>", methods=["GET"])
def shinden_debug_file(filename):
    return send_from_directory(SHINDEN_DEBUG_DIR, filename)


if __name__ == "__main__":
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    app.run(debug=True, threaded=True)
