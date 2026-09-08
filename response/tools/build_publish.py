#!/usr/bin/env python3
"""Assemble the artifact-ready copy of the FEG mock.

Differences from the local file, all forced by the artifact sandbox:
  - no <!doctype>/<html>/<head>/<body> (the publisher supplies that skeleton)
  - Font Awesome inlined (its CSS host and font files are both CSP-blocked)
  - body styling moved into CSS, since the <body> tag is not ours to class
  - Inter loaded from Google Fonts, the one font host the CSP admits, which is
    the face the page's own tailwind config already asks for
  - a synthetic-demo badge in the header, because a published link is opened
    cold by people who never saw the disclosure at the foot of the page
"""
import pathlib, re

HERE = pathlib.Path(__file__).resolve().parent
RESPONSE = HERE.parent
src  = (RESPONSE / "Image 2.html").read_text()
fa   = (HERE / "fa-inline.css").read_text()

# ── pull the pieces out of the local file ────────────────────────────────
styles = re.findall(r'<style data-purpose[^>]*>([\s\S]*?)</style>', src)
assert len(styles) == 2, f"expected 2 style blocks, got {len(styles)}"

tw_cfg = re.search(r'<script>\s*(tailwind\.config[\s\S]*?)\s*</script>', src)
assert tw_cfg, "tailwind config not found"

body = re.search(r'<body[^>]*>([\s\S]*)</body>', src).group(1)

# ── header badge: the disclosure has to survive being opened cold ────────
logo_end = '          FEG\n        </a>'
assert logo_end in body, "logo anchor"
body = body.replace(logo_end, logo_end + """
<span class="hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-400/15 border border-amber-400/40 text-amber-300 text-[9px] font-bold uppercase tracking-wider whitespace-nowrap" title="Prototype on synthetic data. Not a real betting site.">
<i class="fa-solid fa-flask text-[8px]"></i> Synthetic demo
</span>""", 1)

HEAD = """<title>FEG Session Engine</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&amp;display=swap" rel="stylesheet"/>
<style data-purpose="font-awesome-subset">
%s
</style>
<style data-purpose="artifact-base">
  /* The page wrapper belongs to the publisher, so its utilities live here. */
  body {
    background: #0b0f17;
    color: #e5e7eb;
    font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 0.75rem;
    line-height: 1rem;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    overflow-x: hidden;
  }
  ::selection { background: #2563eb; color: #fff; }
  :focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
</style>
<style data-purpose="custom-scrollbars">
%s
</style>
<style data-purpose="badge-styling">
%s
</style>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<script>
%s
</script>
""" % (fa, styles[0].strip(), styles[1].strip(), tw_cfg.group(1))

out = RESPONSE / "feg-session-engine.html"
out.write_text(HEAD + body)
print("wrote %s  (%.0f KB)" % (out.name, out.stat().st_size / 1024))
for bad in ("<!DOCTYPE", "<html", "<head>", "<body"):
    assert bad.lower() not in out.read_text().lower(), "leftover %s" % bad
print("no doctype/html/head/body wrappers")
print("cdnjs stylesheet refs:", out.read_text().count("cdnjs.cloudflare.com"))
