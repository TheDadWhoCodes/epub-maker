import sys
import os
import json
import base64
import requests
import trafilatura
from bs4 import BeautifulSoup
from google import genai
from ebooklib import epub

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def extract_article_content(url):
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None, None
    
    # Enable include_images=True to preserve <img> tags
    text = trafilatura.extract(
        downloaded, 
        include_images=True,
        favor_recall=True,
        include_tables=True,
        include_comments=False
    )
    
    # Fallback if trafilatura returned minimal text
    if not text or len(text.strip()) < 200:
        from trafilatura import baseline
        _, text, _ = baseline(downloaded)
        
    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else "Untitled Article"
    
    return title, text


def summarize_and_format_chapter(title: str, text: str, api_key: str) -> str:
    """Uses Gemini 2.5 Flash to format HTML, preserving image tags."""
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    You are an expert editor formatting web content into a published eBook chapter.
    
    Article Title: {title}
    Article Content:
    {text if text else ''}
    
    Please output clean HTML format for an EPUB chapter containing:
    1. An 'Executive Summary' box at the top (2-3 bullet points).
    2. Clean, well-structured article text broken into logical HTML section headings (<h2>, <p>).
    3. IMPORTANT: Preserve any <img> tags and their src attributes from the original content where relevant.
    
    Return ONLY the raw HTML body content without top-level ```html codeblock wrappers.
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    
    content = response.text or ""
    
    # Clean codeblock markdown if present
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
        
    return content.strip()


def process_and_embed_images(book, chapter_html, chapter_index):
    """Parses HTML for <img> tags, downloads the images, registers them in EPUB, and updates src attributes."""
    soup = BeautifulSoup(chapter_html, 'html.parser')
    images = soup.find_all('img')

    for img_idx, img in enumerate(images):
        img_url = img.get('src')
        if not img_url or not img_url.startswith('http'):
            continue

        try:
            # Download image data
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
        
        # Download and embed article images
        final_html = process_and_embed_images(book, chapter_html, i+1)

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