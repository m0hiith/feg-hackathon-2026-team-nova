#!/usr/bin/env python3
"""
End-to-end check for src/ui/index.html.

Drives the real file in a real browser (file://, no server, no build step) and
replays BOTH sessions to their last event, twice, with no manual intervention.
Also asserts the panel contract that matters on a gambling product:

  * FIRE renders inline, is never a dialog, and nothing is pre-selected
  * both buttons are identical in weight, size and colour
  * no countdown, no urgency wording, no percentages, no recommendation
  * SILENT is a rendered state with a reason, not an empty div
  * DISMISSED closes the panel and the replay carries on to the end
  * no raw PlayerID, betslip number or username on screen

Usage:  .venv-ui/bin/python scripts/check_ui.py [--screenshots DIR]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
UI = REPO_ROOT / "src" / "ui" / "index.html"

HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
BANNED_PANEL = [
    (re.compile(r"\d+\s*%"), "a percentage"),
    (re.compile(r"\b(chance|chances|likely|likelihood|odds of winning)\b", re.I), "a chance/probability claim"),
    (re.compile(r"\b(hurry|expires?|expiring|last chance|act now|don't miss|limited time|ending soon)\b", re.I), "urgency wording"),
    (re.compile(r"\b(we recommend|recommended|suggested for you|best option)\b", re.I), "a recommendation"),
    (re.compile(r"\b\d+\s*(seconds?|secs?|s)\s+(left|remaining)\b", re.I), "a countdown"),
]

# Walks every rendered text node, resolves the effective background through
# transparent ancestors, and applies the WCAG 2.1 contrast formula: 4.5:1 for
# normal text, 3:1 for large text (>=24px, or >=18.66px bold).
CONTRAST_AUDIT = """(() => {
  const rgb = (s) => (s.match(/[\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => {
    const f = (c) => { c /= 255; return c <= .03928 ? c / 12.92
                                : Math.pow((c + .055) / 1.055, 2.4); };
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b);
  };
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
    return (x + .05) / (y + .05);
  };
  const bgOf = (node) => {
    for (let n = node; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length === 4 && c[3] === 0) continue;
      if (c.length >= 3) return c.slice(0, 3);
    }
    return [255, 255, 255];
  };
  const bad = [];
  document.querySelectorAll('body *').forEach((n) => {
    if (!n.offsetParent && getComputedStyle(n).position !== 'fixed') return;
    const own = [...n.childNodes].some(
      (c) => c.nodeType === 3 && c.textContent.trim());
    if (!own) return;
    const st = getComputedStyle(n);
    const size = parseFloat(st.fontSize);
    const weight = parseInt(st.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    const got = ratio(rgb(st.color).slice(0, 3), bgOf(n));
    if (got < need) bad.push({
      tag: n.tagName.toLowerCase(), cls: n.className,
      size: size, got: Math.round(got * 100) / 100, need: need,
      text: n.textContent.trim().slice(0, 40),
    });
  });
  return bad;
})()"""

failures: list[str] = []
checks = 0


def ok(cond: bool, label: str) -> bool:
    global checks
    checks += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)
    return bool(cond)


def panel_contract(page, run: str) -> None:
    """Everything that must be true while the FIRE panel is on screen."""
    panel = page.locator("section.fire")
    ok(panel.count() == 1, f"{run}: FIRE panel rendered inline")

    ok(page.locator("dialog[open]").count() == 0
       and page.locator('[role="dialog"], [role="alertdialog"]').count() == 0,
       f"{run}: not a modal (no dialog role anywhere)")

    ok(panel.evaluate("n => getComputedStyle(n).position") == "static",
       f"{run}: panel is in normal flow, not positioned over anything")

    ok(page.evaluate("document.activeElement === document.body"),
       f"{run}: nothing pre-selected — focus was not stolen")

    btns = panel.locator(".actions button")
    ok(btns.count() == 2, f"{run}: exactly two choices")
    labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
    ok(labels == ["Review slip", "Clear slip"], f"{run}: choices are {labels}")

    styles = [btns.nth(i).evaluate("""n => {
        const s = getComputedStyle(n), r = n.getBoundingClientRect();
        return {bg:s.backgroundColor, fg:s.color, fw:s.fontWeight,
                fs:s.fontSize, bd:s.border, w:Math.round(r.width),
                h:Math.round(r.height)};
    }""") for i in range(2)]
    same = {k: styles[0][k] == styles[1][k] for k in styles[0]}
    ok(all(same.values()),
       f"{run}: both buttons identical in weight, size and colour "
       f"({[k for k, v in same.items() if not v] or 'all match'})")

    ok(all(b.evaluate("n => !n.autofocus && !n.hasAttribute('aria-pressed') "
                      "&& !n.classList.contains('primary')")
           for b in (btns.nth(0), btns.nth(1))),
       f"{run}: neither button is marked as the default")

    txt = panel.inner_text()
    for rx, what in BANNED_PANEL:
        ok(rx.search(txt) is None, f"{run}: panel contains no {what}")
    ok("Illustrative price values" in txt,
       f"{run}: price values are labelled illustrative in the panel")
    ok("price ticker" in txt and "impression log" in txt,
       f"{run}: panel says why the price cannot be attributed")

    ok(panel.locator(".change .from").inner_text().strip() != ""
       and panel.locator(".change .to").inner_text().strip() != "",
       f"{run}: the old → new pair is rendered")
    a, b = [panel.locator(f".change .{c}").evaluate(
        "n => { const s = getComputedStyle(n); "
        "return s.color + '|' + s.fontWeight + '|' + s.fontSize; }")
        for c in ("from", "to")]
    ok(a == b, f"{run}: both directions of a price move render identically")


def run_once(page, run: str, speed: str, dismiss: str, shots: Path | None) -> None:
    print(f"\n── {run} (speed {speed}×, dismiss via {dismiss}) ──")
    page.goto(UI.as_uri())
    page.wait_for_function("() => window.__demo && window.__demo.session === 'A'")
    page.select_option("#speed", speed)

    # ── Session A: must fire, inline, mid-replay ──────────────────
    page.wait_for_function("() => window.__demo.panel === 'fire'", timeout=120_000)
    fired_at = page.evaluate("window.__demo.index")
    total_a = page.evaluate("window.__demo.total")
    ok(0 < fired_at < total_a - 1,
       f"{run}: A fired at event {fired_at + 1} of {total_a}, mid-session")
    ok(page.evaluate("window.__demo.score") == 100,
       f"{run}: stall score is 100 at the moment it fires")
    panel_contract(page, run)
    page.evaluate("window.__panelNode = document.querySelector('section.fire')")
    page.wait_for_function("() => window.__demo.index > %d" % fired_at)
    ok(page.evaluate("window.__panelNode === document.querySelector('section.fire')"),
       f"{run}: panel is not rebuilt underneath the user as events keep arriving")
    page.locator("section.fire .actions button").first.focus()
    page.wait_for_function("() => window.__demo.index > %d" % (fired_at + 1))
    ok(page.evaluate("document.activeElement.textContent === 'Review slip'"),
       f"{run}: keyboard focus inside the panel survives incoming events")
    if shots:
        page.screenshot(path=str(shots / f"{run.lower().replace(' ', '-')}-fire.png"),
                        full_page=True)

    # ── DISMISSED: panel closes, replay carries on ────────────────
    if dismiss == "Esc":
        page.keyboard.press("Escape")
    else:
        page.locator("section.fire .actions button", has_text=dismiss).click()
    page.wait_for_function("() => window.__demo.panel === 'dismissed'")
    ok(page.locator("section.fire").count() == 0, f"{run}: panel closed on dismiss")
    idx_at_dismiss = page.evaluate("window.__demo.index")

    page.wait_for_function("() => window.__demo.done === true", timeout=180_000)
    ok(page.evaluate("window.__demo.index") == total_a - 1,
       f"{run}: A replayed all {total_a} events without manual intervention")
    ok(page.evaluate("window.__demo.index") > idx_at_dismiss,
       f"{run}: replay continued after the panel was dismissed")
    ok(page.evaluate("window.__demo.panel") == "dismissed",
       f"{run}: no second intervention after dismissal")

    # ── Session B: silent all the way through ─────────────────────
    page.click("#tab-B")
    page.wait_for_function("() => window.__demo.session === 'B'")
    page.select_option("#speed", speed)
    seen = page.evaluate("""() => new Promise(res => {
        const seen = new Set();
        const t = setInterval(() => {
            seen.add(window.__demo.panel);
            if (window.__demo.done) { clearInterval(t); res([...seen]); }
        }, 40);
    })""")
    total_b = page.evaluate("window.__demo.total")
    ok(page.evaluate("window.__demo.index") == total_b - 1,
       f"{run}: B replayed all {total_b} events without manual intervention")
    ok(seen == ["silent"], f"{run}: B never left the silent state (saw {seen})")

    silent = page.locator(".silent")
    ok(silent.count() == 1, f"{run}: silent state is rendered, not a blank div")
    stxt = silent.inner_text()
    ok("No intervention — session progressing normally" in stxt,
       f"{run}: silent label present")
    ok("Silence is a returned decision" in stxt and "STAY_SILENT" in stxt,
       f"{run}: silent state names the decision it made")
    ok(page.evaluate("window.__demo.reason") == "already_converted",
       f"{run}: B ends on reason=already_converted")
    if shots:
        page.screenshot(path=str(shots / f"{run.lower().replace(' ', '-')}-silent.png"),
                        full_page=True)

    # ── safety: nothing identifying reached the screen ────────────
    body = page.inner_text("body")
    ok(HEX64.search(body) is None, f"{run}: no raw PlayerID on screen")
    ok("HPC0TR0852VAP800" not in body, f"{run}: betslip number is masked")
    ok("…" in page.inner_text("#sessmeta"), f"{run}: ids on screen are masked")

    # ── accessibility spot checks ─────────────────────────────────
    ok(page.evaluate("[...document.images].every(i => i.hasAttribute('alt'))"),
       f"{run}: every image has alt text")
    ok(page.evaluate("""[...document.querySelectorAll('svg[role=img],[role=img]')]
        .every(n => n.getAttribute('aria-label') || n.getAttribute('aria-labelledby'))"""),
       f"{run}: every graphic has an accessible name")
    ok(page.evaluate("""(() => {
        const b = parseFloat(getComputedStyle(document.body).fontSize);
        const small = [...document.querySelectorAll('body *')]
          .filter(n => n.childElementCount === 0 && n.textContent.trim())
          .filter(n => parseFloat(getComputedStyle(n).fontSize) < 12);
        return b >= 16 && small.length === 0;
    })()"""), f"{run}: base font ≥16px and no text below 12px")
    # Reset the sequential-focus starting point: earlier clicks in this run
    # left it mid-page, and blur() alone does not move it back.
    page.evaluate("document.body.setAttribute('tabindex','-1');"
                  "document.body.focus()")
    page.keyboard.press("Tab")
    ok(page.evaluate("document.activeElement.className.includes('skip')"),
       f"{run}: first Tab reaches the skip link")
    ok(page.evaluate("""(() => {
        const s = document.querySelector('.skip');
        return s.getBoundingClientRect().left >= 0;
    })()"""), f"{run}: the skip link is visible once focused")
    page.keyboard.press("Tab")
    ok(page.evaluate("document.activeElement.getAttribute('role') === 'radio' "
                     "&& document.activeElement.getAttribute('aria-checked') "
                     "=== 'true'"),
       f"{run}: Tab then reaches the checked session radio")
    ok(page.evaluate("""(() => {
        const on = [...document.querySelectorAll('[role=radio]')]
            .filter(n => n.tabIndex === 0);
        return on.length === 1 && on[0].getAttribute('aria-checked') === 'true';
    })()"""), f"{run}: radiogroup is a single tab stop on the checked option")
    was = page.evaluate("window.__demo.session")
    page.keyboard.press("ArrowRight")
    ok(page.evaluate("window.__demo.session") != was,
       f"{run}: arrow keys move between sessions")
    page.evaluate("document.body.removeAttribute('tabindex')")
    ok(page.evaluate("""(() => {
        const s = getComputedStyle(document.activeElement, ':focus-visible');
        document.activeElement.focus();
        return getComputedStyle(document.activeElement).outlineWidth !== '';
    })()"""), f"{run}: focused controls carry a visible outline")

    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        bad = page.evaluate(CONTRAST_AUDIT)
        ok(not bad, f"{run}: WCAG AA text contrast in {scheme} mode "
                    f"({bad[:3] if bad else 'all pass'})")
    page.emulate_media(color_scheme="light")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", type=Path, default=None)
    args = ap.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    if not UI.exists():
        print(f"{UI} missing - run scripts/build_ui.py first")
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" else None)

        run_once(page, "Run 1", "40", "Review slip", args.screenshots)
        run_once(page, "Run 2", "120", "Esc", args.screenshots)

        print()
        ok(not errors, f"no console or page errors ({errors[:2]})")
        browser.close()

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("Both sessions replayed end to end, twice, with no manual intervention.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
