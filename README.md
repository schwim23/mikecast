# MikeCast: Enhanced Daily News Briefing System

MikeCast is an automated daily news briefing system designed for "Big Mike." It generates a comprehensive and personalized news package covering AI/Tech, Business, major companies, and NY sports. The system fetches the latest news, integrates user-submitted content, and delivers the briefing as an HTML email, a podcast, and a static dashboard website.

This project is an enhanced version of a previous Manus scheduled task, now with NYT API integration, content deduplication, a user-submission system, and a web-based dashboard for browsing past briefings.

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
├── mikes_picks.json          # Queue for user-submitted content
├── briefing_history.json     # Rolling 7-day history of processed articles
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── task_prompt.md            # Updated Manus scheduled task prompt
├── data/                     # Directory for daily JSON and audio files
│   └── YYYY-MM-DD.json
└── dashboard/                # Static website files
    ├── index.html
    ├── style.css
    └── app.js
```

## Setup and Installation

1.  **Clone the Project:**
    Place the `mikecast` directory in your desired location (e.g., `/home/ubuntu/`).

2.  **Install Dependencies:**
    Install the required Python libraries using pip.
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set Environment Variables:**
    This system uses environment variables for all secrets and configuration. **Do not hardcode them.**

    ```bash
    export NYTAPIKEY="your_new_york_times_api_key"
    export OPENAI_API_KEY="your_openai_api_key"
    export GMAIL_APP_PASSWORD="your_gmail_app_password"

    # Optional: Override default email addresses
    export GMAIL_FROM="sender@gmail.com"
    export GMAIL_TO="recipient@example.com"
    ```

    *   `NYTAPIKEY`: Get from the [NYT Developer Portal](https://developer.nytimes.com/).
    *   `OPENAI_API_KEY`: Get from your [OpenAI account](https://platform.openai.com/api-keys).
    *   `GMAIL_APP_PASSWORD`: This is a 16-digit "App Password" generated from your Google Account settings, not your regular password. See [Google's documentation](https://support.google.com/accounts/answer/185833) for instructions.

## Usage

### Generating the Daily Briefing

To run the main briefing process, simply execute the `mikecast_briefing.py` script. This is typically done via a scheduled task (see `task_prompt.md`).

```bash
python3 /path/to/mikecast/mikecast_briefing.py
```

The script will perform all steps automatically: fetching news, generating content, saving files to the `data/` directory, and sending the final email.

### Adding to "Mike's Picks"

Use the `mikes_picks_ingest.py` utility to add content to the next briefing.

*   **Add a URL:**
    ```bash
    python3 mikes_picks_ingest.py --url "https://www.theverge.com/2024/2/21/24079459/google-gemini-ai-image-generation-people"
    ```

*   **Add a local PDF file:**
    ```bash
    python3 mikes_picks_ingest.py --pdf "/home/ubuntu/Documents/research_paper.pdf"
    ```

*   **Add raw text (e.g., from a newsletter):**
    ```bash
    python3 mikes_picks_ingest.py --text "This is some interesting analysis I read..."
    ```

*   **Add an item with a custom title:**
    ```bash
    python3 mikes_picks_ingest.py --url "https://example.com" --title "An Interesting Article I Found"
    ```

### Viewing the Dashboard

To view the dashboard, you need to serve the `mikecast` directory from a simple web server. The dashboard files in `dashboard/` are designed to fetch data from the sibling `data/` directory.

1.  **Navigate to the project root:**
    ```bash
    cd /path/to/mikecast
    ```

2.  **Start a simple Python web server:**
    ```bash
    python3 -m http.server 8000
    ```

3.  **Access the dashboard:**
    Open your web browser and go to `http://localhost:8000/dashboard/`.

    The dashboard will load the current day's briefing by default. Use the date picker to browse previous days.
