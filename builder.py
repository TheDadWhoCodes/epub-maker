import sys
import os
import json
import base64
import re
import requests
import trafilatura
from bs4 import BeautifulSoup
from google import genai
from ebooklib import epub
from urllib.parse import urljoin

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def extract_wotc_magic_story(url):
    """
    Dedicated scraper for Wizards of the Coast (Magic Story) pages.
    Extracts the full title and complete story body without aggressive filtering or truncation.
    """
    try:
        from playwright.sync_api import sync_playwright
        print(f"🪄 Extracting WotC Magic Story directly from {url}...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            page = context.new_page()
            
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Scroll aggressively to trigger all lazy-loaded content/images
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 800;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if(totalHeight >= scrollHeight){
                                clearInterval(timer);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            page.wait_for_timeout(2000)
            
            content = page.content()
            browser.close()

            soup = BeautifulSoup(content, 'html.parser')

            # Extract Title
            title_node = soup.find('h1') or soup.find('title')
            title = title_node.get_text(strip=True) if title_node else "Magic Story"

            # WotC story content resides inside specific article/container tags
            # Common WotC story selectors:
            story_container = (
                soup.find('div', class_=re.compile(r'article-body|story-body|body-content|page-content|article-content', re.I)) or
                soup.find('article') or
                soup.find('main')
            )

            if not story_container:
                story_container = soup.body

            # Extract paragraphs, headings, blockquotes, and images
            elements = story_container.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'ul', 'ol', 'figure', 'img'])
            
            clean_html_parts = []
            for el in elements:
                # Filter out obvious UI junk/share buttons
                if el.find_parent(class_=re.compile(r'share|social|nav|footer|header|author-bio', re.I)):
                    continue
                clean_html_parts.append(str(el))

            story_html = "".join(clean_html_parts)
            return title, story_html

    except Exception as e:
        print(f"⚠️ Dedicated WotC scraper failed: {e}")
        return None, None


def extract_with_playwright(url):
    """Fallback engine for dynamic pages."""
    try:
        from playwright.sync_api import sync_playwright
        print(f"🌐 JS/Lazy-load detected for {url}. Rendering with Playwright...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            page = context.new_page()
            
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            
            page.evaluate("""
                async () => {
                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 500;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            if(totalHeight >= scrollHeight || totalHeight > 15000){
                                clearInterval(timer);
                                resolve();
                            }
                        }, 150);
                    });
                }
            """)
            page.wait_for_timeout(2000)
            
            flattened_html = page.evaluate("""
                () => {
                    function unrollShadow(node) {
                        let html = '';
                        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                        if (node.nodeType !== Node.ELEMENT_NODE) return '';
                        
                        const tagName = node.tagName.toLowerCase();
                        if (['script', 'style', 'noscript', 'iframe'].includes(tagName)) return '';

                        html += `<${tagName}`;
                        for (let attr of node.attributes) {
                            html += ` ${attr.name}="${attr.value.replace(/"/g, '&quot;')}"`;
                        }
                        html += '>';

                        if (node.shadowRoot) {
                            for (let child of node.shadowRoot.childNodes) {
                                html += unrollShadow(child);
                            }
                        }
                        for (let child of node.childNodes) {
                            html += unrollShadow(child);
                        }

                        html += `</${tagName}>`;
                        return html;
                    }
                    return unrollShadow(document.body);
                }
            """)
            
            browser.close()
            
            text = trafilatura.extract(
                flattened_html,
                output_format="html",
                include_images=True,
                include_tables=True,
                favor_recall=True
            )
            
            metadata = trafilatura.extract_metadata(flattened_html)
            title = metadata.title if metadata and metadata.title else "Untitled Article"
            
            return title, text
    except Exception as e:
        print(f"⚠️ Playwright rendering failed: {e}")
        return None, None


def extract_article_content(url):
    """Universal scraper with special routing for Magic / WotC domains."""
    # Route WotC / Magic story URLs to the custom handler
    if "magic.wizards.com" in url or "wizards.com" in url:
        w_title, w_html = extract_wotc_magic_story(url)
        if w_html and len(w_html) > 500:
            return w_title, w_html, True  # True indicates it's already clean HTML (skip LLM)

    # Default extraction for other sites
    downloaded = trafilatura.fetch_url(url)
    title = None
    text = None
    
    if downloaded:
        text = trafilatura.extract(
            downloaded,
            output_format="html",
            include_images=True,
            include_tables=True,
            favor_recall=True
        )
        metadata = trafilatura.extract_metadata(downloaded)
        title = metadata.title if metadata and metadata.title else "Untitled Article"

    if not text or len(text.strip()) < 400:
        print(f"⚡ Static scraper got minimal content. Running Playwright for {url}...")
        pw_title, pw_text = extract_with_playwright(url)
        if pw_text:
            return pw_title or title or "Untitled Article", pw_text, False

    return title or "Untitled Article", text, False


def format_chapter_content(title: str, text: str, is_already_html: bool, api_key: str) -> str:
    """Formats HTML content, using Gemini only if raw plain text/unformatted content was retrieved."""
    if is_already_html:
        # For WotC content, we directly clean up the extracted HTML without using LLM to avoid token truncation
        soup = BeautifulSoup(text, 'html.parser')
        # Remove empty tags
        for p in soup.find_all(['p', 'h2', 'h3']):
            if not p.get_text(strip=True) and not p.find('img'):
                p.decompose()
        return str(soup)

    client = genai.Client(api_key=api_key)
    models_to_try = ["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

    
    prompt = f"""
    You are an expert editor formatting web content into a published eBook chapter.
    
    Article Title: {title}
    Article Content:
    {text if text else ''}
    
    Instructions:
- Output the complete, unabridged article content formatted as clean HTML.
- Retain all original paragraphs (<p>), section headings (<h2>, <h3>), lists (<ul>, <ol>), and image tags (<img> with original src attributes).
- Do NOT summarize or shorten the text. Keep all original article prose intact.
- Return ONLY the raw HTML fragment for the chapter body. Do not include ```html markdown codeblock wrappers.
    """
    
    response = None
    last_error = None

    for model in models_to_try:
        try:
            print(f"🤖 Formatting content with Gemini model: {model}...")
            response = client.models.generate_content(
                model=model,
                contents=prompt
            )
            if response and response.text:
                break
        except Exception as e:
            print(f"⚠️ Model {model} failed: {e}")
            last_error = e

    if not response or not response.text:
        return f"<div>{text}</div>"

    content = response.text
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
        
    return content.strip()


def process_and_embed_images(book, chapter_html, chapter_index, base_url=""):
    """Parses HTML for <img> tags, downloads images, and embeds them into EPUB."""
    soup = BeautifulSoup(chapter_html, 'html.parser')
    images = soup.find_all('img')

    for img_idx, img in enumerate(images):
        img_url = img.get('src') or img.get('data-src')
        if not img_url:
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
                    ext = 'png'
                    media_type = 'image/png'
                elif 'gif' in content_type:
                    ext = 'gif'
                    media_type = 'image/gif'
                elif 'webp' in content_type:
                    ext = 'webp'
                    media_type = 'image/webp'

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


def build_epub(title: str, articles: list, cover_b64: str = None, output_path: str = "book.epub"):
    """Compiles extracted chapters and embedded images into an EPUB document."""
    book = epub.EpubBook()
    book.set_title(title or "Web Content Digest")
    book.set_language("en")
    book.add_author("Web-to-EPUB Converter")

    api_key = os.getenv("GEMINI_API_KEY")

    chapters = []

    if cover_b64:
        try:
            image_data = base64.b64decode(cover_b64)
            book.set_cover("cover.jpg", image_data)
        except Exception as e:
            print(f"⚠️ Could not decode cover image: {e}")

    for i, url in enumerate(articles):
        print(f"\n📖 Processing article {i+1}/{len(articles)}: {url}")
        article_title, article_text, is_clean_html = extract_article_content(url)

        if not article_text:
            print(f"⚠️ Warning: Could not extract content for {url}. Skipping...")
            continue

        chapter_html = format_chapter_content(article_title, article_text, is_clean_html, api_key)
        final_html = process_and_embed_images(book, chapter_html, i+1, base_url=url)

        c = epub.EpubHtml(title=article_title, file_name=f"chap_{i+1}.xhtml", lang="en")
        c.content = f"<h1>{article_title}</h1>{final_html}"
        book.add_item(c)
        chapters.append(c)

    if not chapters:
        print("❌ No valid chapters extracted from provided URLs.")
        sys.exit(1)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    book.spine = ["nav"] + chapters

    epub.write_epub(output_path, book)
    print(f"\n✅ EPUB successfully created at: {output_path}")


if __name__ == "__main__":
    payload_raw = os.getenv("CLIENT_PAYLOAD", "{}")
    payload = json.loads(payload_raw)

    title = payload.get("title", "My Web Digest")
    urls = payload.get("urls", [])
    cover_b64 = payload.get("cover_b64")

    if not urls:
        print("❌ No URLs provided in payload.")
        sys.exit(1)

    build_epub(title, urls, cover_b64)