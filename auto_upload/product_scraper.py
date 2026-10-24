import re
import requests
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ProductScraper:
    def scrape(self, product_url):
        if not product_url:
            return {"title": "", "description": "", "keywords": []}
        try:
            resp = requests.get(product_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            html = resp.text
            title = self._extract_title(html) or self._url_to_title(product_url)
            description = self._extract_description(html) or title
            keywords = self._extract_keywords(html) or self._url_keywords(product_url)
            return {"title": title, "description": description, "keywords": keywords}
        except Exception as e:
            logger.warning(f"Could not scrape {product_url}: {e}")
            title = self._url_to_title(product_url)
            return {"title": title, "description": title, "keywords": self._url_keywords(product_url)}

    def _extract_title(self, html):
        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'<title>([^<]+)</title>', html)
        return m.group(1).strip() if m else ""

    def _extract_description(self, html):
        m = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', html)
        return m.group(1) if m else ""

    def _extract_keywords(self, html):
        m = re.search(r'<meta\s+name="keywords"\s+content="([^"]+)"', html)
        if m:
            return [k.strip() for k in m.group(1).split(",")]
        return []

    def _url_to_title(self, url):
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p and not p.isdigit()]
        return " ".join(parts).title() if parts else "Product"

    def _url_keywords(self, url):
        path = urlparse(url).path
        words = set()
        for p in path.split("/"):
            if p and not p.isdigit():
                words.add(p.title())
        return list(words)
