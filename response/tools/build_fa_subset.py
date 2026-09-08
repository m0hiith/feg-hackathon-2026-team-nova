#!/usr/bin/env python3
"""Build a self-contained Font Awesome subset (data: URI fonts + used glyphs)."""
import base64, pathlib, re

SP = pathlib.Path("/private/tmp/claude-501/-Users-mohith-feg-mock-/429ffa8e-8d8b-4b7a-a3b2-2af82544e703/scratchpad")
html = pathlib.Path("/Users/mohith/feg mock /Image 2.html").read_text()
css = (SP / "fa.css").read_text()

# icons referenced anywhere in the page, including the ones the JS builds at runtime
used = set(re.findall(r'fa-([a-z0-9-]+)', html))
# names the JS concatenates at runtime; the regex above only sees the prefix
used |= {"arrow-trend-down", "arrow-trend-up"}

# FA groups aliases into one rule: ".fa-magnifying-glass:before,.fa-search:before{...}"
glyphs = {}
for sels, code in re.findall(r'([^{}]+)\{content:"(\\[0-9a-f]+)"\}', css):
    for name in re.findall(r'\.fa-([a-z0-9-]+):{1,2}before', sels):
        glyphs[name] = code
have = {n: c for n, c in glyphs.items() if n in used}

b64 = lambda f: base64.b64encode((SP / f).read_bytes()).decode()

out = [
 '@font-face{font-family:"Font Awesome 6 Free";font-style:normal;font-weight:400;'
 'font-display:block;src:url(data:font/woff2;base64,%s) format("woff2")}' % b64("fa-regular-400.woff2"),
 '@font-face{font-family:"Font Awesome 6 Free";font-style:normal;font-weight:900;'
 'font-display:block;src:url(data:font/woff2;base64,%s) format("woff2")}' % b64("fa-solid-900.woff2"),
 '.fa,.fas,.fa-solid,.far,.fa-regular{-moz-osx-font-smoothing:grayscale;'
 '-webkit-font-smoothing:antialiased;display:inline-block;font-style:normal;'
 'font-variant:normal;line-height:1;text-rendering:auto;'
 'font-family:"Font Awesome 6 Free"}',
 '.fa-solid,.fas{font-weight:900}.fa-regular,.far{font-weight:400}',
 '.fa-fw{text-align:center;width:1.25em}',
]
out += ['.fa-%s:before{content:"%s"}' % (n, c) for n, c in sorted(have.items())]

dest = SP / "fa-inline.css"
dest.write_text("\n".join(out))
print("icons used in page :", len(used))
print("glyphs embedded    :", len(have))
missing = sorted(n for n in used if n not in glyphs and n not in
                 {"solid", "regular", "brands", "fw"})
print("no glyph (modifiers/partials):", missing)
print("css size: %.0f KB" % (dest.stat().st_size / 1024))
