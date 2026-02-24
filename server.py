#!/usr/bin/env python3
"""
MikeCast Dashboard Server
=========================
Serves the static dashboard files and the data/ directory (JSON + MP3).
Also exposes a /api/manifest endpoint listing all available briefing dates.
"""

import json
import os
from pathlib import Path
from flask import Flask, send_from_directory, jsonify, abort

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
DASHBOARD_DIR = SCRIPT_DIR / "dashboard"

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------------------
# API: manifest of available dates
# ---------------------------------------------------------------------------
@app.route("/api/manifest")
def manifest():
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("????-??-??.json")],
        reverse=True,
    )
    return jsonify({"dates": dates})


# ---------------------------------------------------------------------------
# Data files: JSON briefings and MP3 audio
# ---------------------------------------------------------------------------
@app.route("/data/<path:filename>")
def data_files(filename):
    return send_from_directory(DATA_DIR, filename)


# ---------------------------------------------------------------------------
# Dashboard static files
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    # Serve from dashboard dir
    target = DASHBOARD_DIR / filename
    if target.exists() and target.is_file():
        return send_from_directory(DASHBOARD_DIR, filename)
    abort(404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"MikeCast Dashboard running on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
