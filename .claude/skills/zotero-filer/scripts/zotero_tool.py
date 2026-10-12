#!/usr/bin/env python3
"""Helper for the zotero-filer skill.

Talks to the Zotero Web API (your library) and to CrossRef / doi.org (paper
details). Every command prints JSON to stdout. On failure it prints
{"error": "..."} and exits with code 1.

Settings (environment variables):
  ZOTERO_API_KEY     key from zotero.org/settings/keys (needs write access)
  ZOTERO_USER_ID     your numeric user ID (same page)
  ZOTERO_GROUP_ID    use a group library instead of your own (optional)
  ZOTERO_TO_READ     name of your "to read" folder (default: "To read")
  ZOTERO_FILER_EMAIL your email, sent to CrossRef/Unpaywall as a polite
                     contact (optional; needed for finding free PDFs)
"""

import argparse
import difflib
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

ZOTERO_API = "https://api.zotero.org"
CROSSREF_API = "https://api.crossref.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
USER_AGENT = "zotero-filer/1.0 (https://github.com/gerardchung/claude-zotero-filer-public-plugin)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 30
CACHE_TTL = 3600

# One shared HTTP session. Tests replace this with a fake.
HTTP = requests.Session()

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
DOI_URL_SUFFIXES = ("/full", "/abstract", "/pdf", "/epdf", "/fulltext", "/html", "/meta")


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def clean_doi(raw):
    """Return a bare DOI from text like 'doi:10.1/x.', 'https://doi.org/10.1/x'."""
    if not raw:
        return None
    m = DOI_RE.search(unquote(raw))
    if not m:
        return None
    doi = m.group(1)
    for sep in ("?", "#"):
        doi = doi.split(sep)[0]
    doi = doi.rstrip(".,;:)]}'\"")
    changed = True
    while changed:
        changed = False
        for suffix in DOI_URL_SUFFIXES:
            if doi.lower().endswith(suffix):
                doi = doi[: -len(suffix)]
                changed = True
    return doi or None


def first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def clean_text(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def drop_empty(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def http(method, url, *, ok=(200,), **kw):
    """HTTP call with a timeout, a User-Agent and simple retry on 429/5xx."""
    headers = {"User-Agent": USER_AGENT, **(kw.pop("headers", None) or {})}
    for attempt in range(4):
        try:
            resp = HTTP.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            if attempt == 3:
                raise ToolError(f"Could not reach {urlparse(url).netloc}: {e}")
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            wait = resp.headers.get("Retry-After") or resp.headers.get("Backoff")
            time.sleep(min(float(wait), 30) if wait and wait.isdigit() else 2 ** attempt)
            continue
        if ok and resp.status_code not in ok:
            raise HTTPError(resp)
        return resp
    raise ToolError(f"Gave up on {url}")


class HTTPError(ToolError):
    def __init__(self, resp):
        self.status = resp.status_code
        self.resp = resp
        body = (resp.text or "")[:300].strip()
        super().__init__(f"HTTP {resp.status_code} from {urlparse(resp.url or '').netloc}: {body}")


# --------------------------------------------------------------------------
# Paper details: CrossRef / doi.org / web pages / PDFs
# --------------------------------------------------------------------------

TYPE_MAP = {
    "journal-article": "journalArticle", "article-journal": "journalArticle",
    "book-chapter": "bookSection", "chapter": "bookSection", "book-section": "bookSection",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
    "proceedings-article": "conferencePaper", "paper-conference": "conferencePaper",
    "posted-content": "preprint", "article": "preprint",
    "report": "report", "dissertation": "thesis", "thesis": "thesis",
    "dataset": "dataset", "webpage": "webpage", "post-weblog": "blogPost",
}


def _date_from(m):
    for key in ("issued", "published-print", "published-online", "published", "created"):
        parts = (m.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            p = [int(x) for x in parts[0] if x is not None]
            return "-".join([f"{p[0]:04d}"] + [f"{x:02d}" for x in p[1:3]])
        raw = (m.get(key) or {}).get("raw")
        if raw:
            return raw
    return ""


def _people(lst):
    out = []
    for a in lst or []:
        if a.get("family"):
            out.append(drop_empty({"first": a.get("given", ""), "last": a["family"]}))
        elif a.get("name") or a.get("literal"):
            out.append({"name": a.get("name") or a.get("literal")})
    return out


def normalize_csl(m, source):
    """CrossRef 'message' or CSL-JSON (from doi.org) -> our simple paper dict."""
    title = clean_text(first(m.get("title")))
    subtitle = clean_text(first(m.get("subtitle")))
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"
    abstract = clean_text(m.get("abstract"))
    abstract = re.sub(r"^(abstract|summary)[\s:.-]+", "", abstract, flags=re.I)
    date = _date_from(m)
    container = clean_text(first(m.get("container-title")))
    item_type = TYPE_MAP.get(m.get("type", ""), "journalArticle" if container else "document")
    doi = m.get("DOI") or ""
    return drop_empty({
        "item_type": item_type,
        "title": title,
        "authors": _people(m.get("author")),
        "editors": _people(m.get("editor")),
        "date": date,
        "year": date[:4] if date[:4].isdigit() else "",
        "container": container,
        "container_short": clean_text(first(m.get("short-container-title")))
        or clean_text(m.get("container-title-short")),
        "volume": str(m.get("volume") or ""),
        "issue": str(m.get("issue") or ""),
        "pages": m.get("page") or "",
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else m.get("URL", ""),
        "abstract": abstract,
        "publisher": m.get("publisher") or "",
        "place": m.get("publisher-location") or m.get("publisher-place") or "",
        "issn": first(m.get("ISSN")),
        "isbn": first(m.get("ISBN")),
        "language": m.get("language") or "",
        "source": source,
    })


def crossref_params(extra=None):
    params = dict(extra or {})
    email = os.environ.get("ZOTERO_FILER_EMAIL")
    if email:
        params["mailto"] = email
    return params


def lookup_doi(doi):
    doi = clean_doi(doi) or doi
    try:
        resp = http("GET", f"{CROSSREF_API}/works/{quote(doi, safe='/')}", params=crossref_params())
        return normalize_csl(resp.json()["message"], "crossref")
    except HTTPError as e:
        if e.status != 404:
            raise
    # Not a CrossRef DOI (e.g. arXiv/DataCite): ask doi.org for CSL-JSON.
    try:
        resp = http("GET", f"https://doi.org/{quote(doi, safe='/')}",
                    headers={"Accept": "application/vnd.citationstyles.csl+json"})
        return normalize_csl(resp.json(), "doi.org")
    except (HTTPError, ValueError):
        raise ToolError(f"Could not find details for DOI {doi}. Check it is typed correctly.")


def search_title(title, rows=3):
    resp = http("GET", f"{CROSSREF_API}/works",
                params=crossref_params({"query.bibliographic": title, "rows": rows}))
    out = []
    for m in resp.json()["message"].get("items", []):
        paper = normalize_csl(m, "crossref-search")
        paper["match"] = round(title_similarity(title, paper.get("title", "")), 2)
        out.append(paper)
    return out


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name and a.get("content"):
                self.meta.setdefault(name, []).append(a["content"].strip())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_page_meta(page_html, url):
    """Read citation_* / Dublin Core / OpenGraph tags from a page."""
    p = _MetaParser()
    p.feed(page_html)
    meta = p.meta

    def get(*names):
        for n in names:
            if meta.get(n):
                return meta[n][0]
        return ""

    doi = None
    for n in ("citation_doi", "dc.identifier", "prism.doi", "bepress_citation_doi", "dc.relation"):
        for v in meta.get(n, []):
            doi = doi or clean_doi(v)
    pdf_url = get("citation_pdf_url")
    title = get("citation_title", "dc.title")
    if title:
        authors = []
        for a in meta.get("citation_author", []) or meta.get("dc.creator", []):
            if "," in a:
                last, given = [s.strip() for s in a.split(",", 1)]
            else:
                bits = a.split()
                given, last = " ".join(bits[:-1]), bits[-1] if bits else ""
            authors.append(drop_empty({"first": given, "last": last}))
        date = get("citation_publication_date", "citation_date", "dc.date").replace("/", "-")
        container = get("citation_journal_title", "citation_conference_title", "citation_book_title")
        paper = drop_empty({
            "item_type": "journalArticle" if get("citation_journal_title") else
            ("report" if get("citation_technical_report_number") else "document"),
            "title": clean_text(title), "authors": authors, "date": date, "year": date[:4],
            "container": container, "volume": get("citation_volume"), "issue": get("citation_issue"),
            "pages": "-".join(x for x in (get("citation_firstpage"), get("citation_lastpage")) if x),
            "doi": doi or "", "url": url, "abstract": clean_text(get("citation_abstract", "dc.description")),
            "publisher": get("citation_publisher", "dc.publisher"), "source": "page",
        })
    else:
        published = get("article:published_time", "date")
        paper = drop_empty({
            "item_type": "webpage",
            "title": clean_text(get("og:title") or p.title),
            "authors": [{"name": a} for a in meta.get("author", [])],
            "container": get("og:site_name"), "date": published[:10], "year": published[:4],
            "url": url, "abstract": clean_text(get("og:description", "description")), "source": "page",
        })
    return doi, pdf_url, paper


def save_pdf(resp, url, out_dir=None):
    """Save a response to disk if it is a PDF; return the path, else None."""
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        return None
    out = Path(out_dir or Path(tempfile.gettempdir()) / "zotero-filer")
    out.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(resp.url or url).path).name or "paper.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    path = out / name
    path.write_bytes(resp.content)
    return str(path)


def download(url, out_dir=None):
    """Download a file; return its path if it is a PDF, else None."""
    resp = http("GET", url, ok=None, headers={"User-Agent": BROWSER_UA}, allow_redirects=True)
    return save_pdf(resp, url, out_dir)


def read_pdf(path):
    """Return (doi or None, pdf title or '', first ~1500 chars of text)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ToolError("The 'pypdf' package is missing. Run: python3 -m pip install -r requirements.txt")
    try:
        reader = PdfReader(path)
    except Exception as e:
        raise ToolError(f"Could not open the PDF: {e}")
    info = {}
    try:
        info = {str(k): str(v) for k, v in (reader.metadata or {}).items()}
    except Exception:
        pass
    text = ""
    for page in reader.pages[:2]:
        try:
            text += (page.extract_text() or "") + "\n"
        except Exception:
            pass
    doi = None
    for k in ("/doi", "/DOI", "/Subject", "/Keywords", "/dc:identifier", "/prism:doi"):
        doi = doi or clean_doi(info.get(k, ""))
    doi = doi or clean_doi(re.sub(r"(10\.\d{4,9}/)\s+", r"\1", text))
    title = info.get("/Title", "").strip()
    if len(title) < 15 or re.search(r"^(microsoft word|untitled)|\.(docx?|pdf|tex)$", title, re.I):
        title = ""
    return doi, title, re.sub(r"[ \t]+", " ", text).strip()[:1500]


def detect_input(value):
    if os.path.isfile(os.path.expanduser(value)):
        return "pdf"
    v = value.strip()
    if v.lower().endswith(".pdf") and not re.match(r"^(https?://|www\.)", v, re.I):
        raise ToolError(f"PDF not found: {v}")
    if re.match(r"^(doi:\s*)?10\.\d{4,9}/\S+$", v, re.I):
        return "doi"
    if re.match(r"^(https?://|www\.)", v, re.I):
        return "url"
    return "title"


def lookup(value, rows=3):
    kind = detect_input(value)
    out = {"input_type": kind}
    if kind == "doi":
        out["candidates"] = [lookup_doi(value)]
    elif kind == "title":
        out["candidates"] = search_title(value, rows)
        out["note"] = "Search results. Confirm the right one with the user unless match is ~1.0."
    elif kind == "pdf":
        path = str(Path(os.path.expanduser(value)).resolve())
        doi, title, text = read_pdf(path)
        out.update(pdf_path=path, pdf_text_start=text)
        if doi:
            try:
                out["candidates"] = [lookup_doi(doi)]
            except ToolError as e:
                out["candidates"], out["note"] = [], str(e)
        elif title:
            out["candidates"] = search_title(title, rows)
        else:
            out["candidates"] = []
        flat = norm_title(text)
        for c in out["candidates"]:
            c["title_found_in_pdf"] = norm_title(c.get("title", ""))[:40] in flat
        out.setdefault("note", "Check the candidate's title matches pdf_text_start. "
                       "If not, find the title in pdf_text_start and run lookup with it.")
    else:
        url = value if value.lower().startswith("http") else "https://" + value
        doi = clean_doi(url) if ("doi.org/" in url or "/doi/" in url) else None
        if doi:
            out["candidates"] = [lookup_doi(doi)]
            return out
        resp = http("GET", url, ok=None, headers={"User-Agent": BROWSER_UA}, allow_redirects=True)
        path = save_pdf(resp, url)
        if path:
            return lookup(path, rows)
        if resp.status_code != 200:
            out["candidates"] = []
            out["note"] = (f"The page could not be opened (HTTP {resp.status_code}); many publishers "
                           "block scripts. Ask the user for the DOI or the title.")
            return out
        doi, pdf_url, page_paper = parse_page_meta(resp.text, url)
        if doi:
            try:
                page_paper = lookup_doi(doi)
            except ToolError:
                pass
        out["candidates"] = [page_paper]
        if pdf_url:
            out["pdf_url"] = pdf_url
    return out


def find_free_pdf(doi=None, url=None, out_dir=None):
    tried = []
    urls = [url] if url else []
    email = os.environ.get("ZOTERO_FILER_EMAIL")
    if doi and email:
        try:
            resp = http("GET", f"{UNPAYWALL_API}/{quote(clean_doi(doi) or doi, safe='/')}",
                        params={"email": email})
            data = resp.json()
            locs = [data.get("best_oa_location")] + (data.get("oa_locations") or [])
            for loc in locs:
                if loc and loc.get("url_for_pdf") and loc["url_for_pdf"] not in urls:
                    urls.append(loc["url_for_pdf"])
        except ToolError:
            pass
    for u in urls:
        tried.append(u)
        try:
            path = download(u, out_dir)
        except ToolError:
            path = None
        if path:
            return {"pdf_path": path, "from": u}
    note = "No free PDF found."
    if doi and not email:
        note += " Set ZOTERO_FILER_EMAIL to let the tool search Unpaywall for free copies."
    return {"pdf_path": None, "tried": tried, "note": note}


# --------------------------------------------------------------------------
# Zotero Web API
# --------------------------------------------------------------------------

class Zotero:
    def __init__(self, api_key, library_prefix, base=ZOTERO_API):
        self.key = api_key
        self.prefix = library_prefix  # "/users/123" or "/groups/456"
        self.base = base
        self._templates = {}
        self.last_total = 0

    def call(self, method, path, *, ok=(200,), headers=None, **kw):
        h = {"Zotero-API-Key": self.key, "Zotero-API-Version": "3", **(headers or {})}
        try:
            return http(method, self.base + path, ok=ok, headers=h, **kw)
        except HTTPError as e:
            raise ToolError(zotero_error_message(e)) from None

    def lib(self, method, path, **kw):
        return self.call(method, self.prefix + path, **kw)

    def get_all(self, path, params=None, limit=None):
        out, start = [], 0
        while True:
            page_size = 100 if limit is None else min(100, limit - len(out))
            p = {**(params or {}), "limit": page_size, "start": start}
            resp = self.lib("GET", path, params=p)
            batch = resp.json()
            out.extend(batch)
            total = int(resp.headers.get("Total-Results", len(out)))
            self.last_total = total
            start += len(batch)
            if not batch or start >= total or (limit is not None and len(out) >= limit):
                return out

    def template(self, item_type, **params):
        cache_key = (item_type, tuple(sorted(params.items())))
        if cache_key not in self._templates:
            resp = self.call("GET", "/items/new", params={"itemType": item_type, **params})
            self._templates[cache_key] = resp.json()
        return json.loads(json.dumps(self._templates[cache_key]))

    def collections(self):
        return self.get_all("/collections")

    def create(self, path, objects):
        resp = self.lib("POST", path, json=objects)
        data = resp.json()
        if data.get("failed"):
            msg = next(iter(data["failed"].values())).get("message", "unknown error")
            raise ToolError(f"Zotero refused to save: {msg}")
        return [data["successful"][str(i)]["key"] for i in range(len(objects))]

    def item(self, key):
        return self.lib("GET", f"/items/{key}").json()

    def patch_item(self, key, version, changes):
        self.lib("PATCH", f"/items/{key}", json=changes, ok=(204,),
                 headers={"If-Unmodified-Since-Version": str(version)})

    def upload_file(self, parent_key, file_path):
        """Attach a file to an item (stored in Zotero's own storage)."""
        p = Path(file_path)
        data = p.read_bytes()
        att = self.template("attachment", linkMode="imported_file")
        for k in ("md5", "mtime"):
            att.pop(k, None)
        att.update(parentItem=parent_key, title=p.name, filename=p.name,
                   contentType=mimetypes.guess_type(p.name)[0] or "application/pdf")
        key = self.create("/items", [att])[0]
        form = {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}
        auth = self.lib("POST", f"/items/{key}/file", headers=form, data={
            "md5": hashlib.md5(data).hexdigest(), "filename": p.name,
            "filesize": len(data), "mtime": int(p.stat().st_mtime * 1000),
        }).json()
        if not auth.get("exists"):
            # Upload to Zotero's storage server. No API key goes here.
            body = auth["prefix"].encode() + data + auth["suffix"].encode()
            try:
                http("POST", auth["url"], ok=(201,), data=body,
                     headers={"Content-Type": auth["contentType"]})
            except HTTPError as e:
                raise ToolError(f"Uploading the PDF failed: {e}")
            self.lib("POST", f"/items/{key}/file", headers=form, ok=(204,),
                     data={"upload": auth["uploadKey"]})
        return key


def zotero_error_message(e):
    hints = {
        400: "Zotero did not accept the request",
        403: "Zotero said 'forbidden'. Check ZOTERO_API_KEY and ZOTERO_USER_ID are right, and "
             "that the key has 'Allow write access' ticked (zotero.org/settings/keys)",
        404: "Zotero could not find that item or folder",
        409: "The library is locked for a moment; try again",
        412: "The item was changed somewhere else just now; run the command again",
        413: "Your Zotero file storage is full. Free up space or upgrade storage on zotero.org",
    }
    detail = (e.resp.text or "").strip()[:200]
    return f"{hints.get(e.status, f'Zotero error {e.status}')}. ({detail})" if detail else hints.get(e.status, str(e))


def connect():
    key = os.environ.get("ZOTERO_API_KEY")
    if not key:
        raise ToolError("ZOTERO_API_KEY is not set. Follow 'Setup' in the README.")
    group = os.environ.get("ZOTERO_GROUP_ID")
    user = os.environ.get("ZOTERO_USER_ID")
    if group:
        return Zotero(key, f"/groups/{group}")
    if user:
        return Zotero(key, f"/users/{user}")
    raise ToolError("ZOTERO_USER_ID is not set. Find it at zotero.org/settings/keys.")


def collection_paths(collections):
    """Build 'Parent > Child' paths. Returns {key: {key, name, path, parent, num_items}}."""
    data = {c["key"]: c["data"] for c in collections}
    meta = {c["key"]: c.get("meta", {}) for c in collections}
    out = {}
    for key, d in data.items():
        names, seen, cur = [], set(), key
        while cur and cur in data and cur not in seen:
            seen.add(cur)
            names.append(data[cur]["name"])
            cur = data[cur].get("parentCollection") or None
        out[key] = {"key": key, "name": d["name"], "path": " > ".join(reversed(names)),
                    "parent": d.get("parentCollection") or None,
                    "num_items": meta[key].get("numItems", 0)}
    return out


def find_to_read(paths, override_key=None):
    if override_key:
        if override_key not in paths:
            raise ToolError(f"No folder with key {override_key}.")
        return paths[override_key]
    name = os.environ.get("ZOTERO_TO_READ", "To read").strip()
    matches = [c for c in paths.values()
               if c["name"].lower() == name.lower() or c["path"].lower() == name.lower()]
    if not matches:
        # Allow sorting prefixes such as "1_To read" or "01 - To read".
        bare = lambda n: re.sub(r"^[\W\d_]+", "", n).lower()
        matches = [c for c in paths.values() if bare(c["name"]) == bare(name)]
    if not matches:
        raise ToolError(f"No folder named '{name}' in Zotero. Create it, or set ZOTERO_TO_READ "
                        "to your folder's name.")
    if len(matches) > 1:
        options = "; ".join(f"{c['path']} (key {c['key']})" for c in matches)
        raise ToolError(f"More than one folder is called '{name}': {options}. "
                        "Ask the user which one, then pass --to-read-key KEY.")
    return matches[0]


def item_summary(item, paths, skip_collection=None):
    d = item["data"]
    creators = [c for c in d.get("creators", []) if c.get("creatorType") == "author"] or d.get("creators", [])
    names = [c.get("lastName") or c.get("name", "") for c in creators]
    authors = ""
    if names:
        authors = names[0] + (" et al." if len(names) > 2 else f" & {names[1]}" if len(names) == 2 else "")
    date = d.get("date", "")
    year = re.search(r"\b(1[5-9]|20)\d{2}\b", date)
    return drop_empty({
        "key": item["key"],
        "title": d.get("title", ""),
        "authors": authors,
        "year": year.group(0) if year else "",
        "item_type": d.get("itemType"),
        "journal": d.get("publicationTitle") or d.get("bookTitle") or d.get("proceedingsTitle")
        or d.get("websiteTitle") or d.get("repository", ""),
        "doi": d.get("DOI", ""),
        "url": d.get("url", ""),
        "date_added": d.get("dateAdded", "")[:10],
        "folders": [paths[k]["path"] for k in d.get("collections", [])
                    if k in paths and k != skip_collection],
        "tags": [t["tag"] for t in d.get("tags", [])],
        "abstract": (d.get("abstractNote", "")[:300] + "…") if len(d.get("abstractNote", "")) > 300
        else d.get("abstractNote", ""),
    })


# Which Zotero field each of our keys goes into (first one the item type has).
FIELD_MAP = {
    "title": ["title"],
    "abstract": ["abstractNote"],
    "date": ["date"],
    "container": ["publicationTitle", "bookTitle", "proceedingsTitle", "websiteTitle",
                  "blogTitle", "repository"],
    "container_short": ["journalAbbreviation"],
    "volume": ["volume"],
    "issue": ["issue"],
    "pages": ["pages"],
    "doi": ["DOI"],
    "url": ["url"],
    "publisher": ["publisher", "institution", "university"],
    "place": ["place"],
    "issn": ["ISSN"],
    "isbn": ["ISBN"],
    "language": ["language"],
}
EDITOR_TYPES = {"book", "bookSection", "conferencePaper"}


def _creator(person, role):
    if person.get("name"):
        return {"creatorType": role, "name": person["name"]}
    return {"creatorType": role, "firstName": person.get("first", ""), "lastName": person.get("last", "")}


def paper_to_zotero(paper, template, collections, tags=()):
    item = template
    extra = []
    for ours, fields in FIELD_MAP.items():
        value = paper.get(ours)
        if not value:
            continue
        target = next((f for f in fields if f in item), None)
        if target:
            item[target] = value
        elif ours == "doi":
            extra.append(f"DOI: {value}")
    creators = [_creator(a, "author") for a in paper.get("authors", [])]
    if item.get("itemType") in EDITOR_TYPES:
        creators += [_creator(e, "editor") for e in paper.get("editors", [])]
    item["creators"] = creators
    if extra and "extra" in item:
        item["extra"] = "\n".join(filter(None, [item.get("extra", "")] + extra))
    if "accessDate" in item and paper.get("url") and paper.get("item_type") in ("webpage", "blogPost", "document"):
        item["accessDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item["collections"] = list(collections)
    item["tags"] = [{"tag": t} for t in tags]
    return item


def select_link(zot, key):
    if zot.prefix.startswith("/groups/"):
        return f"zotero://select/groups/{zot.prefix.split('/')[2]}/items/{key}"
    return f"zotero://select/library/items/{key}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cache_file(zot):
    base = Path(os.environ.get("ZOTERO_FILER_CACHE") or Path.home() / ".cache" / "zotero-filer")
    return base / f"collections{zot.prefix.replace('/', '-')}.json"


def cmd_check(zot, args):
    info = zot.call("GET", "/keys/current").json()
    access = info.get("access", {})
    if zot.prefix.startswith("/users/"):
        lib = access.get("user", {})
    else:
        groups = access.get("groups", {})
        lib = groups.get(zot.prefix.split("/")[2]) or groups.get("all") or {}
    result = {"ok": False, "username": info.get("username"), "access": lib, "problems": []}
    problems = result["problems"]
    if zot.prefix.startswith("/users/") and str(info.get("userID")) != zot.prefix.split("/")[2]:
        problems.append(f"ZOTERO_USER_ID does not match the key (the key belongs to user "
                        f"{info.get('userID')}).")
    if not lib.get("library"):
        problems.append("The key cannot read this library. Tick 'Allow library access'.")
        return result
    if not lib.get("write"):
        problems.append("The key cannot write. Tick 'Allow write access' on zotero.org/settings/keys.")
    if zot.prefix.startswith("/users/") and not lib.get("files"):
        problems.append("The key cannot upload files (PDFs).")
    paths = collection_paths(zot.collections())
    result["folders"] = len(paths)
    try:
        result["to_read_folder"] = find_to_read(paths)["path"]
    except ToolError as e:
        problems.append(str(e))
    result["ok"] = not problems
    return result


def cmd_collections(zot, args):
    cache = cache_file(zot)
    if not args.refresh and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        cached = json.loads(cache.read_text())
        if cached.get("samples") == args.samples:
            return cached
    paths = collection_paths(zot.collections())

    def samples(key):
        if not args.samples:
            return key, []
        items = zot.lib("GET", f"/collections/{key}/items/top",
                        params={"limit": args.samples, "sort": "dateAdded", "direction": "desc"}).json()
        return key, [i["data"].get("title", "") for i in items]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, titles in pool.map(samples, list(paths)):
            paths[key]["sample_titles"] = titles
    try:
        to_read = find_to_read(paths)
    except ToolError:
        to_read = None
    result = {"samples": args.samples,
              "to_read": {"key": to_read["key"], "path": to_read["path"]} if to_read else None,
              "collections": sorted(paths.values(), key=lambda c: c["path"].lower())}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result))
    except OSError:
        pass
    return result


def cmd_lookup(zot, args):
    return lookup(args.input, args.rows)


def cmd_fetch_pdf(zot, args):
    if not args.doi and not args.url:
        raise ToolError("Give --doi or --url.")
    return find_free_pdf(args.doi, args.url, args.out_dir)


def cmd_find_dup(zot, args):
    if not args.doi and not args.title:
        raise ToolError("Give --doi and/or --title.")
    found = {}
    if args.doi:
        doi = (clean_doi(args.doi) or args.doi).lower()
        for it in zot.get_all("/items/top", {"q": doi, "qmode": "everything"}, limit=25):
            d = it["data"]
            if doi == (d.get("DOI") or "").lower() or doi in (d.get("extra") or "").lower() \
                    or doi in (d.get("url") or "").lower():
                found[it["key"]] = it
    if args.title:
        words = norm_title(args.title).split()[:10]
        for it in zot.get_all("/items/top", {"q": " ".join(words)}, limit=25):
            if title_similarity(args.title, it["data"].get("title", "")) >= 0.9:
                found[it["key"]] = it
    paths = collection_paths(zot.collections()) if found else {}
    return {"duplicates": [item_summary(it, paths) for it in found.values()]}


def resolve_folder_keys(keys, paths):
    out = []
    for k in keys:
        if k not in paths:
            raise ToolError(f"No folder with key {k}. Run 'collections --refresh' to see current keys.")
        out.append(k)
    return out


def split_keys(value):
    return [k.strip() for k in (value or "").split(",") if k.strip()]


def cmd_add(zot, args):
    if args.meta:
        paper = json.loads(Path(args.meta).read_text())
        if "candidates" in paper:  # whole lookup output: use the first candidate
            if not paper["candidates"]:
                raise ToolError("The lookup output has no candidates.")
            paper = paper["candidates"][0]
    elif args.doi:
        paper = lookup_doi(args.doi)
    else:
        raise ToolError("Give --meta FILE or --doi DOI.")
    if not paper.get("title"):
        raise ToolError("The paper details have no title.")
    paths = collection_paths(zot.collections())
    keys = resolve_folder_keys(split_keys(args.collections), paths)
    if args.to_read:
        to_read = find_to_read(paths, args.to_read_key)["key"]
        if to_read not in keys:
            keys.append(to_read)
    if not keys and not args.allow_unfiled:
        raise ToolError("No folder chosen. Pass --collections and/or --to-read "
                        "(or --allow-unfiled to save it without a folder).")
    if args.pdf and not os.path.isfile(os.path.expanduser(args.pdf)):
        raise ToolError(f"PDF not found: {args.pdf}")
    item_type = paper.get("item_type") or "journalArticle"
    try:
        template = zot.template(item_type)
    except ToolError:
        item_type, template = "document", zot.template("document")
    item = paper_to_zotero(paper, template, keys, split_keys(args.tags))
    key = zot.create("/items", [item])[0]
    result = {"added": {"key": key, "title": paper["title"], "item_type": item_type,
                        "folders": [paths[k]["path"] for k in keys],
                        "open_in_zotero": select_link(zot, key)}}
    if args.pdf:
        try:
            zot.upload_file(key, os.path.expanduser(args.pdf))
            result["added"]["pdf"] = "attached"
        except ToolError as e:
            result["added"]["pdf"] = "failed"
            result["pdf_error"] = f"The paper was saved, but the PDF was not attached: {e}"
    return result


def cmd_new_collection(zot, args):
    paths = collection_paths(zot.collections())
    if args.parent:
        resolve_folder_keys([args.parent], paths)
    for c in paths.values():
        if c["name"].lower() == args.name.lower() and c["parent"] == (args.parent or None):
            return {"collection": c, "already_existed": True}
    key = zot.create("/collections", [{"name": args.name, "parentCollection": args.parent or False}])[0]
    try:
        cache_file(zot).unlink()
    except OSError:
        pass
    path = f"{paths[args.parent]['path']} > {args.name}" if args.parent else args.name
    return {"collection": {"key": key, "name": args.name, "path": path, "parent": args.parent},
            "already_existed": False}


def cmd_to_read(zot, args):
    paths = collection_paths(zot.collections())
    folder = find_to_read(paths, args.to_read_key)
    items = zot.get_all(f"/collections/{folder['key']}/items/top",
                        {"sort": "dateAdded", "direction": "desc"}, limit=args.limit)
    total = zot.last_total

    def has_pdf(item):
        if not item.get("meta", {}).get("numChildren"):
            return False
        kids = zot.lib("GET", f"/items/{item['key']}/children").json()
        return any(k["data"].get("contentType") == "application/pdf" for k in kids)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pdf_flags = list(pool.map(has_pdf, items))
    listed = []
    for n, (it, pdf) in enumerate(zip(items, pdf_flags), 1):
        s = item_summary(it, paths, skip_collection=folder["key"])
        s["n"] = n
        s["has_pdf"] = pdf
        listed.append(s)
    return {"to_read": {"key": folder["key"], "path": folder["path"]},
            "total": total, "shown": len(listed), "items": listed}


def cmd_refile(zot, args):
    paths = collection_paths(zot.collections())
    add = resolve_folder_keys(split_keys(args.add), paths)
    remove = resolve_folder_keys(split_keys(args.remove), paths)
    if args.remove_to_read or args.add_to_read:
        to_read = find_to_read(paths, args.to_read_key)["key"]
        (remove if args.remove_to_read else add).append(to_read)
    item = zot.item(args.item)
    d = item["data"]
    if d.get("parentItem"):
        raise ToolError("That key is an attachment or note, not a paper. Use the paper's key.")
    before = list(d.get("collections", []))
    after = [k for k in before if k not in remove] + [k for k in add if k not in before and k not in remove]
    tags = list(d.get("tags", []))
    have = {t["tag"].lower() for t in tags}
    for t in split_keys(args.tag):
        if t.lower() not in have:
            tags.append({"tag": t})
    changes = {}
    if after != before:
        changes["collections"] = after
    if tags != d.get("tags", []):
        changes["tags"] = tags
    if changes:
        zot.patch_item(args.item, item["version"], changes)
    out = {"item": {"key": args.item, "title": d.get("title", "")},
           "folders_before": [paths[k]["path"] for k in before if k in paths],
           "folders_after": [paths[k]["path"] for k in after if k in paths],
           "tags_added": [t["tag"] for t in tags if t not in d.get("tags", [])],
           "changed": bool(changes)}
    if not after:
        out["note"] = "This paper is now in no folder (it shows under 'Unfiled Items')."
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="zotero_tool.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check the API key, access and the 'To read' folder")

    s = sub.add_parser("collections", help="list all folders with full paths and sample titles")
    s.add_argument("--samples", type=int, default=5, help="sample titles per folder (0 = none)")
    s.add_argument("--refresh", action="store_true", help="ignore the 1-hour cache")

    s = sub.add_parser("lookup", help="find paper details from a PDF path, DOI, link or title")
    s.add_argument("input")
    s.add_argument("--rows", type=int, default=3, help="number of title search results")

    s = sub.add_parser("fetch-pdf", help="try to download a free PDF (link, or Unpaywall by DOI)")
    s.add_argument("--doi")
    s.add_argument("--url")
    s.add_argument("--out-dir")

    s = sub.add_parser("find-dup", help="check whether a paper is already in the library")
    s.add_argument("--doi")
    s.add_argument("--title")

    s = sub.add_parser("add", help="add a paper to the library")
    s.add_argument("--meta", help="JSON file with paper details (output of lookup, or one candidate)")
    s.add_argument("--doi", help="look up this DOI and add it")
    s.add_argument("--collections", help="comma-separated folder keys")
    s.add_argument("--to-read", action="store_true", help="also put it in the 'To read' folder")
    s.add_argument("--to-read-key", help="key of the 'To read' folder, if the name is ambiguous")
    s.add_argument("--pdf", help="PDF file to attach")
    s.add_argument("--tags", help="comma-separated tags")
    s.add_argument("--allow-unfiled", action="store_true")

    s = sub.add_parser("new-collection", help="make a new folder")
    s.add_argument("--name", required=True)
    s.add_argument("--parent", help="key of the parent folder")

    s = sub.add_parser("to-read", help="list papers in the 'To read' folder (newest first)")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--to-read-key")

    s = sub.add_parser("refile", help="change the folders/tags of a paper already in Zotero")
    s.add_argument("--item", required=True, help="the paper's item key")
    s.add_argument("--add", help="comma-separated folder keys to add")
    s.add_argument("--remove", help="comma-separated folder keys to remove")
    s.add_argument("--add-to-read", action="store_true")
    s.add_argument("--remove-to-read", action="store_true")
    s.add_argument("--to-read-key")
    s.add_argument("--tag", help="comma-separated tags to add (e.g. read)")
    return p


COMMANDS = {
    "check": cmd_check, "collections": cmd_collections, "lookup": cmd_lookup,
    "fetch-pdf": cmd_fetch_pdf, "find-dup": cmd_find_dup, "add": cmd_add,
    "new-collection": cmd_new_collection, "to-read": cmd_to_read, "refile": cmd_refile,
}
NO_ZOTERO = {"lookup", "fetch-pdf"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        zot = None if args.command in NO_ZOTERO else connect()
        result = COMMANDS[args.command](zot, args)
    except ToolError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
