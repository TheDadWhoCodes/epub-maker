import sys
import os
import json
import base64
import requests
import trafilatura
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
    
    # 1. Try trafilatura with high recall (gets more text)
    text = trafilatura.extract(
        downloaded, 
        favor_recall=True,      # Include more text content
        include_tables=True,    # Don't strip tables
        include_comments=False
    )
    
    # 2. Fallback: If trafilatura returned under 200 characters, use baseline fallback
    if not text or len(text.strip()) < 200:
        from trafilatura import baseline
        _, text, _ = baseline(downloaded)
        
    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else "Untitled Article"
    
    return title, text

def summarize_and_format_chapter(title: str, text: str, api_key: str) -> str:
    """Uses Gemini 2.5 Flash to generate a structured chapter layout with an executive summary."""
    client = genai.Client(api_key=api_key)
    
    # 1. Truncate text cleanly before inserting into the string template
    truncated_text = text[:8000] if text else ""
    
    # 2. Build the prompt without inline comments
    prompt = f"""
    You are an expert editor formatting web content into a published eBook chapter.
    
    Article Title: {title}
    Article Content:
    {truncated_text}
    
    Please output clean HTML format for an EPUB chapter containing:
    1. An 'Executive Summary' box at the top (2-3 bullet points).
    2. Clean, well-structured article text broken into logical HTML section headings (<h2>, <p>).
    Return ONLY the raw HTML body content without top-level ```html codeblock wrappers.
    """
    
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=prompt
    )
    
    content = response.text or ""
    
    # 3. Clean any markdown codeblock fences Gemini returns
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
        
    return content.strip()

def build_epub(title: str, articles: list, cover_b64: str = None, output_path: str = "book.epub"):
    """Compiles extracted chapters into a valid EPUB document."""
    book = epub.EpubBook()
    book.set_title(title or "Web Content Digest")
    book.set_language("en")
    book.add_author("Web-to-EPUB Converter")

    api_key = os.getenv("GEMINI_API_KEY")
    chapters = []

    # Attach Cover Image if provided from SPA upload
    if cover_b64:
        image_data = base64.b64decode(cover_b64)
        book.set_cover("cover.jpg", image_data)

    # Process each article URL
    for i, url in enumerate(articles):
        article_title, article_text = extract_article_content(url)

        if not article_text:
            print(f"⚠️ Warning: Could not extract content for {url}. Skipping...")
            continue

        chapter_html = summarize_and_format_chapter(article_title, article_text, api_key)

        # Create EPUB chapter using article_title
        c = epub.EpubHtml(title=article_title, file_name=f"chap_{i+1}.xhtml", lang="en")
        c.content = f"<h1>{article_title}</h1>{chapter_html}"
        book.add_item(c)
        chapters.append(c)

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
    # Receive payload passed from GitHub Actions trigger
    payload_raw = os.getenv("CLIENT_PAYLOAD", "{}")
    payload = json.loads(payload_raw)

    title = payload.get("title", "My Web Digest")
    urls = payload.get("urls", [])
    cover_b64 = payload.get("cover_b64")

    if not urls:
        print("❌ No URLs provided in payload.")
        sys.exit(1)

    build_epub(title, urls, cover_b64)