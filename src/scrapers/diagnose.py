"""Plain-language reasons why a website could not be read, and what to do about it."""
from __future__ import annotations

import re

BLOCK_PAGE = re.compile(r"just a moment|attention required|access denied|captcha|are you a robot|verify you are human|checking your browser|request unsuccessful|incapsula|datadome|perimeterx", re.I)
JS_APP = re.compile(r'id=["\'](?:__next|root|app|__nuxt)["\']|ng-version|data-reactroot|window\.__NUXT__|__NEXT_DATA__|<noscript>[^<]*(?:enable|requires)\s+javascript', re.I)


def explain_unreachable(evidence: str, robots_state: str | bool | None, proxy_configured: bool) -> str:
    """robots_state: "robots" (the site's rules forbid reading), "robots-unreachable:<detail>" (the rules file could not be downloaded), or None."""
    state = "robots" if robots_state is True else (robots_state or "")
    if state == "robots":
        return "This site's robots.txt asks automated tools not to read it, so it was skipped on purpose."
    if state.startswith("robots-unreachable"):
        detail = state.split(":", 1)[1] if ":" in state else "no answer"
        return (f"The site's rules file (robots.txt) could not be downloaded ({detail}) after several tries, so the site was not read. "
                "This is usually temporary (the server was busy or rate-limiting): wait a minute and run the analysis again. It is not a ban.")
    m = re.search(r"HTTP (\d{3})", evidence)
    code = int(m.group(1)) if m else None
    tip = "" if proxy_configured else " If you are allowed to read this site, running the tool from your own computer or through a proxy (SCRAPER_PROXY_URL, see README) often works."
    if code in (401, 403, 406, 429, 503):
        return f"The site refused this server (HTTP {code}): it blocks automated access, often from cloud servers.{tip}"
    if code == 404:
        return "The address returned 'page not found' (HTTP 404). Check that the web address is correct."
    if code:
        return f"The site answered with an error (HTTP {code}). Try again later or check the address."
    return f"The site did not answer (timeout, DNS problem or a block).{tip} Check that the address is correct and opens in a browser."


def explain_no_products(home_html: str, tried_stores: list[str], found_links: list[str], technical: str = "") -> str:
    html = home_html or ""
    if "readable price data" in technical:
        return ("Product-style pages exist on this site, but none show a price this tool can read (no structured data and no visible price with an "
                "add-to-cart button). The catalogue may only appear inside an app, or the company may not sell online at all (some take orders by email, phone "
                "or WhatsApp). Check in a browser whether the site really lists prices; if the shop lives at another address, enter that address instead.")
    if BLOCK_PAGE.search(html[:6000]):
        return "The page shown to this server is a bot-check ('verify you are human'), so the products could not be read. Running from your own computer or through a proxy may work."
    links = len(re.findall(r"<a\s", html, re.I))
    if JS_APP.search(html) and links < 25:
        return "This site builds its pages with JavaScript, so there is nothing to read without a real browser. Install the optional browser support (README) or use a site that lists products in plain HTML."
    msg = "No product pages were found at this address. It looks like the company's main website rather than its online store."
    if tried_stores:
        msg += f" We also tried {', '.join(tried_stores[:3])} without finding a store."
    return msg + " If the shop lives at another address (for example https://shop.yourcompany.com), enter that address instead."
