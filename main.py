#!/usr/bin/env python3
"""
כלי להורדת פודקאסטים מספוטיפיי ותמלול בעברית
Spotify Podcast Downloader + Hebrew Transcription

שימוש:
    python main.py

דרישות:
    - Google Gemini API Key (חינמי)
    - קובץ .env עם GEMINI_API_KEY
"""

import os
import re
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote

import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# טען משתני סביבה
load_dotenv()

# הגדרות
SCRIPT_DIR = Path(__file__).parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
TRANSCRIPTS_DIR = SCRIPT_DIR / "transcripts"

# צור תיקיות אם לא קיימות
DOWNLOADS_DIR.mkdir(exist_ok=True)
TRANSCRIPTS_DIR.mkdir(exist_ok=True)

# User Agent לבקשות HTTP
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def print_step(emoji: str, message: str):
    """הדפסת הודעה עם אייקון"""
    print(f"{emoji}  {message}")


def print_error(message: str):
    """הדפסת שגיאה"""
    print(f"\n❌ שגיאה: {message}")


def print_success(message: str):
    """הדפסת הצלחה"""
    print(f"\n✅ {message}")


# =============================================================================
# שלב 1: חילוץ מידע מלינק ספוטיפיי
# =============================================================================

def extract_spotify_ids(url: str) -> dict:
    """
    חילוץ Episode ID ו-Show ID מלינק ספוטיפיי

    תומך בפורמטים:
    - https://open.spotify.com/episode/XXXXX
    - https://open.spotify.com/episode/XXXXX?si=YYYY
    - spotify:episode:XXXXX
    """
    result = {
        "episode_id": None,
        "show_id": None,
        "type": None
    }

    # נקה את ה-URL
    url = url.strip()

    # פורמט URI של ספוטיפיי
    if url.startswith("spotify:episode:"):
        result["episode_id"] = url.split(":")[-1]
        result["type"] = "episode"
        return result

    # פורמט URL רגיל
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    if len(path_parts) >= 2:
        content_type = path_parts[0]
        content_id = path_parts[1].split("?")[0]  # הסר query params

        if content_type == "episode":
            result["episode_id"] = content_id
            result["type"] = "episode"
        elif content_type == "show":
            result["show_id"] = content_id
            result["type"] = "show"

    return result


def get_show_id_from_episode(episode_id: str) -> str:
    """
    מקבל Show ID מתוך Episode ID על ידי scraping של דף ה-embed
    """
    # השתמש בדף embed שמכיל מידע סטטי (לא JavaScript)
    url = f"https://open.spotify.com/embed/episode/{episode_id}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        html_text = response.text

        # חפש show ID בפורמטים שונים
        patterns = [
            r'"showUri":"spotify:show:([a-zA-Z0-9]{22})"',
            r'spotify:show:([a-zA-Z0-9]{22})',
            r'/show/([a-zA-Z0-9]{22})',
        ]

        for pattern in patterns:
            match = re.search(pattern, html_text)
            if match:
                return match.group(1)

    except Exception as e:
        print_error(f"לא הצלחתי לגשת לדף הפרק: {e}")

    return None


# =============================================================================
# שלב 2: מציאת RSS Feed
# =============================================================================

def get_rss_from_itunes(podcast_name: str) -> str:
    """
    מחפש RSS feed אמיתי דרך iTunes Search API
    זה מחזיר את ה-RSS של הפודקאסט המקורי (עם קבצי MP3)
    """
    try:
        encoded_name = quote(podcast_name)
        itunes_url = f"https://itunes.apple.com/search?term={encoded_name}&media=podcast&entity=podcast&limit=5"

        response = requests.get(itunes_url, timeout=15)
        data = response.json()

        results = data.get('results', [])
        if not results:
            return None

        # חפש התאמה מדויקת או קרובה
        podcast_name_lower = podcast_name.lower().strip()
        for result in results:
            name = result.get('collectionName', '').lower().strip()
            feed_url = result.get('feedUrl')

            # בדוק התאמה
            if feed_url and (podcast_name_lower in name or name in podcast_name_lower):
                return feed_url

        # אם אין התאמה מדויקת, תחזיר את הראשון
        first_feed = results[0].get('feedUrl')
        if first_feed:
            return first_feed

    except Exception as e:
        print(f"    (iTunes search failed: {e})")

    return None


def get_rss_from_spotifeed(show_id: str) -> str:
    """
    מקבל RSS feed URL מ-Spotifeed (גיבוי - לא תמיד יש MP3)
    """
    return f"https://spotifeed.timdorr.com/{show_id}"


def get_podcast_info_from_spotify(episode_id: str) -> dict:
    """
    מקבל מידע על הפודקאסט מדף ה-embed של ספוטיפיי
    """
    info = {
        "episode_title": None,
        "show_title": None,
        "show_id": None,
        "duration": None
    }

    try:
        # קבל מידע מדף ה-embed שמכיל __NEXT_DATA__ עם כל המידע
        embed_url = f"https://open.spotify.com/embed/episode/{episode_id}"
        response = requests.get(embed_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        html = response.text

        # חלץ JSON מ-__NEXT_DATA__
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html)
        if match:
            data = json.loads(match.group(1))
            entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})

            if entity:
                info["episode_title"] = entity.get('name') or entity.get('title')
                info["show_title"] = entity.get('subtitle')  # שם הפודקאסט בשדה subtitle
                info["duration"] = entity.get('duration')

                # חלץ show ID מ-relatedEntityUri
                related_uri = entity.get('relatedEntityUri', '')
                if 'spotify:show:' in related_uri:
                    info["show_id"] = related_uri.split(':')[-1]

        # גיבוי: חפש show ID בכל ה-HTML
        if not info["show_id"]:
            match = re.search(r'spotify:show:([a-zA-Z0-9]{22})', html)
            if match:
                info["show_id"] = match.group(1)

    except Exception as e:
        print(f"    (לא הצלחתי לקבל מידע נוסף: {e})")

    return info


def fetch_rss_feed(rss_url: str) -> feedparser.FeedParserDict:
    """
    מוריד ומפענח RSS feed
    """
    try:
        response = requests.get(rss_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return feedparser.parse(response.content)
    except Exception as e:
        print_error(f"לא הצלחתי להוריד RSS feed: {e}")
        return None


def find_episode_in_rss(feed: feedparser.FeedParserDict, episode_id: str, episode_title: str = None) -> dict:
    """
    מוצא פרק ספציפי ב-RSS feed

    מחפש לפי:
    1. Episode ID ב-guid או link
    2. התאמת כותרת
    """
    if not feed or not feed.entries:
        return None

    # חפש לפי episode ID
    for entry in feed.entries:
        # בדוק ב-guid
        guid = entry.get('id', '') or entry.get('guid', '')
        if episode_id in str(guid):
            return extract_episode_data(entry)

        # בדוק בלינק
        link = entry.get('link', '')
        if episode_id in str(link):
            return extract_episode_data(entry)

    # חפש לפי כותרת (אם יש)
    if episode_title:
        episode_title_lower = episode_title.lower().strip()
        for entry in feed.entries:
            entry_title = entry.get('title', '').lower().strip()
            # התאמה מדויקת או חלקית
            if episode_title_lower == entry_title or episode_title_lower in entry_title or entry_title in episode_title_lower:
                return extract_episode_data(entry)

    # אם לא מצאנו, נחזיר את הפרק האחרון (לפעמים זה עובד)
    # אבל רק אם יש פרק אחד או שניים
    if len(feed.entries) <= 3:
        print("    (לא מצאתי התאמה מדויקת, מנסה את הפרק הראשון)")
        return extract_episode_data(feed.entries[0])

    return None


def extract_episode_data(entry) -> dict:
    """
    מחלץ מידע על פרק מ-RSS entry
    """
    data = {
        "title": entry.get('title', 'unknown'),
        "mp3_url": None,
        "duration": entry.get('itunes_duration', ''),
        "published": entry.get('published', ''),
        "description": entry.get('summary', '')[:200] if entry.get('summary') else ''
    }

    # מצא את קובץ ה-MP3 ב-enclosures
    enclosures = entry.get('enclosures', [])
    for enc in enclosures:
        enc_type = enc.get('type', '')
        enc_url = enc.get('href', '') or enc.get('url', '')
        if 'audio' in enc_type or enc_url.endswith('.mp3') or 'mp3' in enc_url:
            data["mp3_url"] = enc_url
            break

    # אם לא מצאנו ב-enclosures, חפש ב-links
    if not data["mp3_url"]:
        links = entry.get('links', [])
        for link in links:
            link_type = link.get('type', '')
            link_url = link.get('href', '')
            if 'audio' in link_type or link_url.endswith('.mp3'):
                data["mp3_url"] = link_url
                break

    # נקה את הכותרת לשם קובץ
    data["safe_title"] = sanitize_filename(data["title"])

    return data


def sanitize_filename(name: str) -> str:
    """
    מנקה שם קובץ מתווים בעייתיים
    """
    # הסר תווים לא חוקיים
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # הסר רווחים מיותרים
    name = re.sub(r'\s+', ' ', name).strip()
    # קצר אם צריך
    if len(name) > 100:
        name = name[:100]
    return name


# =============================================================================
# שלב 3: הורדת MP3
# =============================================================================

def download_mp3(url: str, output_path: Path, show_progress: bool = True) -> bool:
    """
    מוריד קובץ MP3 עם הצגת התקדמות
    """
    try:
        # התחל הורדה עם streaming
        response = requests.get(url, headers=HEADERS, stream=True, timeout=60)
        response.raise_for_status()

        # קבל גודל הקובץ
        total_size = int(response.headers.get('content-length', 0))
        total_mb = total_size / (1024 * 1024) if total_size else 0

        downloaded = 0
        chunk_size = 8192

        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    if show_progress and total_size:
                        percent = (downloaded / total_size) * 100
                        downloaded_mb = downloaded / (1024 * 1024)
                        print(f"\r    הורדה: {percent:.1f}% ({downloaded_mb:.1f}/{total_mb:.1f} MB)", end='', flush=True)

        if show_progress:
            print()  # שורה חדשה אחרי ההתקדמות

        return True

    except Exception as e:
        print_error(f"שגיאה בהורדה: {e}")
        return False


# =============================================================================
# שלב 4: תמלול עם Gemini
# =============================================================================

def transcribe_with_gemini(audio_path: Path) -> str:
    """
    מתמלל קובץ אודיו עם Google Gemini API
    """
    import shutil
    import tempfile

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print_error("חסר GEMINI_API_KEY!")
        print("\nאנא צור קובץ .env עם:")
        print("GEMINI_API_KEY=your_api_key_here")
        print("\nלקבלת API Key חינמי:")
        print("1. גש ל: https://aistudio.google.com")
        print("2. לחץ על 'Get API key'")
        print("3. לחץ 'Create API key in new project'")
        print("4. העתק את ה-Key לקובץ .env")
        return None

    try:
        from google import genai

        # צור client
        client = genai.Client(api_key=api_key)

        # בדוק גודל הקובץ
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"    גודל הקובץ: {file_size_mb:.1f} MB")

        # העתק לקובץ זמני עם שם ASCII (בגלל באג בספריית httpx)
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir) / "podcast_audio.mp3"
        shutil.copy2(audio_path, temp_path)

        # העלה את הקובץ
        print("    מעלה קובץ ל-Gemini...")
        audio_file = client.files.upload(file=str(temp_path))

        # המתן שהקובץ יהיה מוכן
        print("    ממתין לעיבוד הקובץ...")
        while audio_file.state.name == "PROCESSING":
            time.sleep(2)
            audio_file = client.files.get(name=audio_file.name)

        if audio_file.state.name == "FAILED":
            print_error("העלאת הקובץ נכשלה")
            return None

        # בקש תמלול
        print("    מתמלל... (זה יכול לקחת כמה דקות)")

        prompt = """תמלל את קובץ האודיו הזה בעברית.

דרישות:
1. תמלל את כל הדיבור בצורה מדויקת
2. הוסף timestamps בפורמט [MM:SS] בתחילת כל פסקה או כל דקה-שתיים
3. אם יש יותר מדובר אחד, סמן אותם כ: [דובר 1], [דובר 2] וכו'
4. שמור על פיסוק נכון - נקודות, פסיקים, סימני שאלה
5. חלק לפסקאות לקריאות

התמלול:"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, audio_file]
        )

        # מחק את הקובץ מ-Gemini ואת הקובץ הזמני
        try:
            client.files.delete(name=audio_file.name)
        except:
            pass

        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        return response.text

    except ImportError:
        print_error("ספריית google-genai לא מותקנת!")
        print("הרץ: pip install google-genai")
        return None

    except Exception as e:
        error_msg = str(e)
        if "API_KEY_INVALID" in error_msg or "invalid" in error_msg.lower():
            print_error("ה-API Key לא תקין!")
            print("אנא בדוק את ה-GEMINI_API_KEY בקובץ .env")
        elif "RESOURCE_EXHAUSTED" in error_msg or "quota" in error_msg.lower():
            print_error("חרגת ממכסת השימוש החינמית של Gemini")
            print("נסה שוב מחר או המתן כמה דקות")
        else:
            print_error(f"שגיאה בתמלול: {e}")
        return None


# =============================================================================
# פונקציה ראשית
# =============================================================================

def process_podcast(spotify_url: str) -> tuple:
    """
    מעבד פודקאסט מלינק ספוטיפיי

    Returns:
        tuple: (mp3_path, transcript_path) או (None, None) בכישלון
    """
    print("\n" + "=" * 60)
    print_step("🎙️", "מתחיל עיבוד פודקאסט")
    print("=" * 60)

    # שלב 1: חלץ IDs
    print_step("📍", "מחלץ מידע מהלינק...")
    ids = extract_spotify_ids(spotify_url)

    if not ids["episode_id"]:
        print_error("לא הצלחתי לחלץ Episode ID מהלינק")
        print("וודא שהלינק הוא של פרק פודקאסט (episode) ולא של show שלם")
        return None, None

    print(f"    Episode ID: {ids['episode_id']}")

    # שלב 2: מצא Show ID
    print_step("🔍", "מחפש את הפודקאסט...")

    show_id = ids.get("show_id")
    if not show_id:
        show_id = get_show_id_from_episode(ids["episode_id"])

    if not show_id:
        print_error("לא הצלחתי למצוא את הפודקאסט")
        print("ייתכן שזה פודקאסט בלעדי לספוטיפיי (Spotify Exclusive)")
        return None, None

    print(f"    Show ID: {show_id}")

    # קבל מידע נוסף על הפרק
    podcast_info = get_podcast_info_from_spotify(ids["episode_id"])
    if podcast_info["episode_title"]:
        print(f"    פרק: {podcast_info['episode_title']}")
    if podcast_info["show_title"]:
        print(f"    פודקאסט: {podcast_info['show_title']}")

    # שלב 3: מצא RSS feed אמיתי (עם קבצי MP3)
    print_step("📡", "מחפש RSS feed...")

    # נסה קודם למצוא RSS אמיתי דרך iTunes (יש שם MP3)
    rss_url = None
    show_title = podcast_info.get("show_title") or "podcast"

    if show_title and show_title != "podcast":
        print(f"    מחפש ב-iTunes: {show_title}")
        rss_url = get_rss_from_itunes(show_title)
        if rss_url:
            print(f"    נמצא RSS אמיתי: {rss_url[:60]}...")

    # אם לא מצאנו ב-iTunes, נסה Spotifeed (גיבוי)
    if not rss_url:
        print("    לא נמצא ב-iTunes, מנסה Spotifeed...")
        rss_url = get_rss_from_spotifeed(show_id)
        print(f"    RSS: {rss_url}")

    feed = fetch_rss_feed(rss_url)
    if not feed or not feed.entries:
        print_error("לא הצלחתי לקבל RSS feed")
        print("ייתכן שהפודקאסט לא זמין דרך RSS או שהוא בלעדי לספוטיפיי")
        return None, None

    print(f"    נמצאו {len(feed.entries)} פרקים ב-feed")

    # שם הפודקאסט מה-feed (עדכון אם יש שם טוב יותר)
    show_title = feed.feed.get('title', show_title)

    # שלב 4: מצא את הפרק הספציפי
    print_step("🎯", "מחפש את הפרק ב-RSS...")
    episode = find_episode_in_rss(feed, ids["episode_id"], podcast_info.get("episode_title"))

    if not episode or not episode.get("mp3_url"):
        print_error("לא הצלחתי למצוא את הפרק או את קובץ ה-MP3")

        # הצע פרקים אחרונים
        print("\nפרקים אחרונים שנמצאו:")
        for i, entry in enumerate(feed.entries[:5]):
            print(f"  {i+1}. {entry.get('title', 'ללא שם')}")

        return None, None

    print(f"    נמצא: {episode['title']}")
    print(f"    MP3 URL: {episode['mp3_url'][:80]}...")

    # שלב 5: הורד MP3
    print_step("⬇️", "מוריד את הפודקאסט...")

    # צור שם קובץ
    date_str = datetime.now().strftime("%Y%m%d")
    safe_show = sanitize_filename(show_title)[:30]
    safe_episode = episode["safe_title"][:50]
    mp3_filename = f"{date_str}_{safe_show}_{safe_episode}.mp3"
    mp3_path = DOWNLOADS_DIR / mp3_filename

    if not download_mp3(episode["mp3_url"], mp3_path):
        return None, None

    print(f"    נשמר: {mp3_path}")

    # שלב 6: תמלל
    print_step("📝", "מתמלל עם Gemini...")
    transcript = transcribe_with_gemini(mp3_path)

    if not transcript:
        print("    התמלול נכשל, אבל הקובץ MP3 נשמר")
        return mp3_path, None

    # שלב 7: שמור תמלול
    print_step("💾", "שומר תמלול...")

    transcript_filename = f"{date_str}_{safe_show}_{safe_episode}_transcript.txt"
    transcript_path = TRANSCRIPTS_DIR / transcript_filename

    # הוסף header לתמלול
    header = f"""═══════════════════════════════════════════════════════════
תמלול פודקאסט
═══════════════════════════════════════════════════════════
פודקאסט: {show_title}
פרק: {episode['title']}
תאריך עיבוד: {datetime.now().strftime('%Y-%m-%d %H:%M')}
לינק מקורי: {spotify_url}
═══════════════════════════════════════════════════════════

"""

    with open(transcript_path, 'w', encoding='utf-8') as f:
        f.write(header + transcript)

    print(f"    נשמר: {transcript_path}")

    return mp3_path, transcript_path


def main():
    """
    הפונקציה הראשית
    """
    print("\n" + "=" * 60)
    print("🎙️  כלי הורדת פודקאסט מספוטיפיי + תמלול בעברית")
    print("=" * 60)
    print("\nהכלי הזה מוריד פודקאסטים מספוטיפיי ומתמלל אותם בעברית.")
    print("שים לב: לא כל הפודקאסטים זמינים להורדה (Spotify Exclusives).\n")

    # בדוק API Key
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        print("⚠️  לא נמצא GEMINI_API_KEY!")
        print("\nכדי להשתמש בתמלול, אתה צריך API Key חינמי מגוגל:")
        print("1. גש ל: https://aistudio.google.com")
        print("2. לחץ על 'Get API key' בתפריט השמאלי")
        print("3. לחץ 'Create API key in new project'")
        print("4. העתק את ה-Key")
        print("5. צור קובץ .env עם התוכן: GEMINI_API_KEY=your_key_here")
        print("\nאתה יכול להמשיך בלי API Key (רק הורדה, בלי תמלול).")

        continue_anyway = input("\nלהמשיך בלי תמלול? (כ/ל) [כ=כן, ל=לא]: ").strip().lower()
        if continue_anyway not in ['כ', 'k', 'y', 'yes', '']:
            print("להתראות!")
            return

    # לולאה ראשית
    while True:
        print("\n" + "-" * 40)
        spotify_url = input("הדבק לינק של פרק פודקאסט מספוטיפיי (או 'יציאה' לסיום): ").strip()

        if spotify_url.lower() in ['exit', 'quit', 'יציאה', 'צא', 'q']:
            print("\nתודה שהשתמשת! להתראות 👋")
            break

        if not spotify_url:
            continue

        # ולידציה בסיסית
        if "spotify.com" not in spotify_url and not spotify_url.startswith("spotify:"):
            print_error("זה לא נראה כמו לינק ספוטיפיי")
            print("דוגמה: https://open.spotify.com/episode/XXXXXXXX")
            continue

        if "/episode/" not in spotify_url and ":episode:" not in spotify_url:
            print_error("זה נראה כמו לינק של פודקאסט שלם, לא של פרק")
            print("אני צריך לינק של פרק ספציפי (episode)")
            print("דוגמה: https://open.spotify.com/episode/XXXXXXXX")
            continue

        # עבד את הפודקאסט
        mp3_path, transcript_path = process_podcast(spotify_url)

        if mp3_path:
            print_success("הפעולה הושלמה!")
            print(f"\n📁 קבצים שנוצרו:")
            print(f"   MP3: {mp3_path}")
            if transcript_path:
                print(f"   תמלול: {transcript_path}")
        else:
            print("\n😔 לא הצלחתי לעבד את הפודקאסט הזה.")
            print("נסה פודקאסט אחר.")


if __name__ == "__main__":
    main()
