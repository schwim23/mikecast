#!/usr/bin/env python3
"""
Generate the README diagrams as hand-built SVG:

    docs/diagrams/pipeline.svg   daily pipeline (crew path), Steps 0–11
    docs/diagrams/aws.svg        AWS deploy / run / observe / signup data flow

    python scripts/make_diagrams.py          # rewrite both files
    python scripts/make_diagrams.py --check  # exit 1 if either is stale (CI runs this via
                                             # crew/tests/test_docs_in_sync.py)

Model names and numbers come from the same source readers as scripts/gen_docs.py, so
changing a model default or a threshold in code makes the committed SVG stale and fails
CI until this script is re-run. Everything else (boxes, arrows, wording) is the layout
below — edit it here, never the SVG by hand.

Each SVG carries its own light and dark palettes (prefers-color-scheme), so it reads on
both GitHub themes. Output is deterministic (no timestamps) so the check is a plain diff.
"""
from __future__ import annotations

import argparse
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_docs as g  # noqa: E402  (same-dir helper)

ROOT = g.ROOT
OUT_DIR = ROOT / "docs" / "diagrams"

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

STYLE = """
.bg{fill:#ffffff}.band{fill:#f6f8fa}.box{fill:#ffffff;stroke:#d0d7de;stroke-width:1.2}
.guard{stroke:#bf8700;stroke-width:1.8}.dash{stroke-dasharray:5 4}
.t{fill:#1f2328;font-weight:600}.s{fill:#59636e}.h{fill:#1f2328;font-weight:700}
.k{fill:#59636e;font-weight:600;letter-spacing:.08em}.lbl{fill:#59636e;font-style:italic}
.edge{stroke:#8c959f;stroke-width:1.4;fill:none}.ah{fill:#8c959f}
.chip{fill:#ddf4ff;stroke:#54aeff;stroke-width:.8}.chipt{fill:#0969da}
.amber{fill:#9a6700}
@media (prefers-color-scheme: dark){
.bg{fill:#0d1117}.band{fill:#161b22}.box{fill:#0d1117;stroke:#3d444d}
.guard{stroke:#d29922}.t{fill:#e6edf3}.s{fill:#9198a1}.h{fill:#f0f6fc}.k{fill:#9198a1}
.lbl{fill:#9198a1}.edge{stroke:#6e7681}.ah{fill:#6e7681}
.chip{fill:#0c2d6b;stroke:#1f6feb}.chipt{fill:#79c0ff}.amber{fill:#d29922}}
"""


class LayoutError(Exception):
    pass


def _w(text: str, size: float, bold: bool = False) -> float:
    """Rough rendered width; used only to fail loudly when a label won't fit."""
    return len(text) * size * (0.60 if bold else 0.55)


class Svg:
    def __init__(self, w: int, h: int, label: str):
        self.w, self.h, self.label = w, h, label
        self.parts: list[str] = []

    def add(self, s: str):
        self.parts.append(s)

    def text(self, x, y, s, size=12, cls="s", anchor="start", mono=False):
        fam = MONO if mono else FONT
        self.add(f'<text x="{x:g}" y="{y:g}" font-size="{size}" font-family="{fam}" '
                 f'class="{cls}" text-anchor="{anchor}">{escape(s)}</text>')

    def chip(self, x, y, s):
        w = _w(s, 10.5) + 14
        self.add(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="17" rx="8.5" class="chip"/>')
        self.text(x + w / 2, y + 12.3, s, 10.5, "chipt", "middle", mono=True)
        return w

    def band(self, y, h, step, name):
        self.add(f'<rect x="12" y="{y}" width="{self.w - 24}" height="{h}" rx="12" class="band"/>')
        self.text(28, y + 26, step, 10.5, "k")
        self.text(28, y + 45, name, 15, "h")

    def box(self, x, y, w, h, title, sub=(), chip=None, kind=""):
        """kind: '' | 'guard' (amber outline) | 'dash' (observes only)."""
        cls = "box" + (" guard" if kind == "guard" else "") + (" dash" if kind == "dash" else "")
        self.add(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="8" class="{cls}"/>')
        pad, inner = 12, w - 24
        chip_w = _w(chip, 10.5) + 14 if chip else 0
        chip_inline = chip and _w(title, 13, True) + 10 + chip_w <= inner
        if _w(title, 13, True) > inner:
            raise LayoutError(f"title too wide for {w}px box: {title!r}")
        self.text(x + pad, y + 21, title, 13, "t")
        if chip_inline:
            self.chip(x + w - pad - chip_w, y + 8, chip)
        for i, line in enumerate(sub):
            cols = line.split("\t")          # "\t" splits a line into two aligned columns
            cw = inner / len(cols)
            for j, part in enumerate(cols):
                if _w(part, 11.5) > cw - (8 if len(cols) > 1 else 0):
                    raise LayoutError(f"sub-line too wide for {w}px box: {part!r}")
                self.text(x + pad + j * cw, y + 38 + i * 15, part, 11.5, "s")
        need = 38 + 15 * max(len(sub) - 1, 0) + 8
        if chip and not chip_inline:
            cy = y + 38 + 15 * len(sub) - 6
            self.chip(x + pad, cy, chip)
            need = cy - y + 17 + 8
        if need > h + 0.5:
            raise LayoutError(f"box {title!r} needs {need:.0f}px height, has {h}")
        return (x, y, w, h)

    def arrow(self, pts, label=None, lx=None, ly=None, anchor="start"):
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:g},{y:g}" for i, (x, y) in enumerate(pts))
        self.add(f'<path d="{d}" class="edge" marker-end="url(#ah)"/>')
        if label:
            if lx is None:
                (x1, y1), (x2, y2) = pts[-2], pts[-1]
                lx, ly = (x1 + x2) / 2 + 6, (y1 + y2) / 2 + 4
            self.text(lx, ly, label, 11, "lbl", anchor)

    def legend(self, x, y):
        cx = x
        self.text(cx, y + 12.5, "Legend:", 11.5, "s")
        cx += 56
        cx += self.chip(cx, y, "model") + 8
        self.text(cx, y + 12.5, "LLM call (default model)", 11.5, "s")
        cx += 158
        self.add(f'<rect x="{cx}" y="{y}" width="26" height="17" rx="5" class="box guard"/>')
        self.text(cx + 34, y + 12.5, "guard against stale or invented content", 11.5, "s")
        cx += 270
        self.add(f'<rect x="{cx}" y="{y}" width="26" height="17" rx="5" class="box dash"/>')
        self.text(cx + 34, y + 12.5, "observes only (logs, never rewrites)", 11.5, "s")

    def render(self) -> str:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.label)}">\n'
            f"<title>{escape(self.label)}</title>\n<style>{STYLE}</style>\n"
            '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="ah"/>'
            "</marker></defs>\n"
            f'<rect width="{self.w}" height="{self.h}" class="bg"/>\n'
        )
        return head + "\n".join(self.parts) + "\n</svg>\n"


def _short(model: str) -> str:
    return model.split("/", 1)[-1]


def facts() -> dict:
    env = g.env_defaults("mc_config.py")
    return {
        "writer": _short(env["CLAUDE_WRITER_MODEL"]),
        "scorer": _short(env["OPENAI_SCORER_MODEL"]),
        "critic": _short(env["OPENAI_CRITIC_MODEL"]),
        "helper": _short(env["OPENAI_HELPER_MODEL"]),
        "grok": g.call_kwarg("mc_plan.py", "create", "model")[0],
        "top": g.module_const("mc_config.py", "TOP_ARTICLES_TOTAL"),
        "enrich": g.func_default("crew/research_crew.py", "run_research", "enrich_top_n"),
        "stale": g.call_kwarg("crew/research_crew.py", "filter_stale_articles", "max_age_days")[0],
        "lookback": g.module_const("mc_collect.py", "REPEAT_LOOKBACK_DAYS"),
        "wmin": g.module_const("eval/score.py", "_PODCAST_WORD_MIN"),
        "wmax": g.module_const("eval/score.py", "_PODCAST_WORD_MAX"),
        "expand": g.module_const("crew/writing_crew.py", "_EXPAND_BELOW_WORDS"),
        "weak": g.module_const("crew/critic_crew.py", "_WEAK_THRESHOLD"),
        "schedule": g.deploy_schedule(),
    }


# ── Pipeline diagram ─────────────────────────────────────────────────────────

def pipeline() -> str:
    f = facts()
    W, X0, X1 = 1200, 176, 1180          # content spans X0..X1 (left gutter holds step labels)
    CW = X1 - X0
    GAP = 40                              # vertical gap between bands (holds the flow arrow)
    s = Svg(W, 0, f"MikeCast daily pipeline: news is collected, filtered, scored and checked "
                  f"before three Claude writers produce the briefing and two podcast scripts, "
                  f"which are audited, voiced, published to S3 and posted to X and Instagram.")
    s.text(24, 38, "MikeCast daily pipeline", 22, "h")
    s.text(24, 60, "Default --crew path · runs 6:30 AM ET on ECS Fargate · models and numbers "
                   "are read from code by scripts/make_diagrams.py", 12, "s")
    s.legend(24, 74)
    y = 108

    def flow(y_from, label):
        cx = X0 + CW / 2
        s.arrow([(cx, y_from + 4), (cx, y_from + GAP - 4)], label, cx + 10, y_from + GAP / 2 + 4)

    # Step 0 — plan
    h = 74
    s.band(y, h, "STEP 0", "Plan")
    s.box(X0, y + 10, 330, 54, "Search planner", ["live web + X search → queries per category"],
          chip=f["grok"])
    s.text(X0 + 350, y + 41, "Grok writes search queries and a trending-topics list; it is never "
                             "used as a news source.", 11.5, "s")
    flow(y + h, "dynamic queries + trending topics")
    y += h + GAP

    # Steps 1–6 — research
    h = 252
    s.band(y, h, "STEPS 1–6", "Research")
    lane_w, bw, bh, gx = 704, 164, 88, 16
    s.text(X0, y + 22, "Non-sports · procedural pipeline (crew/research_crew.py)", 11.5, "k")
    sx = X0 + lane_w + 20
    s.text(sx, y + 22, "NY Sports · CrewAI agents", 11.5, "k")
    ra, rb = y + 34, y + 34 + bh + 24
    xs = [X0 + i * (bw + gx) for i in range(4)]
    a = [
        s.box(xs[0], ra, bw, bh, "Collect", ["NYT, RSS, HN, Reddit", "Google News · wires"]),
        s.box(xs[1], ra, bw, bh, "Dedupe", ["URLs + titles seen in", "the last 7 days"]),
        s.box(xs[2], ra, bw, bh, "Drop old news", [f"stale >{f['stale']}d · undated", "future · resurfaced"],
              kind="guard"),
        s.box(xs[3], ra, bw, bh, "Cluster", ["group near-duplicates"], chip=f["helper"]),
    ]
    b = [
        s.box(xs[0], rb, bw, bh, "Score & rank", ["per category, with", "trending boost"], chip=f["scorer"]),
        s.box(xs[1], rb, bw, bh, "Merge duplicates", ["same event in two", "sections → keep best"],
              chip=f["scorer"], kind="guard"),
        s.box(xs[2], rb, bw, bh, "Drop re-reports", [f"vs last {f['lookback']} episodes,", "2-of-3 vote"],
              chip=f["scorer"], kind="guard"),
        s.box(xs[3], rb, bw, bh, "Select & enrich", [f"top {f['top']}, ≥3 per section", f"full text for top {f['enrich']}"],
              chip=f["helper"]),
    ]
    for row in (a, b):
        for (x1, y1, w1, h1), (x2, _, _, _) in zip(row, row[1:]):
            s.arrow([(x1 + w1 + 1, y1 + h1 / 2), (x2 - 1, y1 + h1 / 2)])
    # wrap from end of row a to start of row b
    xa, ya, wa, ha = a[3]
    xb = b[0][0] + bw / 2
    mid = ya + ha + 12
    s.arrow([(xa + wa / 2, ya + ha), (xa + wa / 2, mid), (xb, mid), (xb, rb - 1)])
    sw = X1 - sx
    g1 = s.box(sx, ra, sw, bh, "Gatekeeper", ["SPORTS_TRUSTED_SOURCES allowlist", "drops aggregators (e.g. AOL)"],
               kind="guard")
    g2 = s.box(sx, rb, sw, bh, "NY Sports Researcher", ["ESPN box scores, standings,", "injuries (max 15 steps / 180s)"],
               chip=f["scorer"])
    s.arrow([(sx + sw / 2, ra + bh), (sx + sw / 2, rb - 1)])
    flow(y + h, f"{f['top']} articles + verified ESPN facts")
    y += h + GAP

    # Step 7 — prompt inputs
    h = 96
    s.band(y, h, "STEP 7 + FACTS", "Prompt inputs")
    w3 = (CW - 2 * 16) / 3
    s.box(X0, y + 12, w3, 72, "Mike's Picks", ["queued URLs, PDFs, pasted text"], chip=f["helper"])
    s.box(X0 + w3 + 16, y + 12, w3, 72, "Officeholder + CEO facts",
          ["live Wikidata → S3 cache → fallback", "added to every writer prompt"], kind="guard")
    s.box(X0 + 2 * (w3 + 16), y + 12, w3, 72, "Dates + market calendar",
          ["publish time on every article", "computed NYSE trading days"], kind="guard")
    flow(y + h, "articles + picks + facts → every writer prompt")
    y += h + GAP

    # Step 8 — write
    h = 104
    s.band(y, h, "STEP 8", "Write")
    s.text(X0, y + 20, "Three Claude writers run in parallel", 11.5, "k")
    s.box(X0, y + 28, w3, 66, "HTML briefing", ["email body + web page"], chip=f["writer"])
    s.box(X0 + w3 + 16, y + 28, w3, 66, "Single-host script",
          [f"{f['wmin']}–{f['wmax']} words · one expansion", f"pass if under {f['expand']} words"], chip=f["writer"])
    s.box(X0 + 2 * (w3 + 16), y + 28, w3, 66, "3-host script",
          [f"{f['wmin']}–{f['wmax']} words · [MIKE] [ELIZABETH]", "[JESSE] · same expansion pass"], chip=f["writer"])
    flow(y + h, "HTML + 2 scripts")
    y += h + GAP

    # Steps 8b–8d — check
    h = 196
    s.band(y, h, "STEPS 8b–8d", "Check")
    cw = 300
    c1 = s.box(X0, y + 12, cw, 82, "Critic scorer", ["grades each section 1–10"], chip=f["critic"])
    c2x = X0 + cw + 120
    c2 = s.box(c2x, y + 12, cw, 82, "Section patcher", ["rewrites a weak section once,", "keeps source links"],
               chip=f["writer"])
    s.arrow([(X0 + cw + 1, y + 53), (c2x - 1, y + 53)], f"score < {f['weak']}", X0 + cw + 60, y + 47, "middle")
    s.text(X0 + cw + 60, y + 70, "NY Sports: never", 11, "amber", "middle")
    s.text(X0 + cw + 60, y + 84, "auto-patched", 11, "amber", "middle")
    c3x = c2x + cw + 20
    s.box(c3x, y + 12, X1 - c3x, 82, "Stale-title fix", ["deterministic rewrite, no LLM:",
                                                          '"former President X" → "President X"'],
          kind="guard")
    half = (CW - 16) / 2
    s.box(X0, y + 106, half, 78, "Grounding check", ["every non-sports sentence vs today's articles",
                                                     "logs [Grounding] UNSUPPORTED, rewrites nothing"],
          chip=f["scorer"], kind="dash")
    s.box(X0 + half + 16, y + 106, half, 78, "NY Sports fact-checker",
          ["NY Sports HTML + [JESSE] lines vs articles", "logs a WARNING, never patches"],
          chip=f["helper"], kind="dash")
    flow(y + h, "checked HTML + scripts")
    y += h + GAP

    # Step 9 — audio
    h = 96
    s.band(y, h, "STEP 9", "Audio")
    a1 = s.box(X0, y + 12, 330, 72, "ElevenLabs · 3 voices", ["neighbouring lines sent as context;",
                                                            "suspiciously long clips retried / dropped"])
    a2x = X0 + 330 + 60
    s.box(a2x, y + 12, 300, 72, "ffmpeg stitch", ["re-encode one stream: CBR 128 kbps,", "−16 LUFS, duration saved in JSON"])
    s.arrow([(X0 + 331, y + 48), (a2x - 1, y + 48)], "segments", X0 + 360, y + 42, "middle")
    s.box(a2x + 320, y + 12, X1 - (a2x + 320), 72, "OpenAI TTS fallback",
          ["tts-1-hd, single voice", "used only if ElevenLabs fails"])
    flow(y + h, "episode MP3")
    y += h + GAP

    # Step 10 — deliver
    h = 96
    s.band(y, h, "STEP 10", "Deliver")
    s.box(X0, y + 12, 400, 72, "Publish to S3 → CloudFront",
          ["episode JSON + MP3, manifest.json, feed.xml", "(feed.xml no-cache) → mikecast.io, Apple, Spotify"])
    s.box(X0 + 416, y + 12, 280, 72, "Gmail email", ["HTML briefing + audio", "to the personal inbox"])
    s.box(X0 + 712, y + 12, X1 - (X0 + 712), 72, "Resend newsletter", ["broadcast to confirmed", "subscribers"])
    flow(y + h, "episode URL · every send (email, newsletter, X, IG) fires once per date")
    y += h + GAP

    # Step 11 — social
    h = 112
    s.band(y, h, "STEP 11", "Social")
    s.box(X0, y + 12, 330, 72, "Social copywriter", ["hook + deep link to ?date=<day>",
                                                     "code adds headers and fallbacks"], chip=f["writer"])
    sx2 = X0 + 330 + 60
    xw = (X1 - sx2 - 16) / 2
    s.arrow([(X0 + 331, y + 48), (sx2 - 1, y + 48)], "copy", X0 + 352, y + 42)
    fork_y = y + 98
    s.add(f'<path d="M{X0 + 300:g},{y + 84:g} L{X0 + 300:g},{fork_y:g} L{sx2 + xw + 16 + 40:g},{fork_y:g}" class="edge"/>')
    s.arrow([(sx2 + xw + 16 + 40, fork_y), (sx2 + xw + 16 + 40, y + 85)])
    s.box(sx2, y + 12, xw, 72, "X · @mikecastai", ["tweet + 9:16 captioned reel,", "fits X's 280 weighted chars"])
    s.box(sx2 + xw + 16, y + 12, xw, 72, "Instagram · @mikecastai", ["9:16 captioned reel", "(falls back to 1080×1080 card)"])
    y += h + 16

    s.h = int(y)
    return s.render()


# ── AWS diagram ──────────────────────────────────────────────────────────────

def aws() -> str:
    f = facts()
    W, X0, X1 = 1400, 176, 1380
    CW = X1 - X0
    col_g = 56
    col_w = (CW - 4 * col_g) / 5
    col = [X0 + i * (col_w + col_g) for i in range(5)]
    s = Svg(W, 0, "MikeCast on AWS: a push to main is tested and deployed to ECS; EventBridge "
                  "starts the Fargate task daily, which calls external APIs, writes to S3 behind "
                  "CloudFront, and reports to CloudWatch and Datadog; newsletter signups go through "
                  "API Gateway and a Lambda to Resend.")
    s.text(24, 38, "MikeCast on AWS", 22, "h")
    s.text(24, 60, "us-east-1 · how code reaches prod, what the daily task touches, and how "
                   "failures surface", 12, "s")
    y = 82
    GAP = 22

    # Deploy
    h = 108
    s.band(y, h, "ON PUSH TO MAIN", "Deploy")
    n, gx = 6, 14
    bw = (CW - (n - 1) * gx) / n
    steps = [
        ("GitHub", ["push to main", "(*.md-only: skipped)"]),
        ("Test job", ["pytest crew/tests/", "+ doc and diagram sync"]),
        ("Build job", ["docker build", "python:3.11 + ffmpeg"]),
        ("Amazon ECR", ["mikecast:<commit sha>", "+ :latest"]),
        ("ECS task def", ["new revision: only", "the image changes"]),
        ("Scheduler update", ["mikecast-daily → new", "task-def revision"]),
    ]
    dep = []
    for i, (t, sub) in enumerate(steps):
        dep.append(s.box(X0 + i * (bw + gx), y + 14, bw, 80, t, sub))
    for (x1, y1, w1, h1), (x2, *_ ) in zip(dep, dep[1:]):
        s.arrow([(x1 + w1 + 1, y1 + h1 / 2), (x2 - 1, y1 + h1 / 2)])
    ecr = dep[3]
    y += h + GAP

    # Run + serve
    h = 216
    s.band(y, h, "DAILY", "Run + serve")
    r1, r2, bh = y + 18, y + 120, 82
    sch = s.box(col[0], r1, col_w, bh, "EventBridge Scheduler",
                [f["schedule"].replace("`", "").rsplit(" ", 1)[0], f["schedule"].rsplit(" ", 1)[1]])
    ssm = s.box(col[0], r2, col_w, bh, "SSM Parameter Store", ["API keys + creds as", "task-def secrets"])
    task = s.box(col[1], r1, col_w, r2 + bh - r1, "ECS Fargate task",
                 ["mikecast_briefing.py", "~20 min, then exits", "", "ephemeral disk: all", "state lives in S3"])
    s.arrow([(col[0] + col_w + 1, r1 + 30), (col[1] - 1, r1 + 30)], "starts", col[0] + col_w + col_g / 2, r1 + 24, "middle")
    s.arrow([(col[0] + col_w + 1, r2 + 40), (col[1] - 1, r2 + 40)], "secrets", col[0] + col_w + col_g / 2, r2 + 34, "middle")
    ex, ey, ew = ecr[0] + ecr[2] / 2, ecr[1] + ecr[3], col[1] + col_w * 0.7
    s.arrow([(ex, ey + 1), (ex, y - 8), (ew, y - 8), (ew, r1 - 1)], "pulls image", ew + 8, y + 12)
    s3 = s.box(col[2], r1, col_w, bh, "S3 · mikecast-io-data", ["episode JSON, MP3,", "manifest.json, feed.xml"])
    cf = s.box(col[3], r1, col_w, bh, "CloudFront", ["mikecast.io", "(Route 53 alias)"])
    ls = s.box(col[4], r1, col_w, bh, "Listeners", ["browsers · Apple Podcasts", "Spotify (poll feed.xml)"])
    s.arrow([(col[1] + col_w + 1, r1 + 30), (col[2] - 1, r1 + 30)], "writes", col[1] + col_w + col_g / 2, r1 + 24, "middle")
    s.arrow([(col[2] + col_w + 1, r1 + 30), (col[3] - 1, r1 + 30)], "origin", col[2] + col_w + col_g / 2, r1 + 24, "middle")
    s.arrow([(col[3] + col_w + 1, r1 + 30), (col[4] - 1, r1 + 30)], "serves", col[3] + col_w + col_g / 2, r1 + 24, "middle")
    exw = X1 - col[2]
    s.box(col[2], r2, exw, bh, "External APIs (HTTPS, public egress)",
          ["News: NYT, RSS, Google News, ESPN\tLLMs: Anthropic, OpenAI, xAI",
           "TTS: ElevenLabs, OpenAI\tSend: Gmail SMTP, Resend, X, Instagram"])
    s.arrow([(col[1] + col_w + 1, r2 + 40), (col[2] - 1, r2 + 40)], "calls", col[1] + col_w + col_g / 2, r2 + 34, "middle")
    y += h + GAP

    # Observe
    h = 108
    s.band(y, h, "ALWAYS", "Observe")
    o = y + 14
    dd = s.box(col[0], o, col_w, 80, "Datadog", ["traces, metrics, logs", "(no-op w/o DD_API_KEY)"])
    lg = s.box(col[1], o, col_w, 80, "CloudWatch Logs", ["/ecs/mikecast"])
    al = s.box(col[2], o, col_w, 80, "Alarm MikeCastErrors", ["≥3 ERROR lines", "in 1 hour"])
    sn = s.box(col[3], o, col_w, 80, "SNS mikecast-alerts", ["email to Mike"])
    tx = task[0] + task[2] / 2
    s.arrow([(tx, task[1] + task[3] + 1), (tx, o - 1)], "stdout logs", tx + 8, task[1] + task[3] + 18)
    s.arrow([(task[0] + 30, task[1] + task[3] + 1), (task[0] + 30, o - 14), (col[0] + col_w / 2, o - 14),
             (col[0] + col_w / 2, o - 1)], "OTLP traces + metrics", col[0] + 12, o - 20)
    s.arrow([(col[1] + col_w + 1, o + 40), (col[2] - 1, o + 40)], "filter", col[1] + col_w + col_g / 2, o + 34, "middle")
    s.arrow([(col[2] + col_w + 1, o + 40), (col[3] - 1, o + 40)], "notifies", col[2] + col_w + col_g / 2, o + 34, "middle")
    s.arrow([(col[1] - 1, o + 62), (col[0] + col_w + 1, o + 62)], "forwarder", col[0] + col_w + col_g / 2, o + 76, "middle")
    s.text(col[4], o + 30, "No dashboards to watch:", 11.5, "s")
    s.text(col[4], o + 46, "a failed run pages by email,", 11.5, "s")
    s.text(col[4], o + 62, "Datadog is for digging in.", 11.5, "s")
    y += h + GAP

    # Signup
    h = 108
    s.band(y, h, "ON DEMAND", "Newsletter signup")
    o = y + 14
    su = [
        s.box(col[0], o, col_w, 80, "Signup form", ["mikecast.io, signup.js"]),
        s.box(col[1], o, col_w, 80, "API Gateway (HTTP)", ["api.mikecast.io", "POST /signup, GET /confirm"]),
        s.box(col[2], o, col_w, 80, "Lambda", ["mikecast-newsletter-signup", "HMAC-signed 24h token"]),
        s.box(col[3], o, col_w, 80, "Resend", ["confirm email, then", "add to audience"]),
    ]
    for (x1, y1, w1, h1), (x2, *_ ) in zip(su, su[1:]):
        s.arrow([(x1 + w1 + 1, y1 + 40), (x2 - 1, y1 + 40)])
    s.text(col[4], o + 30, "Lambda Function URLs are", 11.5, "s")
    s.text(col[4], o + 46, "blocked in this account,", 11.5, "s")
    s.text(col[4], o + 62, "hence API Gateway.", 11.5, "s")
    y += h + 16

    s.h = int(y)
    return s.render()


DIAGRAMS = {"pipeline.svg": pipeline, "aws.svg": aws}


def stale() -> list[str]:
    out = []
    for name, fn in DIAGRAMS.items():
        p = OUT_DIR / name
        if not p.exists() or p.read_text() != fn():
            out.append(f"docs/diagrams/{name}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    try:
        if args.check:
            st = stale()
            if st:
                print(f"Stale diagrams: {', '.join(st)} — run: python scripts/make_diagrams.py")
                return 1
            print("Diagrams are up to date.")
            return 0
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, fn in DIAGRAMS.items():
            (OUT_DIR / name).write_text(fn())
            print(f"wrote docs/diagrams/{name}")
        return 0
    except (g.DocsGenError, LayoutError) as exc:
        print(f"make_diagrams: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
