#!/usr/bin/env python3
"""
Telegram job bot: watches company career sites directly.

Every run (GitHub Actions, every 10 minutes) the bot:
  1. Opens the careers pages / job APIs of the companies in SOURCES.
  2. Finds jobs that were not there on the previous check.
  3. Keeps only London jobs that match the role, level and stop-word filters.
  4. Sends each new job to Telegram with a link.

On the very first check of each company the bot only remembers the jobs that
already exist (it does not send them), so you only get NEW jobs after that.

Secrets (GitHub -> Settings -> Secrets and variables -> Actions):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

"python bot.py --test" sends a report: how many jobs the bot sees per company.
"""

import gzip
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# =====================================================================
# FILTERS (edit to change what the bot sends you)
# =====================================================================

# Title (or the top of the job page) must contain one of these...
ROLE_KEYWORDS = [
    "marketing", "pr", "public relations", "press",
    "communications", "communication", "comms",
    "events", "event",
    "brand", "social media", "influencer",
]

# ...and one of these.
LEVEL_KEYWORDS = [
    "intern", "internship", "interns", "executive",
    "assistant", "coordinator", "co-ordinator",
]

# Never send jobs containing these.
STOP_WORDS = [
    "unpaid", "commission based", "commission-based",
    "commission only", "commission-only",
]

# Job must be in this city (London areas and postcodes count too).
LOCATION_KEYWORDS = [
    "london", "shoreditch", "whitechapel", "southwark", "soho", "mayfair",
    "covent garden", "king's cross", "kings cross", "marylebone", "spitalfields",
    "clerkenwell", "fitzrovia", "camden", "islington", "hackney", "wapping",
    "battersea", "chelsea", "kensington", "knightsbridge", "canary wharf",
    "london bridge", "holborn", "westminster", "farringdon", "victoria",
    "notting hill", "hammersmith", "white city", "stratford", "borough",
]
LONDON_POSTCODE = r"\b(?:EC|WC|E|N|NW|SE|SW|W)\d{1,2}[A-Z]?(?:\s?\d[A-Z]{2})?\b"

# =====================================================================
# COMPANIES TO WATCH
#
# type "page":       a careers page; the bot finds job links with link_re
#                    (group 1 = job id) and opens each new job page.
# type "workday":    Workday job site (public JSON).
# type "greenhouse": Greenhouse job board (public API).
# type "lever":      Lever job board (public API).
# type "smartrecruiters": SmartRecruiters (public API).
#
# To add a company, send Claude the link to its careers page.
# =====================================================================

SOURCES = [
    {
        "name": "LVMH (Dior, Louis Vuitton, Celine, Moët Hennessy, Guerlain...)",
        "type": "page",
        "urls": [
            "https://www.lvmh.com/en/our-job-offers",
            "https://www.lvmh.com/en/join-us/our-job-offers",
        ],
        "link_re": r"/(?:en/)?join-us/our-job-offers/([A-Z]{2,8}\d{3,7})\b",
        # Job references like MHE02525, CDC15230, GUER05495, LVMH03215 anywhere in the page data.
        "json_re": r'(?<![A-Za-z0-9])([A-Z]{3,6}\d{5})(?![A-Za-z0-9])',
        "job_url": "https://www.lvmh.com/en/join-us/our-job-offers/{id}",
    },
    {
        "name": "Richemont (Cartier, Van Cleef & Arpels, Chloé...)",
        "type": "page",
        "urls": [
            "https://careers.richemont.com/en/jobs/?location=London",
            "https://careers.richemont.com/en/jobs/",
        ],
        "link_re": r"/en/jobs/(jr\d+)/[\w\-%]+/?",
    },
    {
        "name": "Estée Lauder Companies (MAC, Jo Malone, La Mer, Clinique...)",
        "type": "page",
        "urls": ["https://elchxmprod.jobs.hr.cloud.sap/search/?q=&locationsearch=London"],
        "link_re": r"/job/[^\"'\s<>?#]+?/(\d{7,})/?",
    },
    {
        "name": "CHANEL",
        "type": "workday",
        "host": "cc.wd3.myworkdayjobs.com",
        "tenant": "cc",
        "site": "ChanelCareers",
        "search": "London",
        "every_minutes": 30,   # this site limits frequent requests
        "max_pages": 1,
    },

    # ---------- Marketing & PR agencies ----------
    {"name": "Hanover (PR agency)", "type": "greenhouse", "token": "hanover"},
    {"name": "DEPT (marketing agency)", "type": "greenhouse", "token": "dept"},
    {"name": "Fully Vested (PR agency)", "type": "lever", "company": "fullyvested"},
    {
        "name": "Influencer (creator marketing agency)",
        "type": "page",
        "urls": ["https://influencer.teamtailor.com/jobs", "https://influencer.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "Uncovered (social agency)",
        "type": "page",
        "urls": ["https://uncoveredgroup-1688718843.teamtailor.com/jobs", "https://uncoveredgroup-1688718843.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "TEAM LEWIS (PR agency)",
        "type": "page",
        "urls": ["https://www.teamlewis.com/opportunities/"],
        "link_re": r"teamlewis\.com/opportunities/([a-z0-9][\w\-]+)/",
    },
    {
        "name": "Freuds (PR agency)",
        "type": "page",
        "urls": ["https://www.freuds.com/careers"],
        "link_re": r"(?:freuds\.com/|[\"'/])careers/([a-z0-9][\w\-]+)",
        "job_url": "https://www.freuds.com/careers/{id}",
    },

    # ---------- Restaurants, coffee, hospitality ----------
    {"name": "Soho House", "type": "greenhouse", "token": "sohohouseco", "eu": True},
    {
        "name": "Dishoom",
        "type": "page",
        "urls": ["https://jobs.dishoom.com/en-GB", "https://jobs.dishoom.com/"],
        "link_re": r"/job/([A-Za-z0-9]{15,26})",
    },
    {
        "name": "Black Sheep Coffee",
        "type": "page",
        "urls": ["https://careers.blacksheepcoffee.co.uk/", "https://careers.leavetheherdbehind.com/"],
        "link_re": r"/job/([A-Za-z0-9]{15,26})",
        "job_url": "https://careers.blacksheepcoffee.co.uk/job/{id}",
    },
    # ---------- Beauty, fashion, retail (added) ----------
    {"name": "Charlotte Tilbury", "type": "workable", "slug": "charlotte-tilbury"},
    {"name": "Farfetch", "type": "lever", "company": "farfetch"},
    {"name": "Mulberry", "type": "lever", "company": "mulberry"},
    {"name": "Lyst", "type": "workable", "slug": "lyst"},
    {"name": "Jaded London", "type": "workable", "slug": "jaded-london-2"},
    {"name": "Beauty Pie", "type": "greenhouse", "token": "beautypie", "eu": True},
    {"name": "KIKO Milano", "type": "recruitee", "company": "kikomilano"},
    {
        "name": "Selfridges",
        "type": "page",
        "urls": ["https://jobsearch.selfridges.com/jobs/", "https://jobsearch.selfridges.com/"],
        "link_re": r"/jobs/job/[^\"'\s<>?#/]+/(\d+)",
    },
    {
        "name": "END. Clothing",
        "type": "page",
        "urls": ["https://careers.endclothing.com/"],
        "link_re": r"/p/([0-9a-f]{10,})-[\w\-]+",
    },
    {
        "name": "White Stuff",
        "type": "page",
        "urls": ["https://careers.whitestuff.com/jobs", "https://careers.whitestuff.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },    # ---------- Media, entertainment, tech and other great London businesses ----------
    {"name": "The Business of Fashion", "type": "greenhouse", "token": "businessoffashion"},
    {"name": "Financial Times", "type": "greenhouse", "token": "financialtimes33", "eu": True},
    {"name": "The Economist Group", "type": "greenhouse", "token": "theeconomistgroup", "eu": True},
    {"name": "Platform Media", "type": "greenhouse", "token": "platformmedia", "eu": True},
    {
        "name": "Flightstory (The Diary of a CEO)",
        "type": "page",
        "urls": ["https://flightstory.teamtailor.com/jobs", "https://flightstory.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {"name": "OLIVER (in-house agencies)", "type": "greenhouse", "token": "oliver"},
    {
        "name": "Lucky Seven Marketing",
        "type": "page",
        "urls": ["https://thegrowthfoundation-1640252175.teamtailor.com/jobs", "https://thegrowthfoundation-1640252175.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {"name": "GWI", "type": "greenhouse", "token": "globalwebindex"},
    {"name": "Fospha", "type": "greenhouse", "token": "fosphamarketing"},
    {"name": "dunnhumby", "type": "greenhouse", "token": "dunnhumby"},
    {"name": "Founders Forum Group", "type": "workable", "slug": "foundersforum"},
    {"name": "Togather (events)", "type": "workable", "slug": "togather"},
    {"name": "TGP International (hospitality)", "type": "workable", "slug": "tgp-international-3"},
    {"name": "Queensmith (jewellery)", "type": "workable", "slug": "queensmith"},
    {"name": "Stef Philips (jewellery)", "type": "recruitee", "company": "stefphilips"},

    # ---------- DTC fashion & jewellery brands ----------
    {"name": "APM Monaco", "type": "workable", "slug": "apm-monaco"},
    {
        "name": "Missoma",
        "type": "page",
        "urls": ["https://careers.missoma.com/jobs", "https://careers.missoma.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "RIXO",
        "type": "page",
        "urls": ["https://rixo.teamtailor.com/jobs", "https://rixo.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "Space NK",
        "type": "page",
        "urls": ["https://careers.spacenk.com/jobs", "https://careers.spacenk.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "Monica Vinader",
        "type": "page",
        "urls": ["https://careers.monicavinader.com/jobs", "https://careers.monicavinader.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },
    {
        "name": "Astrid & Miyu",
        "type": "page",
        "urls": ["https://astridmiyu.teamtailor.com/jobs", "https://astridmiyu.teamtailor.com/jobs.rss"],
        "link_re": r"/jobs/(\d{5,})-[\w\-]+",
    },

    # ---------- Agencies (PR, social, influencer, media) ----------
    {"name": "Brunswick Group", "type": "greenhouse", "token": "brunswickgroup"},
    {"name": "Burson", "type": "greenhouse", "token": "bursonglobal"},
    {"name": "Axicom (PR agency)", "type": "greenhouse", "token": "axicom"},
    {"name": "Ogilvy UK", "type": "greenhouse", "token": "ogilvyuk"},
    {"name": "Movers+Shakers", "type": "greenhouse", "token": "moversshakers"},
    {"name": "Fuse / OMG UK", "type": "greenhouse", "token": "omguk"},
    {"name": "PMG", "type": "greenhouse", "token": "pmg"},
    {"name": "Game Seven", "type": "greenhouse", "token": "gameseven"},
    {"name": "Awin", "type": "greenhouse", "token": "awin"},
    {"name": "LTK", "type": "greenhouse", "token": "ltkuk"},
    {"name": "VCCP Roar (PR agency)", "type": "greenhouse", "token": "vccproar", "eu": True},
    {"name": "Brainlabs", "type": "greenhouse", "token": "brainlabs", "eu": True},
    {"name": "Sunshine Sachs (PR agency)", "type": "lever", "company": "sunshinesachs"},
    {"name": "Edelman", "type": "lever", "company": "djeholdings"},
    {"name": "Zeno Group (PR agency)", "type": "lever", "company": "zenogroup"},
    {"name": "We Are Social (social agency)", "type": "workable", "slug": "we-are-social-1"},
    {"name": "re:act marketing", "type": "workable", "slug": "re-act-marketing-ltd"},
    {"name": "Inspired Thinking Group", "type": "workable", "slug": "itg"},
    {"name": "Channel Factory", "type": "workable", "slug": "channel-factory"},
    {"name": "Eventogy (events)", "type": "workable", "slug": "eventogy"},
    {"name": "The Specialist Works", "type": "workable", "slug": "thespecialistworks"},
    {"name": "Living Things", "type": "workable", "slug": "living-things-1"},
    {"name": "The Romans (PR agency)", "type": "page", "urls": ["https://theromans.teamtailor.com/jobs", "https://theromans.teamtailor.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "MHP Group / Mischief", "type": "page", "urls": ["https://mhpgroup.teamtailor.com/jobs", "https://mhpgroup.teamtailor.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "M&C Saatchi", "type": "page", "urls": ["https://careers.mcsaatchi.com/jobs", "https://careers.mcsaatchi.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "Publicis Groupe UK (Taylor Herring, Saatchi...)", "type": "smartrecruiters", "company": "PublicisGroupe", "extra": "&country=gb"},

    # ---------- Music, media, entertainment ----------
    {"name": "Sony Music", "type": "greenhouse", "token": "sonymusicentertainment"},
    {"name": "AWAL", "type": "greenhouse", "token": "awalsme"},
    {"name": "DICE", "type": "greenhouse", "token": "dicefm-careers", "eu": True},
    {"name": "Spotify", "type": "lever", "company": "spotify"},
    {"name": "Amuse", "type": "page", "urls": ["https://careers.amuse.io/jobs", "https://careers.amuse.io/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "The Stage Media Company", "type": "page", "urls": ["https://thestagemediacompany.teamtailor.com/jobs", "https://thestagemediacompany.teamtailor.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "Authentic Brands Group", "type": "greenhouse", "token": "authenticbrandsgroup"},

    # ---------- Beauty, fashion, lifestyle brands ----------
    {"name": "Gymshark", "type": "greenhouse", "token": "gymshark", "eu": True},
    {"name": "Wolf & Badger", "type": "workable", "slug": "wolfandbadger"},
    {"name": "Oner Active", "type": "page", "urls": ["https://oneractive.teamtailor.com/jobs", "https://oneractive.teamtailor.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "Oh Polly", "type": "page", "urls": ["https://careers.ohpolly.com/jobs", "https://careers.ohpolly.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},
    {"name": "Tatcha / Prestige Beauty", "type": "greenhouse", "token": "prestigeukhub", "eu": True},
    {"name": "Glossier", "type": "greenhouse", "token": "glossier"},
    {"name": "Reformation", "type": "greenhouse", "token": "reformation"},
    {"name": "e.l.f. Beauty", "type": "lever", "company": "elfbeauty"},
    {"name": "Skin + Me", "type": "workable", "slug": "skinandme"},
    {"name": "Wonderskin", "type": "workable", "slug": "wonderskin"},
    {"name": "Trinny London", "type": "workable", "slug": "trinnylondon"},
    {"name": "Ocean Bottle", "type": "recruitee", "company": "oceanbottle"},
    {"name": "Olsam Group", "type": "recruitee", "company": "olsamgroup"},
    {"name": "Huel", "type": "page", "urls": ["https://careers.huel.com/jobs", "https://careers.huel.com/jobs.rss"], "link_re": r"/jobs/(\d{5,})-[\w\-]+"},

    # ---------- Food, drink, hospitality ----------
    {"name": "Deliveroo", "type": "greenhouse", "token": "deliveroo"},
    {"name": "Too Good To Go", "type": "greenhouse", "token": "toogoodtogo"},
    {"name": "Joe & The Juice UK", "type": "greenhouse", "token": "jjuk", "eu": True},
    {"name": "Convene", "type": "greenhouse", "token": "convene"},
    {"name": "Accor UK (The Savoy, Fairmont, Ennismore)", "type": "smartrecruiters", "company": "AccorHotel", "extra": "&city=London"},
    {"name": "EatClub", "type": "workable", "slug": "eatclub"},
    {
        "name": "innocent drinks",
        "type": "page",
        "urls": ["https://careers.innocentdrinks.co.uk/"],
        "link_re": r"/job/(\d+)",
    },
    {
        "name": "Greene King",
        "type": "page",
        "urls": ["https://jobs.greeneking.co.uk/"],
        "link_re": r"/job/[^\"'\s<>?#]*?-jid-(\d+)",
    },
]

PARALLEL_CHECKS = 10  # companies checked at the same time
MAX_JOB_PAGES_PER_SOURCE = 25  # new job pages opened per company per run
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_SEEN = 30000
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 personal-job-alert-bot/2.1"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-GB,en;q=0.9",
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# =====================================================================
# Filters
# =====================================================================


def _word_regex(words):
    parts = [r"\b" + re.escape(w).replace(r"\ ", r"[\s\-]+") + r"\b" for w in words]
    return re.compile("|".join(parts), re.IGNORECASE)


ROLE_RE = _word_regex(ROLE_KEYWORDS)
LEVEL_RE = _word_regex(LEVEL_KEYWORDS)
STOP_RE = _word_regex(STOP_WORDS)
LOCATION_RE = re.compile(_word_regex(LOCATION_KEYWORDS).pattern + "|" + LONDON_POSTCODE,
                         re.IGNORECASE)


def fits(title, extra="", full_text=""):
    """Role + level match on title/extra, no stop words anywhere."""
    if STOP_RE.search(f"{title} {extra} {full_text}"):
        return False
    text = f"{title} {extra}"
    return bool(ROLE_RE.search(text)) and bool(LEVEL_RE.search(text))


# =====================================================================
# State
# =====================================================================


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("seen", {})
            data.setdefault("meta", {})
            data["meta"].setdefault("baselined", [])
            data["meta"].setdefault("errors", {})
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"seen": {}, "meta": {"baselined": [], "errors": {}}}


def save_state(state):
    if len(state["seen"]) > MAX_SEEN:
        newest = sorted(state["seen"].items(), key=lambda kv: kv[1], reverse=True)
        state["seen"] = dict(newest[:MAX_SEEN])
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# =====================================================================
# Telegram
# =====================================================================


def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] not configured. Message:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": False}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.RequestException as e:
            print(f"[telegram] {e}")
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
            continue
        if not r.ok:
            print(f"[telegram] error {r.status_code}: {r.text[:300]}")
            return False
        time.sleep(1.1)
        return True
    return False


def format_job(job):
    lines = [f"🆕 <b>{html.escape(job['title'][:250])}</b>",
             f"🏢 {html.escape(job['company'])}"]
    if job.get("location"):
        lines.append(f"📍 {html.escape(job['location'][:120])}")
    lines.append(html.escape(job["url"], quote=False))
    return "\n".join(lines)


# =====================================================================
# Source adapters. Each returns a list of "stubs":
#   {"key", "url", "title"(optional), "location"(optional), "needs_page"(bool)}
# =====================================================================


def _get(url, timeout=25, **kw):
    """GET with one retry for slow or busy sites."""
    r = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout if attempt == 0 else 60, **kw)
        except requests.Timeout:
            if attempt == 1:
                raise
            continue
        if r.status_code not in (408, 429, 502, 503, 504):
            return r
        time.sleep(5)
    return r


def _collect(text, base_url, src, found, order):
    link_re = re.compile(src["link_re"])
    for m in link_re.finditer(text):
        jid = m.group(1)
        if jid in found:
            continue
        if src.get("job_url"):
            url = src["job_url"].format(id=jid)
        else:
            url = urljoin(base_url, m.group(0).replace("&amp;", "&").lstrip("\"'"))
        found[jid] = url
        order.append(jid)
    if src.get("json_re") and src.get("job_url"):
        for jid in re.findall(src["json_re"], text):
            if jid not in found:
                found[jid] = src["job_url"].format(id=jid)
                order.append(jid)


def _sitemap_urls(page_url):
    """Find sitemap files of a site via robots.txt, else /sitemap.xml."""
    parts = urlparse(page_url)
    root = f"{parts.scheme}://{parts.netloc}"
    maps = []
    try:
        r = _get(root + "/robots.txt")
        if r.ok:
            maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
    except requests.RequestException:
        pass
    return maps or [root + "/sitemap.xml"]


def scan_sitemaps(src, found, order, max_files=15, max_bytes=8_000_000):
    queue = list(src.get("sitemaps", []))
    for u in src["urls"]:
        for sm in _sitemap_urls(u):
            if sm not in queue:
                queue.append(sm)
    seen_files, total = set(), 0
    while queue and len(seen_files) < max_files and total < max_bytes:
        # Prefer sitemap files that look job related.
        queue.sort(key=lambda u: 0 if re.search(r"job|career|vacanc|offer|position", u, re.I) else 1)
        sm = queue.pop(0)
        if sm in seen_files:
            continue
        seen_files.add(sm)
        try:
            r = _get(sm)
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        text = r.text
        if sm.endswith(".gz") or r.content[:2] == b"\x1f\x8b":
            try:
                text = gzip.decompress(r.content).decode("utf-8", "replace")
            except OSError:
                pass
        total += len(text)
        _collect(text, sm, src, found, order)
        if "<sitemapindex" in text:
            for child in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text):
                if child not in seen_files:
                    queue.append(child.replace("&amp;", "&"))


def list_page(src):
    found, order = {}, []
    for page_url in src["urls"]:
        try:
            r = _get(page_url)
        except requests.RequestException as e:
            print(f"[{src['name']}] {page_url} failed: {e}")
            continue
        if not r.ok:
            print(f"[{src['name']}] {page_url} -> HTTP {r.status_code}")
            continue
        _collect(r.text, page_url, src, found, order)
    src["_via"] = "page"
    if not order:
        # Jobs are probably loaded by JavaScript: try the site's sitemap.
        scan_sitemaps(src, found, order)
        src["_via"] = "sitemap"
    # Newest jobs usually have the highest ids / come last in sitemaps.
    return [{"key": f"{src['name']}|{jid}", "url": found[jid], "needs_page": True} for jid in order]


def list_workday(src):
    api = f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}/jobs"
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(f"https://{src['host']}/en-US/{src['site']}", timeout=25)
    except requests.RequestException:
        pass
    stubs, offset = [], 0
    max_offset = 20 * src.get("max_pages", 10)
    while offset < max_offset:
        body = {"appliedFacets": {}, "limit": 20, "offset": offset,
                "searchText": src.get("search", "")}
        hdrs = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json",
                "Origin": f"https://{src['host']}",
                "Referer": f"https://{src['host']}/en-US/{src['site']}"}
        for attempt in range(2):
            r = sess.post(api, json=body, headers=hdrs, timeout=25)
            if r.status_code != 429:
                break
            time.sleep(15)
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings", [])
        for p in posts:
            path = p.get("externalPath", "")
            if not path:
                continue
            stubs.append({
                "key": f"{src['name']}|{path}",
                "url": f"https://{src['host']}/en-US/{src['site']}{path}",
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "needs_page": False,
            })
        offset += 20
        if len(posts) < 20 or offset >= data.get("total", 0):
            break
    return stubs


def list_greenhouse(src):
    token = src["token"]
    hosts = (["boards-api.eu.greenhouse.io", "boards-api.greenhouse.io"] if src.get("eu")
             else ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"])
    for host in hosts:
        try:
            r = _get(f"https://{host}/v1/boards/{token}/jobs")
        except requests.RequestException:
            continue
        if r.ok:
            src["_via"] = "api"
            return [{
                "key": f"{src['name']}|{j['id']}",
                "url": j.get("absolute_url", ""),
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "needs_page": False,
            } for j in r.json().get("jobs", [])]
    # API closed: read the public job board page instead.
    page_src = {
        "name": src["name"],
        "urls": [f"https://job-boards.eu.greenhouse.io/{token}",
                 f"https://job-boards.greenhouse.io/{token}"]
                if src.get("eu") else
                [f"https://job-boards.greenhouse.io/{token}",
                 f"https://job-boards.eu.greenhouse.io/{token}"],
        "link_re": r"(?:https://job-boards(?:\.eu)?\.greenhouse\.io)?/" + re.escape(token) + r"/jobs/(\d+)",
    }
    found, order = {}, []
    for u in page_src["urls"]:
        try:
            r = _get(u)
        except requests.RequestException:
            continue
        if r.ok:
            _collect(r.text, u, page_src, found, order)
        if order:
            break
    src["_via"] = "board page"
    return [{"key": f"{src['name']}|{jid}", "url": found[jid], "needs_page": True} for jid in order]


def list_lever(src):
    r = _get(f"https://api.lever.co/v0/postings/{src['company']}?mode=json")
    if r.status_code == 404:
        # Some companies use Lever's European servers.
        r = _get(f"https://api.eu.lever.co/v0/postings/{src['company']}?mode=json")
    r.raise_for_status()
    return [{
        "key": f"{src['name']}|{j['id']}",
        "url": j.get("hostedUrl", ""),
        "title": j.get("text", ""),
        "location": (j.get("categories") or {}).get("location", ""),
        "needs_page": False,
    } for j in r.json()]


def list_smartrecruiters(src):
    r = _get(f"https://api.smartrecruiters.com/v1/companies/{src['company']}/postings?limit=100"
             + src.get("extra", ""))
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append({
            "key": f"{src['name']}|{j['id']}",
            "url": f"https://jobs.smartrecruiters.com/{src['company']}/{j['id']}",
            "title": j.get("name", ""),
            "location": ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
            "needs_page": False,
        })
    return out


def list_workable(src):
    slug = src["slug"]
    r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = j.get("locations") or []
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        if not loc and locs:
            loc = ", ".join(x for x in [locs[0].get("city"), locs[0].get("country")] if x)
        code = j.get("shortcode") or j.get("code") or j.get("url")
        out.append({
            "key": f"{src['name']}|{code}",
            "url": j.get("url") or f"https://apply.workable.com/{slug}/j/{code}/",
            "title": j.get("title", ""),
            "location": loc,
            "needs_page": False,
        })
    return out


def list_recruitee(src):
    r = _get(f"https://{src['company']}.recruitee.com/api/offers/")
    r.raise_for_status()
    out = []
    for j in r.json().get("offers", []):
        out.append({
            "key": f"{src['name']}|{j.get('id')}",
            "url": j.get("careers_url") or j.get("url", ""),
            "title": j.get("title", ""),
            "location": j.get("location") or ", ".join(
                x for x in [j.get("city"), j.get("country")] if x),
            "needs_page": False,
        })
    return out


ADAPTERS = {
    "workable": list_workable,
    "recruitee": list_recruitee,
    "page": list_page,
    "workday": list_workday,
    "greenhouse": list_greenhouse,
    "lever": list_lever,
    "smartrecruiters": list_smartrecruiters,
}


def read_job_page(url):
    """Open a job page: return (title, head_text, full_text)."""
    r = _get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = ""
    h1 = soup.find("h1")
    og = soup.find("meta", property="og:title")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif og and og.get("content"):
        title = og["content"].strip()
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    # The top of the page usually holds department, location and contract type.
    start = text.find(title) if title else -1
    head = text[max(start, 0):max(start, 0) + 700]
    return title, head, text


# =====================================================================
# Main
# =====================================================================


def check_source(src, state, now):
    """Return list of jobs to send for this source."""
    name = src["name"]
    stubs = ADAPTERS[src["type"]](src)
    print(f"[{name}] jobs listed: {len(stubs)}")
    if not stubs:
        # Job-board services answer reliably, so "0 jobs" there really means none are open:
        # start watching now so the very first new job is sent. For other sites 0 may mean
        # the page did not load properly, so first-check memory waits for real data.
        if src["type"] != "page" and name not in state["meta"]["baselined"]:
            state["meta"]["baselined"].append(name)
        return []

    # First successful check: remember what exists, send nothing.
    if name not in state["meta"]["baselined"]:
        for s in stubs:
            state["seen"][s["key"]] = now
        state["meta"]["baselined"].append(name)
        print(f"[{name}] first check: remembered {len(stubs)} existing jobs")
        return []

    to_send, opened = [], 0
    for s in stubs:
        if s["key"] in state["seen"]:
            continue
        if s["needs_page"]:
            if opened >= MAX_JOB_PAGES_PER_SOURCE:
                break  # the rest will be checked next run
            opened += 1
            try:
                title, head, full = read_job_page(s["url"])
            except Exception as e:
                print(f"[{name}] could not open {s['url']}: {e}")
                continue
            in_city = bool(LOCATION_RE.search(head) or LOCATION_RE.search(s["url"])
                           or LOCATION_RE.search(full[:4000]))
            ok = in_city and fits(title, head, full)
            location = "London" if in_city else ""
            job = {"title": title or s["url"], "url": s["url"], "company": name,
                   "location": location}
        else:
            loc = s.get("location", "")
            in_city = bool(LOCATION_RE.search(loc) or LOCATION_RE.search(s["title"]))
            if not in_city and src["type"] == "workday" and re.search(r"\d+\s+Locations", loc):
                in_city = True  # multi-location posting found by a London search
            ok = in_city and fits(s["title"])
            job = {"title": s["title"], "url": s["url"], "company": name, "location": loc}
        state["seen"][s["key"]] = now
        if ok:
            to_send.append(job)
        else:
            print(f"[{name}] skipped: {job['title'][:80]}")
    return to_send


def run():
    state = load_state()
    now = int(time.time())
    newly_baselined_before = set(state["meta"]["baselined"])
    status_lines, problems = [], []

    all_jobs = []
    due = []
    for src in SOURCES:
        name = src["name"]
        every = src.get("every_minutes")
        last_ok = state["meta"].setdefault("last_check", {}).get(name, 0)
        if every and now - last_ok < every * 60 - 120:
            continue
        state["meta"]["last_check"][name] = now
        due.append(src)

    def work(src):
        try:
            return src, check_source(src, state, now), None
        except Exception as e:
            return src, [], e

    # Several companies are checked at the same time so 100+ sites fit into a few minutes.
    with ThreadPoolExecutor(max_workers=PARALLEL_CHECKS) as ex:
        results = list(ex.map(work, due))

    for src, jobs, e in results:
        name = src["name"]
        if e is None:
            all_jobs.extend(jobs)
            state["meta"]["errors"].pop(name, None)
            continue
        print(f"[{name}] ERROR: {e}")
        err = state["meta"]["errors"].setdefault(name, {"count": 0, "alerted": 0})
        err["count"] += 1
        # Alert after about an hour of failures (a full day for "too many requests"),
        # then at most once a day.
        limit = 48 if "429" in str(e) else 6
        if err["count"] >= limit and now - err.get("alerted", 0) > 24 * 3600:
            problems.append(f"• {name}: {e}")
            err["alerted"] = now

    # Tell the user once when companies start being watched.
    started = [n for n in state["meta"]["baselined"] if n not in newly_baselined_before]
    if started:
        status_lines.append("👀 Бот начал следить за компаниями:")
        status_lines += [f"• {n}" for n in started]
        status_lines.append("Уже открытые вакансии запомнены, дальше будут приходить только новые.")
        tg_send("\n".join(status_lines))

    sent = 0
    for job in all_jobs:
        if tg_send(format_job(job)):
            sent += 1

    if problems:
        tg_send("⚠️ Бот вакансий не может проверить:\n" + html.escape("\n".join(problems))
                + "\n\nПерешлите это сообщение Claude, он поправит.")

    state["meta"]["last_run"] = now
    save_state(state)
    print(f"[done] new jobs sent: {sent}")


def diagnose(src):
    """Short technical notes about a site where no jobs were found."""
    notes = []
    for u in src.get("urls", [])[:3]:
        try:
            r = _get(u)
            t = r.text
            notes.append(f"{u} -> {r.status_code}, {len(t)//1000} KB, final {r.url}")
            for word in ["algolia", "__NEXT_DATA__", "graphql", "smartrecruiters", "workday",
                         "greenhouse", "lever.co", "teamtailor", "successfactors", "avature",
                         "jobvite", "icims", "taleo", "oraclecloud", "harri", "/api/"]:
                if word.lower() in t.lower():
                    notes.append(f"  contains: {word}")
            m = re.findall(r'(?i)(?:appId|applicationId|ALGOLIA_APP_ID)["\']?\s*[:=]\s*["\']([A-Z0-9]{8,12})', t)
            if m:
                notes.append(f"  algolia app: {m[0]}")
            refs = sorted(set(re.findall(r'(?<![A-Za-z0-9])([A-Z]{3,6}\d{5})(?![A-Za-z0-9])', t)))
            if refs:
                notes.append(f"  refs: {len(refs)} e.g. {', '.join(refs[:6])}")
            for api in sorted(set(re.findall(r'https?://[\w.-]+/[\w./-]*(?:api|search|jobs)[\w./-]*', t)))[:5]:
                notes.append(f"  url: {api[:120]}")
        except requests.RequestException as e:
            notes.append(f"{u} -> error {str(e)[:80]}")
    if src.get("urls"):
        maps = _sitemap_urls(src["urls"][0])
        notes.append("  sitemaps: " + ", ".join(m[:80] for m in maps[:3]))
    return notes


def tg_send_long(text):
    """Send a long text as several Telegram messages."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            tg_send(html.escape(chunk))
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        tg_send(html.escape(chunk))


def test():
    def probe(src):
        try:
            stubs = ADAPTERS[src["type"]](src)
            extra = diagnose(src) if (not stubs and src["type"] == "page") else []
            return src, stubs, None, extra
        except Exception as e:
            return src, [], e, []

    with ThreadPoolExecutor(max_workers=PARALLEL_CHECKS) as ex:
        results = list(ex.map(probe, SOURCES))

    ok_lines, bad_lines, details = [], [], []
    for src, stubs, e, extra in results:
        if e is not None:
            bad_lines.append(f"❌ {src['name']}: {str(e)[:120]}")
            continue
        via = {"sitemap": " (через карту сайта)", "board page": " (через страницу вакансий)"}.get(
            src.get("_via", ""), "")
        if stubs:
            ok_lines.append(f"✅ {src['name']}: {len(stubs)}{via}")
        else:
            bad_lines.append(f"⚠️ {src['name']}: вакансий не видно")
            if extra:
                details.extend([f"[{src['name'][:30]}]"] + extra)

    lines = ["✅ Бот вакансий подключён к Telegram.", "",
             f"Работают {len(ok_lines)} из {len(SOURCES)} компаний.", ""]
    if bad_lines:
        lines += ["Не получилось проверить:"] + bad_lines + [""]
    lines += ["Работают (сколько вакансий видно сейчас):"] + ok_lines
    lines += ["", "Если где-то ⚠️ или ❌, перешлите эти сообщения Claude."]
    msg = "\n".join(lines)
    print(msg)
    tg_send_long(msg)
    if details:
        detail_msg = "🔧 Технические детали для Claude:\n" + "\n".join(details)
        print(detail_msg)
        tg_send_long(detail_msg[:11000])


if __name__ == "__main__":
    test() if "--test" in sys.argv else run()
