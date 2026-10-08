import sys
import os
import json
import base64
import requests
import trafilatura
from dotenv import load_dotenv
from google import genai
from ebooklib import epub

load_dotenv()

def extract_article_content(url: str) -> dict:
    """Extracts clean main body text and title from web article URL."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return {"title": "Untitled Article", "content": f"Failed to fetch content from {url}"}
    
    content = trafilatura.extract(downloaded)
    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else "Untitled Article"
    return {"title": title, "content": content or ""}

def summarize_and_format_chapter(title: str, text: str, api_key: str) -> str:
    """Uses Gemini 2.5 Flash to generate a structured chapter layout with an executive summary."""
    client = genai.Client(api_key=api_key)
    prompt = f"""
    You are an expert editor formatting web content into a published eBook chapter.
    
    Article Title: {title}
    Article Content:
    {text[:8000]}  # Truncate if exceptionally long
    
    Please output clean HTML format for an EPUB chapter containing:
    1. An 'Executive Summary' box at the top (2-3 bullet points).
    2. Clean, well-structured article text broken into logical HTML section headings (<h2>, <p>).
    """
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )
    return response.text

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
    for i, item in enumerate(articles):
        raw_data = extract_article_content(item)
        chapter_html = summarize_and_format_chapter(raw_data["title"], raw_data["content"], api_key)
        
        c = epub.EpubHtml(title=raw_data["title"], file_name=f"chap_{i+1}.xhtml", lang="en")
        c.content = f"<h1>{raw_data['title']}</h1>{chapter_html}"
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
    cover_b64 = payload.get("cover_base64")

    if not urls:
        print("❌ No URLs provided in payload.")
        sys.exit(1)

    build_epub(title, urls, cover_b64)