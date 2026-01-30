# Podcast to Post - ExplAIn

כלי AI להורדה ותמלול פודקאסטים מספוטיפיי, חילוץ נושאים ויצירת פוסטים לוואטסאפ.

## פריסה מהירה (Deploy)

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/podcast-to-post?referralCode=dvir)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Dvir1998/podcast-to-post)

## תכונות

- **הורדת פודקאסטים** - הורדה אוטומטית מספוטיפיי דרך RSS feeds
- **תמלול בעברית** - תמלול מדויק עם Google Gemini 2.5 Pro
- **חילוץ נושאים** - ניתוח מעמיק של התמלול לזיהוי נושאים לפוסטים
- **יצירת פוסטים** - כתיבה אוטומטית בסגנון AIDA
- **פרומפטים לאינפוגרפיקה** - יצירת פרומפטים לתמונות בסגנון ExplAIn

## התקנה מקומית

```bash
# Clone the repository
git clone https://github.com/Dvir1998/podcast-to-post.git
cd podcast-to-post

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

פתחו את הדפדפן ב-http://localhost:5001

## שימוש

1. **הזינו מפתח API** - קבלו מפתח חינם מ-[Google AI Studio](https://aistudio.google.com/app/apikey)
2. **הדביקו לינק** - העתיקו לינק של פרק ספציפי מספוטיפיי
3. **המתינו לתמלול** - התהליך אוטומטי
4. **חלצו נושאים** - לחצו על "חלץ נושאים"
5. **צרו פוסטים** - לחצו על "צור פוסט" לכל נושא

## דרישות

- Python 3.9+
- מפתח API של Google Gemini (חינם)

## טכנולוגיות

- **Backend:** Flask, Python
- **AI:** Google Gemini 2.5 Pro
- **Frontend:** HTML, CSS, JavaScript

## רישיון

MIT License

---

נבנה על ידי **דביר - ExplAIn** | לומדים בינה מלאכותית בגובה האוזניים
