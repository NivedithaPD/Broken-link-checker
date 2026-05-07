"""
Broken Link Checker - Desktop App
==================================
Double-click or run: python broken_link_checker_app.py
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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA}

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
    start = time.time()
    def try_req(verify=True):
        try:
            r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=verify)
            elapsed = round(time.time() - start, 2)
            if r.status_code in (405,403,400):
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=verify); r.close()
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
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type",""): return r.text
    except: pass
    return None

@app.route("/api/crawl-site", methods=["POST"])
def crawl_site():
    data = req.get_json(force=True); start_url = data.get("url","").strip()
    max_depth = min(data.get("max_depth",3),5); max_pages = min(data.get("max_pages",100),500)
    if not start_url: return jsonify({"error":"URL required"}),400
    if not start_url.startswith(("http://","https://")): start_url = "https://" + start_url
    def gen():
        queue = deque([(start_url,0)]); crawled = set(); all_links = {}
        yield f"data: {json.dumps({'type':'status','message':'Starting deep crawl...'})}\n\n"
        while queue and len(crawled) < max_pages:
            page_url, depth = queue.popleft(); pn = normalize_url(page_url)
            if pn in crawled or not is_crawlable(page_url): continue
            crawled.add(pn)
            yield f"data: {json.dumps({'type':'crawling','page':page_url,'depth':depth,'pages_crawled':len(crawled)})}\n\n"
            html = fetch_page(page_url)
            if not html: continue
            for lk in extract_links(html, page_url):
                ln = normalize_url(lk["url"])
                if ln not in all_links:
                    all_links[ln] = {"url":lk["url"],"link_text":lk["link_text"],"source_pages":[page_url]}
                else:
                    if page_url not in all_links[ln]["source_pages"]: all_links[ln]["source_pages"].append(page_url)
                    if lk["link_text"] and not all_links[ln]["link_text"]: all_links[ln]["link_text"] = lk["link_text"]
                if depth < max_depth and is_same_domain(lk["url"], start_url):
                    cn = normalize_url(lk["url"])
                    if cn not in crawled: queue.append((lk["url"], depth+1))
        total = len(all_links)
        yield f"data: {json.dumps({'type':'crawl_done','pages_crawled':len(crawled),'total_links':total})}\n\n"
        link_list = [{"url":v["url"],"link_text":v["link_text"],"source_pages":v["source_pages"]} for v in all_links.values()]
        yield f"data: {json.dumps({'type':'init','total':total,'links':link_list})}\n\n"
        if not total: yield f"data: {json.dumps({'type':'done','total':0,'pages_crawled':len(crawled)})}\n\n"; return
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
        try: resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False); resp.raise_for_status()
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

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Broken Link Checker</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#0b0c10;color:#c9d1d9;font-family:'DM Mono','Fira Code',monospace;padding:40px 20px 80px}
::placeholder{color:#555}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.container{max-width:1060px;margin:0 auto}
h1{font-family:'Outfit',sans-serif;font-size:38px;font-weight:700;letter-spacing:-0.03em;
   background:linear-gradient(135deg,#fff,#6b7280);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.subtitle{color:#4b5563;font-size:13px;margin-top:8px;font-family:'Outfit',sans-serif}
.badge{display:inline-flex;align-items:center;gap:8px;border-radius:6px;padding:5px 14px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:14px}
.badge .dot{width:6px;height:6px;border-radius:50%}
.tabs{display:inline-flex;gap:2px;margin:24px 0 16px;background:rgba(255,255,255,.03);border-radius:8px;padding:3px;border:1px solid rgba(255,255,255,.05)}
.tab{background:transparent;color:#64748b;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-family:inherit;font-size:11px;font-weight:500}
.tab.active{background:rgba(99,102,241,.12);color:#a5b4fc}
.input-wrap{margin-bottom:12px;border:1px solid rgba(255,255,255,.06);border-radius:10px;background:rgba(255,255,255,.015);padding:5px}
.input-wrap input,.input-wrap textarea{width:100%;background:transparent;border:none;outline:none;color:#e2e8f0;font-size:14px;padding:12px 14px;font-family:inherit}
.input-wrap textarea{font-size:13px;resize:vertical;line-height:1.7;min-height:120px}
.settings{display:flex;gap:20px;margin-bottom:20px;align-items:center;flex-wrap:wrap}
.settings label{font-size:11px;color:#64748b}
.settings input[type=number]{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:6px;color:#e2e8f0;padding:6px 10px;font-family:inherit;font-size:12px;width:60px;text-align:center}
.settings .hint{font-size:10px;color:#374151}
.actions{display:flex;gap:8px;margin-bottom:28px;flex-wrap:wrap;align-items:center}
.btn{border-radius:8px;padding:10px 28px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:500;border:1px solid;transition:all .15s}
.btn-primary{background:rgba(99,102,241,.12);color:#a5b4fc;border-color:rgba(99,102,241,.25)}
.btn-primary:hover:not(:disabled){background:rgba(99,102,241,.25)}
.btn-primary:disabled{background:rgba(255,255,255,.02);color:#334155;border-color:rgba(255,255,255,.04);cursor:default}
.btn-stop{background:rgba(239,68,68,.1);color:#f87171;border-color:rgba(239,68,68,.2)}
.btn-ghost{background:transparent;color:#4b5563;border-color:rgba(255,255,255,.06);font-size:12px;padding:10px 20px}
#error-box{background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.12);border-radius:8px;padding:12px 16px;color:#fca5a5;font-size:12px;margin-bottom:20px;display:none}
#crawl-box{background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.12);border-radius:8px;padding:10px 16px;color:#a5b4fc;font-size:11px;margin-bottom:20px;display:none}
#progress-box{margin-bottom:24px;display:none}
.progress-info{display:flex;justify-content:space-between;font-size:11px;color:#4b5563;margin-bottom:6px}
.progress-bar{height:3px;background:rgba(255,255,255,.04);border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#a78bfa);border-radius:2px;transition:width .25s ease;width:0}
#filters-box{display:none;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.filter{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:6px;padding:5px 14px;cursor:pointer;font-family:inherit;font-size:11px;font-weight:500;color:#4b5563}
.filter:hover{background:rgba(255,255,255,.06)}
.filter.active{background:rgba(255,255,255,.07);color:#e2e8f0;border-color:rgba(255,255,255,.1)}
#results-table{border:1px solid rgba(255,255,255,.04);border-radius:12px;overflow:hidden;display:none}
.tbl-header{display:grid;grid-template-columns:28px minmax(100px,1fr) minmax(100px,1.2fr) minmax(100px,1fr) 50px 44px 64px;
  gap:6px;padding:10px 14px;background:rgba(255,255,255,.02);border-bottom:1px solid rgba(255,255,255,.04);
  font-size:9px;color:#4b5563;text-transform:uppercase;letter-spacing:.08em}
.tbl-row{display:grid;grid-template-columns:28px minmax(100px,1fr) minmax(100px,1.2fr) minmax(100px,1fr) 50px 44px 64px;
  gap:6px;align-items:center;padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.025);animation:fadeUp .25s ease both}
.tbl-row:last-child{border-bottom:none}
.tbl-row:hover{background:rgba(255,255,255,.025)}
.tbl-row.is-broken{background:rgba(239,68,68,.02)}
.tbl-row.is-blocked{background:rgba(249,115,22,.02)}
.icon-badge{width:24px;height:24px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600}
.cell{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell a{color:#64748b;font-size:10px;text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell .sub{font-size:9px;color:#374151;margin-top:1px}
.cell .more{font-size:9px;color:#6366f1;margin-top:1px}
#summary-box{margin-top:20px;padding:14px 18px;border-radius:8px;font-size:12px;font-family:'Outfit',sans-serif;line-height:1.8;display:none}
</style>
</head>
<body>
<div class="container">
  <div class="badge" id="badge"><span class="dot" id="badge-dot"></span><span id="badge-text">Checking...</span></div>
  <h1>Broken Link Checker</h1>
  <p class="subtitle">Recursively crawl a site and find every broken link across all pages.</p>

  <div class="tabs" id="tabs">
    <button class="tab active" data-mode="deep">&#128269; Deep Crawl</button>
    <button class="tab" data-mode="single">Single Page</button>
    <button class="tab" data-mode="list">Check List</button>
  </div>

  <div class="input-wrap" id="input-wrap">
    <input type="text" id="url-input" placeholder="https://support.smartbear.com/swagger/portal/docs">
  </div>

  <div class="settings" id="settings-box">
    <div style="display:flex;align-items:center;gap:8px">
      <label>Max depth:</label><input type="number" id="max-depth" min="1" max="5" value="3">
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <label>Max pages:</label><input type="number" id="max-pages" min="1" max="500" value="50" style="width:70px">
    </div>
    <div class="hint" id="settings-hint"></div>
  </div>

  <div class="actions">
    <button class="btn btn-primary" id="scan-btn">&#128269; Deep Crawl</button>
    <button class="btn btn-stop" id="stop-btn" style="display:none">&#9632; Stop</button>
    <button class="btn btn-ghost" id="export-btn" style="display:none;margin-left:auto">&#8595; Export CSV</button>
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
</div>

<script>
const S={ok:{l:"OK",c:"#22c55e",bg:"rgba(34,197,94,.1)",i:"\\u2713"},broken:{l:"Broken",c:"#ef4444",bg:"rgba(239,68,68,.1)",i:"\\u2715"},
redirect:{l:"Redirect",c:"#eab308",bg:"rgba(234,179,8,.1)",i:"\\u2197"},timeout:{l:"Timeout",c:"#a78bfa",bg:"rgba(167,139,250,.1)",i:"\\u23F1"},
blocked:{l:"Blocked",c:"#f97316",bg:"rgba(249,115,22,.1)",i:"\\u26A0"},pending:{l:"Waiting",c:"#475569",bg:"rgba(71,85,105,.06)",i:"\\u25CC"}};

let mode="deep", results=[], scanning=false, filter="all", pagesCrawled=0, abortCtrl=null;
const $=id=>document.getElementById(id);

// Elements
const badge=$("badge"), badgeDot=$("badge-dot"), badgeTxt=$("badge-text");
const urlInput=$("url-input"), scanBtn=$("scan-btn"), stopBtn=$("stop-btn"), exportBtn=$("export-btn");
const errorBox=$("error-box"), crawlBox=$("crawl-box"), progBox=$("progress-box");
const progText=$("prog-text"), progPct=$("prog-pct"), progFill=$("prog-fill");
const filtersBox=$("filters-box"), resultsTable=$("results-table"), resultsBody=$("results-body");
const summaryBox=$("summary-box"), settingsBox=$("settings-box"), settingsHint=$("settings-hint");
const maxDepthInput=$("max-depth"), maxPagesInput=$("max-pages");

// Health check
fetch("/api/health").then(r=>{if(r.ok){badge.style.background="rgba(34,197,94,.08)";badge.style.borderColor="rgba(34,197,94,.2)";
  badgeDot.style.background="#22c55e";badgeTxt.textContent="Backend Connected";badgeTxt.style.color="#4ade80";badgeDot.style.color="#4ade80";}
}).catch(()=>{badge.style.background="rgba(239,68,68,.08)";badgeDot.style.background="#ef4444";badgeTxt.textContent="Backend Offline";badgeTxt.style.color="#f87171";});

// Tabs
document.querySelectorAll(".tab").forEach(btn=>{btn.addEventListener("click",()=>{
  mode=btn.dataset.mode;
  document.querySelectorAll(".tab").forEach(b=>b.classList.remove("active"));btn.classList.add("active");
  settingsBox.style.display=mode==="deep"?"flex":"none";
  if(mode==="list"){urlInput.parentElement.innerHTML='<textarea id="url-input" placeholder="Paste URLs, one per line"></textarea>';
  }else{const wrap=$("input-wrap");if(wrap.querySelector("textarea")){
    wrap.innerHTML='<input type="text" id="url-input" placeholder="'+(mode==="deep"?"https://support.smartbear.com/swagger/portal/docs":"https://example.com")+'">';}}
  scanBtn.textContent=mode==="deep"?"\\uD83D\\uDD0D Deep Crawl":mode==="single"?"Crawl & Check \\u2192":"Check All \\u2192";
  updateHint();
});});

function updateHint(){settingsHint.textContent=`Follows internal links up to ${maxDepthInput.value} levels, scanning up to ${maxPagesInput.value} pages.`;}
maxDepthInput.addEventListener("change",updateHint);maxPagesInput.addEventListener("change",updateHint);updateHint();

// Scan
scanBtn.addEventListener("click",startScan);
stopBtn.addEventListener("click",()=>{if(abortCtrl)abortCtrl.abort();scanning=false;updateUI();});
exportBtn.addEventListener("click",doExport);

function startScan(){
  const inp=$("url-input");const text=(inp.value||"").trim();if(!text)return;
  results=[];scanning=true;filter="all";pagesCrawled=0;
  errorBox.style.display="none";crawlBox.style.display="none";summaryBox.style.display="none";
  resultsBody.innerHTML="";filtersBox.innerHTML="";
  updateUI();

  abortCtrl=new AbortController();
  let endpoint,body;
  if(mode==="deep"){let u=text;if(!/^https?:\\/\\//i.test(u))u="https://"+u;
    endpoint="/api/crawl-site";body=JSON.stringify({url:u,max_depth:+maxDepthInput.value,max_pages:+maxPagesInput.value});}
  else if(mode==="single"){endpoint="/api/check-url";body=JSON.stringify({url:text});}
  else{const urls=text.split(/[\\n,]+/).map(u=>u.trim()).filter(u=>u).map(u=>/^https?:\\/\\//i.test(u)?u:"https://"+u);
    endpoint="/api/check-links";body=JSON.stringify({urls});}

  fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body,signal:abortCtrl.signal})
  .then(res=>{const reader=res.body.getReader();const dec=new TextDecoder();let buf="";
    function read(){reader.read().then(({done,value})=>{
      if(done){scanning=false;crawlBox.style.display="none";updateUI();return;}
      buf+=dec.decode(value,{stream:true});const lines=buf.split("\\n");buf=lines.pop()||"";
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
    <div class="cell"><div style="font-size:11px;font-weight:${r.linkText?"500":"400"};color:${r.linkText?(r.status==="broken"?"#fca5a5":"#e2e8f0"):"#374151"};${r.linkText?"":"font-style:italic"}" title="${(r.linkText||"").replace(/"/g,"&quot;")}">${r.linkText||"\\u2014"}</div></div>
    <div class="cell"><a href="${r.url}" target="_blank" title="${r.url}">${r.url}</a>${r.note?`<div class="sub">${r.note}</div>`:""}</div>
    <div class="cell">${sp?`<a href="${sp}" target="_blank" title="${sp}">${sp.replace(/https?:\\/\\/[^/]+/,"")}</a>`:`<span style="color:#374151;font-size:10px">\\u2014</span>`}${moreCnt>0?`<div class="more">+${moreCnt} more</div>`:""}</div>
    <div style="text-align:center;font-size:12px;font-weight:600;color:${cfg.c}">${r.code||"\\u2014"}</div>
    <div style="text-align:center;font-size:10px;color:#4b5563">${r.time!=null?r.time+"s":"\\u2014"}</div>
    <div style="text-align:right;font-size:9px;font-weight:600;color:${cfg.c};text-transform:uppercase;letter-spacing:.06em">${cfg.l}</div>`;

  if(!existing)resultsBody.appendChild(row);
  resultsTable.style.display="block";
  if(filter!=="all"&&r.status!==filter)row.style.display="none";
}

function updateUI(){
  scanBtn.style.display=scanning?"none":"";stopBtn.style.display=scanning?"":"none";
  exportBtn.style.display=(!scanning&&results.length>0)?"":"none";

  // Filters
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

    // Summary
    if(!scanning){
      const broken=counts.broken||0,blocked=counts.blocked||0;
      summaryBox.style.display="block";
      summaryBox.style.background=broken>0?"rgba(239,68,68,.04)":"rgba(34,197,94,.04)";
      summaryBox.style.border=`1px solid ${broken>0?"rgba(239,68,68,.1)":"rgba(34,197,94,.1)"}`;
      summaryBox.style.color=broken>0?"#fca5a5":"#86efac";
      let txt=broken>0?`Found <strong>${broken}</strong> broken link${broken>1?"s":""} out of ${results.length} checked`
        :`All ${results.length} links are healthy`;
      if(pagesCrawled>0)txt+=` across ${pagesCrawled} pages`;txt+=".";
      if((counts.redirect||0)>0)txt+=` &middot; ${counts.redirect} redirect${counts.redirect>1?"s":""}`;
      if(blocked>0)txt+=`<div style="color:#f97316;margin-top:4px;font-size:11px">\\u26A0 ${blocked} link${blocked>1?"s":""} blocked by proxy/firewall.</div>`;
      summaryBox.innerHTML=txt;
    }
  }
}

function applyFilter(){
  document.querySelectorAll(".tbl-row").forEach(row=>{
    row.style.display=(filter==="all"||row.dataset.status===filter)?"":"none";});
}

function doExport(){
  const h="Link Text,URL,Status,HTTP Code,Time,Found On Page(s),Note,Final URL\\n";
  const rows=results.map(r=>`"${(r.linkText||"").replace(/"/g,'""')}","${r.url}","${S[r.status]?.l||r.status}","${r.code||""}","${r.time||""}s","${(r.sourcePages||[]).join(" | ")}","${r.note}","${r.finalUrl}"`).join("\\n");
  const blob=new Blob([h+rows],{type:"text/csv"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="broken-links-report.csv";a.click();
}

urlInput.addEventListener("keydown",e=>{if(e.key==="Enter"&&!scanning)startScan();});
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