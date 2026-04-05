"""
Website Content Scraper
-----------------------
Crawls your company website and saves the text content into
knowledge_base.txt for the Claude chat engine to use.

Usage:
    python scrape_website.py https://www.yourcompany.com

Options:
    --max-pages 20       Max pages to crawl (default: 20)
    --output FILE        Output file (default: knowledge_base.txt)
"""

import argparse, re, sys, time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def clean_text(html: str) -> str:
    """Extract readable text from HTML, stripping nav/footer/script."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "iframe", "noscript", "svg", "form"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def crawl(start_url: str, max_pages: int = 20) -> dict[str, str]:
    """
    Crawl pages on the same domain starting from start_url.
    Returns {url: cleaned_text}.
    """
    domain = urlparse(start_url).netloc
    visited: set[str] = set()
    queue: list[str] = [start_url]
    pages: dict[str, str] = {}

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "KnowledgeBaseBot/1.0 (internal-use)"
            })
            if resp.status_code != 200:
                continue
            if "text/html" not in resp.headers.get("content-type", ""):
                continue
        except Exception as exc:
            print(f"  skip {url}: {exc}")
            continue

        text = clean_text(resp.text)
        if len(text) > 100:  # Skip near-empty pages
            pages[url] = text
            print(f"  [{len(pages)}/{max_pages}] {url}  ({len(text)} chars)")

        # Find internal links
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0].split("?")[0]
            if urlparse(link).netloc == domain and link not in visited:
                queue.append(link)

        time.sleep(0.5)  # Be polite

    return pages


def build_knowledge_base(pages: dict[str, str]) -> str:
    """Format crawled pages into a single knowledge base document."""
    sections = []
    for url, text in pages.items():
        # Truncate very long pages
        if len(text) > 3000:
            text = text[:3000] + "\n[... page truncated ...]"
        sections.append(f"--- Page: {url} ---\n{text}")
    return "\n\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description="Scrape website into knowledge base")
    parser.add_argument("url", help="Starting URL to crawl")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--output", default="knowledge_base.txt")
    args = parser.parse_args()

    print(f"Crawling {args.url} (max {args.max_pages} pages)...\n")
    pages = crawl(args.url, args.max_pages)

    if not pages:
        print("No pages found!")
        sys.exit(1)

    kb = build_knowledge_base(pages)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(kb)

    print(f"\nDone! Saved {len(pages)} pages to {args.output} ({len(kb)} chars)")


if __name__ == "__main__":
    main()
