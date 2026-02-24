# MikeCast: Enhanced Daily News Briefing System

MikeCast is an automated daily news briefing system designed for "Big Mike." It generates a comprehensive and personalized news package covering AI/Tech, Business, major companies, and NY sports. The system fetches the latest news, integrates user-submitted content, and delivers the briefing as an HTML email, a podcast, and a static dashboard website.

## Features

1.  **Multi-Source News Aggregation**: Collects articles from:
    *   **NYT Top Stories API**: Pulls top headlines from relevant sections.
    *   **NYT Article Search API**: Performs targeted keyword searches for each category.
    *   **Google News (RSS)**: A fallback search to ensure broad coverage.

2.  **Content Deduplication**: Maintains a 7-day history of processed articles (`briefing_history.json`) to avoid repeating stories. It identifies significant updates to previously seen stories and flags them with an `[Updated]` tag.

3.  **"Mike's Picks"**: A dedicated section in the briefing for user-submitted content. A separate utility script (`mikes_picks_ingest.py`) allows the user to easily add URLs, local PDFs, or raw text snippets to a queue (`mikes_picks.json`) for inclusion.

4.  **Multi-Format Generation**:
    *   **HTML Briefing**: A clean, professional HTML document with an executive summary, categorized stories, and clickable links.
    *   **Podcast Script & Audio**: A conversational 5-10 minute podcast script is generated and then converted to high-quality audio using the OpenAI TTS API.
    *   **JSON Data File**: All generated content for the day is saved to a dated JSON file (e.g., `data/2026-02-22.json`), which powers the dashboard.

5.  **Email Distribution**: The complete package (HTML briefing in the body, podcast script and audio as attachments) is emailed to the user via Gmail SMTP.

6.  **Static Dashboard Website**: A responsive, dark-themed static website (`dashboard/`) allows the user to browse and review briefings from any date. It features a date picker, an embedded audio player, and a collapsible script viewer.

## Project Structure

```
mikecast/
├── mikecast_briefing.py      # Main script to generate and send the briefing
├── mikes_picks_ingest.py     # Utility to add items to "Mike's Picks"
├── run_mikecast.sh           # Cron wrapper: sets env vars, runs script, pushes to GitHub
├── mikes_picks.json          # Queue for user-submitted content
├── briefing_history.json     # Rolling 7-day history of processed articles
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── task_prompt.md            # Original Manus scheduled task prompt
├── .venv/                    # Python virtual environment (not committed)
├── data/                     # Directory for daily JSON and audio files
│   └── YYYY-MM-DD.json
└── dashboard/                # Static website files
    ├── index.html
    ├── style.css
    └── app.js
```

## Setup and Installation (Local Linux)

This project runs locally on a Linux machine (Ubuntu/Debian) with Python 3.12+.

### 1. Clone the Repository

```bash
cd ~
git clone https://github.com/schwim23/mikecast.git
cd mikecast
```

### 2. Install System Dependencies

```bash
sudo apt install -y python3.12-venv python3-pip poppler-utils
```

`poppler-utils` provides `pdftotext`, which is required for PDF ingestion in Mike's Picks.

### 3. Create a Virtual Environment and Install Python Dependencies

```bash
cd ~/mikecast
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Set Environment Variables

Add the following to both `~/.bashrc` (interactive terminals) and `~/.profile` (login shells and cron):

```bash
export NYTAPIKEY="your_new_york_times_api_key"
export OPENAI_API_KEY="your_openai_api_key"
export GMAIL_APP_PASSWORD="your_16_digit_gmail_app_password"
export GMAIL_FROM="sender@gmail.com"
export GMAIL_TO="recipient@example.com"
```

**Important:** Cron jobs do not source `~/.bashrc`. The `run_mikecast.sh` wrapper script explicitly sources `~/.profile` at startup, so cron will always have access to the variables.

*   `NYTAPIKEY`: Get from the [NYT Developer Portal](https://developer.nytimes.com/).
*   `OPENAI_API_KEY`: Get from your [OpenAI account](https://platform.openai.com/api-keys).
*   `GMAIL_APP_PASSWORD`: A 16-digit App Password from your Google Account (not your regular password). See [Google's documentation](https://support.google.com/accounts/answer/185833).

### 5. Configure Git Credentials (for GitHub Pages auto-push)

After each run, the script automatically commits and pushes updated data to GitHub. To enable non-interactive pushes:

```bash
git -C ~/mikecast config credential.helper store
```

Then do one manual push (entering your GitHub username and a Personal Access Token as the password). Credentials will be stored in `~/.git-credentials` for all future automated pushes.

### 6. Test a Manual Run

```bash
source ~/.profile   # load env vars in current shell
cd ~/mikecast
.venv/bin/python3 mikecast_briefing.py
```

Verify: email is received, `data/YYYY-MM-DD.json` is created, and the GitHub repo is updated.

### 7. Schedule the Daily Cron Job

The cron job is already configured. To view or edit it:

```bash
crontab -e
```

Current entry (runs at **6:45 AM ET** daily):

```
45 6 * * * /home/mike-schwimmer/mikecast/run_mikecast.sh >> /home/mike-schwimmer/mikecast/mikecast.log 2>&1
```

The `run_mikecast.sh` wrapper:
- Sets all required environment variables
- `cd`s to the project directory
- Runs `mikecast_briefing.py` using the virtual environment
- Commits and pushes updated data to GitHub (which updates the GitHub Pages dashboard)

## Usage

### Generating the Daily Briefing

The briefing runs automatically via cron. To run manually:

```bash
cd ~/mikecast && .venv/bin/python3 mikecast_briefing.py
```

### Adding to "Mike's Picks"

Use the `mikes_picks_ingest.py` utility to queue content for the next briefing.

*   **Add a URL:**
    ```bash
    .venv/bin/python3 mikes_picks_ingest.py --url "https://example.com/article"
    ```

*   **Add a local PDF file:**
    ```bash
    .venv/bin/python3 mikes_picks_ingest.py --pdf "/home/mike-schwimmer/Documents/paper.pdf"
    ```

*   **Add raw text:**
    ```bash
    .venv/bin/python3 mikes_picks_ingest.py --text "Some interesting analysis..."
    ```

*   **Add with a custom title:**
    ```bash
    .venv/bin/python3 mikes_picks_ingest.py --url "https://example.com" --title "My Custom Title"
    ```

### Viewing the Dashboard

There are two ways to host the dashboard. Each has different trade-offs:

---

#### Option A: GitHub Pages (Remote, Auto-Updated)

After each daily run, `run_mikecast.sh` commits and pushes new data to GitHub, which automatically updates the public GitHub Pages site.

**URL:** `https://schwim23.github.io/mikecast/`

No setup required — it just works as long as the cron job is running and pushing.

> **Limitation:** The archive date-picker dropdown relies on a `/api/manifest` API call that GitHub Pages (a static host) cannot serve. Browsing by date via the dropdown will not work on GitHub Pages. All other dashboard features (today's briefing, audio player, article links) work fine.

---

#### Option B: Local Server (Full Functionality)

Run `server.py`, a lightweight Flask server that serves the dashboard and exposes the `/api/manifest` endpoint, enabling full archive navigation.

**Setup** (one time):
```bash
cd ~/mikecast
.venv/bin/pip install flask
```

**Run:**
```bash
cd ~/mikecast
.venv/bin/python3 server.py
```

Then open `http://localhost:8080/dashboard/` in your browser.

This gives you full functionality: the date-picker archive dropdown, audio playback, and article links all work. Run it in a `screen` or `tmux` session to keep it persistent, or wrap it in a systemd service for auto-start on boot.

### Monitoring

Check the cron log after each run:

```bash
cat ~/mikecast/mikecast.log
```
