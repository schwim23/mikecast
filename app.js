/**
 * MikeCast Dashboard — Client-side application
 * Static version for GitHub Pages.
 * Loads briefing JSON and audio from the data/ directory in the repo.
 * Fetches data/manifest.json to populate the archive dropdown.
 */
(function () {
  "use strict";

  const datePicker    = document.getElementById("date-picker");
  const btnToday      = document.getElementById("btn-today");
  const btnPrev       = document.getElementById("btn-prev");
  const btnNext       = document.getElementById("btn-next");
  const archiveSelect = document.getElementById("archive-select");
  const loadingEl     = document.getElementById("loading");
  const errorEl       = document.getElementById("error");
  const contentEl     = document.getElementById("content");
  const briefingHtml  = document.getElementById("briefing-html");
  const podcastPlayer = document.getElementById("podcast-player");
  const podcastMeta   = document.getElementById("podcast-meta");
  const noAudioEl     = document.getElementById("no-audio");
  const articlesList  = document.getElementById("articles-list");
  const scriptToggle  = document.getElementById("script-toggle");
  const scriptContent = document.getElementById("script-content");
  const scriptArrow   = document.getElementById("script-arrow");
  const scriptText    = document.getElementById("script-text");
  const footerDate    = document.getElementById("footer-date");

  function todayStr() {
    const d = new Date();
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function shiftDate(dateStr, days) {
    const d = new Date(dateStr + "T12:00:00");
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function formatDisplayDate(dateStr) {
    const d = new Date(dateStr + "T12:00:00");
    return d.toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  async function loadManifest() {
    try {
      const resp = await fetch("data/manifest.json");
      if (!resp.ok) return;
      const data = await resp.json();
      const dates = data.dates || [];
      archiveSelect.innerHTML = '<option value="">Archive &#9662;</option>';
      for (const d of dates) {
        const opt = document.createElement("option");
        opt.value = d;
        opt.textContent = formatDisplayDate(d);
        archiveSelect.appendChild(opt);
      }
    } catch (e) { /* manifest unavailable */ }
  }

  async function loadBriefing(dateStr) {
    contentEl.style.display = "none";
    errorEl.style.display   = "none";
    loadingEl.style.display = "block";
    try {
      const resp = await fetch(`data/${dateStr}.json`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      renderBriefing(data, dateStr);
    } catch (err) {
      loadingEl.style.display = "none";
      errorEl.textContent = `No briefing available for ${formatDisplayDate(dateStr)}.`;
      errorEl.style.display = "block";
    }
  }

  function renderBriefing(data, dateStr) {
    loadingEl.style.display = "none";

    // 1. HTML briefing
    if (data.html_briefing) {
      const tmp = document.createElement("div");
      tmp.innerHTML = data.html_briefing;
      const innerBody = tmp.querySelector("body");
      briefingHtml.innerHTML = innerBody ? innerBody.innerHTML : tmp.innerHTML;
    } else {
      briefingHtml.innerHTML = "<p class='muted'>No briefing content for this edition.</p>";
    }

    // 2. Podcast audio
    if (data.audio_file) {
      podcastPlayer.src = `data/${data.audio_file}`;
      podcastPlayer.style.display = "block";
      noAudioEl.style.display = "none";
      if (podcastMeta) podcastMeta.textContent = "tts-1-hd \u00b7 alloy \u00b7 OpenAI";
    } else {
      podcastPlayer.style.display = "none";
      noAudioEl.style.display = "block";
      if (podcastMeta) podcastMeta.textContent = "";
    }

    // 3. Article links
    articlesList.innerHTML = "";
    const articles = data.articles || {};
    for (const [cat, arts] of Object.entries(articles)) {
      if (!arts || arts.length === 0) continue;
      const div = document.createElement("div");
      div.className = "article-category";
      let html = `<h3>${escapeHtml(cat)}</h3><ul>`;
      for (const art of arts) {
        const rawTitle = art.title || "Untitled";
        const isUpdated = rawTitle.startsWith("[Updated]");
        const title = escapeHtml(rawTitle.replace(/^\[Updated\]\s*/, ""));
        const url = art.url || "#";
        const source = art.source ? `<span class="article-source">\u2014 ${escapeHtml(art.source)}</span>` : "";
        const badge = isUpdated ? '<span style="background:#ffa657;color:#000;font-size:.7rem;padding:1px 5px;border-radius:3px;margin-right:4px;">Updated</span>' : "";
        html += `<li>${badge}<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${title}</a>${source}</li>`;
      }
      html += "</ul>";
      div.innerHTML = html;
      articlesList.appendChild(div);
    }

    // Mike's Picks
    const picks = data.mikes_picks || [];
    if (picks.length > 0) {
      const div = document.createElement("div");
      div.className = "article-category";
      let html = `<h3>\ud83c\udfaf Mike's Picks</h3><ul>`;
      for (const p of picks) {
        const title = escapeHtml(p.title || "Untitled");
        html += p.url
          ? `<li><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${title}</a></li>`
          : `<li>${title}</li>`;
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
    scriptContent.style.display = "none";
    scriptArrow.classList.remove("open");

    // 5. Footer
    if (footerDate) {
      footerDate.textContent = data.generated_at
        ? new Date(data.generated_at).toLocaleString("en-US", { dateStyle: "long", timeStyle: "short" })
        : formatDisplayDate(dateStr);
    }

    contentEl.style.display = "flex";
    if (archiveSelect) archiveSelect.value = dateStr;
  }

  datePicker.addEventListener("change", function () { loadBriefing(this.value); });
  btnToday.addEventListener("click", function () {
    (async () => {
      try {
        const resp = await fetch("data/manifest.json");
        if (resp.ok) {
          const data = await resp.json();
          const latest = (data.dates || [])[0] || todayStr();
          datePicker.value = latest;
          loadBriefing(latest);
        } else {
          const t = todayStr(); datePicker.value = t; loadBriefing(t);
        }
      } catch (e) {
        const t = todayStr(); datePicker.value = t; loadBriefing(t);
      }
    })();
  });
  btnPrev.addEventListener("click", function () {
    const prev = shiftDate(datePicker.value || todayStr(), -1);
    datePicker.value = prev; loadBriefing(prev);
  });
  btnNext.addEventListener("click", function () {
    const next = shiftDate(datePicker.value || todayStr(), 1);
    if (next > todayStr()) return;
    datePicker.value = next; loadBriefing(next);
  });
  if (archiveSelect) {
    archiveSelect.addEventListener("change", function () {
      if (this.value) { datePicker.value = this.value; loadBriefing(this.value); }
    });
  }
  scriptToggle.addEventListener("click", function () {
    const isOpen = scriptContent.style.display !== "none";
    scriptContent.style.display = isOpen ? "none" : "block";
    scriptArrow.classList.toggle("open", !isOpen);
  });

  const today = todayStr();
  datePicker.max = today;

  // Load manifest first, then default to the most recent available date
  (async () => {
    try {
      const resp = await fetch("data/manifest.json");
      if (resp.ok) {
        const data = await resp.json();
        const dates = data.dates || [];
        archiveSelect.innerHTML = '<option value="">Archive &#9662;</option>';
        for (const d of dates) {
          const opt = document.createElement("option");
          opt.value = d;
          opt.textContent = formatDisplayDate(d);
          archiveSelect.appendChild(opt);
        }
        const latestDate = dates.length > 0 ? dates[0] : today;
        datePicker.value = latestDate;
        loadBriefing(latestDate);
      } else {
        datePicker.value = today;
        loadBriefing(today);
      }
    } catch (e) {
      datePicker.value = today;
      loadBriefing(today);
    }
  })();

})();
