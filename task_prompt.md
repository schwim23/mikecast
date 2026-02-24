## MikeCast Daily Briefing — Scheduled Task

**Objective:**
Execute the enhanced MikeCast daily news briefing system. This involves cloning the repository, running the main Python script to collect news, generate content, distribute the briefing, and then committing and pushing the updated state files.

**Trigger:**
This task should be scheduled to run daily at 7:00 AM EST.

**Execution Steps:**

1.  **Clone the repository:**
    ```bash
    git clone https://$GH_TOKEN@github.com/schwim23/mikecast.git /home/ubuntu/mikecast
    ```

2.  **Navigate to Project Directory:**
    ```bash
    cd /home/ubuntu/mikecast
    ```

3.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the Briefing Script:**
    Execute the main briefing script using Python 3.
    ```bash
    python3 mikecast_briefing.py
    ```

5.  **Commit and Push Updated State Files:**
    After a successful run, commit and push the updated `briefing_history.json`, `mikes_picks.json`, and the `data/` directory.
    ```bash
    cd /home/ubuntu/mikecast
    git config user.email "prometheusagent23@gmail.com"
    git config user.name "MikeCast Bot"
    git add briefing_history.json mikes_picks.json data/
    git commit -m "MikeCast briefing - $(date +%Y-%m-%d)"
    git push
    ```

**Environment Variables:**
- `NYTAPIKEY`
- `OPENAI_API_KEY`
- `GMAIL_APP_PASSWORD`
- `GMAIL_FROM` (defaults to prometheusagent23@gmail.com)
- `GMAIL_TO` (defaults to Michael.schwimmer@gmail.com)
- `GH_TOKEN`
