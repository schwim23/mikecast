/**
 * MikeCast Dashboard — Client-side application
 * ==============================================
 * Loads daily briefing JSON from the relative `data/` directory and renders
 * the briefing, article links, podcast player, and script viewer.
 *
 * This is a zero-dependency static site — no build step, no framework.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------
  const datePicker     = document.getElementById("date-picker");
  const btnToday       = document.getElementById("btn-today");
  const btnPrev        = document.getElementById("btn-prev");
  const btnNext        = document.getElementById("btn-next");
  const loadingEl      = document.getElementById("loading");
  const errorEl        = document.getElementById("error");
  const contentEl      = document.getElementById("content");
  const briefingHtml   = document.getElementById("briefing-html");
  const podcastPlayer  = document.getElementById("podcast-player");
  const noAudioEl      = document.getElementById("no-audio");
  const articlesList   = document.getElementById("articles-list");
  const scriptToggle   = document.getElementById("script-toggle");
  const scriptContent  = document.getElementById("script-content");
  const scriptArrow    = document.getElementById("script-arrow");
  const scriptText     = document.getElementById("script-text");

  // ---------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------

  /** Return today's date as YYYY-MM-DD in local timezone. */
  function todayStr() {
    const d = new Date();
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  /** Shift a YYYY-MM-DD string by `days` (positive = forward). */
  function shiftDate(dateStr, days) {
    const d = new Date(dateStr + "T12:00:00");  // noon to avoid DST issues
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  /** Determine the base path for data files. */
  function dataBasePath() {
    // Works when dashboard/ is a sibling of data/
    // i.e. served from mikecast/ root, or from dashboard/ with ../data/
    // Try relative path first
    return "../data";
  }

  // ---------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------

  async function loadBriefing(dateStr) {
    // Reset UI
    contentEl.style.display = "none";
    errorEl.style.display   = "none";
    loadingEl.style.display = "block";

    const basePath = dataBasePath();
    const url = `${basePath}/${dateStr}.json`;

    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      renderBriefing(data, dateStr);
    } catch (err) {
      loadingEl.style.display = "none";
      errorEl.textContent = `No briefing available for ${dateStr}.`;
      errorEl.style.display = "block";
      console.warn("Load error:", err);
    }
  }

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------

  function renderBriefing(data, dateStr) {
    loadingEl.style.display = "none";

    // 1. HTML briefing
    if (data.html_briefing) {
      briefingHtml.innerHTML = data.html_briefing;
      // Strip the outer <html>/<body> wrapper if present
      const innerBody = briefingHtml.querySelector("body");
      if (innerBody) {
        briefingHtml.innerHTML = innerBody.innerHTML;
      }
    } else {
      briefingHtml.innerHTML = "<p class='muted'>No briefing content.</p>";
    }

    // 2. Podcast audio
    if (data.audio_file) {
      const audioUrl = `${dataBasePath()}/${data.audio_file}`;
      podcastPlayer.src = audioUrl;
      podcastPlayer.style.display = "block";
      noAudioEl.style.display = "none";
    } else {
      podcastPlayer.style.display = "none";
      noAudioEl.style.display = "block";
    }

    // 3. Article links by category
    articlesList.innerHTML = "";
    const articles = data.articles || {};
    for (const [cat, arts] of Object.entries(articles)) {
      if (!arts || arts.length === 0) continue;
      const div = document.createElement("div");
      div.className = "article-category";
      let html = `<h3>${escapeHtml(cat)}</h3><ul>`;
      for (const art of arts) {
        const title  = escapeHtml(art.title || "Untitled");
        const url    = art.url || "#";
        const source = art.source ? `<span class="article-source">— ${escapeHtml(art.source)}</span>` : "";
        html += `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${title}</a>${source}</li>`;
      }
      html += "</ul>";
      div.innerHTML = html;
      articlesList.appendChild(div);
    }

    // Mike's Picks in article links
    const picks = data.mikes_picks || [];
    if (picks.length > 0) {
      const div = document.createElement("div");
      div.className = "article-category";
      let html = `<h3>🎯 Mike's Picks</h3><ul>`;
      for (const p of picks) {
        const title = escapeHtml(p.title || "Untitled");
        if (p.url) {
          html += `<li><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${title}</a></li>`;
        } else {
          html += `<li>${title}</li>`;
        }
      }
      html += "</ul>";
      div.innerHTML = html;
      articlesList.appendChild(div);
    }

    // 4. Podcast script
    if (data.podcast_script) {
      scriptText.textContent = data.podcast_script;
      document.getElementById("script-section").style.display = "block";
    } else {
      document.getElementById("script-section").style.display = "none";
    }

    // Collapse script by default
    scriptContent.style.display = "none";
    scriptArrow.classList.remove("open");

    contentEl.style.display = "flex";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // ---------------------------------------------------------------
  // Event listeners
  // ---------------------------------------------------------------

  datePicker.addEventListener("change", function () {
    loadBriefing(this.value);
  });

  btnToday.addEventListener("click", function () {
    const today = todayStr();
    datePicker.value = today;
    loadBriefing(today);
  });

  btnPrev.addEventListener("click", function () {
    const current = datePicker.value || todayStr();
    const prev = shiftDate(current, -1);
    datePicker.value = prev;
    loadBriefing(prev);
  });

  btnNext.addEventListener("click", function () {
    const current = datePicker.value || todayStr();
    const next = shiftDate(current, 1);
    datePicker.value = next;
    loadBriefing(next);
  });

  scriptToggle.addEventListener("click", function () {
    const isOpen = scriptContent.style.display !== "none";
    scriptContent.style.display = isOpen ? "none" : "block";
    scriptArrow.classList.toggle("open", !isOpen);
  });

  // ---------------------------------------------------------------
  // Initialise — load today's briefing
  // ---------------------------------------------------------------
  const today = todayStr();
  datePicker.value = today;
  datePicker.max   = today;
  loadBriefing(today);

})();
