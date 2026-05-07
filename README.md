Broken Link Checker
A single-file Python app that crawls websites and finds broken links. Just run one command and it opens in your browser.
![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Flask](https://img.shields.io/badge/Flask-3.x-green)
Features
Deep Crawl — recursively follows internal links across multiple pages to find broken links buried in sub-pages
Single Page — checks all links on one page
Check List — paste multiple URLs to check at once
Link Text — shows which clickable text on the page is broken
Source Page — shows which page the broken link was found on
Corporate Network Friendly — detects proxy/firewall blocks and marks them separately from real broken links
CSV Export — download full report with link text, source page, status codes, and response times
Live Streaming — results appear in real-time as links are checked
Quick Start
1. Install dependencies (one time only)
```bash
pip install flask requests beautifulsoup4
```
2. Run
```bash
python broken_link_checker_app.py
```
That's it! The app opens automatically in your browser at `http://localhost:5000`.
How It Works
The app runs a local Flask server that:
Fetches the target page
Parses all `<a href>` links using BeautifulSoup
In Deep Crawl mode, follows internal links up to N levels deep
Checks every link using HEAD/GET requests with 10 concurrent threads
Streams results back to the browser in real-time via Server-Sent Events
Settings
Setting	Default	Description
Max Depth	3	How many levels deep to follow internal links
Max Pages	50	Maximum number of pages to crawl
Status Types
Status	Meaning
✓ OK	Link works (2xx response)
✕ Broken	Link is dead (4xx/5xx or connection failed)
↗ Redirect	Link works but redirects (3xx)
⏱ Timeout	Server didn't respond in time
⚠ Blocked	Blocked by corporate proxy/firewall
Requirements
Python 3.10+
flask
requests
beautifulsoup4
License
MIT
