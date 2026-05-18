"""Generate MikeCast CrewAI architecture diagram (--crew path)."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# ── colour palette ────────────────────────────────────────────────────────────
BG          = "#0d1117"
BORDER      = "#30363d"

C_XAI       = "#7c3aed"   # purple  – xAI Grok / planning
C_COLLECT   = "#0ea5e9"   # sky     – collection (legacy procedural)
C_PROCESS   = "#0284c7"   # blue    – dedup / cluster / select
C_SCORE     = "#6366f1"   # indigo  – scoring (GPT-4o)
C_SPORTS    = "#22c55e"   # green   – NY Sports crew (ESPN-backed)
C_GENERATE  = "#10b981"   # emerald – Claude writers
C_CRITIC    = "#f59e0b"   # amber   – critic
C_AUDIO     = "#ec4899"   # pink    – audio
C_DELIVER   = "#ef4444"   # red     – delivery
C_AGENT     = "#facc15"   # yellow  – CrewAI agent badge
C_MUTED     = "#8b949e"
C_WHITE     = "#f0f6fc"
C_ARROW     = "#58a6ff"

FIG_W, FIG_H = 20, 26

fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), facecolor=BG)
ax.set_facecolor(BG)
ax.set_xlim(0, FIG_W)
ax.set_ylim(0, FIG_H)
ax.axis("off")


def box(x, y, w, h, color, label, sublabel=None, fontsize=11, radius=0.35,
        label_color=C_WHITE):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        linewidth=1.5, edgecolor=color, facecolor=f"{color}22", zorder=3,
    )
    ax.add_patch(rect)
    cy = y + h / 2 + (0.14 if sublabel else 0)
    ax.text(x + w / 2, cy, label,
            ha="center", va="center", fontsize=fontsize, fontweight="bold",
            color=label_color, zorder=4)
    if sublabel:
        ax.text(x + w / 2, y + h / 2 - 0.26, sublabel,
                ha="center", va="center", fontsize=8.5, color=C_MUTED, zorder=4)


def small_box(x, y, w, h, color, label, fontsize=9, label_color=C_WHITE):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0,rounding_size=0.2",
        linewidth=1.2, edgecolor=color, facecolor=f"{color}33", zorder=4,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, label,
            ha="center", va="center", fontsize=fontsize,
            color=label_color, zorder=5)


def agent_badge(x, y, label):
    """Small yellow chip showing 'CrewAI Agent — <role>'."""
    rect = FancyBboxPatch(
        (x, y), 2.2, 0.28,
        boxstyle="round,pad=0,rounding_size=0.1",
        linewidth=1.0, edgecolor=C_AGENT, facecolor=f"{C_AGENT}44", zorder=6,
    )
    ax.add_patch(rect)
    ax.text(x + 1.1, y + 0.14, label,
            ha="center", va="center", fontsize=7.2, fontweight="bold",
            color=C_AGENT, zorder=7)


def llm_badge(x, y, label, color):
    rect = FancyBboxPatch(
        (x, y), 2.0, 0.26,
        boxstyle="round,pad=0,rounding_size=0.08",
        linewidth=1.0, edgecolor=color, facecolor=f"{color}55", zorder=6,
    )
    ax.add_patch(rect)
    ax.text(x + 1.0, y + 0.13, label,
            ha="center", va="center", fontsize=7.0, fontweight="bold",
            color=C_WHITE, zorder=7)


def arrow(x0, y0, x1, y1, color=C_ARROW, lw=1.8):
    ax.annotate("",
        xy=(x1, y1), xytext=(x0, y0),
        arrowprops=dict(arrowstyle="-|>", color=color, lw=lw, mutation_scale=14),
        zorder=5,
    )


def section_label(x, y, text, color):
    ax.text(x, y, text,
            ha="left", va="center", fontsize=8.5, fontweight="bold",
            color=color, alpha=0.95, zorder=4,
            bbox=dict(facecolor=f"{color}22", edgecolor="none",
                      boxstyle="round,pad=0.22"))


# ── title ─────────────────────────────────────────────────────────────────────
ax.text(FIG_W / 2, 25.3, "MikeCast — CrewAI Pipeline",
        ha="center", va="center", fontsize=28, fontweight="bold", color=C_WHITE)
ax.text(FIG_W / 2, 24.8,
        "End-to-end daily news brief & podcast  ·  --crew execution path",
        ha="center", va="center", fontsize=13, color=C_MUTED)
ax.plot([1, 19], [24.5, 24.5], color=BORDER, lw=1, zorder=3)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 0 — Planning Crew
# ══════════════════════════════════════════════════════════════════════════════
Y0 = 23.2
box(1.0, Y0, 18, 0.95, C_XAI,
    "STEP 0  ·  Planning Crew",
    "xai_grok_search tool  →  {dynamic_queries, trending_context, trending}",
    fontsize=12)
section_label(1.2, Y0 + 0.78, "crew/planning_crew.py", C_XAI)
llm_badge(15.6, Y0 + 0.5, "xAI Grok-3", C_XAI)
arrow(FIG_W / 2, Y0, FIG_W / 2, Y0 - 0.32)


# ══════════════════════════════════════════════════════════════════════════════
# STEPS 1–6 — Research (two columns: non-sports vs NY Sports)
# ══════════════════════════════════════════════════════════════════════════════
Y_RES_TOP = 22.6
Y_RES_BOT = 17.0
RES_H = Y_RES_TOP - Y_RES_BOT  # 5.6
container = FancyBboxPatch(
    (1.0, Y_RES_BOT), 18, RES_H,
    boxstyle="round,pad=0,rounding_size=0.4",
    linewidth=1.5, edgecolor=BORDER, facecolor="#11151b", zorder=2,
)
ax.add_patch(container)
ax.text(1.3, Y_RES_TOP - 0.28,
        "STEPS 1–6  ·  Research",
        fontsize=11, fontweight="bold", color=C_WHITE, zorder=4)
ax.text(1.3, Y_RES_TOP - 0.6,
        "Non-sports flows through legacy procedural pipeline  ·  NY Sports is the only CrewAI-driven research path",
        fontsize=9, color=C_MUTED, zorder=4)


# ─── Left column: non-sports research (procedural) ─────────────────────────
LX, LW = 1.4, 8.6
LY = Y_RES_TOP - 1.05
section_label(LX, LY, "Research Crew (non-sports)  ·  crew/research_crew.py", C_COLLECT)

# Source collection bar
LY -= 0.7
sources = ["NYT", "20+ RSS", "Reddit", "HN", "ESPN RSS", "Google News"]
sw = (LW - 0.2 - 0.1 * (len(sources) - 1)) / len(sources)
for i, s in enumerate(sources):
    small_box(LX + i * (sw + 0.1), LY - 0.55, sw, 0.55, C_COLLECT, s, fontsize=7.5)
ax.text(LX + LW / 2, LY + 0.05, "Step 1  ·  collect_all_news (parallel)",
        ha="center", va="bottom", fontsize=8.5, color=C_COLLECT)

# Procedural stages
LY -= 1.0
stages = [
    ("Step 2 — Deduplicate\n7-day history", C_PROCESS),
    ("Step 2b — Filter stale\n(max 3 days)", C_PROCESS),
    ("Step 3 — Cluster\nGPT-4o-mini", C_PROCESS),
]
sw2 = (LW - 0.2 - 0.1 * (len(stages) - 1)) / len(stages)
for i, (lbl, col) in enumerate(stages):
    small_box(LX + i * (sw2 + 0.1), LY - 0.7, sw2, 0.7, col, lbl, fontsize=7.5)

LY -= 1.05
stages2 = [
    ("Step 4 — Score & Rank\nGPT-4o per category", C_SCORE),
    ("Step 5 — Select top 25\nproportional", C_PROCESS),
    ("Step 6 — Enrich top 15\nGPT-4o-mini 'why it matters'", C_PROCESS),
]
for i, (lbl, col) in enumerate(stages2):
    small_box(LX + i * (sw2 + 0.1), LY - 0.7, sw2, 0.7, col, lbl, fontsize=7.5)

# Note
ax.text(LX + LW / 2, LY - 0.95,
        "Wraps mc_collect.* directly  ·  no LLM agents in this column",
        ha="center", va="center", fontsize=8, color=C_MUTED, style="italic")

# Down arrow out of left column to Step 7
arrow(LX + LW / 2, LY - 1.15, LX + LW / 2, Y_RES_BOT + 0.05)


# ─── Right column: NY Sports Crew ───────────────────────────────────────────
RX = 10.4
RW = 8.2
RY = Y_RES_TOP - 1.05
section_label(RX, RY, "NY Sports Crew  ·  crew/sports_research_crew.py", C_SPORTS)

# Gatekeeper (procedural filter, same as non-sports column)
RY -= 0.85
small_box(RX, RY - 0.55, RW, 0.55, C_SPORTS,
          "Gatekeeper  ·  SPORTS_TRUSTED_SOURCES allowlist  (filter_sports_by_trusted_sources)",
          fontsize=8)
ax.text(RX + RW / 2, RY + 0.05, "trust-list filter  ·  drops AOL & untrusted aggregators",
        ha="center", va="bottom", fontsize=7.5, color=C_MUTED)

# Researcher (real CrewAI Agent)
RY -= 0.95
researcher_h = 1.7
rect = FancyBboxPatch(
    (RX, RY - researcher_h), RW, researcher_h,
    boxstyle="round,pad=0,rounding_size=0.25",
    linewidth=1.6, edgecolor=C_SPORTS, facecolor=f"{C_SPORTS}22", zorder=4,
)
ax.add_patch(rect)
ax.text(RX + RW / 2, RY - 0.22, "NY Sports Researcher",
        ha="center", va="center", fontsize=10, fontweight="bold",
        color=C_WHITE, zorder=5)
agent_badge(RX + 0.15, RY - 0.45, "CrewAI Agent")
llm_badge(RX + RW - 2.15, RY - 0.45, "GPT-4o", C_SCORE)

ax.text(RX + RW / 2, RY - 0.7,
        "max_iter=15  ·  max_execution_time=180s",
        ha="center", va="center", fontsize=7.5, color=C_MUTED, zorder=5)

# ESPN tools row
tools = ["fetch_sports_box_score", "fetch_sports_standings", "fetch_team_injury_report"]
tw = (RW - 0.4 - 0.15 * (len(tools) - 1)) / len(tools)
for i, t in enumerate(tools):
    small_box(RX + 0.2 + i * (tw + 0.15), RY - researcher_h + 0.1,
              tw, 0.55, C_SPORTS, t, fontsize=6.8)
ax.text(RX + RW / 2, RY - researcher_h + 0.78,
        "ESPN site.api  ·  primary-source verification",
        ha="center", va="bottom", fontsize=7.2, color=C_MUTED)

# Output: verified_sports_facts
RY -= researcher_h + 0.55
small_box(RX, RY - 0.55, RW, 0.55, C_SPORTS,
          "→ verified_sports_facts  {team: 'Yankees beat Red Sox 7-3 …'}",
          fontsize=8)

# Down arrow out of right column to Step 7
arrow(RX + RW / 2, RY - 0.65, RX + RW / 2, Y_RES_BOT + 0.05)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Picks Crew
# ══════════════════════════════════════════════════════════════════════════════
Y7 = 16.45
box(1.0, Y7, 18, 0.7, C_COLLECT,
    "STEP 7  ·  Picks Crew    (pass-through to mc_collect.process_picks)",
    "URLs · PDFs · raw text  →  summarised pick objects",
    fontsize=10)
arrow(FIG_W / 2, Y7, FIG_W / 2, Y7 - 0.32)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8 — Writing Crew (3 parallel Claude agents)
# ══════════════════════════════════════════════════════════════════════════════
Y8 = 14.55
write_h = 1.55
rect = FancyBboxPatch(
    (1.0, Y8), 18, write_h,
    boxstyle="round,pad=0,rounding_size=0.35",
    linewidth=1.5, edgecolor=C_GENERATE, facecolor=f"{C_GENERATE}11", zorder=2,
)
ax.add_patch(rect)
ax.text(1.3, Y8 + write_h - 0.22,
        "STEP 8  ·  Writing Crew    (3 Claude agents running in parallel via ThreadPoolExecutor)",
        fontsize=11, fontweight="bold", color=C_GENERATE, zorder=4)

writers = [
    ("HTML Briefing Writer",
     "1200–1800 words  ·  sections in ALL CAPS\n[Source](URL) links  ·  wrapped in styled template"),
    ("Single-Voice Writer",
     "1800–2000 words  ·  10–14 min\nSpoken-form  ·  no URLs"),
    ("3-Voice Conversational Writer",
     "Tagged [MIKE]/[ELIZABETH]/[JESSE]\nELIZABETH covers tech/biz  ·  JESSE covers sports"),
]
ww = (18 - 0.4 - 0.3 * 2) / 3
for i, (title, sub) in enumerate(writers):
    bx = 1.2 + i * (ww + 0.3)
    rect = FancyBboxPatch(
        (bx, Y8 + 0.15), ww, write_h - 0.55,
        boxstyle="round,pad=0,rounding_size=0.22",
        linewidth=1.3, edgecolor=C_GENERATE, facecolor=f"{C_GENERATE}28", zorder=4,
    )
    ax.add_patch(rect)
    ax.text(bx + ww / 2, Y8 + write_h - 0.5, title,
            ha="center", va="center", fontsize=9.5, fontweight="bold",
            color=C_WHITE, zorder=5)
    ax.text(bx + ww / 2, Y8 + 0.55, sub,
            ha="center", va="center", fontsize=7.8, color=C_MUTED, zorder=5)
    agent_badge(bx + 0.1, Y8 + 0.18, "CrewAI Agent")
    llm_badge(bx + ww - 2.1, Y8 + 0.18, "Claude Sonnet 4.6", C_GENERATE)

arrow(FIG_W / 2, Y8, FIG_W / 2, Y8 - 0.32)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8b — Critic Crew  (Scorer + Patcher + NY Sports Fact-Checker observability)
# ══════════════════════════════════════════════════════════════════════════════
Y8b = 11.6
crit_h = 2.5
rect = FancyBboxPatch(
    (1.0, Y8b), 18, crit_h,
    boxstyle="round,pad=0,rounding_size=0.35",
    linewidth=1.5, edgecolor=C_CRITIC, facecolor=f"{C_CRITIC}11", zorder=2,
)
ax.add_patch(rect)
ax.text(1.3, Y8b + crit_h - 0.22,
        "STEP 8b  ·  Critic Crew",
        fontsize=11, fontweight="bold", color=C_CRITIC, zorder=4)
ax.text(1.3, Y8b + crit_h - 0.52,
        "Scorer grades each category 1–10  ·  sections < 7 patched by Patcher  ·  NY Sports NEVER patched  ·  fact-check observability always runs",
        fontsize=8.5, color=C_MUTED, zorder=4)

# Scorer
sx = 1.4
sw = 7.6
sh = 0.9
ScY = Y8b + 1.0   # scorer/patcher row
rect = FancyBboxPatch(
    (sx, ScY), sw, sh,
    boxstyle="round,pad=0,rounding_size=0.22",
    linewidth=1.3, edgecolor=C_CRITIC, facecolor=f"{C_CRITIC}28", zorder=4,
)
ax.add_patch(rect)
ax.text(sx + sw / 2, ScY + 0.62, "Section Quality Scorer",
        ha="center", va="center", fontsize=10, fontweight="bold",
        color=C_WHITE, zorder=5)
ax.text(sx + sw / 2, ScY + 0.3,
        "JSON: {category_scores, issues, overall_passed}",
        ha="center", va="center", fontsize=7.8, color=C_MUTED, zorder=5)
agent_badge(sx + 0.1, ScY + 0.04, "CrewAI Agent")
llm_badge(sx + sw - 2.1, ScY + 0.04, "GPT-4o", C_SCORE)

# Patcher
px = 11.0
pw = 7.6
rect = FancyBboxPatch(
    (px, ScY), pw, sh,
    boxstyle="round,pad=0,rounding_size=0.22",
    linewidth=1.3, edgecolor=C_CRITIC, facecolor=f"{C_CRITIC}28", zorder=4,
)
ax.add_patch(rect)
ax.text(px + pw / 2, ScY + 0.62, "Section Patcher",
        ha="center", va="center", fontsize=10, fontweight="bold",
        color=C_WHITE, zorder=5)
ax.text(px + pw / 2, ScY + 0.3,
        "Rewrites weak <h3>+<p> fragment  ·  per-category, NY Sports excluded",
        ha="center", va="center", fontsize=7.8, color=C_MUTED, zorder=5)
agent_badge(px + 0.1, ScY + 0.04, "CrewAI Agent")
llm_badge(px + pw - 2.1, ScY + 0.04, "Claude Sonnet 4.6", C_GENERATE)

# Scorer → Patcher arrow (conditional)
arrow(sx + sw, ScY + 0.45, px, ScY + 0.45, color=C_CRITIC, lw=1.5)
ax.text((sx + sw + px) / 2, ScY + 0.72, "if score < 7",
        ha="center", va="bottom", fontsize=7.2, color=C_CRITIC, style="italic")

# NY Sports Fact-Checker row (read-only observability — always runs)
FcY = Y8b + 0.18
fcw = 17.2
rect = FancyBboxPatch(
    (1.4, FcY), fcw, 0.7,
    boxstyle="round,pad=0,rounding_size=0.22",
    linewidth=1.3, edgecolor=C_SPORTS, facecolor=f"{C_SPORTS}22", zorder=4,
)
ax.add_patch(rect)
ax.text(1.55, FcY + 0.42, "NY Sports Fact-Checker  (read-only)",
        ha="left", va="center", fontsize=9.5, fontweight="bold",
        color=C_WHITE, zorder=5)
ax.text(1.55, FcY + 0.14,
        "validate_claim_against_articles for each sentence in NY Sports HTML + [JESSE] block  ·  logs WARNING for unsupported claims  ·  never patches",
        ha="left", va="center", fontsize=7.6, color=C_MUTED, zorder=5)
llm_badge(1.4 + fcw - 2.1, FcY + 0.16, "GPT-4o-mini", C_XAI)

arrow(FIG_W / 2, Y8b, FIG_W / 2, Y8b - 0.32)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 9 — Audio (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
Y9 = 10.5
aud_h = 1.55
rect = FancyBboxPatch(
    (1.0, Y9), 18, aud_h,
    boxstyle="round,pad=0,rounding_size=0.35",
    linewidth=1.5, edgecolor=C_AUDIO, facecolor=f"{C_AUDIO}11", zorder=2,
)
ax.add_patch(rect)
ax.text(1.3, Y9 + aud_h - 0.22,
        "STEP 9  ·  Audio Generation    (shared with --legacy path)",
        fontsize=11, fontweight="bold", color=C_AUDIO, zorder=4)
ax.text(1.3, Y9 + aud_h - 0.52,
        "ElevenLabs 3-voice preferred  ·  OpenAI TTS single-voice fallback",
        fontsize=8.5, color=C_MUTED, zorder=4)

small_box(1.4, Y9 + 0.15, 8.6, 0.85, C_AUDIO,
          "ElevenLabs 3-voice MP3\nMike · Elizabeth · Jesse  ·  ★ podcast feed",
          fontsize=8.5)
small_box(10.6, Y9 + 0.15, 8.0, 0.85, C_AUDIO,
          "OpenAI TTS  ·  tts-1-hd / alloy\nSingle-voice MP3  ·  email + backup",
          fontsize=8.5)

arrow(FIG_W / 2, Y9, FIG_W / 2, Y9 - 0.32)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 10 — Delivery (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
Y10 = 8.2
del_h = 1.85
rect = FancyBboxPatch(
    (1.0, Y10), 18, del_h,
    boxstyle="round,pad=0,rounding_size=0.35",
    linewidth=1.5, edgecolor=C_DELIVER, facecolor=f"{C_DELIVER}11", zorder=2,
)
ax.add_patch(rect)
ax.text(1.3, Y10 + del_h - 0.22,
        "STEP 10  ·  Save & Deliver    (shared with --legacy path)",
        fontsize=11, fontweight="bold", color=C_DELIVER, zorder=4)
ax.text(1.3, Y10 + del_h - 0.52,
        "Persist JSON  ·  upload to S3 + CloudFront (feed.xml with Cache-Control: no-cache)  ·  send email",
        fontsize=8.5, color=C_MUTED, zorder=4)

dels = [
    ("Gmail SMTP\nHTML email + audio", C_DELIVER),
    ("S3 + CloudFront\ndata/YYYY-MM-DD.json", C_DELIVER),
    ("manifest.json\nArchive index", C_DELIVER),
    ("RSS 2.0 feed.xml\nPodcast subscription", C_DELIVER),
]
dw = (18 - 0.4 - 0.2 * 3) / 4
for i, (lbl, col) in enumerate(dels):
    small_box(1.2 + i * (dw + 0.2), Y10 + 0.15, dw, 0.95, col, lbl, fontsize=8.5)


# ══════════════════════════════════════════════════════════════════════════════
# Final outputs
# ══════════════════════════════════════════════════════════════════════════════
Yout = 6.4
outs = [
    (1.0,  "[ Email Briefing ]",     C_DELIVER),
    (5.0,  "[ Podcast Episode ]",    C_AUDIO),
    (9.0,  "[ Web Dashboard ]",      C_COLLECT),
    (13.0, "[ RSS / Apple Podcasts ]", C_XAI),
]
for x, lbl, col in outs:
    arrow(x + 1.75, Y10, x + 1.75, Yout + 0.62)
    w = 3.5
    rect = FancyBboxPatch(
        (x, Yout), w, 0.62,
        boxstyle="round,pad=0,rounding_size=0.25",
        linewidth=1.5, edgecolor=col, facecolor=f"{col}33", zorder=3,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, Yout + 0.31, lbl,
            ha="center", va="center", fontsize=8.8, fontweight="bold",
            color=C_WHITE, zorder=4)

# YouTube/social (separate downstream)
x = 17.0
w = 2.6
rect = FancyBboxPatch(
    (x, Yout), w, 0.62,
    boxstyle="round,pad=0,rounding_size=0.25",
    linewidth=1.5, edgecolor=C_GENERATE, facecolor=f"{C_GENERATE}33", zorder=3,
)
ax.add_patch(rect)
ax.text(x + w / 2, Yout + 0.31, "[ YouTube + Ads ]",
        ha="center", va="center", fontsize=8.5, fontweight="bold",
        color=C_WHITE, zorder=4)


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar: where the real CrewAI agents live
# ══════════════════════════════════════════════════════════════════════════════
Ysb_top = 5.4
sb_h = 1.75
rect = FancyBboxPatch(
    (1.0, Ysb_top - sb_h), 8.5, sb_h,
    boxstyle="round,pad=0,rounding_size=0.3",
    linewidth=1.3, edgecolor=C_AGENT, facecolor=f"{C_AGENT}11", zorder=3,
)
ax.add_patch(rect)
ax.text(1.3, Ysb_top - 0.28,
        "CrewAI Agents (live)",
        fontsize=10, fontweight="bold", color=C_AGENT, zorder=4)
ax.text(1.3, Ysb_top - 0.55,
        "Real Agent + Task + Crew kickoffs  ·  the rest are procedural wrappers",
        fontsize=8, color=C_MUTED, zorder=4)
live_agents = [
    "•  NY Sports Researcher  (GPT-4o + ESPN tools)",
    "•  HTML / Single-Voice / 3-Voice Writers  (Claude × 3 parallel)",
    "•  Section Quality Scorer  (GPT-4o)",
    "•  Section Patcher  (Claude, NY Sports excluded)",
]
for i, line in enumerate(live_agents):
    ax.text(1.4, Ysb_top - 0.85 - i * 0.22, line,
            ha="left", va="center", fontsize=8.2, color=C_WHITE, zorder=4)


# Sidebar: hallucination invariants
rect = FancyBboxPatch(
    (10.0, Ysb_top - sb_h), 9.0, sb_h,
    boxstyle="round,pad=0,rounding_size=0.3",
    linewidth=1.3, edgecolor=C_CRITIC, facecolor=f"{C_CRITIC}11", zorder=3,
)
ax.add_patch(rect)
ax.text(10.3, Ysb_top - 0.28,
        "Hallucination guardrails (carried verbatim from legacy)",
        fontsize=10, fontweight="bold", color=C_CRITIC, zorder=4)
inv = [
    "•  Only discuss articles present in input (no training knowledge)",
    "•  SPORTS TEAM rule  ·  don't infer team affiliations or rosters",
    "•  NY Sports section never auto-patched by critic",
    "•  Sports allowlist: SPORTS_TRUSTED_SOURCES (fail-closed)",
    "•  7/10 critic threshold  ·  max_iter = 1  ·  no retry loop",
]
for i, line in enumerate(inv):
    ax.text(10.4, Ysb_top - 0.55 - i * 0.22, line,
            ha="left", va="center", fontsize=8.0, color=C_WHITE, zorder=4)


# ══════════════════════════════════════════════════════════════════════════════
# Legend
# ══════════════════════════════════════════════════════════════════════════════
Yleg = 3.1
ax.plot([1, 19], [Yleg + 0.6, Yleg + 0.6], color=BORDER, lw=0.8, zorder=3)
ax.text(1.2, Yleg + 0.32, "Legend",
        ha="left", va="center", fontsize=10, fontweight="bold", color=C_WHITE)

legend_items = [
    (C_XAI,     "xAI / Planning"),
    (C_COLLECT, "Data Collection"),
    (C_PROCESS, "Procedural"),
    (C_SCORE,   "GPT-4o Scoring"),
    (C_SPORTS,  "NY Sports Crew"),
    (C_GENERATE,"Claude Writers"),
    (C_CRITIC,  "Critic"),
    (C_AUDIO,   "Audio / TTS"),
    (C_DELIVER, "Delivery"),
]
lx = 3.0
ly = Yleg + 0.32
for col, lbl in legend_items:
    rect = FancyBboxPatch(
        (lx, ly - 0.13), 0.26, 0.26,
        boxstyle="round,pad=0,rounding_size=0.06",
        linewidth=1, edgecolor=col, facecolor=f"{col}55", zorder=4,
    )
    ax.add_patch(rect)
    ax.text(lx + 0.35, ly, lbl, ha="left", va="center",
            fontsize=8, color=C_MUTED, zorder=4)
    lx += 1.85


# Provider badges row
Ypb = 2.3
ax.text(1.2, Ypb + 0.15, "LLM Providers",
        ha="left", va="center", fontsize=10, fontweight="bold", color=C_WHITE)
providers = [
    ("Anthropic Claude Sonnet 4.6", C_GENERATE, "writers, patcher"),
    ("OpenAI GPT-4o",               C_SCORE,    "scorers, critic"),
    ("OpenAI GPT-4o-mini",          C_PROCESS,  "cluster, enrich, planner, picks"),
    ("xAI Grok-3",                  C_XAI,      "live web + X search"),
    ("ElevenLabs",                  C_AUDIO,    "3-voice TTS"),
    ("OpenAI TTS (tts-1-hd)",       C_AUDIO,    "single-voice fallback"),
]
px = 3.0
for name, col, role in providers:
    rect = FancyBboxPatch(
        (px, Ypb - 0.1), 2.6, 0.5,
        boxstyle="round,pad=0,rounding_size=0.12",
        linewidth=1.2, edgecolor=col, facecolor=f"{col}33", zorder=4,
    )
    ax.add_patch(rect)
    ax.text(px + 1.3, Ypb + 0.22, name,
            ha="center", va="center", fontsize=7.6, fontweight="bold",
            color=C_WHITE, zorder=5)
    ax.text(px + 1.3, Ypb + 0.0, role,
            ha="center", va="center", fontsize=6.8, color=C_MUTED, zorder=5)
    px += 2.75


# ── watermark ─────────────────────────────────────────────────────────────────
ax.text(FIG_W / 2, 0.6,
        "mikecast.io   ·   --crew path   ·   shadow-validated alongside --legacy",
        ha="center", va="center", fontsize=8.5, color=C_MUTED, alpha=0.7)


# ── save ──────────────────────────────────────────────────────────────────────
out = "/home/mike-schwimmer/mikecast/mikecast_crewai_architecture.png"
plt.savefig(out, dpi=160, bbox_inches="tight", facecolor=BG, edgecolor="none")
plt.close()
print(f"Saved: {out}")
