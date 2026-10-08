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


def extract_with_playwright(url):
    """Fallback engine: Launches a headless Chrome browser, unrolls Shadow DOMs, auto-scrolls, and extracts DOM."""
    try:
        from playwright.sync_api import sync_playwright
        print(f"🌐 JS/Lazy-load/Shadow DOM detected for {url}. Rendering with Playwright...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            page = context.new_page()
            
            # Navigate and wait for DOM load
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            
            # Auto-scroll to trigger lazy loading
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
            page.wait_for_timeout(2000) # Wait 2s for late scripts
            
            # JS Helper: Recursively flatten/unroll Shadow DOMs into open HTML
            flattened_html = page.evaluate("""
                () => {
                    function unrollShadow(node) {
                        let html = '';
                        if (node.nodeType === Node.TEXT_NODE) {
                            return node.textContent;
                        }
                        if (node.nodeType !== Node.ELEMENT_NODE) {
                            return '';
                        }
                        
                        const tagName = node.tagName.toLowerCase();
                        if (['script', 'style', 'noscript', 'iframe'].includes(tagName)) {
                            return '';
                        }

                        html += `<${tagName}`;
                        for (let attr of node.attributes) {
                            html += ` ${attr.name}="${attr.value.replace(/"/g, '&quot;')}"`;
                        }
                        html += '>';

                        // Flatten shadow root if present
                        if (node.shadowRoot) {
                            for (let child of node.shadowRoot.childNodes) {
                                html += unrollShadow(child);
                            }
                        }
                        
                        // Process regular child nodes
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
            
            # Extract main content using trafilatura on the flattened DOM
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
    """Universal scraper: Tries fast static extraction first, falls back to full browser rendering if needed."""
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

    # Universal Fallback Threshold:
    # If static extraction returned empty or under 400 characters, trigger Playwright
    if not text or len(text.strip()) < 400:
        print(f"⚡ Static scraper got minimal content. Running headless browser for {url}...")
        pw_title, pw_text = extract_with_playwright(url)
        if pw_text:
            return pw_title or title or "Untitled Article", pw_text

    return title or "Untitled Article", text


def summarize_and_format_chapter(title: str, text: str, api_key: str) -> str:
    """Uses Gemini to generate a structured chapter layout, falling back across models if needed."""
    client = genai.Client(api_key=api_key)
    
    # Prioritized list of valid production Gemini models
    models_to_try = [
        "gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"
    ]
    
    prompt = f"""
    You are an expert editor formatting web content into a published eBook chapter.
    
    Article Title: {title}
    Article Content:
    {text if text else ''}
    
    Instructions:
    - Output the complete, unabridged article content formatted as clean HTML.
    - Start directly from the main content / first paragraph of the article.
    - Retain all original paragraphs (<p>), section headings (<h2>, <h3>), lists (<ul>, <ol>), and image tags (<img> with original src attributes).
    - Remove any extraneous site navigation, header elements, footer links, share buttons, or ads.
    - Do NOT summarize or shorten the text. Keep all original article prose intact.
    - Return ONLY the raw HTML fragment for the chapter body. Do not include ```html markdown codeblock wrappers or <html>/<body> boilerplate.
    """
    
    response = None
    last_error = None

    for model in models_to_try:
        try:
            print(f"🤖 Trying Gemini model: {model}...")
            response = client.models.generate_content(
                model=model,
                contents=prompt
            )
            if response and response.text:
                print(f"✅ Successfully generated content using {model}")
                break
        except Exception as e:
            print(f"⚠️ Model {model} failed: {e}")
            last_error = e

    if not response or not response.text:
        raise RuntimeError(f"❌ All Gemini models failed. Last error: {last_error}")

    content = response.text
    
    # Clean codeblock markdown if present
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
        
    return content.strip()


def process_and_embed_images(book, chapter_html, chapter_index, base_url=""):
    """Parses HTML for <img> tags, resolves relative links, downloads images, and embeds them into EPUB."""
    soup = BeautifulSoup(chapter_html, 'html.parser')
    images = soup.find_all('img')

    for img_idx, img in enumerate(images):
        img_url = img.get('src')
        if not img_url:
            continue
            
        # Convert relative URLs (/assets/img.jpg) to absolute (https://site.com/assets/img.jpg)
        if not img_url.startswith('http'):
            img_url = urljoin(base_url, img_url)

        try:
            resp = requests.get(img_url, timeout=10)
            if resp.status_code == 200:
                content_type = resp.headers.get('Content-Type', '')
                
                # Determine extension & media type
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

                # Create EPUB image item
                img_item = epub.EpubItem(
                    uid=f"img_{chapter_index}_{img_idx+1}",
                    file_name=internal_filename,
                    media_type=media_type,
                    content=resp.content
                )
                book.add_item(img_item)

                # Update <img> tag in HTML to point to local EPUB file
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
    if not api_key:
        print("❌ GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    chapters = []

    # Attach Cover Image if provided from SPA upload
    if cover_b64:
        try:
            image_data = base64.b64decode(cover_b64)
            book.set_cover("cover.jpg", image_data)
        except Exception as e:
            print(f"⚠️ Could not decode cover image: {e}")

    # Process each article URL
    for i, url in enumerate(articles):
        print(f"📖 Processing article {i+1}/{len(articles)}: {url}")
        article_title, article_text = extract_article_content(url)

        if not article_text:
            print(f"⚠️ Warning: Could not extract content for {url}. Skipping...")
            continue

        chapter_html = summarize_and_format_chapter(article_title, article_text, api_key)
        
        # Download and embed article images (PASS base_url=url)
        final_html = process_and_embed_images(book, chapter_html, i+1, base_url=url)

        # Create EPUB chapter using article_title
        c = epub.EpubHtml(title=article_title, file_name=f"chap_{i+1}.xhtml", lang="en")
        c.content = f"<h1>{article_title}</h1>{final_html}"
        book.add_item(c)
        chapters.append(c)

    if not chapters:
        print("❌ No valid chapters extracted from provided URLs.")
        sys.exit(1)

    # Table of Contents & Navigation
    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Spine configuration
    book.spine = ["nav"] + chapters

    # Save to disk
    epub.write_epub(output_path, book)
    print(f"✅ EPUB successfully created at: {output_path}")


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