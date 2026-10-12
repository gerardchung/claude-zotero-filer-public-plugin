"""Offline tests for zotero_tool.py. No network: all HTTP goes to FakeHTTP."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude/skills/zotero-filer/scripts"))
import zotero_tool as zt  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, content=None, url=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.url = url
        if content is not None:
            self.content = content
        else:
            self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        if self._body is None:
            return json.loads(self.text)
        return self._body


class FakeHTTP:
    """Routes (METHOD, url-without-query) to a handler or a fixed response."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        base = url.split("?")[0]
        self.calls.append({"method": method, "url": base, "headers": headers or {}, **kw})
        handler = self.routes.get((method, base))
        if handler is None:
            raise AssertionError(f"Unexpected request: {method} {base}")
        resp = handler(kw) if callable(handler) else handler
        resp.url = resp.url or url
        return resp


LIB = "https://api.zotero.org/users/42"

COLLECTIONS = [
    {"key": "TOREAD1", "data": {"key": "TOREAD1", "name": "To read", "parentCollection": False},
     "meta": {"numItems": 2}},
    {"key": "PHD00001", "data": {"key": "PHD00001", "name": "PhD", "parentCollection": False},
     "meta": {"numItems": 0}},
    {"key": "CH2AAAAA", "data": {"key": "CH2AAAAA", "name": "Ch2", "parentCollection": "PHD00001"},
     "meta": {"numItems": 5}},
    {"key": "MEASURE1", "data": {"key": "MEASURE1", "name": "Measurement", "parentCollection": "CH2AAAAA"},
     "meta": {"numItems": 9}},
]

CROSSREF_MSG = {
    "type": "journal-article",
    "title": ["Loneliness in <i>older</i> adults"],
    "subtitle": ["A meta-analysis"],
    "author": [{"given": "Ann", "family": "Smith"}, {"given": "Bo", "family": "Lee"},
               {"name": "The Study Group"}],
    "container-title": ["Journal of Social Work"],
    "short-container-title": ["J Soc Work"],
    "volume": "12", "issue": "3", "page": "100-120",
    "issued": {"date-parts": [[2024, 3, 5]]},
    "DOI": "10.1234/jsw.2024.001",
    "abstract": "<jats:title>Abstract</jats:title><jats:p>We study  loneliness.</jats:p>",
    "ISSN": ["1234-5678"],
    "publisher": "SAGE",
}

JOURNAL_TEMPLATE = {
    "itemType": "journalArticle", "title": "", "creators": [{"creatorType": "author", "firstName": "", "lastName": ""}],
    "abstractNote": "", "publicationTitle": "", "volume": "", "issue": "", "pages": "", "date": "",
    "journalAbbreviation": "", "DOI": "", "ISSN": "", "url": "", "accessDate": "", "language": "",
    "extra": "", "tags": [], "collections": [], "relations": {},
}
PREPRINT_TEMPLATE = {
    "itemType": "preprint", "title": "", "creators": [], "abstractNote": "", "repository": "",
    "date": "", "url": "", "extra": "", "tags": [], "collections": [], "relations": {},
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOTERO_API_KEY", "secret-key")
    monkeypatch.setenv("ZOTERO_USER_ID", "42")
    monkeypatch.delenv("ZOTERO_GROUP_ID", raising=False)
    monkeypatch.delenv("ZOTERO_TO_READ", raising=False)
    monkeypatch.delenv("ZOTERO_FILER_EMAIL", raising=False)
    monkeypatch.setenv("ZOTERO_FILER_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(zt.time, "sleep", lambda s: None)


def install(monkeypatch, routes):
    fake = FakeHTTP(routes)
    monkeypatch.setattr(zt, "HTTP", fake)
    return fake


def run(capsys, *argv):
    code = zt.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- helpers

@pytest.mark.parametrize("raw,expected", [
    ("doi:10.1037/0003-066X.59.1.29.", "10.1037/0003-066X.59.1.29"),
    ("https://doi.org/10.1002/abc.123", "10.1002/abc.123"),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/abc.123", "10.1002/abc.123"),
    ("https://www.tandfonline.com/doi/abs/10.1080/1234.2020.1/full?src=x", "10.1080/1234.2020.1"),
    ("(see 10.5555/xyz)", "10.5555/xyz"),
    ("https://example.org/10.1000%2Fabc", "10.1000/abc"),
    ("no doi here", None),
])
def test_clean_doi(raw, expected):
    assert zt.clean_doi(raw) == expected


def test_detect_input(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert zt.detect_input(str(pdf)) == "pdf"
    assert zt.detect_input("10.1037/abc") == "doi"
    assert zt.detect_input("doi: 10.1037/abc") == "doi"
    assert zt.detect_input("https://journals.sagepub.com/doi/10.1/x") == "url"
    assert zt.detect_input("Loneliness in older adults") == "title"
    with pytest.raises(zt.ToolError, match="PDF not found"):
        zt.detect_input(str(tmp_path / "missing.pdf"))


def test_normalize_crossref():
    p = zt.normalize_csl(CROSSREF_MSG, "crossref")
    assert p["item_type"] == "journalArticle"
    assert p["title"] == "Loneliness in older adults: A meta-analysis"
    assert p["authors"] == [{"first": "Ann", "last": "Smith"}, {"first": "Bo", "last": "Lee"},
                            {"name": "The Study Group"}]
    assert p["date"] == "2024-03-05" and p["year"] == "2024"
    assert p["container"] == "Journal of Social Work"
    assert p["abstract"] == "We study loneliness."
    assert p["url"] == "https://doi.org/10.1234/jsw.2024.001"
    assert p["issn"] == "1234-5678"


def test_normalize_datacite_csl_preprint():
    csl = {"type": "article", "title": "Attention Is All You Need", "DOI": "10.48550/arXiv.1706.03762",
           "author": [{"given": "Ashish", "family": "Vaswani"}], "publisher": "arXiv",
           "issued": {"date-parts": [[2017]]}}
    p = zt.normalize_csl(csl, "doi.org")
    assert p["item_type"] == "preprint"
    assert p["date"] == "2017" and p["year"] == "2017"


def test_paper_to_zotero_journal():
    paper = zt.normalize_csl(CROSSREF_MSG, "crossref")
    item = zt.paper_to_zotero(paper, json.loads(json.dumps(JOURNAL_TEMPLATE)), ["MEASURE1"], ["new"])
    assert item["publicationTitle"] == "Journal of Social Work"
    assert item["journalAbbreviation"] == "J Soc Work"
    assert item["DOI"] == "10.1234/jsw.2024.001"
    assert item["creators"][0] == {"creatorType": "author", "firstName": "Ann", "lastName": "Smith"}
    assert item["creators"][2] == {"creatorType": "author", "name": "The Study Group"}
    assert item["collections"] == ["MEASURE1"]
    assert item["tags"] == [{"tag": "new"}]
    assert item["accessDate"] == ""  # only set for web pages


def test_paper_to_zotero_doi_goes_to_extra_when_no_field():
    paper = {"item_type": "preprint", "title": "T", "doi": "10.48550/arXiv.1", "container": "arXiv"}
    item = zt.paper_to_zotero(paper, dict(PREPRINT_TEMPLATE), [], [])
    assert item["repository"] == "arXiv"
    assert item["extra"] == "DOI: 10.48550/arXiv.1"


def test_collection_paths_and_to_read(monkeypatch):
    paths = zt.collection_paths(COLLECTIONS)
    assert paths["MEASURE1"]["path"] == "PhD > Ch2 > Measurement"
    assert paths["MEASURE1"]["num_items"] == 9
    assert zt.find_to_read(paths)["key"] == "TOREAD1"
    monkeypatch.setenv("ZOTERO_TO_READ", "reading list")
    with pytest.raises(zt.ToolError, match="No folder named 'reading list'"):
        zt.find_to_read(paths)


def test_find_to_read_with_sort_prefix():
    renamed = [dict(c, data=dict(c["data"], name="1_To read")) if c["key"] == "TOREAD1" else c
               for c in COLLECTIONS]
    assert zt.find_to_read(zt.collection_paths(renamed))["key"] == "TOREAD1"


def test_find_to_read_ambiguous(monkeypatch):
    monkeypatch.delenv("ZOTERO_TO_READ", raising=False)
    extra = {"key": "TOREAD2", "data": {"key": "TOREAD2", "name": "to read", "parentCollection": "PHD00001"}}
    paths = zt.collection_paths(COLLECTIONS + [extra])
    with pytest.raises(zt.ToolError, match="More than one folder"):
        zt.find_to_read(paths)
    assert zt.find_to_read(paths, "TOREAD2")["path"] == "PhD > to read"


def test_parse_page_meta_with_doi():
    page = """<html><head><title>Ignored</title>
      <meta name="citation_title" content="A Paper">
      <meta name="citation_author" content="Smith, Ann">
      <meta name="citation_doi" content="doi:10.1234/abc">
      <meta name="citation_pdf_url" content="https://pub.example/paper.pdf">
      <meta name="citation_journal_title" content="J X">
    </head></html>"""
    doi, pdf_url, paper = zt.parse_page_meta(page, "https://pub.example/a")
    assert doi == "10.1234/abc"
    assert pdf_url == "https://pub.example/paper.pdf"
    assert paper["item_type"] == "journalArticle"
    assert paper["authors"] == [{"first": "Ann", "last": "Smith"}]


def test_parse_page_meta_plain_webpage():
    page = """<html><head><title>My blog post</title>
      <meta property="og:site_name" content="Some Blog">
      <meta property="article:published_time" content="2025-01-02T10:00:00Z"></head></html>"""
    doi, pdf_url, paper = zt.parse_page_meta(page, "https://blog.example/post")
    assert doi is None and pdf_url == ""
    assert paper["item_type"] == "webpage"
    assert paper["title"] == "My blog post"
    assert paper["container"] == "Some Blog"
    assert paper["date"] == "2025-01-02"


def make_pdf(path, text):
    """Write a tiny one-page PDF containing `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def test_lookup_pdf_finds_doi(env, monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    make_pdf(pdf, "Loneliness in older adults: A meta-analysis. doi: 10.1234/jsw.2024.001")
    install(monkeypatch, {
        ("GET", "https://api.crossref.org/works/10.1234/jsw.2024.001"):
            FakeResponse(body={"message": CROSSREF_MSG}),
    })
    out = zt.lookup(str(pdf))
    assert out["input_type"] == "pdf"
    assert out["pdf_path"] == str(pdf.resolve())
    assert out["candidates"][0]["doi"] == "10.1234/jsw.2024.001"
    assert out["candidates"][0]["title_found_in_pdf"] is True


def test_lookup_doi_falls_back_to_doi_org(env, monkeypatch):
    install(monkeypatch, {
        ("GET", "https://api.crossref.org/works/10.48550/arXiv.1706.03762"): FakeResponse(404, content=b"Not found"),
        ("GET", "https://doi.org/10.48550/arXiv.1706.03762"):
            FakeResponse(body={"type": "article", "title": "Attention", "DOI": "10.48550/arXiv.1706.03762"}),
    })
    p = zt.lookup_doi("10.48550/arXiv.1706.03762")
    assert p["source"] == "doi.org" and p["item_type"] == "preprint"


def test_lookup_title_search(env, monkeypatch):
    other = dict(CROSSREF_MSG, title=["Something else entirely"], subtitle=[])
    install(monkeypatch, {
        ("GET", "https://api.crossref.org/works"):
            FakeResponse(body={"message": {"items": [CROSSREF_MSG, other]}}),
    })
    out = zt.lookup("Loneliness in older adults: a meta-analysis")
    assert out["input_type"] == "title"
    assert out["candidates"][0]["match"] == 1.0
    assert out["candidates"][1]["match"] < 0.6


# ---------------------------------------------------------------- Zotero commands

def zotero_routes(**extra):
    routes = {
        ("GET", f"{LIB}/collections"): FakeResponse(body=COLLECTIONS, headers={"Total-Results": "4"}),
    }
    routes.update(extra)
    return routes


def test_missing_key(env, monkeypatch, capsys):
    monkeypatch.delenv("ZOTERO_API_KEY")
    code, out = run(capsys, "to-read")
    assert code == 1 and "ZOTERO_API_KEY" in out["error"]


def test_collections_command_and_cache(env, monkeypatch, capsys):
    def items(kw):
        return FakeResponse(body=[{"key": "I1", "data": {"title": "Sample paper"}}])
    routes = zotero_routes()
    for c in COLLECTIONS:
        routes[("GET", f"{LIB}/collections/{c['key']}/items/top")] = items
    fake = install(monkeypatch, routes)
    code, out = run(capsys, "collections")
    assert code == 0
    assert out["to_read"] == {"key": "TOREAD1", "path": "To read"}
    paths = [c["path"] for c in out["collections"]]
    assert paths == ["PhD", "PhD > Ch2", "PhD > Ch2 > Measurement", "To read"]
    assert out["collections"][0]["sample_titles"] == ["Sample paper"]
    assert fake.calls[0]["headers"]["Zotero-API-Key"] == "secret-key"
    n = len(fake.calls)
    code, again = run(capsys, "collections")
    assert again == out and len(fake.calls) == n  # second run served from cache


def journal_item(key, title, collections, num_children=0, tags=()):
    return {"key": key, "version": 7, "meta": {"numChildren": num_children},
            "data": {"key": key, "itemType": "journalArticle", "title": title,
                     "creators": [{"creatorType": "author", "firstName": "A", "lastName": "Smith"},
                                  {"creatorType": "author", "firstName": "B", "lastName": "Lee"}],
                     "date": "2023-05-01", "publicationTitle": "J X", "dateAdded": "2025-09-01T10:00:00Z",
                     "collections": list(collections), "tags": [{"tag": t} for t in tags],
                     "abstractNote": "About measurement."}}


def test_to_read_lists_newest_first_with_pdf_flag(env, monkeypatch, capsys):
    items = [journal_item("AAA", "Paper A", ["TOREAD1", "MEASURE1"], num_children=1),
             journal_item("BBB", "Paper B", ["TOREAD1"])]
    fake = install(monkeypatch, zotero_routes() | {
        ("GET", f"{LIB}/collections/TOREAD1/items/top"):
            FakeResponse(body=items, headers={"Total-Results": "2"}),
        ("GET", f"{LIB}/items/AAA/children"):
            FakeResponse(body=[{"data": {"itemType": "attachment", "contentType": "application/pdf"}}]),
    })
    code, out = run(capsys, "to-read")
    assert code == 0
    assert out["total"] == 2 and out["shown"] == 2
    a, b = out["items"]
    assert a["n"] == 1 and a["title"] == "Paper A" and a["has_pdf"] is True
    assert a["folders"] == ["PhD > Ch2 > Measurement"]  # 'To read' itself is not repeated
    assert a["authors"] == "Smith & Lee" and a["year"] == "2023"
    assert b["has_pdf"] is False
    sort_call = next(c for c in fake.calls if c["url"].endswith("/TOREAD1/items/top"))
    assert sort_call["params"]["sort"] == "dateAdded" and sort_call["params"]["direction"] == "desc"


def test_refile_after_reading(env, monkeypatch, capsys):
    patched = {}

    def patch(kw):
        patched.update(kw)
        return FakeResponse(204, content=b"")
    fake = install(monkeypatch, zotero_routes() | {
        ("GET", f"{LIB}/items/AAA"): FakeResponse(body=journal_item("AAA", "Paper A", ["TOREAD1"])),
        ("PATCH", f"{LIB}/items/AAA"): patch,
    })
    code, out = run(capsys, "refile", "--item", "AAA", "--add", "MEASURE1,CH2AAAAA",
                    "--remove-to-read", "--tag", "read")
    assert code == 0, out
    assert patched["json"] == {"collections": ["MEASURE1", "CH2AAAAA"], "tags": [{"tag": "read"}]}
    patch_call = next(c for c in fake.calls if c["method"] == "PATCH")
    assert patch_call["headers"]["If-Unmodified-Since-Version"] == "7"
    assert out["folders_before"] == ["To read"]
    assert out["folders_after"] == ["PhD > Ch2 > Measurement", "PhD > Ch2"]
    assert out["tags_added"] == ["read"]


def test_refile_unknown_folder(env, monkeypatch, capsys):
    install(monkeypatch, zotero_routes())
    code, out = run(capsys, "refile", "--item", "AAA", "--add", "NOPE")
    assert code == 1 and "No folder with key NOPE" in out["error"]


def test_add_with_pdf_upload(env, monkeypatch, capsys, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"candidates": [zt.normalize_csl(CROSSREF_MSG, "crossref")]}))
    created = []

    def template(kw):
        t = kw["params"]["itemType"]
        if t == "attachment":
            return FakeResponse(body={"itemType": "attachment", "linkMode": "imported_file", "title": "",
                                      "parentItem": "", "contentType": "", "filename": "", "md5": None,
                                      "mtime": None, "tags": [], "relations": {}})
        return FakeResponse(body=json.loads(json.dumps(JOURNAL_TEMPLATE)))

    def create(kw):
        created.append(kw["json"][0])
        key = "NEWITEM" if len(created) == 1 else "NEWATT"
        return FakeResponse(body={"successful": {"0": {"key": key}}, "success": {"0": key}, "failed": {}})

    file_steps = []

    def file_endpoint(kw):
        file_steps.append(kw["data"])
        if "upload" in kw["data"]:
            return FakeResponse(204, content=b"")
        return FakeResponse(body={"url": "https://zoterofilestorage.s3.amazonaws.com/", "contentType": "multipart/x",
                                  "prefix": "PRE", "suffix": "SUF", "uploadKey": "UPK"})
    s3 = []
    fake = install(monkeypatch, zotero_routes() | {
        ("GET", "https://api.zotero.org/items/new"): template,
        ("POST", f"{LIB}/items"): create,
        ("POST", f"{LIB}/items/NEWATT/file"): file_endpoint,
        ("POST", "https://zoterofilestorage.s3.amazonaws.com/"):
            lambda kw: s3.append(kw) or FakeResponse(201, content=b""),
    })
    code, out = run(capsys, "add", "--meta", str(meta), "--collections", "MEASURE1", "--to-read",
                    "--pdf", str(pdf))
    assert code == 0, out
    item, att = created
    assert item["title"] == "Loneliness in older adults: A meta-analysis"
    assert item["collections"] == ["MEASURE1", "TOREAD1"]
    assert att["parentItem"] == "NEWITEM" and att["filename"] == "paper.pdf"
    assert "md5" not in att and "mtime" not in att
    assert file_steps[0]["md5"] and file_steps[0]["filesize"] == len(b"%PDF-1.4 fake")
    assert file_steps[1] == {"upload": "UPK"}
    assert s3[0]["data"] == b"PRE%PDF-1.4 fakeSUF"
    s3_call = next(c for c in fake.calls if "s3.amazonaws" in c["url"])
    assert "Zotero-API-Key" not in s3_call["headers"]  # key never sent to storage server
    assert out["added"]["folders"] == ["PhD > Ch2 > Measurement", "To read"]
    assert out["added"]["pdf"] == "attached"
    assert out["added"]["open_in_zotero"] == "zotero://select/library/items/NEWITEM"


def test_add_requires_a_folder(env, monkeypatch, capsys, tmp_path):
    meta = tmp_path / "m.json"
    meta.write_text(json.dumps({"title": "T"}))
    install(monkeypatch, zotero_routes())
    code, out = run(capsys, "add", "--meta", str(meta))
    assert code == 1 and "No folder chosen" in out["error"]


def test_add_keeps_item_when_pdf_upload_fails(env, monkeypatch, capsys, tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF")
    meta = tmp_path / "m.json"
    meta.write_text(json.dumps({"title": "T", "item_type": "journalArticle"}))
    calls = {"n": 0}

    def create(kw):
        calls["n"] += 1
        key = "NEWITEM" if calls["n"] == 1 else "NEWATT"
        return FakeResponse(body={"successful": {"0": {"key": key}}, "failed": {}})
    install(monkeypatch, zotero_routes() | {
        ("GET", "https://api.zotero.org/items/new"): lambda kw: FakeResponse(body=dict(JOURNAL_TEMPLATE)),
        ("POST", f"{LIB}/items"): create,
        ("POST", f"{LIB}/items/NEWATT/file"): FakeResponse(413, content=b"File would exceed quota"),
    })
    code, out = run(capsys, "add", "--meta", str(meta), "--to-read", "--pdf", str(pdf))
    assert code == 0
    assert out["added"]["key"] == "NEWITEM" and out["added"]["pdf"] == "failed"
    assert "storage is full" in out["pdf_error"]


def test_find_dup_by_doi_and_title(env, monkeypatch, capsys):
    hit = journal_item("DUP", "Loneliness in older adults: A meta-analysis", ["MEASURE1"])
    hit["data"]["DOI"] = "10.1234/JSW.2024.001"
    near_miss = journal_item("NOT", "Loneliness in teenagers", [])

    def search(kw):
        return FakeResponse(body=[hit, near_miss], headers={"Total-Results": "2"})
    install(monkeypatch, zotero_routes() | {("GET", f"{LIB}/items/top"): search})
    code, out = run(capsys, "find-dup", "--doi", "10.1234/jsw.2024.001",
                    "--title", "Loneliness in older adults - a meta-analysis")
    assert code == 0
    assert [d["key"] for d in out["duplicates"]] == ["DUP"]
    assert out["duplicates"][0]["folders"] == ["PhD > Ch2 > Measurement"]


def test_new_collection_reuses_existing(env, monkeypatch, capsys):
    install(monkeypatch, zotero_routes())
    code, out = run(capsys, "new-collection", "--name", "measurement", "--parent", "CH2AAAAA")
    assert code == 0 and out["already_existed"] is True and out["collection"]["key"] == "MEASURE1"


def test_new_collection_creates(env, monkeypatch, capsys):
    sent = []
    install(monkeypatch, zotero_routes() | {
        ("POST", f"{LIB}/collections"):
            lambda kw: sent.append(kw["json"]) or FakeResponse(body={"successful": {"0": {"key": "NEWCOL"}}, "failed": {}}),
    })
    code, out = run(capsys, "new-collection", "--name", "Interventions", "--parent", "CH2AAAAA")
    assert code == 0 and out["collection"]["path"] == "PhD > Ch2 > Interventions"
    assert sent[0] == [{"name": "Interventions", "parentCollection": "CH2AAAAA"}]


def test_check_reports_missing_write_access(env, monkeypatch, capsys):
    install(monkeypatch, zotero_routes() | {
        ("GET", "https://api.zotero.org/keys/current"):
            FakeResponse(body={"userID": 42, "username": "gc", "access": {"user": {"library": True, "files": True}}}),
    })
    code, out = run(capsys, "check")
    assert code == 0 and out["ok"] is False
    assert any("write" in p for p in out["problems"])
    assert out["to_read_folder"] == "To read"


def test_forbidden_error_is_friendly(env, monkeypatch, capsys):
    install(monkeypatch, {("GET", f"{LIB}/collections"): FakeResponse(403, content=b"Forbidden")})
    code, out = run(capsys, "to-read")
    assert code == 1 and "Allow write access" in out["error"]
