"""
Broken Link Checker - Desktop App
==================================
Double-click or run: python broken_link_checker.py
Opens automatically in your browser.

Install once:  pip install flask requests beautifulsoup4
"""

import time, json, urllib3, webbrowser, threading
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
import requests
from bs4 import BeautifulSoup
from flask import Flask, request as req, jsonify, Response, stream_with_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

TIMEOUT = 15
MAX_WORKERS = 10

# Use a session for cookie persistence and connection pooling (more browser-like)
session = requests.Session()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
session.headers.update(HEADERS)

def normalize_url(url):
    p = urlparse(url); return p._replace(fragment="").geturl().rstrip("/")

def extract_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser"); links = []; seen = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("#","javascript:","mailto:","tel:")): continue
        absolute = urljoin(base_url, href); parsed = urlparse(absolute)
        if parsed.scheme not in ("http","https"): continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean); links.append({"url": clean, "link_text": (tag.get_text(strip=True) or "")[:200]})
    return links

def is_same_domain(url, base): return urlparse(url).netloc == urlparse(base).netloc

def is_crawlable(url):
    path = urlparse(url).path.lower()
    skip = ('.pdf','.png','.jpg','.jpeg','.gif','.svg','.css','.js','.zip','.xml','.json','.ico','.woff','.woff2','.ttf','.mp4','.mp3','.webp')
    return not any(path.endswith(e) for e in skip)

def check_one_link(url):
    """Check a single link with HEAD first, falling back to GET on any error status."""
    start = time.time()
    def try_req(verify=True):
        try:
            # Try HEAD first (faster, less bandwidth)
            r = session.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=verify)
            elapsed = round(time.time() - start, 2)
            # Many servers reject HEAD or return wrong status — fall back to GET
            if r.status_code >= 400 or r.status_code in (405, 403, 400):
                r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=verify); r.close()
                elapsed = round(time.time() - start, 2)
            st = "ok"
            if r.status_code >= 400: st = "broken"
            elif r.history: st = "redirect"
            return {"status":st,"code":r.status_code,"final_url":r.url if r.history else "","time":elapsed,"note":""}
        except requests.exceptions.SSLError: return None
        except requests.exceptions.Timeout:
            return {"status":"timeout","code":None,"final_url":"","time":round(time.time()-start,2),"note":"Timed out"}
        except requests.exceptions.ConnectionError as e:
            es = str(e).lower()
            if "proxy" in es or "tunnel" in es or "certificate" in es:
                return {"status":"blocked","code":None,"final_url":"","time":round(time.time()-start,2),"note":"Blocked by proxy/firewall"}
            return {"status":"broken","code":None,"final_url":"","time":round(time.time()-start,2),"note":"Connection failed"}
        except Exception as e:
            return {"status":"broken","code":None,"final_url":"","time":round(time.time()-start,2),"note":str(e)[:100]}
    result = try_req(True)
    if result is None: result = try_req(False)
    if result is None: result = {"status":"blocked","code":None,"final_url":"","time":round(time.time()-start,2),"note":"SSL/proxy issue"}
    return result

def fetch_page(url):
    """Fetch a page with browser-like headers."""
    try:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        hdrs = dict(HEADERS)
        hdrs["Referer"] = referer
        r = session.get(url, headers=hdrs, timeout=TIMEOUT, verify=False, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "text/html" in ct:
            return r.text
        # Log failures for debugging
        print(f"  [fetch] {url} -> {r.status_code} ({ct[:40]})")
    except Exception as e:
        print(f"  [fetch] {url} -> ERROR: {e}")
    return None

def is_within_library(url, base_url):
    """Check if URL is within the same documentation library path prefix."""
    base_path = urlparse(base_url).path.rstrip("/")
    url_path = urlparse(url).path.rstrip("/")
    return url_path.startswith(base_path) and is_same_domain(url, base_url)

def parse_toc_list(ul_tag, base_url):
    """Recursively parse a <ul> navigation list into a nested TOC tree."""
    items = []
    for li in ul_tag.find_all("li", recursive=False):
        a = li.find("a", href=True, recursive=False)
        if not a:
            a = li.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        clean = parsed._replace(fragment="").geturl()
        text = a.get_text(strip=True)[:200]
        children = []
        sub_ul = li.find("ul", recursive=False)
        if sub_ul:
            children = parse_toc_list(sub_ul, base_url)
        items.append({"url": clean, "text": text, "children": children})
    return items

def fetch_page_urllib(url):
    """Fallback fetcher using urllib (different TLS fingerprint than requests)."""
    import urllib.request, ssl
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        rq = urllib.request.Request(url, headers=HEADERS)
        parsed = urlparse(url)
        rq.add_header("Referer", f"{parsed.scheme}://{parsed.netloc}/")
        with urllib.request.urlopen(rq, timeout=TIMEOUT, context=ctx) as resp:
            ct = resp.headers.get("Content-Type", "")
            if resp.status == 200 and "text/html" in ct:
                data = resp.read()
                # Try to detect encoding
                enc = "utf-8"
                if "charset=" in ct:
                    enc = ct.split("charset=")[-1].split(";")[0].strip()
                return data.decode(enc, errors="replace")
    except Exception as e:
        print(f"  [urllib] {url} -> ERROR: {e}")
    return None

SMARTBEAR_PRODUCTS = [
    {"url": "https://support.smartbear.com/testcomplete/docs/", "text": "TestComplete"},
    {"url": "https://support.smartbear.com/readyapi/docs/", "text": "ReadyAPI"},
    {"url": "https://support.smartbear.com/swagger/docs/", "text": "Swagger"},
    {"url": "https://support.smartbear.com/swagger/portal/docs/", "text": "Swagger Portal"},
    {"url": "https://support.smartbear.com/swagger/contract-testing/docs/", "text": "Swagger Contract Testing"},
    {"url": "https://support.smartbear.com/collaborator/docs/", "text": "Collaborator"},
    {"url": "https://support.smartbear.com/reflect/docs/", "text": "Reflect"},
    {"url": "https://support.smartbear.com/bitbar/docs/", "text": "BitBar"},
    {"url": "https://support.smartbear.com/bugsnag/docs/", "text": "BugSnag"},
    {"url": "https://support.smartbear.com/zephyr-enterprise/docs/", "text": "Zephyr Enterprise"},
    {"url": "https://support.smartbear.com/zephyr-essential-dc/docs/", "text": "Zephyr Essential DC"},
    {"url": "https://support.smartbear.com/zephyr-scale-cloud/docs/", "text": "Zephyr Scale Cloud"},
    {"url": "https://support.smartbear.com/zephyr-scale-server/docs/", "text": "Zephyr Scale Server"},
    {"url": "https://support.smartbear.com/zephyr-squad-cloud/docs/", "text": "Zephyr Squad Cloud"},
    {"url": "https://support.smartbear.com/zephyr-squad-server/docs/", "text": "Zephyr Squad Server"},
    {"url": "https://support.smartbear.com/qmetry-test-management-for-jira-cloud/docs/", "text": "QMetry TM for Jira Cloud"},
    {"url": "https://support.smartbear.com/qmetry-test-management-for-jira-dc/docs/", "text": "QMetry TM for Jira DC"},
    {"url": "https://support.smartbear.com/administration/docs/", "text": "Administration"},
    {"url": "https://support.smartbear.com/testexecute/docs/", "text": "TestExecute"},
    {"url": "https://support.smartbear.com/visualtest/docs/", "text": "VisualTest"},
    {"url": "https://support.smartbear.com/crossbrowsertesting/docs/", "text": "CrossBrowserTesting"},
    {"url": "https://support.smartbear.com/alertsite/docs/", "text": "AlertSite"},
]

@app.route("/api/fetch-toc", methods=["POST"])
def fetch_toc():
    """Fetch the documentation page, find its navigation/TOC, and return a tree."""
    data = req.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Special case: SmartBear support homepage (JS-rendered, can't be scraped)
    clean = url.rstrip("/").lower()
    if clean in ("https://support.smartbear.com", "http://support.smartbear.com"):
        return jsonify({"toc": [{"url": p["url"], "text": p["text"], "children": []} for p in SMARTBEAR_PRODUCTS], "base_url": url})

    # For any support.smartbear.com product URL, check if it matches a known product
    # and offer it directly if we can't scrape it
    is_smartbear = "support.smartbear.com" in url.lower()

    # Try multiple URL patterns — landing pages sometimes block bots but sub-pages don't
    urls_to_try = [url]
    base = url.rstrip("/")
    for suffix in ["/en/index-en.html", "/en/", "/index.html"]:
        candidate = base + suffix
        if candidate not in urls_to_try:
            urls_to_try.append(candidate)

    # Collect HTML from all pages we can reach
    pages_html = {}  # url -> html
    for try_url in urls_to_try:
        html = fetch_page(try_url)
        if html:
            # Check if it's a JS-only page (very short or contains "enable JavaScript")
            if len(html) < 1000 and "javascript" in html.lower():
                print(f"  [toc] {try_url} is JS-rendered, skipping")
                continue
            pages_html[try_url] = html
    if not pages_html:
        print("  [toc] Trying urllib fallback...")
        for try_url in urls_to_try:
            html = fetch_page_urllib(try_url)
            if html and not (len(html) < 1000 and "javascript" in html.lower()):
                pages_html[try_url] = html

    if not pages_html:
        # If this is a SmartBear URL, fall back to the product catalog
        if is_smartbear:
            # Try to find the matching product or return all products
            matching = [p for p in SMARTBEAR_PRODUCTS if p["url"].rstrip("/") in url or url.rstrip("/") in p["url"]]
            if matching:
                return jsonify({"toc": [{"url": p["url"], "text": p["text"], "children": []} for p in matching], "base_url": url})
            return jsonify({"toc": [{"url": p["url"], "text": p["text"], "children": []} for p in SMARTBEAR_PRODUCTS], "base_url": url})
        return jsonify({"error": "Could not fetch any page. The server may be blocking automated requests."}), 400

    base_path = urlparse(url).path.rstrip("/")
    toc_tree = []

    # Try each fetched page to find navigation
    for used_url, html in pages_html.items():
        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: Look for sidebar nav with nested <ul> tree
        for selector in ["nav", ".toc", ".sidebar", ".side-nav", ".menu", "#toc",
                          '[role="navigation"]', ".leftnav", ".left-nav", ".docs-nav"]:
            nav = soup.select_one(selector)
            if nav:
                ul = nav.find("ul")
                if ul:
                    toc_tree = parse_toc_list(ul, used_url)
                    if toc_tree:
                        break
        if toc_tree:
            break

    # Strategy 2: Collect ALL in-library links from ALL fetched pages
    if not toc_tree:
        seen = set()
        all_items = []
        for used_url, html in pages_html.items():
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                absolute = urljoin(used_url, href)
                parsed = urlparse(absolute)
                if parsed.scheme not in ("http", "https"):
                    continue
                clean = parsed._replace(fragment="").geturl()
                clean_path = parsed.path.rstrip("/")
                if (is_same_domain(clean, url) and
                        clean_path.startswith(base_path) and
                        clean_path != base_path and
                        clean not in seen):
                    seen.add(clean)
                    text = a.get_text(strip=True)[:200]
                    if text and len(text) > 1:
                        all_items.append({"url": clean, "text": text, "children": []})
        if all_items:
            toc_tree = all_items

    # Strategy 3: Fetch a sub-page to find its full sidebar nav
    if not toc_tree:
        for used_url, html in pages_html.items():
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                absolute = urljoin(used_url, href)
                parsed = urlparse(absolute)
                if parsed.scheme not in ("http", "https"):
                    continue
                clean = parsed._replace(fragment="").geturl()
                if (is_same_domain(clean, url) and
                        parsed.path.rstrip("/").startswith(base_path) and
                        parsed.path.rstrip("/") != base_path):
                    sub_html = fetch_page(clean)
                    if sub_html:
                        sub_soup = BeautifulSoup(sub_html, "html.parser")
                        for sel in ["nav", ".toc", ".sidebar", ".side-nav", ".menu",
                                    '[role="navigation"]', ".leftnav", ".left-nav", ".docs-nav"]:
                            nav = sub_soup.select_one(sel)
                            if nav:
                                ul = nav.find("ul")
                                if ul:
                                    toc_tree = parse_toc_list(ul, clean)
                                    if toc_tree:
                                        break
                        # Also try: collect all links from this sub-page
                        if not toc_tree:
                            seen2 = set()
                            for a2 in sub_soup.find_all("a", href=True):
                                href2 = a2["href"].strip()
                                if href2.startswith(("#", "javascript:", "mailto:", "tel:")):
                                    continue
                                abs2 = urljoin(clean, href2)
                                p2 = urlparse(abs2)
                                if p2.scheme not in ("http", "https"):
                                    continue
                                c2 = p2._replace(fragment="").geturl()
                                cp2 = p2.path.rstrip("/")
                                if (is_same_domain(c2, url) and cp2.startswith(base_path) and
                                        cp2 != base_path and c2 not in seen2):
                                    seen2.add(c2)
                                    t2 = a2.get_text(strip=True)[:200]
                                    if t2 and len(t2) > 1:
                                        toc_tree.append({"url": c2, "text": t2, "children": []})
                        if toc_tree:
                            break
            if toc_tree:
                break

    if not toc_tree:
        # Last resort: if SmartBear URL, fall back to product catalog
        if is_smartbear:
            matching = [p for p in SMARTBEAR_PRODUCTS if p["url"].rstrip("/") in url or url.rstrip("/") in p["url"]]
            if matching:
                toc_tree = [{"url": p["url"], "text": p["text"], "children": []} for p in matching]
            else:
                toc_tree = [{"url": p["url"], "text": p["text"], "children": []} for p in SMARTBEAR_PRODUCTS]
        else:
            sample_len = len(list(pages_html.values())[0]) if pages_html else 0
            print(f"  [toc] No TOC found. Pages fetched: {list(pages_html.keys())}. HTML length: {sample_len}")
            return jsonify({"error": "Could not find a table of contents. The page may use JavaScript rendering."}), 400

    # Deduplicate by URL
    seen_urls = set()
    deduped = []
    for item in toc_tree:
        nu = normalize_url(item["url"])
        if nu not in seen_urls:
            seen_urls.add(nu)
            deduped.append(item)
    toc_tree = deduped

    # For top-level topics without children, try fetching their page for a sidebar
    for item in toc_tree:
        if not item["children"] and is_within_library(item["url"], url):
            sub_html = fetch_page(item["url"])
            if sub_html:
                sub_soup = BeautifulSoup(sub_html, "html.parser")
                for selector in ["nav", ".toc", ".sidebar", ".side-nav", ".menu",
                                  '[role="navigation"]', ".leftnav", ".left-nav", ".docs-nav"]:
                    nav = sub_soup.select_one(selector)
                    if nav:
                        ul = nav.find("ul")
                        if ul:
                            full_tree = parse_toc_list(ul, item["url"])
                            if full_tree:
                                item_norm = normalize_url(item["url"])
                                for node in full_tree:
                                    if normalize_url(node["url"]) == item_norm and node["children"]:
                                        item["children"] = node["children"]
                                        break
                                break

    return jsonify({"toc": toc_tree, "base_url": url})

@app.route("/api/crawl-site", methods=["POST"])
def crawl_site():
    data = req.get_json(force=True)
    start_url = data.get("url","").strip()
    topic_urls = data.get("topic_urls", [])
    if not start_url:
        return jsonify({"error":"URL required"}), 400
    if not start_url.startswith(("http://","https://")):
        start_url = "https://" + start_url

    allowed_prefixes = []
    if topic_urls:
        for tu in topic_urls:
            p = urlparse(tu).path.rstrip("/")
            if p:
                allowed_prefixes.append(p)

    def url_in_scope(u):
        if not allowed_prefixes:
            return is_within_library(u, start_url)
        up = urlparse(u).path.rstrip("/")
        return is_same_domain(u, start_url) and any(up.startswith(pfx) for pfx in allowed_prefixes)

    def gen():
        seeds = topic_urls if topic_urls else [start_url]
        queue = deque([(u, 0) for u in seeds])
        crawled = set(); all_links = {}
        topic_names = ", ".join([urlparse(u).path.split("/")[-1].replace(".html","").replace("-"," ").title() for u in seeds[:3]])
        if len(seeds) > 3:
            topic_names += f" +{len(seeds)-3} more"
        yield f"data: {json.dumps({'type':'status','message':f'Crawling selected topics: {topic_names}...'})}\n\n"

        while queue:
            page_url, depth = queue.popleft()
            pn = normalize_url(page_url)
            if pn in crawled or not is_crawlable(page_url):
                continue
            crawled.add(pn)
            yield f"data: {json.dumps({'type':'crawling','page':page_url,'depth':depth,'pages_crawled':len(crawled)})}\n\n"
            html = fetch_page(page_url)
            if not html:
                continue
            for lk in extract_links(html, page_url):
                ln = normalize_url(lk["url"])
                if ln not in all_links:
                    all_links[ln] = {"url":lk["url"],"link_text":lk["link_text"],"source_pages":[page_url]}
                else:
                    if page_url not in all_links[ln]["source_pages"]:
                        all_links[ln]["source_pages"].append(page_url)
                    if lk["link_text"] and not all_links[ln]["link_text"]:
                        all_links[ln]["link_text"] = lk["link_text"]
                if url_in_scope(lk["url"]):
                    cn = normalize_url(lk["url"])
                    if cn not in crawled:
                        queue.append((lk["url"], depth+1))

        total = len(all_links)
        yield f"data: {json.dumps({'type':'crawl_done','pages_crawled':len(crawled),'total_links':total})}\n\n"
        link_list = [{"url":v["url"],"link_text":v["link_text"],"source_pages":v["source_pages"]} for v in all_links.values()]
        yield f"data: {json.dumps({'type':'init','total':total,'links':link_list})}\n\n"
        if not total:
            yield f"data: {json.dumps({'type':'done','total':0,'pages_crawled':len(crawled)})}\n\n"
            return
        done = 0; items = list(all_links.items())
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check_one_link, info["url"]):(n,info) for n,info in items}
            for f in as_completed(futs):
                n,info = futs[f]; r = f.result(); done += 1
                r["url"]=info["url"];r["type"]="result";r["done"]=done
                r["link_text"]=info["link_text"];r["source_pages"]=info["source_pages"]
                yield f"data: {json.dumps(r)}\n\n"
        yield f"data: {json.dumps({'type':'done','total':total,'pages_crawled':len(crawled)})}\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Access-Control-Allow-Origin":"*"})

@app.route("/api/check-url", methods=["POST"])
def check_url():
    data = req.get_json(force=True); url = data.get("url","").strip()
    if not url: return jsonify({"error":"URL required"}),400
    if not url.startswith(("http://","https://")): url = "https://" + url
    def gen():
        try: resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False); resp.raise_for_status()
        except Exception as e: yield f"data: {json.dumps({'type':'error','message':str(e)[:200]})}\n\n"; return
        links = extract_links(resp.text, url)
        yield f"data: {json.dumps({'type':'init','total':len(links),'links':links})}\n\n"
        if not links: yield f"data: {json.dumps({'type':'done','total':0})}\n\n"; return
        meta = {l["url"]:l for l in links}; done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check_one_link, l["url"]):l["url"] for l in links}
            for f in as_completed(futs):
                r = f.result(); u = futs[f]; done += 1
                r["url"]=u;r["type"]="result";r["done"]=done
                m = meta.get(u,{}); r["link_text"]=m.get("link_text",""); r["source_pages"]=[url]
                yield f"data: {json.dumps(r)}\n\n"
        yield f"data: {json.dumps({'type':'done','total':len(links)})}\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","Access-Control-Allow-Origin":"*"})

@app.route("/api/check-links", methods=["POST"])
def check_links():
    data = req.get_json(force=True); urls = data.get("urls",[])
    if not urls: return jsonify({"error":"urls required"}),400
    def gen():
        yield f"data: {json.dumps({'type':'init','total':len(urls),'links':urls})}\n\n"
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check_one_link, u):u for u in urls}
            for f in as_completed(futs):
                r = f.result(); u = futs[f]; done += 1
                r["url"]=u;r["type"]="result";r["done"]=done
                yield f"data: {json.dumps(r)}\n\n"
        yield f"data: {json.dumps({'type':'done','total':len(urls)})}\n\n"
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","Access-Control-Allow-Origin":"*"})

@app.route("/api/health")
def health(): return jsonify({"status":"ok"})

@app.route("/")
def index(): return HTML_PAGE

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Broken Link Checker</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#f7f7f5;color:#1e293b;font-family:'DM Mono','Fira Code',monospace;padding:0;overflow:hidden}
::placeholder{color:#94a3b8}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}

.app-layout{display:flex;height:100vh}

.sidebar{width:330px;min-width:330px;background:#ffffff;border-right:1px solid #e2e8f0;
  display:flex;flex-direction:column;overflow:hidden}
.sidebar-header{padding:20px 20px 0;flex-shrink:0}
.sidebar-header h1{font-family:'Outfit',sans-serif;font-size:22px;font-weight:700;letter-spacing:-0.03em;color:#0f172a}
.sidebar-header .subtitle{color:#64748b;font-size:11px;margin-top:4px;font-family:'Outfit',sans-serif}
.sidebar-input{padding:16px 20px 0;flex-shrink:0}
.sidebar-input .input-wrap{border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;padding:3px}
.sidebar-input .input-wrap input{width:100%;background:transparent;border:none;outline:none;color:#1e293b;font-size:12px;padding:9px 10px;font-family:inherit}
.sidebar-input .load-btn{width:100%;margin-top:8px;border-radius:7px;padding:8px;cursor:pointer;font-family:inherit;font-size:11px;
  font-weight:500;border:1px solid #4f46e5;background:#4f46e5;color:#fff;transition:all .15s}
.sidebar-input .load-btn:hover:not(:disabled){background:#4338ca}
.sidebar-input .load-btn:disabled{opacity:.5;cursor:default}

.toc-area{flex:1;overflow-y:auto;padding:12px 12px 20px;margin-top:12px}
.toc-area::-webkit-scrollbar{width:4px}
.toc-area::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:2px}
.toc-empty{color:#94a3b8;font-size:11px;padding:20px 8px;text-align:center;line-height:1.7}
.toc-toolbar{display:flex;gap:6px;margin-bottom:10px;padding:0 4px}
.toc-toolbar button{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:5px;
  padding:4px 10px;cursor:pointer;font-family:inherit;font-size:10px;color:#475569;transition:all .15s}
.toc-toolbar button:hover{background:#e2e8f0;color:#1e293b}
.toc-node{padding-left:0}
.toc-item{display:flex;align-items:flex-start;gap:6px;padding:4px 6px;border-radius:5px;cursor:pointer;transition:background .1s}
.toc-item:hover{background:#f1f5f9}
.toc-item input[type=checkbox]{margin-top:3px;accent-color:#4f46e5;cursor:pointer;flex-shrink:0}
.toc-item .arrow{width:14px;height:14px;margin-top:2px;flex-shrink:0;color:#94a3b8;font-size:9px;
  display:flex;align-items:center;justify-content:center;transition:transform .15s;cursor:pointer;user-select:none}
.toc-item .arrow.open{transform:rotate(90deg)}
.toc-item .arrow.empty{visibility:hidden}
.toc-item .lbl{font-size:11px;color:#475569;line-height:1.5;word-break:break-word}
.toc-item input:checked ~ .lbl{color:#1e293b;font-weight:500}
.toc-children{padding-left:18px;overflow:hidden}
.selected-count{padding:12px 20px;border-top:1px solid #e2e8f0;flex-shrink:0;
  font-size:10px;color:#64748b;font-family:'Outfit',sans-serif}
.selected-count strong{color:#4f46e5}

.main-panel{flex:1;padding:30px 36px 60px;overflow-y:auto;min-width:0;background:#f7f7f5}
.badge{display:inline-flex;align-items:center;gap:8px;border-radius:6px;padding:5px 14px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:14px;background:#f1f5f9;border:1px solid #e2e8f0}
.badge .dot{width:6px;height:6px;border-radius:50%}
.tabs{display:inline-flex;gap:2px;margin:0 0 16px;background:#f1f5f9;border-radius:8px;padding:3px;border:1px solid #e2e8f0}
.tab{background:transparent;color:#64748b;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-family:inherit;font-size:11px;font-weight:500}
.tab.active{background:#fff;color:#4f46e5;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.actions{display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap;align-items:center}
.btn{border-radius:8px;padding:10px 28px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:500;border:1px solid;transition:all .15s}
.btn-primary{background:#4f46e5;color:#fff;border-color:#4f46e5}
.btn-primary:hover:not(:disabled){background:#4338ca}
.btn-primary:disabled{background:#e2e8f0;color:#94a3b8;border-color:#e2e8f0;cursor:default}
.btn-stop{background:#fee2e2;color:#dc2626;border-color:#fecaca}
.btn-ghost{background:#fff;color:#475569;border-color:#e2e8f0;font-size:12px;padding:10px 20px}
.btn-ghost:hover{background:#f1f5f9}
#list-input-wrap{display:none;margin-bottom:12px;border:1px solid #e2e8f0;border-radius:10px;background:#fff;padding:5px}
#list-input-wrap textarea{width:100%;background:transparent;border:none;outline:none;color:#1e293b;font-size:13px;padding:12px 14px;font-family:inherit;resize:vertical;line-height:1.7;min-height:120px}
#single-input-wrap{display:none;margin-bottom:12px;border:1px solid #e2e8f0;border-radius:10px;background:#fff;padding:5px}
#single-input-wrap input{width:100%;background:transparent;border:none;outline:none;color:#1e293b;font-size:14px;padding:12px 14px;font-family:inherit}
#error-box{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;color:#dc2626;font-size:12px;margin-bottom:20px;display:none}
#crawl-box{background:#eef2ff;border:1px solid #c7d2fe;border-radius:8px;padding:10px 16px;color:#4338ca;font-size:11px;margin-bottom:20px;display:none}
#progress-box{margin-bottom:24px;display:none}
.progress-info{display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-bottom:6px}
.progress-bar{height:3px;background:#e2e8f0;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#4f46e5,#818cf8);border-radius:2px;transition:width .25s ease;width:0}
#filters-box{display:none;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.filter{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:5px 14px;cursor:pointer;font-family:inherit;font-size:11px;font-weight:500;color:#475569}
.filter:hover{background:#f1f5f9}
.filter.active{background:#fff;color:#1e293b;border-color:#cbd5e1;box-shadow:0 1px 2px rgba(0,0,0,.06)}
#results-table{border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;display:none;background:#fff}
.tbl-header{display:grid;grid-template-columns:28px minmax(100px,1fr) minmax(100px,1.2fr) minmax(100px,1fr) 50px 44px 64px;
  gap:6px;padding:10px 14px;background:#f8fafc;border-bottom:1px solid #e2e8f0;
  font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.08em}
.tbl-row{display:grid;grid-template-columns:28px minmax(100px,1fr) minmax(100px,1.2fr) minmax(100px,1fr) 50px 44px 64px;
  gap:6px;align-items:center;padding:9px 14px;border-bottom:1px solid #f1f5f9;animation:fadeUp .25s ease both}
.tbl-row:last-child{border-bottom:none}
.tbl-row:hover{background:#f8fafc}
.tbl-row.is-broken{background:#fef2f2}
.tbl-row.is-blocked{background:#fff7ed}
.icon-badge{width:24px;height:24px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600}
.cell{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell a{color:#475569;font-size:10px;text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell a:hover{color:#4f46e5}
.cell .sub{font-size:9px;color:#94a3b8;margin-top:1px}
.cell .more{font-size:9px;color:#4f46e5;margin-top:1px}
#summary-box{margin-top:20px;padding:14px 18px;border-radius:8px;font-size:12px;font-family:'Outfit',sans-serif;line-height:1.8;display:none}

.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:80px 40px;text-align:center}
.empty-state .icon{font-size:40px;margin-bottom:16px;opacity:.25}
.empty-state .title{font-family:'Outfit',sans-serif;font-size:16px;color:#475569;margin-bottom:6px}
.empty-state .desc{font-size:11px;color:#94a3b8;max-width:320px;line-height:1.7}

.spinner{width:14px;height:14px;border:2px solid #c7d2fe;border-top-color:#4f46e5;border-radius:50%;animation:spin .6s linear infinite;display:inline-block;vertical-align:middle;margin-right:6px}
</style>
</head>
<body>
<div class="app-layout">

  <!-- ===== LEFT SIDEBAR ===== -->
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h1>Link Checker</h1>
      <div class="subtitle">Select topics to scan for broken links</div>
    </div>

    <div class="sidebar-input">
      <div class="input-wrap">
        <input type="text" id="url-input" placeholder="https://support.smartbear.com/"
               value="https://support.smartbear.com/">
      </div>
      <button class="load-btn" id="load-toc-btn">Load Topics</button>
    </div>

    <div class="toc-area" id="toc-area">
      <div class="toc-empty" id="toc-empty">
        Enter a documentation URL above and click <strong>Load Topics</strong> to see the table of contents.
      </div>
      <div id="toc-tree" style="display:none"></div>
    </div>

    <div class="selected-count" id="selected-count" style="display:none">
      <strong id="sel-num">0</strong> topic(s) selected
    </div>
  </div>

  <!-- ===== MAIN PANEL ===== -->
  <div class="main-panel" id="main-panel">
    <div class="badge" id="badge"><span class="dot" id="badge-dot"></span><span id="badge-text">Checking...</span></div>

    <div class="tabs" id="tabs">
      <button class="tab active" data-mode="deep">&#128269; Deep Crawl</button>
      <button class="tab" data-mode="single">Single Page</button>
      <button class="tab" data-mode="list">Check List</button>
    </div>

    <div id="single-input-wrap">
      <input type="text" id="single-url-input" placeholder="https://example.com">
    </div>
    <div id="list-input-wrap">
      <textarea id="list-url-input" placeholder="Paste URLs, one per line"></textarea>
    </div>


    <div class="actions">
      <button class="btn btn-primary" id="scan-btn">&#128269; Crawl Selected Topics</button>
      <button class="btn btn-stop" id="stop-btn" style="display:none">&#9632; Stop</button>

    </div>

    <div id="error-box"></div>
    <div id="crawl-box"></div>

    <div id="progress-box">
      <div class="progress-info"><span id="prog-text"></span><span id="prog-pct"></span></div>
      <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
    </div>

    <div id="filters-box"></div>
    <div id="results-table">
      <div class="tbl-header">
        <span></span><span>Link Text</span><span>Destination URL</span><span>Found On Page</span>
        <span style="text-align:center">Code</span><span style="text-align:center">Time</span><span style="text-align:right">Status</span>
      </div>
      <div id="results-body"></div>
    </div>
    <div id="summary-box"></div>

    <div class="empty-state" id="empty-state">
      <div class="icon">&#128279;</div>
      <div class="title">Ready to scan</div>
      <div class="desc">Load the table of contents from the sidebar, select the topics you want to check, then hit Crawl.</div>
    </div>
  </div>
</div>

<script>
const S={ok:{l:"OK",c:"#16a34a",bg:"#ecfdf5",i:"\u2713"},broken:{l:"Broken",c:"#dc2626",bg:"#fef2f2",i:"\u2715"},
redirect:{l:"Redirect",c:"#a16207",bg:"#fefce8",i:"\u2197"},timeout:{l:"Timeout",c:"#7c3aed",bg:"#f5f3ff",i:"\u23F1"},
blocked:{l:"Blocked",c:"#c2410c",bg:"#fff7ed",i:"\u26A0"},pending:{l:"Waiting",c:"#64748b",bg:"#f1f5f9",i:"\u25CC"}};

let mode="deep", results=[], scanning=false, filter="all", pagesCrawled=0, abortCtrl=null;
let tocData=[], baseUrl="";
const $=id=>document.getElementById(id);

const badge=$("badge"), badgeDot=$("badge-dot"), badgeTxt=$("badge-text");
const urlInput=$("url-input"), scanBtn=$("scan-btn"), stopBtn=$("stop-btn");
const errorBox=$("error-box"), crawlBox=$("crawl-box"), progBox=$("progress-box");
const progText=$("prog-text"), progPct=$("prog-pct"), progFill=$("prog-fill");
const filtersBox=$("filters-box"), resultsTable=$("results-table"), resultsBody=$("results-body");
const summaryBox=$("summary-box");
const tocArea=$("toc-area"), tocTree=$("toc-tree"), tocEmpty=$("toc-empty");
const loadTocBtn=$("load-toc-btn"), selectedCount=$("selected-count"), selNum=$("sel-num");
const emptyState=$("empty-state");
const singleInput=$("single-url-input"), listInput=$("list-url-input");

// Health check
fetch("/api/health").then(r=>{if(r.ok){badge.style.background="#ecfdf5";badge.style.borderColor="#a7f3d0";
  badgeDot.style.background="#22c55e";badgeTxt.textContent="Backend Connected";badgeTxt.style.color="#16a34a";}
}).catch(()=>{badge.style.background="#fef2f2";badgeDot.style.background="#ef4444";badgeTxt.textContent="Backend Offline";badgeTxt.style.color="#dc2626";});

// ===== TOC LOADING =====
loadTocBtn.addEventListener("click", loadTOC);
urlInput.addEventListener("keydown", e=>{if(e.key==="Enter")loadTOC();});

function loadTOC(){
  const url = urlInput.value.trim();
  if(!url) return;
  loadTocBtn.disabled = true;
  loadTocBtn.innerHTML = '<span class="spinner"></span>Loading...';
  tocTree.style.display="none"; tocEmpty.style.display="block";
  tocEmpty.textContent="Fetching table of contents...";

  fetch("/api/fetch-toc",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({url})})
  .then(r=>r.json()).then(data=>{
    loadTocBtn.disabled=false; loadTocBtn.textContent="Load Topics";
    if(data.error){tocEmpty.textContent=data.error;return;}
    tocData=data.toc; baseUrl=data.base_url;
    renderTOC();
  }).catch(e=>{
    loadTocBtn.disabled=false; loadTocBtn.textContent="Load Topics";
    tocEmpty.textContent="Failed to load: "+e.message;
  });
}

function renderTOC(){
  tocTree.innerHTML="";
  if(!tocData.length){tocEmpty.style.display="block";tocEmpty.textContent="No topics found.";tocTree.style.display="none";return;}
  tocEmpty.style.display="none"; tocTree.style.display="block";

  const toolbar = document.createElement("div");
  toolbar.className="toc-toolbar";
  const selAll = document.createElement("button"); selAll.textContent="Select All";
  selAll.onclick=()=>{tocTree.querySelectorAll('input[type=checkbox]').forEach(c=>{c.checked=true;});updateSelectedCount();};
  const clrAll = document.createElement("button"); clrAll.textContent="Clear All";
  clrAll.onclick=()=>{tocTree.querySelectorAll('input[type=checkbox]').forEach(c=>{c.checked=false;});updateSelectedCount();};
  toolbar.appendChild(selAll); toolbar.appendChild(clrAll);
  tocTree.appendChild(toolbar);

  tocData.forEach(item=>tocTree.appendChild(buildTocNode(item, 0)));
  updateSelectedCount();
}

function buildTocNode(item, depth){
  const wrap = document.createElement("div");
  wrap.className="toc-node";

  const row = document.createElement("div");
  row.className="toc-item";
  row.style.paddingLeft = (depth * 12) + "px";

  const arrow = document.createElement("span");
  arrow.className = "arrow" + (item.children && item.children.length ? "" : " empty");
  arrow.textContent = "\u25B6";

  const cb = document.createElement("input");
  cb.type="checkbox";
  cb.dataset.url = item.url;

  const lbl = document.createElement("span");
  lbl.className="lbl";
  lbl.textContent = item.text || item.url.split("/").pop().replace(".html","").replace(/-/g," ");

  row.appendChild(arrow);
  row.appendChild(cb);
  row.appendChild(lbl);
  wrap.appendChild(row);

  let childWrap = null;
  if(item.children && item.children.length){
    childWrap = document.createElement("div");
    childWrap.className="toc-children";
    childWrap.style.display="none";
    item.children.forEach(ch=>childWrap.appendChild(buildTocNode(ch, depth+1)));
    wrap.appendChild(childWrap);
  }

  arrow.addEventListener("click", e=>{
    e.stopPropagation();
    if(!childWrap) return;
    const open = childWrap.style.display !== "none";
    childWrap.style.display = open ? "none" : "block";
    arrow.classList.toggle("open", !open);
  });

  lbl.addEventListener("click", ()=>{cb.checked = !cb.checked; onCheckChange(cb, childWrap);});
  cb.addEventListener("change", ()=>onCheckChange(cb, childWrap));

  return wrap;
}

function onCheckChange(cb, childWrap){
  if(childWrap){
    childWrap.querySelectorAll('input[type=checkbox]').forEach(c=>{c.checked=cb.checked;});
    if(cb.checked){childWrap.style.display="block";
      const arrow = cb.parentElement.querySelector(".arrow");
      if(arrow) arrow.classList.add("open");
    }
  }
  updateSelectedCount();
}

function updateSelectedCount(){
  const checked = tocTree.querySelectorAll('input[type=checkbox]:checked');
  selNum.textContent = checked.length;
  selectedCount.style.display = tocData.length ? "" : "none";
}

function getSelectedTopicUrls(){
  const urls=[];
  tocTree.querySelectorAll('input[type=checkbox]:checked').forEach(cb=>{
    if(cb.dataset.url) urls.push(cb.dataset.url);
  });
  return urls;
}

// ===== TABS =====
document.querySelectorAll(".tab").forEach(btn=>{btn.addEventListener("click",()=>{
  mode=btn.dataset.mode;
  document.querySelectorAll(".tab").forEach(b=>b.classList.remove("active"));btn.classList.add("active");
  $("single-input-wrap").style.display=mode==="single"?"block":"none";
  $("list-input-wrap").style.display=mode==="list"?"block":"none";
  $("sidebar").style.display=mode==="deep"?"flex":"none";
  scanBtn.textContent=mode==="deep"?"\uD83D\uDD0D Crawl Selected Topics":mode==="single"?"Crawl & Check \u2192":"Check All \u2192";
});});

// ===== SCAN =====
scanBtn.addEventListener("click",startScan);
stopBtn.addEventListener("click",()=>{if(abortCtrl)abortCtrl.abort();scanning=false;updateUI();});

function startScan(){
  results=[];scanning=true;filter="all";pagesCrawled=0;
  errorBox.style.display="none";crawlBox.style.display="none";summaryBox.style.display="none";
  emptyState.style.display="none";
  resultsBody.innerHTML="";filtersBox.innerHTML="";
  updateUI();

  abortCtrl=new AbortController();
  let endpoint,body;

  if(mode==="deep"){
    const topicUrls = getSelectedTopicUrls();
    const u = urlInput.value.trim() || "https://support.smartbear.com/administration/docs/";
    if(!topicUrls.length){
      errorBox.textContent="Please select at least one topic from the sidebar to crawl.";
      errorBox.style.display="block";scanning=false;updateUI();return;
    }
    endpoint="/api/crawl-site";
    body=JSON.stringify({url:u, topic_urls:topicUrls});
  } else if(mode==="single"){
    const text = singleInput.value.trim();
    if(!text){scanning=false;updateUI();return;}
    endpoint="/api/check-url";body=JSON.stringify({url:text});
  } else {
    const text = listInput.value.trim();
    if(!text){scanning=false;updateUI();return;}
    const urls=text.split(/[\n,]+/).map(u=>u.trim()).filter(u=>u).map(u=>/^https?:\/\//i.test(u)?u:"https://"+u);
    endpoint="/api/check-links";body=JSON.stringify({urls});
  }

  fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body,signal:abortCtrl.signal})
  .then(res=>{const reader=res.body.getReader();const dec=new TextDecoder();let buf="";
    function read(){reader.read().then(({done,value})=>{
      if(done){scanning=false;crawlBox.style.display="none";updateUI();return;}
      buf+=dec.decode(value,{stream:true});const lines=buf.split("\n");buf=lines.pop()||"";
      for(const line of lines){if(!line.startsWith("data: "))continue;try{const d=JSON.parse(line.slice(6));
        if(d.type==="error"){errorBox.textContent=d.message;errorBox.style.display="block";scanning=false;updateUI();return;}
        if(d.type==="status"||d.type==="crawling"){
          if(d.pages_crawled)pagesCrawled=d.pages_crawled;
          const msg=d.type==="crawling"?`Crawling page ${d.pages_crawled} (depth ${d.depth}): ${d.page.length>55?"..."+d.page.slice(-52):d.page}`:d.message;
          crawlBox.textContent=msg;crawlBox.style.display="block";}
        if(d.type==="crawl_done"){pagesCrawled=d.pages_crawled;
          crawlBox.textContent=`Crawled ${d.pages_crawled} pages. Found ${d.total_links} links. Checking...`;}
        if(d.type==="init"){
          results=d.links.map(lk=>({url:typeof lk==="string"?lk:lk.url,linkText:typeof lk==="string"?"":lk.link_text||"",
            sourcePages:typeof lk==="string"?[]:lk.source_pages||[],status:"pending",code:null,note:"",time:null,finalUrl:""}));
          progBox.style.display="block";updateProgress(0,results.length);}
        if(d.type==="result"){const idx=results.findIndex(r=>r.url===d.url);
          if(idx!==-1){results[idx]={url:d.url,linkText:d.link_text||results[idx].linkText||"",
            sourcePages:d.source_pages||results[idx].sourcePages||[],status:d.status,code:d.code,
            note:d.note||"",time:d.time,finalUrl:d.final_url||""};}
          updateProgress(d.done,results.length);addRow(results[idx],idx);}
        if(d.type==="done"){scanning=false;crawlBox.style.display="none";if(d.pages_crawled)pagesCrawled=d.pages_crawled;updateUI();}
      }catch(e){}}read();});}read();})
  .catch(e=>{if(e.name!=="AbortError"){errorBox.textContent="Could not reach backend.";errorBox.style.display="block";}
    scanning=false;updateUI();});
}

function updateProgress(done,total){
  const pct=total>0?Math.round(done/total*100):0;
  progText.textContent=`${done} / ${total} links checked${pagesCrawled>0?` (from ${pagesCrawled} pages)`:""}`;
  progPct.textContent=pct+"%";progFill.style.width=pct+"%";
}

function addRow(r,idx){
  if(!r)return;const cfg=S[r.status]||S.pending;
  const existing=document.querySelector(`[data-idx="${idx}"]`);
  const row=existing||document.createElement("div");
  row.className="tbl-row"+(r.status==="broken"?" is-broken":"")+(r.status==="blocked"?" is-blocked":"");
  row.dataset.idx=idx;row.dataset.status=r.status;
  row.style.animationDelay=existing?"0ms":Math.min(idx*20,300)+"ms";

  const sp=(r.sourcePages&&r.sourcePages.length>0)?r.sourcePages[0]:"";
  const moreCnt=(r.sourcePages&&r.sourcePages.length>1)?r.sourcePages.length-1:0;

  row.innerHTML=`
    <span class="icon-badge" style="background:${cfg.bg};color:${cfg.c}">${cfg.i}</span>
    <div class="cell"><div style="font-size:11px;font-weight:${r.linkText?"500":"400"};color:${r.linkText?(r.status==="broken"?"#dc2626":"#1e293b"):"#94a3b8"};${r.linkText?"":"font-style:italic"}" title="${(r.linkText||"").replace(/"/g,"&quot;")}">${r.linkText||"\u2014"}</div></div>
    <div class="cell"><a href="${r.url}" target="_blank" title="${r.url}">${r.url}</a>${r.note?`<div class="sub">${r.note}</div>`:""}</div>
    <div class="cell">${sp?`<a href="${sp}" target="_blank" title="${sp}">${sp.replace(/https?:\/\/[^/]+/,"")}</a>`:`<span style="color:#374151;font-size:10px">\u2014</span>`}${moreCnt>0?`<div class="more">+${moreCnt} more</div>`:""}</div>
    <div style="text-align:center;font-size:12px;font-weight:600;color:${cfg.c}">${r.code||"\u2014"}</div>
    <div style="text-align:center;font-size:10px;color:#4b5563">${r.time!=null?r.time+"s":"\u2014"}</div>
    <div style="text-align:right;font-size:9px;font-weight:600;color:${cfg.c};text-transform:uppercase;letter-spacing:.06em">${cfg.l}</div>`;

  if(!existing)resultsBody.appendChild(row);
  resultsTable.style.display="block";
  if(filter!=="all"&&r.status!==filter)row.style.display="none";
}

function updateUI(){
  scanBtn.style.display=scanning?"none":"";stopBtn.style.display=scanning?"":"none";
  emptyState.style.display=(!scanning&&results.length===0)?"flex":"none";

  if(results.length>0){
    const counts={};results.forEach(r=>{counts[r.status]=(counts[r.status]||0)+1;});
    filtersBox.style.display="flex";filtersBox.innerHTML="";
    const allBtn=document.createElement("button");allBtn.className="filter"+(filter==="all"?" active":"");
    allBtn.textContent=`All (${results.length})`;allBtn.onclick=()=>{filter="all";applyFilter();updateUI();};filtersBox.appendChild(allBtn);
    Object.entries(S).forEach(([k,cfg])=>{if(k==="pending"||!(counts[k]>0))return;
      const fb=document.createElement("button");fb.className="filter"+(filter===k?" active":"");
      fb.textContent=`${cfg.i} ${cfg.l} (${counts[k]})`;fb.style.color=filter===k?cfg.c:"";
      fb.style.background=filter===k?cfg.bg:"";fb.style.borderColor=filter===k?cfg.c+"33":"";
      fb.onclick=()=>{filter=filter===k?"all":k;applyFilter();updateUI();};filtersBox.appendChild(fb);});

    if(!scanning){
      const broken=counts.broken||0,blocked=counts.blocked||0;
      summaryBox.style.display="block";
      summaryBox.style.background=broken>0?"#fef2f2":"#ecfdf5";
      summaryBox.style.border=`1px solid ${broken>0?"#fecaca":"#a7f3d0"}`;
      summaryBox.style.color=broken>0?"#dc2626":"#16a34a";
      let txt=broken>0?`Found <strong>${broken}</strong> broken link${broken>1?"s":""} out of ${results.length} checked`
        :`All ${results.length} links are healthy`;
      if(pagesCrawled>0)txt+=` across ${pagesCrawled} pages`;txt+=".";
      if((counts.redirect||0)>0)txt+=` &middot; ${counts.redirect} redirect${counts.redirect>1?"s":""}`;
      if(blocked>0)txt+=`<div style="color:#f97316;margin-top:4px;font-size:11px">\u26A0 ${blocked} link${blocked>1?"s":""} blocked by proxy/firewall.</div>`;
      summaryBox.innerHTML=txt;
    }
  }
}

function applyFilter(){
  document.querySelectorAll(".tbl-row").forEach(row=>{
    row.style.display=(filter==="all"||row.dataset.status===filter)?"":"none";});
}


</script>
</body>
</html>"""

if __name__ == "__main__":
    print("\n  Broken Link Checker")
    print("  " + "-" * 30)
    print("  Opening in your browser...")
    print("  URL: http://localhost:5000")
    print("  Press Ctrl+C to quit.\n")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000, threaded=True)