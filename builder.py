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


def discover_stories_from_plane_page(input_url: str) -> list[dict]:
    """
    Crawls WotC Plane/Story pages (like /story/fiora-plane) by triggering 
    interactive elements, unrolling accordions, and extracting hidden text or links.
    """
    print(f"🔎 Scanning WotC Plane Hub page: {input_url}")
    stories = []

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # Navigate to the page
            page.goto(input_url, wait_until="networkidle", timeout=60000)

            # Step 1: Click all accordions, "Read Story", "Expand", or tab elements on the page
            page.evaluate("""
                () => {
                    const selectors = [
                        'button', '[role="button"]', '.accordion-header', 
                        '.story-card', '.expandable', '[data-toggle]'
                    ];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            try { el.click(); } catch(e) {}
                        });
                    });
                }
            """)
            page.wait_for_timeout(2000)

            # Step 2: Auto-scroll down the entire page
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const timer = setInterval(() => {
                            window.scrollBy(0, 800);
                            totalHeight += 800;
                            if(totalHeight >= document.body.scrollHeight || totalHeight > 25000){
                                clearInterval(timer);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            page.wait_for_timeout(2000)

            # Step 3: Extract story text rendered directly on the plane page (Accordion text)
            rendered_html = page.content()
            browser.close()

            soup = BeautifulSoup(rendered_html, 'html.parser')

            # Check if story text is embedded directly on the page (in sections/accordions)
            story_sections = soup.find_all(['section', 'article', 'div'], class_=re.compile(r'story|accordion|chapter|section-body', re.I))
            
            for idx, sec in enumerate(story_sections, 1):
                p_tags = sec.find_all('p')
                # If a section has at least 3 paragraphs of story text embedded directly:
                if len(p_tags) >= 3:
                    header = sec.find(['h1', 'h2', 'h3', 'h4', 'h5'])
                    sec_title = header.get_text(strip=True) if header else f"Story Part {idx}"
                    
                    clean_parts = [str(tag) for tag in sec.find_all(['p', 'h2', 'h3', 'h4', 'blockquote', 'figure', 'img'])]
                    stories.append({
                        'title': sec_title,
                        'html': "".join(clean_parts),
                        'url': input_url
                    })

            # Step 4: Extract external links to child story pages if present
            found_urls = []
            for a_tag in soup.find_all('a', href=True):
                href = urljoin(input_url, a_tag['href']).split('?')[0].split('#')[0]
                if ("wizards.com" in href) and href != input_url:
                    if re.search(r'/(news/magic-story|story|articles)/', href) and href not in found_urls:
                        found_urls.append(href)

            # If external chapter links were found, add them to the queue
            if found_urls:
                print(f"🔗 Discovered {len(found_urls)} external chapter links on plane page.")
                for link in found_urls:
                    stories.append({'title': None, 'html': None, 'url': link})

    except Exception as e:
        print(f"⚠️ Playwright hub extraction failed: {e}")

    return stories


def extract_wotc_story_direct(url: str):
    """Fallback server-side request for standalone story article URLs."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return None, None

        soup = BeautifulSoup(response.text, 'html.parser')
        title_node = soup.find('h1') or soup.find('title')
        title = title_node.get_text(strip=True) if title_node else "Magic Story"

        # Find container with highest paragraph count
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
    """Downloads images and embeds them into EPUB."""
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
    """Builds EPUB book from WotC URLs."""
    book = epub.EpubBook()
    book.set_title(title or "Fiora - Magic Story")
    book.set_language("en")
    book.add_author("WotC Magic Story Converter")

    if cover_b64:
        try:
            image_data = base64.b64decode(cover_b64)
            book.set_cover("cover.jpg", image_data)
        except Exception as e:
            print(f"⚠️ Could not decode cover image: {e}")

    chapters = []
    chapter_counter = 1

    for input_url in urls:
        discovered_stories = discover_stories_from_plane_page(input_url)

        for story in discovered_stories:
            chap_title = story.get('title')
            chap_html = story.get('html')
            story_url = story.get('url')

            # If story text wasn't directly embedded on the plane page, fetch it from story_url
            if not chap_html and story_url:
                print(f"\n📖 Fetching story from child page: {story_url}")
                chap_title, chap_html = extract_wotc_story_direct(story_url)

            if not chap_html:
                continue

            chap_title = chap_title or f"Chapter {chapter_counter}"
            print(f"\n✅ Adding Chapter {chapter_counter}: {chap_title}")

            final_html = process_and_embed_images(book, chap_html, chapter_counter, base_url=story_url or input_url)

            c = epub.EpubHtml(title=chap_title, file_name=f"chap_{chapter_counter}.xhtml", lang="en")
            c.content = f"<h1>{chap_title}</h1>{final_html}"
            book.add_item(c)
            chapters.append(c)
            chapter_counter += 1

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
    payload_raw = os.getenv("CLIENT_PAYLOAD", "{}")
    payload = json.loads(payload_raw)

    title = payload.get("title", "Fiora Plane Story")
    urls = payload.get("urls", ["https://magic.wizards.com/en/story/fiora-plane"])
    cover_b64 = payload.get("cover_b64")

    build_epub(title, urls, cover_b64)