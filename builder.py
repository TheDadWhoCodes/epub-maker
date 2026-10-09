import sys
import os
import json
import base64
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from urllib.parse import urljoin

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def extract_page_content_playwright(url: str):
    """
    Renders the page with Playwright, clicks all interactive tabs/accordions,
    and extracts complete expanded DOM text.
    """
    print(f"🌐 Fetching complete page content via Playwright: {url}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Step 1: Click all interactive tabs, accordions, and "Read More" buttons to expand hidden sections
            page.evaluate("""
                () => {
                    const expandables = document.querySelectorAll('button, [role="tab"], .accordion-header, [data-toggle], .read-more');
                    expandables.forEach(el => {
                        try { el.click(); } catch(e) {}
                    });
                }
            """)
            page.wait_for_timeout(1500)

            # Step 2: Auto-scroll down the entire page to trigger lazy components
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

            # Extract title
            title = page.title() or "Magic Story"
            h1 = page.query_selector("h1")
            if h1:
                title = h1.inner_text().strip()

            # Clean out site boilerplate (nav, header, footer, ads)
            page.evaluate("""
                () => {
                    const selectors = ['header', 'footer', 'nav', '.share-buttons', '.social-share', 'script', 'style', 'iframe', '#cookie-banner'];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                }
            """)

            main_html = page.evaluate("""
                () => {
                    const main = document.querySelector('main') || document.querySelector('article') || document.body;
                    return main.innerHTML;
                }
            """)

            browser.close()
            return title, main_html

    except Exception as e:
        print(f"⚠️ Playwright extraction failed: {e}")
        return None, None


def clean_and_format_html(html_raw: str) -> str:
    """Cleans extracted raw HTML to preserve all readable story text, headings, and images for EPUB."""
    soup = BeautifulSoup(html_raw, 'html.parser')
    
    # Target all text and image nodes across standard and custom elements
    content_tags = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'p', 'div', 'span', 'li', 'ul', 'ol', 'blockquote', 'img'])
    
    clean_elements = []
    seen_text = set()

    for tag in content_tags:
        # Avoid duplicate text from nested container tags
        if tag.name in ['div', 'span'] and tag.find(['p', 'h1', 'h2', 'h3', 'h4', 'div']):
            continue

        text = tag.get_text(strip=True)
        
        # Filter empty tags or extreme duplicates
        if not text and not tag.find('img'):
            continue
        if text in seen_text and not tag.find('img'):
            continue
            
        if text:
            seen_text.add(text)

        if tag.name in ['p', 'span', 'div']:
            clean_elements.append(f"<p>{tag.decode_contents()}</p>")
        elif tag.name.startswith('h'):
            clean_elements.append(f"<{tag.name}>{text}</{tag.name}>")
        elif tag.name == 'img':
            clean_elements.append(str(tag))
        elif tag.name in ['ul', 'ol']:
            clean_elements.append(str(tag))

    return "".join(clean_elements)


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
                ext, media_type = 'jpg', 'image/jpeg'
                if 'png' in content_type: ext, media_type = 'png', 'image/png'
                elif 'gif' in content_type: ext, media_type = 'gif', 'image/gif'
                elif 'webp' in content_type: ext, media_type = 'webp', 'image/webp'

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
    """Takes input URLs, extracts rendered content directly, and builds the EPUB."""
    book = epub.EpubBook()
    book.set_title(title or "Magic Content Collection")
    book.set_language("en")
    book.add_author("WotC Content Converter")

    if cover_b64:
        try:
            image_data = base64.b64decode(cover_b64)
            book.set_cover("cover.jpg", image_data)
        except Exception as e:
            print(f"⚠️ Could not decode cover image: {e}")

    chapters = []

    for idx, url in enumerate(urls, 1):
        print(f"\n📖 Processing URL {idx}/{len(urls)}: {url}")
        chap_title, raw_html = extract_page_content_playwright(url)

        if not raw_html:
            print(f"⚠️ Could not retrieve content for {url}. Skipping...")
            continue

        clean_html = clean_and_format_html(raw_html)
        final_html = process_and_embed_images(book, clean_html, idx, base_url=url)

        c = epub.EpubHtml(title=chap_title, file_name=f"chap_{idx}.xhtml", lang="en")
        c.content = f"<h1>{chap_title}</h1>{final_html}"
        book.add_item(c)
        chapters.append(c)
        print(f"  ✅ Chapter '{chap_title}' added successfully!")

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
    title = "Magic Content Collection"
    cover_b64 = None

    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        payload_raw = os.getenv("CLIENT_PAYLOAD", "{}")
        try:
            payload = json.loads(payload_raw)
            title = payload.get("title", title)
            urls = payload.get("urls", [])
            cover_b64 = payload.get("cover_b64")
        except json.JSONDecodeError:
            pass

    if not urls:
        print("❌ Error: No URLs provided.")
        sys.exit(1)

    build_epub(title, urls, cover_b64)