import sys
import os
import json
import base64
import re
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from urllib.parse import urljoin, urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def discover_plane_story_urls(input_url: str) -> list[str]:
    """
    Crawls a WotC Plane page by listening to network requests (APIs)
    and unrolling dynamic UI elements/Shadow DOMs to discover all story URLs.
    """
    print(f"🔎 Scanning WotC Hub page: {input_url}")
    discovered_urls = set()

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # Network Interceptor: Listen for JSON payloads returned by WotC background APIs
            def handle_response(response):
                try:
                    if "json" in response.headers.get("content-type", ""):
                        data = response.json()
                        json_str = json.dumps(data)
                        # Scan JSON for story article paths
                        matches = re.findall(r'/(?:en/)?(?:news/magic-story|story|articles)/[a-zA-Z0-9_-]+', json_str)
                        for match in matches:
                            full = urljoin("https://magic.wizards.com", match)
                            if full != input_url:
                                discovered_urls.add(full)
                except Exception:
                    pass

            page.on("response", handle_response)

            # Navigate
            page.goto(input_url, wait_until="networkidle", timeout=60000)

            # Scroll and trigger lazy components
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const timer = setInterval(() => {
                            window.scrollBy(0, 800);
                            totalHeight += 800;
                            if(totalHeight >= document.body.scrollHeight || totalHeight > 20000){
                                clearInterval(timer);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            page.wait_for_timeout(2000)

            # Extract links across open DOM + Shadow Roots
            extracted_links = page.evaluate("""
                () => {
                    const links = new Set();
                    function collectFromNode(node) {
                        if (node.tagName === 'A' && node.href) {
                            links.add(node.href);
                        }
                        if (node.shadowRoot) {
                            node.shadowRoot.querySelectorAll('*').forEach(collectFromNode);
                        }
                        node.childNodes.forEach(collectFromNode);
                    }
                    collectFromNode(document.body);
                    return Array.from(links);
                }
            """)

            browser.close()

            # Filter valid story URLs
            for link in extracted_links:
                clean_link = link.split('?')[0].split('#')[0]
                if "wizards.com" in clean_link and clean_link != input_url:
                    if re.search(r'/(news/magic-story|story|articles)/', clean_link):
                        discovered_urls.add(clean_link)

    except Exception as e:
        print(f"⚠️ Playwright discovery error: {e}")

    url_list = list(discovered_urls)
    print(f"✅ Discovered {len(url_list)} story links from hub page.")
    return url_list


def extract_wotc_story_article(url: str):
    """Fetches and parses clean HTML prose from an individual story article URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return None, None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Title extraction
        title_node = soup.find('h1') or soup.find('title')
        title = title_node.get_text(strip=True) if title_node else "Magic Story Chapter"

        # Find main content container by paragraph density
        containers = soup.find_all(['div', 'article', 'section'], class_=re.compile(r'body|content|story|article', re.I))
        best_container = None
        max_p = 0

        for c in containers:
            p_tags = c.find_all('p')
            if len(p_tags) > max_p:
                max_p = len(p_tags)
                best_container = c

        if best_container and max_p >= 3:
            clean_parts = []
            for tag in best_container.find_all(['p', 'h2', 'h3', 'h4', 'blockquote', 'ul', 'ol', 'figure', 'img']):
                if tag.find_parent(class_=re.compile(r'share|social|footer|header|nav|author', re.I)):
                    continue
                clean_parts.append(str(tag))

            return title, "".join(clean_parts)
    except Exception as e:
        print(f"⚠️ Direct extraction failed for {url}: {e}")

    return None, None


def process_and_embed_images(book, chapter_html, chapter_index, base_url=""):
    """Downloads images and embeds them inside the EPUB."""
    soup = BeautifulSoup(chapter_html, 'html.parser')
    images = soup.find_all('img')

    for img_idx, img in enumerate(images):
        img_url = img.get('src') or img.get('data-src') or img.get('data-original')
        if not img_url or img_url.startswith('data:'):
            continue

        if not img_url.startswith('http'):
            img_url = urljoin(base_url, img_url)

        try:
            resp = requests.get(img_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code == 200:
                content_type = resp.headers.get('Content-Type', '')
                ext = 'jpg'
                media_type = 'image/jpeg'
                if 'png' in content_type:
                    ext, media_type = 'png', 'image/png'
                elif 'gif' in content_type:
                    ext, media_type = 'gif', 'image/gif'
                elif 'webp' in content_type:
                    ext, media_type = 'webp', 'image/webp'

                internal_filename = f"images/chap_{chapter_index}_img_{img_idx+1}.{ext}"

                img_item = epub.EpubItem(
                    uid=f"img_{chapter_index}_{img_idx+1}",
                    file_name=internal_filename,
                    media_type=media_type,
                    content=resp.content
                )
                book.add_item(img_item)
                img['src'] = internal_filename
                print(f"  📸 Embedded image: {internal_filename}")
        except Exception as e:
            print(f"  ⚠️ Could not download image {img_url}: {e}")

    return str(soup)


def build_epub(title: str, urls: list, cover_b64: str = None, output_path: str = "book.epub"):
    """Builds the EPUB file from plane hub URLs or direct story URLs."""
    book = epub.EpubBook()
    book.set_title(title or "Magic Story Collection")
    book.set_language("en")
    book.add_author("WotC Magic Story Converter")

    if cover_b64:
        try:
            image_data = base64.b64decode(cover_b64)
            book.set_cover("cover.jpg", image_data)
        except Exception as e:
            print(f"⚠️ Could not decode cover image: {e}")

    # Phase 1: Expand input URLs into chapter URLs
    all_story_urls = []
    for input_url in urls:
        # Check if input URL is already a single story article
        if re.search(r'/news/magic-story/', input_url):
            all_story_urls.append(input_url)
        else:
            discovered = discover_plane_story_urls(input_url)
            all_story_urls.extend(discovered)

    # Deduplicate while preserving order
    unique_urls = []
    for u in all_story_urls:
        if u not in unique_urls:
            unique_urls.append(u)

    if not unique_urls:
        print("❌ No story URLs could be resolved from input.")
        sys.exit(1)

    # Phase 2: Build Chapters
    chapters = []
    for i, story_url in enumerate(unique_urls, 1):
        print(f"\n📖 Processing Story {i}/{len(unique_urls)}: {story_url}")
        chap_title, chap_html = extract_wotc_story_article(story_url)

        if not chap_html:
            print(f"⚠️ Skipping empty or non-article page: {story_url}")
            continue

        print(f"  ✅ Extracted: {chap_title}")
        final_html = process_and_embed_images(book, chap_html, i, base_url=story_url)

        c = epub.EpubHtml(title=chap_title, file_name=f"chap_{i}.xhtml", lang="en")
        c.content = f"<h1>{chap_title}</h1>{final_html}"
        book.add_item(c)
        chapters.append(c)

    if not chapters:
        print("❌ Failed to parse any valid story content.")
        sys.exit(1)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    epub.write_epub(output_path, book)
    print(f"\n🎉 Success! EPUB generated at: {output_path} ({len(chapters)} chapters included)")


if __name__ == "__main__":
    urls = []
    title = "Magic Plane Stories"
    cover_b64 = None

    # Option A: Read from CLI arguments (e.g. python main.py "https://...")
    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        # Option B: Read from CLIENT_PAYLOAD environment variable
        payload_raw = os.getenv("CLIENT_PAYLOAD", "{}")
        try:
            payload = json.loads(payload_raw)
            title = payload.get("title", title)
            urls = payload.get("urls", [])
            cover_b64 = payload.get("cover_b64")
        except json.JSONDecodeError:
            print("⚠️ Warning: Invalid JSON in CLIENT_PAYLOAD")

    # If still no URLs provided, exit cleanly with error message
    if not urls:
        print("❌ Error: No URLs provided.")
        print("Usage (CLI): python main.py <url1> <url2> ...")
        print("Usage (Payload): Set CLIENT_PAYLOAD environment variable with 'urls' array.")
        sys.exit(1)

    build_epub(title, urls, cover_b64)