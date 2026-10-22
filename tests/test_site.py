import html.parser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")
PAGES = ["index.html", "displays.html", "controller.html", "cloud.html", "audio.html", "contact.html"]


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if tag == "a" and k == "href":
                self.links.append(v)
            elif tag == "link" and k == "href":
                self.links.append(v)
            elif tag in ("img", "script") and k == "src":
                self.links.append(v)


def _page(name):
    with open(os.path.join(SITE, name), encoding="utf-8") as f:
        return f.read()


def test_all_pages_exist_with_shared_chrome():
    for name in PAGES:
        s = _page(name)
        assert "<title>" in s, name
        assert "assets/site.css" in s and "assets/site.js" in s, name
        for target in ("displays.html", "controller.html", "cloud.html", "audio.html", "contact.html"):
            assert f'href="{target}"' in s, (name, target)
        assert "/cloud/static/" in s, name  # cloud login stays on the portal
        assert "hello@nsldisplays.com" in s, name


def test_no_external_dependencies():
    for name in PAGES:
        s = _page(name)
        p = _Links()
        p.feed(s)
        for v in p.links:
            if not v or v.startswith(("mailto:", "data:", "#")):
                continue
            assert not v.startswith(("http://", "https://", "//")), (name, v)
            if v.startswith("/cloud/"):
                continue  # served by the portal proxy, not the static site
            assert not v.startswith("/"), (name, v)
            target = os.path.normpath(os.path.join(SITE, v))
            assert os.path.isfile(target), (name, v)


def test_quote_form_composes_email_only():
    s = _page("contact.html")
    assert 'data-quote="hello@nsldisplays.com"' in s
    assert "<form" in s and 'action=' not in s.split("<form")[1].split(">")[0]


def test_caddy_serves_site_and_cloud():
    with open(os.path.join(os.path.dirname(SITE), "deploy", "config", "Caddyfile")) as f:
        c = f.read()
    assert "handle /cloud/*" in c and "strip_prefix /cloud" in c
    assert "reverse_proxy portal:8000" in c
    assert "root * /srv/site" in c
    assert "handle /portal/*" in c  # Pi agents + bookmarks keep working
    assert "respond \"nsl portal: ok\"" not in c


def test_compose_mounts_site():
    with open(os.path.join(os.path.dirname(SITE), "deploy", "docker-compose.yml")) as f:
        c = f.read()
    assert "site:/srv/site:ro" in c


def test_portal_prefix_covers_cloud():
    root = os.path.dirname(SITE)
    for rel in ("portal/frontend/app.js", "portal/frontend/controller.html",
                "portal/frontend/next-stop.html", "portal/frontend/fontedit.js"):
        with open(os.path.join(root, rel)) as f:
            s = f.read()
        assert 'startsWith("/cloud")' in s, rel
        assert '"/portal"' in s, rel
