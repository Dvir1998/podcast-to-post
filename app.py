#!/usr/bin/env python3
"""
Web Interface for Spotify Podcast Downloader + Transcription
Flask-based modern web application
"""

import os
import re
import json
import time
import uuid
import shutil
import tempfile
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

from flask import Flask, render_template, request, jsonify, send_file, session
from dotenv import load_dotenv
import requests
import feedparser

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))  # For session management


def get_api_key():
    """Get API key from request header, session, or environment"""
    # First check request header
    api_key = request.headers.get('X-API-Key')
    if api_key:
        return api_key
    # Then check session
    if 'api_key' in session:
        return session['api_key']
    # Finally fall back to environment
    return os.getenv("GEMINI_API_KEY")

# Configuration
SCRIPT_DIR = Path(__file__).parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
TRANSCRIPTS_DIR = SCRIPT_DIR / "transcripts"
WHATSAPP_SCRIPTS_DIR = Path.home() / ".claude" / "skills" / "whatsapp" / "scripts"

DOWNLOADS_DIR.mkdir(exist_ok=True)
TRANSCRIPTS_DIR.mkdir(exist_ok=True)

# WhatsApp Groups - Explain Community
WHATSAPP_GROUPS = [
    {"id": "120363201937987456@g.us", "name": "עדכונים וטיפים על בינה מלאכותית"},
    {"id": "120363306760997369@g.us", "name": "עדכונים וטיפים על בינה מלאכותית #2"},
]

# Job storage (in production, use Redis or database)
jobs = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


# =============================================================================
# Helper Functions (from main.py)
# =============================================================================

def extract_spotify_ids(url: str) -> dict:
    """Extract Episode ID from Spotify URL"""
    result = {"episode_id": None, "show_id": None, "type": None}
    url = url.strip()

    if url.startswith("spotify:episode:"):
        result["episode_id"] = url.split(":")[-1]
        result["type"] = "episode"
        return result

    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    if len(path_parts) >= 2:
        content_type = path_parts[0]
        content_id = path_parts[1].split("?")[0]

        if content_type == "episode":
            result["episode_id"] = content_id
            result["type"] = "episode"
        elif content_type == "show":
            result["show_id"] = content_id
            result["type"] = "show"

    return result


def get_show_id_from_episode(episode_id: str) -> str:
    """Get Show ID from Episode ID via embed page"""
    url = f"https://open.spotify.com/embed/episode/{episode_id}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        html_text = response.text

        patterns = [
            r'"showUri":"spotify:show:([a-zA-Z0-9]{22})"',
            r'spotify:show:([a-zA-Z0-9]{22})',
            r'/show/([a-zA-Z0-9]{22})',
        ]

        for pattern in patterns:
            match = re.search(pattern, html_text)
            if match:
                return match.group(1)
    except:
        pass

    return None


def get_podcast_info_from_spotify(episode_id: str) -> dict:
    """Get podcast info from Spotify embed page"""
    info = {
        "episode_title": None,
        "show_title": None,
        "show_id": None,
        "duration": None
    }

    try:
        embed_url = f"https://open.spotify.com/embed/episode/{episode_id}"
        response = requests.get(embed_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        html = response.text

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html)
        if match:
            data = json.loads(match.group(1))
            entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})

            if entity:
                info["episode_title"] = entity.get('name') or entity.get('title')
                info["show_title"] = entity.get('subtitle')
                info["duration"] = entity.get('duration')

                related_uri = entity.get('relatedEntityUri', '')
                if 'spotify:show:' in related_uri:
                    info["show_id"] = related_uri.split(':')[-1]

        if not info["show_id"]:
            match = re.search(r'spotify:show:([a-zA-Z0-9]{22})', html)
            if match:
                info["show_id"] = match.group(1)
    except:
        pass

    return info


def get_rss_from_itunes(podcast_name: str) -> str:
    """Search iTunes for real RSS feed"""
    try:
        encoded_name = quote(podcast_name)
        itunes_url = f"https://itunes.apple.com/search?term={encoded_name}&media=podcast&entity=podcast&limit=5"

        response = requests.get(itunes_url, timeout=15)
        data = response.json()

        results = data.get('results', [])
        if not results:
            return None

        podcast_name_lower = podcast_name.lower().strip()
        for result in results:
            name = result.get('collectionName', '').lower().strip()
            feed_url = result.get('feedUrl')

            if feed_url and (podcast_name_lower in name or name in podcast_name_lower):
                return feed_url

        return results[0].get('feedUrl')
    except:
        pass

    return None


def fetch_rss_feed(rss_url: str):
    """Fetch and parse RSS feed"""
    try:
        response = requests.get(rss_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return feedparser.parse(response.content)
    except:
        return None


def find_episode_in_rss(feed, episode_id: str, episode_title: str = None) -> dict:
    """Find specific episode in RSS feed"""
    if not feed or not feed.entries:
        return None

    def extract_episode_data(entry):
        data = {
            "title": entry.get('title', 'unknown'),
            "mp3_url": None,
            "duration": entry.get('itunes_duration', ''),
            "safe_title": sanitize_filename(entry.get('title', 'unknown'))
        }

        for enc in entry.get('enclosures', []):
            enc_type = enc.get('type', '')
            enc_url = enc.get('href', '') or enc.get('url', '')
            if 'audio' in enc_type or enc_url.endswith('.mp3') or 'mp3' in enc_url:
                data["mp3_url"] = enc_url
                break

        if not data["mp3_url"]:
            for link in entry.get('links', []):
                link_type = link.get('type', '')
                link_url = link.get('href', '')
                if 'audio' in link_type or link_url.endswith('.mp3'):
                    data["mp3_url"] = link_url
                    break

        return data

    # Search by episode ID
    for entry in feed.entries:
        guid = entry.get('id', '') or entry.get('guid', '')
        if episode_id in str(guid):
            return extract_episode_data(entry)

        link = entry.get('link', '')
        if episode_id in str(link):
            return extract_episode_data(entry)

    # Search by title
    if episode_title:
        episode_title_lower = episode_title.lower().strip()
        for entry in feed.entries:
            entry_title = entry.get('title', '').lower().strip()
            if episode_title_lower == entry_title or episode_title_lower in entry_title or entry_title in episode_title_lower:
                return extract_episode_data(entry)

    return None


def sanitize_filename(name: str) -> str:
    """Clean filename from problematic characters"""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 100:
        name = name[:100]
    return name


def download_mp3(url: str, output_path: Path, progress_callback=None) -> bool:
    """Download MP3 file with progress"""
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=60)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        chunk_size = 8192

        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback and total_size:
                        percent = (downloaded / total_size) * 100
                        progress_callback(percent)

        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False


def transcribe_with_gemini(audio_path: Path, progress_callback=None, api_key: str = None) -> str:
    """Transcribe audio with Gemini API"""
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print("ERROR: No API key provided for transcription")
        return None

    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        # Copy to temp with ASCII name
        temp_dir = tempfile.mkdtemp()
        temp_path = Path(temp_dir) / "podcast_audio.mp3"
        shutil.copy2(audio_path, temp_path)

        if progress_callback:
            progress_callback("uploading")

        audio_file = client.files.upload(file=str(temp_path))

        # Wait for processing - check more frequently for speed
        while audio_file.state.name == "PROCESSING":
            time.sleep(0.5)  # Faster polling
            audio_file = client.files.get(name=audio_file.name)

        if audio_file.state.name == "FAILED":
            shutil.rmtree(temp_dir)
            return None

        if progress_callback:
            progress_callback("transcribing")

        prompt = """תמלל את קובץ האודיו הזה בעברית במדויק.

**חשוב מאוד:**
- תמלול מילה במילה - אל תדלג, אל תקצר, אל תסכם
- כל מה שנאמר חייב להופיע בתמלול

**פורמט:**
1. [MM:SS] - timestamp בתחילת כל קטע (כל דקה-שתיים)
2. [דובר X] - אם יש יותר מדובר אחד
3. פיסוק מלא - נקודות, פסיקים, סימני שאלה וקריאה
4. פסקאות - חלק לפסקאות לפי נושאים

**התחל לתמלל:**"""

        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=[prompt, audio_file]
        )

        # Cleanup
        try:
            client.files.delete(name=audio_file.name)
        except:
            pass

        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        return response.text

    except Exception as e:
        print(f"Transcription error: {e}")
        return None


# =============================================================================
# Topic Extraction & Post Generation (Explain Protocol)
# =============================================================================

TAPLINK_URL = "https://dvirlinkai.taplink.ws/"
WHATSAPP_GROUP_LINK = "https://chat.whatsapp.com/DIyQ3SPLVb93oHqajN0B5S?mode=hqrc"
PHONE_NUMBER = "0585777177"

# Full signature for posts
POST_SIGNATURE = """
אם אהבתם וקיבלתם ערך מהפוסט - 🌹 יעשה לי את היום

להזמנת לליווי אישי, הרצאה, סדנה או ייעוץ להטמעת כלי בינה מלאכותית ואוטומציות אצלכם בארגון צרו איתי קשר בווצאפ - 0585777177 👂
יש לכם.ן חברים שעדיין לא בקבוצות בהן אני שולח תכנים נגישים על בינה מלאכותית? שלחו להם שיצטרפו 👇🏼
https://chat.whatsapp.com/DIyQ3SPLVb93oHqajN0B5S?mode=hqrc
דביר - ExplAIn לומדים בינה מלאכותית בגובה האוזניים 👂 ובכיף.

https://dvirlinkai.taplink.ws/ 🌹
"""

def extract_topics_from_transcript(transcript: str, api_key: str = None) -> list:
    """
    מנתח את התמלול לעומק ומחלץ נושאים לפוסטים.
    ניתוח מקצועי של התוכן לזיהוי תובנות, טיפים ורעיונות.
    """
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: No API key found!")
        return []

    print(f"Starting topic extraction... Transcript length: {len(transcript)} chars")

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        # Use a focused portion of the transcript
        transcript_to_analyze = transcript[:30000] if len(transcript) > 30000 else transcript

        prompt = f"""אתה מנתח תוכן מקצועי עם ניסיון בשיווק דיגיטלי ובינה מלאכותית.

המשימה: לנתח תמלול פודקאסט ולמצוא נושאים שיהפכו לפוסטים *שוברי שגרה* לקהילת וואטסאפ ישראלית.

## קהל היעד:
- ישראלים שמתעניינים ב-AI
- לא טכניים בהכרח - אנשים רגילים שרוצים ללמוד
- מחפשים ערך מיידי ופרקטי
- אוהבים תוכן "בגובה העיניים" - לא אקדמי

## איך לנתח את התמלול:

**שלב 1 - קרא את כל התמלול**

**שלב 2 - סמן לעצמך רגעי "וואו":**
- מתי הדובר אמר משהו מפתיע?
- מתי היה טיפ ספציפי וברור?
- מתי הוזכר כלי או שיטה ספציפית?
- מתי הייתה דוגמה מעניינה מהשטח?
- מתי הדובר הזהיר מטעות נפוצה?

**שלב 3 - לכל רגע "וואו" תשאל:**
- האם זה ספציפי מספיק? (לא "AI עוזר" אלא "הפרומפט הזה ב-Claude חוסך שעה")
- האם הקורא יכול לעשות עם זה משהו?
- האם זה מעניין מספיק שאנשים ישתפו?
- האם זה לא מידע שכולם כבר יודעים?

## סוגי נושאים לחפש:

🔥 **Hacks & Tricks** - טריקים ספציפיים, קיצורים, שיטות
💡 **תובנות** - הבנות חדשות, זוויות מפתיעות
⚠️ **טעויות** - מה לא לעשות, מלכודות נפוצות
🛠️ **כלים** - כלי AI ספציפיים + איך להשתמש
📈 **מגמות** - לאן הולך התחום, תחזיות
💬 **דילמות** - שאלות מעניינות לדיון

## פורמט הפלט - JSON בלבד:

```json
{{
  "topics": [
    {{
      "title": "כותרת קצרה שתופסת את העין (עד 10 מילים)",
      "summary": "הסבר מדויק של הנושא ב-2-3 משפטים - מה הערך? מה הפרקטיקה?",
      "quote": "ציטוט מילולי מהתמלול שממחיש את הנושא (או null)",
      "why_interesting": "למה קהילת AI תתעניין? מה הכאב/רווח?",
      "key_points": ["נקודה מפתח 1", "נקודה מפתח 2", "נקודה מפתח 3"],
      "hook_idea": "רעיון לפתיחה שתעצור את הגלילה - פרובוקטיבי, מבטיח ערך, או מעורר סקרנות"
    }}
  ]
}}
```

## התמלול:

{transcript_to_analyze}

## הנחיות אחרונות:
- אל תמציא - רק דברים שבאמת מופיעים בתמלול
- עדיף 3 נושאים מעולים מ-7 נושאים רדודים
- ה-hook_idea צריך להיות ספציפי ופרובוקטיבי
- החזר רק JSON, בלי הסברים"""

        print("Sending request to Gemini...")
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=[prompt]
        )

        response_text = response.text
        print(f"Got response from Gemini. Length: {len(response_text)} chars")

        # Debug: print first 500 chars of response
        print(f"Response preview: {response_text[:500]}...")

        # Extract JSON from response
        json_str = None

        # Try to find JSON in markdown code block first
        json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            print("Found JSON in markdown code block")
        else:
            # Try to find raw JSON
            json_match = re.search(r'\{[\s\S]*"topics"[\s\S]*\}', response_text)
            if json_match:
                json_str = json_match.group(0)
                print("Found raw JSON")
            else:
                print(f"ERROR: Could not find JSON in response: {response_text[:1000]}")
                return []

        # Parse JSON
        try:
            data = json.loads(json_str)
            topics = data.get("topics", [])
            print(f"Successfully parsed {len(topics)} topics")
            return topics
        except json.JSONDecodeError as je:
            print(f"JSON parse error: {je}")
            print(f"Problematic JSON: {json_str[:500]}...")
            return []

    except Exception as e:
        print(f"Topic extraction error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return []


def generate_post_for_topic(topic: dict, podcast_name: str, episode_name: str, api_key: str = None) -> dict:
    """
    יוצר פוסט בסגנון Explain לפי הפרוטוקול המלא.
    מבנה AIDA + כל החוקים.
    """
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        # Get hook idea if available from topic extraction
        hook_idea = topic.get('hook_idea', '')
        hook_section = f"\nרעיון להוק: {hook_idea}" if hook_idea else ""

        prompt = f"""אתה דביר מ-ExplAIn. כתוב פוסט וואטסאפ מפורט ואיכותי.

**הנושא:**
{topic.get('title', '')}
{topic.get('summary', '')}
{f"ציטוט מהתמלול: {topic.get('quote')}" if topic.get('quote') else ""}{hook_section}

**מי אתה - דביר:**
- מומחה AI שמדבר בגובה העיניים, לא מתנשא
- מסביר הכל בפשטות, כמו שמדברים בחיי היום-יום
- לא יוצא מנקודת הנחה שאנשים יודעים - מסביר כל שלב
- נותן ערך אמיתי ופרקטי
- כותב פוסטים ארוכים ומפורטים - אנשים קוראים!

**מבנה הפוסט (חובה!):**

📌 **כותרת:**
פורמט: 🛑 [כותרת קליטה] 🛑
הכותרת צריכה למשוך - לספר מה יקבלו (למשל: "3 טיפים ל-X שאתם חייבים להכיר")

📌 **פתיחה (2-3 משפטים):**
שאלה או אמירה שיוצרת הזדהות
דוגמה: "חשבתם שאתם מכירים את X? היום אני רוצה לצלול איתכם לניואנסים הקטנים."

📌 **גוף הפוסט - סעיפים ממוספרים:**
כל סעיף במבנה:
[אימוג'י] [מספר]. "[כותרת משנה קליטה]"
[הסבר מפורט - מה הבעיה? מה הפתרון? איך עושים את זה צעד אחר צעד?]
[טיפ ספציפי שאפשר ליישם]

דוגמה לסעיף:
📊 1. "למה הוא לא נותן לי להעלות אקסל?!"
מכירים את זה שאתם מנסים לגרור קובץ Excel והוא מסרב? מתסכל.
הפתרון הפשוט: תעלו את האקסל ל-Google Sheets, שמרו, ואז בתוך הכלי תבחרו ב"Google Drive" -> "Sheets".
זה עובד חלק, והוא קורא את הנתונים מעולה.

📌 **בונוס (אופציונלי):**
💎 בונוס למתקדמים: [טיפ נוסף מתקדם]

📌 **סיום:**
- קריאה לפעולה: "יאללה, לכו לנסות ותגידו לי איך עבד לכם 👇"
- או: "קשה? ממש לא. לכו לנסות!"

📌 **חתימה קבועה (בדיוק ככה!):**
{POST_SIGNATURE}

**חוקים קריטיים:**
1. פוסט ארוך ומפורט - תסביר הכל צעד אחר צעד
2. אל תצא מנקודת הנחה שאנשים יודעים - תסביר איך מגיעים לכל דבר
3. הדגשות = *כוכבית אחת* בלבד (לא שתיים!)
4. אימוג'ים לסימון סעיפים: 📊 🗑️ ⚙️ 💡 💎 ⚠️
5. כותרות משנה במירכאות: "למה זה חשוב?"
6. שפה פשוטה, כמו שמדברים עם חבר
7. דוגמאות קונקרטיות - לא תיאוריה יבשה

**כתוב את הפוסט:**"""

        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=[prompt]
        )

        post_text = response.text

        # Now generate infographic prompt using AI for better results
        infographic_prompt = generate_infographic_prompt_with_ai(topic, podcast_name, episode_name, api_key)

        return {
            "post": post_text,
            "infographic_prompt": infographic_prompt,
            "topic_title": topic.get('title', '')
        }

    except Exception as e:
        print(f"Post generation error: {e}")
        return None


def generate_infographic_prompt(topic: dict, podcast_name: str) -> str:
    """
    יוצר פרומפט לאינפוגרפיקה עבור Nano Banana בפורמט ExplAIn.
    צבעי זהב-שחור, פורמט 4:5, RTL, 3 שלבים מימין לשמאל.
    """
    title = topic.get('title', 'נושא')
    summary = topic.get('summary', '')
    key_points = topic.get('key_points', [])

    # יצירת תיאורי שלבים מנקודות המפתח
    steps_description = ""
    if key_points and len(key_points) >= 3:
        steps_description = f"""
The visual is divided into 3 steps, flowing from **Right to Left**:

1. **RIGHT Side:** Icon representing the first concept. Text in Hebrew: "{key_points[0]}".

2. **CENTER:** Icon representing the second concept. Text in Hebrew: "{key_points[1]}".

3. **LEFT Side:** Icon representing the third concept/result. Text in Hebrew: "{key_points[2]}".
"""
    else:
        # אם אין מספיק נקודות, ליצור מבנה גנרי מהתקציר
        steps_description = f"""
The visual is divided into 3 connected sections, flowing from **Right to Left**:

1. **RIGHT Side (The Challenge/Question):** Icon of a question mark or thinking person. Text in Hebrew describing the problem/question.

2. **CENTER (The Method/Tool):** Icon of a gear, lightbulb, or tool. Text in Hebrew describing the approach/solution.

3. **LEFT Side (The Result/Value):** Icon of a checkmark, star, or trophy. Text in Hebrew describing the outcome/benefit.

Content to visualize: {summary}
"""

    prompt = f"""צור תמונה

Infographic, vertical 4:5 aspect ratio. Dark premium background with subtle tech/circuit patterns.
Top Center: "ExplAIn" logo in Gold.
Main Title in Hebrew (Gold): "{title}".
Subtitle in White Hebrew: תובנות מפודקאסט {podcast_name}.
{steps_description}
Bottom: Small golden text: "קהילת ExplAIn | לומדים AI יחד".
Style: Clean, tech-oriented, professional. Strong visual hierarchy from Right to Left (RTL). Gold (#D4AF37) accents on dark background (#0A0A0A). White text for descriptions. Minimalist icons in line-art style."""

    return prompt


def generate_infographic_prompt_with_ai(topic: dict, podcast_name: str, episode_name: str, api_key: str = None) -> str:
    """
    יוצר פרומפט לאינפוגרפיקה באמצעות Gemini - מותאם אישית לנושא.
    """
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return generate_infographic_prompt(topic, podcast_name)

    title = topic.get('title', 'נושא')
    summary = topic.get('summary', '')

    system_prompt = """אתה מומחה ביצירת פרומפטים לאינפוגרפיקות בסגנון ExplAIn.

הפורמט הקבוע שאתה חייב לעקוב אחריו:

```
צור תמונה

Infographic, vertical 4:5 aspect ratio. Dark premium background with subtle tech/circuit patterns.
Top Center: "ExplAIn" logo in Gold.
Main Title in Hebrew (Gold): "[כותרת בעברית]".
Subtitle in White Hebrew: "[תת-כותרת בעברית]".

The visual is divided into 3 steps, flowing from **Right to Left**:

1. **RIGHT Side:** [תיאור אייקון מתאים]. Text in Hebrew: "[טקסט קצר בעברית]".

2. **CENTER:** [תיאור אייקון מתאים]. Text in Hebrew: "[טקסט קצר בעברית]".

3. **LEFT Side:** [תיאור אייקון מתאים]. Text in Hebrew: "[טקסט קצר בעברית]".

Bottom: Small golden text: "[טקסט ייחוס] | קהילת ExplAIn".
Style: Clean, tech-oriented, professional. Strong visual hierarchy from Right to Left (RTL). Gold accents on dark background.
```

דוגמאות לאייקונים טובים:
- Icon of a Lightbulb / Brain / Gear (לרעיונות/חשיבה)
- Icon of a Magnifying glass / Search (לחקירה/חיפוש)
- Icon of Multiple Documents merging (לאיחוד מידע)
- Icon of a Robot / AI chip (לבינה מלאכותית)
- Icon of a Diamond / Star / Trophy (לתוצאה/הצלחה)
- Icon of a Rocket / Arrow (לצמיחה/התקדמות)
- Icon of a Shield / Lock (לאבטחה)
- Icon of a Chat bubble / People (לתקשורת)

חשוב:
1. הכותרות והטקסטים חייבים להיות בעברית
2. הזרימה תמיד מימין לשמאל (RTL)
3. האייקונים צריכים להיות רלוונטיים לתוכן
4. השלבים צריכים לספר סיפור הגיוני"""

    user_prompt = f"""צור פרומפט לאינפוגרפיקה עבור הנושא הבא:

**נושא:** {title}
**תקציר:** {summary}
**פודקאסט:** {podcast_name}
**פרק:** {episode_name}

צור פרומפט שמציג את הנושא ב-3 שלבים ברורים עם אייקונים מתאימים.
החזר רק את הפרומפט המוכן, בלי הסברים נוספים."""

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-pro")

        response = model.generate_content(
            f"{system_prompt}\n\n---\n\n{user_prompt}",
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=1000,
            )
        )

        return response.text.strip()
    except Exception as e:
        print(f"Error generating AI infographic prompt: {e}")
        return generate_infographic_prompt(topic, podcast_name)


# =============================================================================
# Background Processing
# =============================================================================

def process_podcast_job(job_id: str, spotify_url: str, api_key: str = None):
    """Process podcast in background"""
    job = jobs[job_id]
    # Use provided api_key or get from job
    if not api_key:
        api_key = job.get('api_key')

    try:
        # Step 1: Extract IDs
        job["status"] = "extracting"
        job["message"] = "מחלץ מידע מהלינק..."

        ids = extract_spotify_ids(spotify_url)
        if not ids["episode_id"]:
            job["status"] = "error"
            job["message"] = "לא הצלחתי לחלץ Episode ID מהלינק"
            return

        job["episode_id"] = ids["episode_id"]

        # Step 2: Get podcast info
        job["status"] = "searching"
        job["message"] = "מחפש את הפודקאסט..."

        show_id = ids.get("show_id") or get_show_id_from_episode(ids["episode_id"])
        if not show_id:
            job["status"] = "error"
            job["message"] = "לא הצלחתי למצוא את הפודקאסט. ייתכן שזה Spotify Exclusive."
            return

        podcast_info = get_podcast_info_from_spotify(ids["episode_id"])
        job["episode_title"] = podcast_info.get("episode_title", "Unknown")
        job["show_title"] = podcast_info.get("show_title", "Unknown")

        # Step 3: Find RSS feed
        job["status"] = "finding_rss"
        job["message"] = "מחפש RSS feed..."

        rss_url = None
        if podcast_info.get("show_title"):
            rss_url = get_rss_from_itunes(podcast_info["show_title"])

        if not rss_url:
            rss_url = f"https://spotifeed.timdorr.com/{show_id}"

        feed = fetch_rss_feed(rss_url)
        if not feed or not feed.entries:
            job["status"] = "error"
            job["message"] = "לא הצלחתי לקבל RSS feed"
            return

        show_title = feed.feed.get('title', podcast_info.get("show_title", "podcast"))

        # Step 4: Find episode
        job["status"] = "finding_episode"
        job["message"] = "מחפש את הפרק..."

        episode = find_episode_in_rss(feed, ids["episode_id"], podcast_info.get("episode_title"))
        if not episode or not episode.get("mp3_url"):
            job["status"] = "error"
            job["message"] = "לא הצלחתי למצוא את הפרק או את קובץ ה-MP3"
            return

        # Step 5: Download MP3
        job["status"] = "downloading"
        job["message"] = "מוריד את הפודקאסט..."
        job["download_progress"] = 0

        date_str = datetime.now().strftime("%Y%m%d")
        safe_show = sanitize_filename(show_title)[:30]
        safe_episode = episode["safe_title"][:50]
        mp3_filename = f"{date_str}_{safe_show}_{safe_episode}.mp3"
        mp3_path = DOWNLOADS_DIR / mp3_filename

        def download_progress(percent):
            job["download_progress"] = percent

        if not download_mp3(episode["mp3_url"], mp3_path, download_progress):
            job["status"] = "error"
            job["message"] = "שגיאה בהורדת הקובץ"
            return

        job["mp3_path"] = str(mp3_path)
        job["mp3_filename"] = mp3_filename

        # Step 6: Transcribe
        job["status"] = "transcribing"
        job["message"] = "מתמלל את הפודקאסט... (זה יכול לקחת כמה דקות)"

        def transcribe_progress(stage):
            if stage == "uploading":
                job["message"] = "מעלה קובץ לשרת..."
            elif stage == "transcribing":
                job["message"] = "מתמלל... אנא המתן"

        transcript = transcribe_with_gemini(mp3_path, transcribe_progress, api_key)

        if transcript:
            # Save transcript
            transcript_filename = f"{date_str}_{safe_show}_{safe_episode}_transcript.txt"
            transcript_path = TRANSCRIPTS_DIR / transcript_filename

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

            job["transcript_path"] = str(transcript_path)
            job["transcript_filename"] = transcript_filename
            job["transcript_preview"] = transcript[:500] + "..." if len(transcript) > 500 else transcript

        job["status"] = "completed"
        job["message"] = "הושלם בהצלחה!"

    except Exception as e:
        job["status"] = "error"
        job["message"] = f"שגיאה: {str(e)}"


# =============================================================================
# Flask Routes
# =============================================================================

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/set-api-key', methods=['POST'])
def set_api_key():
    """Set the user's API key for the session"""
    data = request.get_json()
    api_key = data.get('api_key', '').strip()

    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    if not api_key.startswith('AIza'):
        return jsonify({"error": "מפתח API לא תקין. המפתח צריך להתחיל ב-AIza"}), 400

    session['api_key'] = api_key
    return jsonify({"success": True, "message": "API key saved successfully"})


@app.route('/api/check-api-key', methods=['GET'])
def check_api_key():
    """Check if API key is configured"""
    api_key = get_api_key()
    has_key = bool(api_key)
    # Don't reveal the full key, just indicate if it exists
    return jsonify({
        "has_key": has_key,
        "source": "session" if 'api_key' in session else ("environment" if api_key else "none")
    })


@app.route('/api/process', methods=['POST'])
def start_processing():
    """Start processing a podcast URL"""
    data = request.get_json()
    spotify_url = data.get('url', '').strip()

    # Validate URL
    if not spotify_url:
        return jsonify({"error": "URL is required"}), 400

    if "spotify.com" not in spotify_url and not spotify_url.startswith("spotify:"):
        return jsonify({"error": "זה לא נראה כמו לינק ספוטיפיי"}), 400

    if "/episode/" not in spotify_url and ":episode:" not in spotify_url:
        return jsonify({"error": "זה נראה כמו לינק של פודקאסט שלם, לא של פרק. אני צריך לינק של פרק ספציפי."}), 400

    # Get API key for this job
    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "נדרש מפתח API. הזן את המפתח שלך בהגדרות."}), 400

    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "url": spotify_url,
        "status": "starting",
        "message": "מתחיל...",
        "created_at": datetime.now().isoformat(),
        "episode_title": None,
        "show_title": None,
        "mp3_path": None,
        "transcript_path": None,
        "download_progress": 0,
        "api_key": api_key  # Store API key for background processing
    }

    # Start background processing
    thread = threading.Thread(target=process_podcast_job, args=(job_id, spotify_url, api_key))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route('/api/status/<job_id>')
def get_status(job_id):
    """Get job status"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job)


@app.route('/api/download/<job_id>/<file_type>')
def download_file(job_id, file_type):
    """Download MP3 or transcript"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if file_type == "mp3" and job.get("mp3_path"):
        return send_file(
            job["mp3_path"],
            as_attachment=True,
            download_name=job["mp3_filename"]
        )
    elif file_type == "transcript" and job.get("transcript_path"):
        return send_file(
            job["transcript_path"],
            as_attachment=True,
            download_name=job["transcript_filename"]
        )

    return jsonify({"error": "File not found"}), 404


@app.route('/api/transcript/<job_id>')
def get_transcript(job_id):
    """Get full transcript text"""
    job = jobs.get(job_id)
    if not job or not job.get("transcript_path"):
        return jsonify({"error": "Transcript not found"}), 404

    try:
        with open(job["transcript_path"], 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({"transcript": content})
    except:
        return jsonify({"error": "Could not read transcript"}), 500


@app.route('/api/extract-topics/<job_id>', methods=['POST'])
def extract_topics(job_id):
    """Extract topics from transcript for post generation"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if not job.get("transcript_path"):
        return jsonify({"error": "No transcript available"}), 400

    # Check if topics already extracted
    if job.get("topics"):
        return jsonify({"topics": job["topics"]})

    try:
        with open(job["transcript_path"], 'r', encoding='utf-8') as f:
            transcript = f.read()

        # Get API key from job or request
        api_key = job.get('api_key') or get_api_key()
        if not api_key:
            return jsonify({"error": "נדרש מפתח API"}), 400

        topics = extract_topics_from_transcript(transcript, api_key)

        if not topics:
            return jsonify({"error": "לא הצלחתי לחלץ נושאים מהתמלול"}), 500

        # Store topics in job
        job["topics"] = topics

        return jsonify({"topics": topics})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate-post/<job_id>/<int:topic_index>', methods=['POST'])
def generate_single_post(job_id, topic_index):
    """Generate a post for a specific topic"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    topics = job.get("topics", [])
    if topic_index >= len(topics):
        return jsonify({"error": "Topic not found"}), 404

    topic = topics[topic_index]

    # Check if post already generated
    if "posts" not in job:
        job["posts"] = {}

    if str(topic_index) in job["posts"]:
        return jsonify(job["posts"][str(topic_index)])

    # Get API key from job or request
    api_key = job.get('api_key') or get_api_key()
    if not api_key:
        return jsonify({"error": "נדרש מפתח API"}), 400

    result = generate_post_for_topic(
        topic,
        job.get("show_title", "פודקאסט"),
        job.get("episode_title", "פרק"),
        api_key
    )

    if not result:
        return jsonify({"error": "לא הצלחתי ליצור פוסט"}), 500

    # Store generated post
    job["posts"][str(topic_index)] = result

    return jsonify(result)


@app.route('/api/generate-all-posts/<job_id>', methods=['POST'])
def generate_all_posts(job_id):
    """Generate posts for all topics"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    topics = job.get("topics", [])
    if not topics:
        return jsonify({"error": "No topics found. Extract topics first."}), 400

    # Get API key from job or request
    api_key = job.get('api_key') or get_api_key()
    if not api_key:
        return jsonify({"error": "נדרש מפתח API"}), 400

    if "posts" not in job:
        job["posts"] = {}

    results = []

    for i, topic in enumerate(topics):
        # Skip if already generated
        if str(i) in job["posts"]:
            results.append(job["posts"][str(i)])
            continue

        result = generate_post_for_topic(
            topic,
            job.get("show_title", "פודקאסט"),
            job.get("episode_title", "פרק"),
            api_key
        )

        if result:
            job["posts"][str(i)] = result
            results.append(result)
        else:
            results.append({"error": f"Failed to generate post for topic {i}"})

    return jsonify({"posts": results})


@app.route('/api/generate-image', methods=['POST'])
def generate_image():
    """Generate infographic image using nano-banana-poster skill"""
    data = request.json
    prompt = data.get('prompt', '')
    job_id = data.get('job_id', '')
    topic_index = data.get('topic_index', 0)

    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    print(f"[IMAGE] Starting image generation...")
    print(f"[IMAGE] Prompt length: {len(prompt)} chars")

    try:
        # Use nano-banana-poster skill for image generation
        nano_banana_dir = Path.home() / ".claude" / "skills" / "nano-banana-poster" / "scripts"

        # Create images directory if not exists
        images_dir = SCRIPT_DIR / "static" / "generated_images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"infographic_{job_id}_{topic_index}_{timestamp}.jpg"

        # Clean prompt - remove problematic characters
        clean_prompt = prompt.replace('"', "'").replace('\n', ' ').strip()
        print(f"[IMAGE] Clean prompt: {clean_prompt[:200]}...")

        # Run nano-banana-poster with 2:3 aspect ratio (vertical for WhatsApp)
        cmd = [
            "npx", "ts-node", "generate_poster.ts",
            "--aspect", "2:3",
            clean_prompt
        ]

        print(f"[IMAGE] Running command in: {nano_banana_dir}")
        result = subprocess.run(
            cmd,
            cwd=str(nano_banana_dir),
            capture_output=True,
            text=True,
            timeout=120  # 2 minutes timeout for image generation
        )
        print(f"[IMAGE] Command finished. Return code: {result.returncode}")
        print(f"[IMAGE] stdout: {result.stdout[:500] if result.stdout else 'empty'}")
        print(f"[IMAGE] stderr: {result.stderr[:500] if result.stderr else 'empty'}")

        if result.returncode == 0:
            # Find the generated image (poster_0.jpg)
            generated_file = nano_banana_dir / "poster_0.jpg"

            if generated_file.exists():
                # Move to our images directory
                filepath = images_dir / filename
                shutil.move(str(generated_file), str(filepath))

                # Return URL to the image
                image_url = f"/static/generated_images/{filename}"

                return jsonify({
                    "success": True,
                    "image_url": image_url,
                    "filename": filename
                })
            else:
                return jsonify({"error": "התמונה נוצרה אך לא נמצאה. נסה שוב."}), 500
        else:
            error_output = result.stderr or result.stdout
            print(f"Image generation error: {error_output}")
            return jsonify({"error": f"שגיאה ביצירת התמונה: {error_output[:200]}"}), 500

    except subprocess.TimeoutExpired:
        return jsonify({"error": "יצירת התמונה לקחה יותר מדי זמן. נסה שוב."}), 500
    except Exception as e:
        error_msg = str(e)
        print(f"Image generation error: {error_msg}")
        return jsonify({"error": f"שגיאה ביצירת התמונה: {error_msg}"}), 500


@app.route('/static/generated_images/<filename>')
def serve_generated_image(filename):
    """Serve generated images"""
    images_dir = SCRIPT_DIR / "static" / "generated_images"
    return send_file(images_dir / filename, mimetype='image/png')


# =============================================================================
# WhatsApp Integration
# =============================================================================

@app.route('/api/whatsapp/send-message', methods=['POST'])
def send_whatsapp_message():
    """Send a text message to WhatsApp"""
    data = request.json
    phone = data.get('phone', '')
    message = data.get('message', '')
    group_id = data.get('group_id', '')

    if not message:
        return jsonify({"error": "No message provided"}), 400

    if not phone and not group_id:
        return jsonify({"error": "No phone or group_id provided"}), 400

    try:
        # Build command
        script_path = WHATSAPP_SCRIPTS_DIR / "send-message.ts"
        cmd = ["npx", "ts-node", str(script_path)]

        if group_id:
            cmd.extend(["--group", group_id])
        else:
            cmd.extend(["--phone", phone])

        cmd.extend(["--message", message])

        # Run the script
        result = subprocess.run(
            cmd,
            cwd=str(WHATSAPP_SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            return jsonify({
                "success": True,
                "output": result.stdout
            })
        else:
            return jsonify({
                "error": result.stderr or result.stdout or "Failed to send message"
            }), 500

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout sending message"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/whatsapp/send-image', methods=['POST'])
def send_whatsapp_image():
    """Send an image to WhatsApp"""
    data = request.json
    phone = data.get('phone', '')
    group_id = data.get('group_id', '')
    image_path = data.get('image_path', '')
    caption = data.get('caption', '')

    if not image_path:
        return jsonify({"error": "No image path provided"}), 400

    if not phone and not group_id:
        return jsonify({"error": "No phone or group_id provided"}), 400

    # Convert relative path to absolute if needed
    if image_path.startswith('/static/'):
        image_path = str(SCRIPT_DIR / image_path.lstrip('/'))

    if not os.path.exists(image_path):
        return jsonify({"error": f"Image not found: {image_path}"}), 400

    try:
        # Build command
        script_path = WHATSAPP_SCRIPTS_DIR / "send-image.ts"
        cmd = ["npx", "ts-node", str(script_path)]

        if group_id:
            cmd.extend(["--phone", group_id])  # send-image uses --phone for both
        else:
            cmd.extend(["--phone", phone])

        cmd.extend(["--image", image_path])

        if caption:
            cmd.extend(["--caption", caption])

        # Run the script
        result = subprocess.run(
            cmd,
            cwd=str(WHATSAPP_SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            return jsonify({
                "success": True,
                "output": result.stdout
            })
        else:
            return jsonify({
                "error": result.stderr or result.stdout or "Failed to send image"
            }), 500

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout sending image"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/whatsapp/send-post', methods=['POST'])
def send_whatsapp_post():
    """Send a complete post (image + text as caption) to WhatsApp - ONE message per group"""
    data = request.json
    post_text = data.get('post', '')
    image_path = data.get('image_path', '')
    send_to_all = data.get('send_to_all', True)  # Default: send to all configured groups

    if not post_text and not image_path:
        return jsonify({"error": "No content to send"}), 400

    # Convert image path to absolute if needed
    abs_image_path = None
    if image_path:
        if image_path.startswith('/static/'):
            abs_image_path = str(SCRIPT_DIR / image_path.lstrip('/'))
        elif image_path.startswith('http'):
            abs_image_path = None  # Skip URL images for now
        else:
            abs_image_path = image_path

        if abs_image_path and not os.path.exists(abs_image_path):
            return jsonify({"error": f"Image not found: {abs_image_path}"}), 400

    results = []

    # Send to all configured groups
    for group in WHATSAPP_GROUPS:
        group_id = group["id"]
        group_name = group["name"]

        try:
            if abs_image_path:
                # Send image with caption (ONE message)
                script_path = WHATSAPP_SCRIPTS_DIR / "send-image.ts"
                cmd = ["npx", "ts-node", str(script_path), "--phone", group_id, "--image", abs_image_path]

                if post_text:
                    cmd.extend(["--caption", post_text])

                result = subprocess.run(
                    cmd,
                    cwd=str(WHATSAPP_SCRIPTS_DIR),
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                results.append({
                    "group": group_name,
                    "success": result.returncode == 0,
                    "type": "image+caption"
                })

            else:
                # Text only
                script_path = WHATSAPP_SCRIPTS_DIR / "send-message.ts"
                cmd = ["npx", "ts-node", str(script_path), "--group", group_id, "--message", post_text]

                result = subprocess.run(
                    cmd,
                    cwd=str(WHATSAPP_SCRIPTS_DIR),
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                results.append({
                    "group": group_name,
                    "success": result.returncode == 0,
                    "type": "text"
                })

            # Small delay between groups to avoid rate limiting
            time.sleep(1)

        except Exception as e:
            results.append({
                "group": group_name,
                "success": False,
                "error": str(e)
            })

    # Check overall success
    all_success = all(r.get("success", False) for r in results)

    return jsonify({
        "success": all_success,
        "results": results,
        "message": f"נשלח ל-{len([r for r in results if r['success']])} מתוך {len(WHATSAPP_GROUPS)} קבוצות"
    })


@app.route('/api/whatsapp/config', methods=['GET'])
def get_whatsapp_config():
    """Check if WhatsApp is configured"""
    env_path = WHATSAPP_SCRIPTS_DIR / ".env"
    is_configured = env_path.exists()

    return jsonify({
        "configured": is_configured,
        "scripts_dir": str(WHATSAPP_SCRIPTS_DIR)
    })


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("   Spotify Podcast Downloader + Transcription")
    print("   Web Interface")
    print("=" * 60)
    print("\n   Open in browser: http://localhost:5001\n")
    app.run(debug=True, host='0.0.0.0', port=5001)
