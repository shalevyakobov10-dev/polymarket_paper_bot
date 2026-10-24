# פריסה ב-Railway — Paper Research Bot

## מצב הפרויקט
המאגר כולל שירות Python, Dockerfile, בדיקות יחידה, backtest היסטורי והגדרת Railway Infrastructure as Code ב-`.railway/railway.ts`. **השירות עדיין לא נפרס בענן**: יש להתחבר לחשבון Railway, לבדוק את תוכנית הפריסה ולאשר את יצירת השירות וה-Volume.

## פריסה עם תצורת התשתית שבמאגר

הקובץ `.railway/railway.ts` מגדיר שירות אחד בשם `polymarket-paper-bot` ממאגר GitHub הזה ומ-branch `main`, Volume בנפח 1GB, חיבור Volume אל `/app/data`, health check, וערכי סיכון קבועים ל-Paper Trading. הוא מציב את השירות ואת ה-Volume באזור `europe-west4`.

Railway ממליצה כיום על Infrastructure as Code דרך `.railway/railway.ts`; קובצי `railway.json`/`railway.toml` הישנים הוצאו משימוש עבור שירותים חדשים. ראו [Railway IaC](https://docs.railway.com/infrastructure-as-code) ו-[Reference](https://docs.railway.com/infrastructure-as-code/reference).

במחשב Windows עם Node.js 22 ומעלה ובגרסה עדכנית של Railway CLI:

1. שכפל את המאגר והיכנס לתיקייה:
   ```powershell
   git clone https://github.com/shalevyakobov10-dev/polymarket_paper_bot.git
   cd polymarket_paper_bot
   ```
2. התקן את חבילות התצורה:
   ```powershell
   npm install
   ```
3. התקן את [Railway CLI](https://docs.railway.com/cli/installation), ואז התחבר:
   ```powershell
   railway login
   ```
4. בחר או צור פרויקט Railway ייעודי וקשור את התיקייה:
   ```powershell
   railway link
   ```
5. **תחילה בצע תכנון בלבד** ובדוק שהוא מוסיף רק את שירות הבוט ואת ה-Volume הרצויים:
   ```powershell
   railway config plan
   ```
6. בדוק את התוכנית, את עלויות החשבון/Volume, וודא שהפרויקט הנכון נבחר. רק לאחר הבדיקה החל את התוכנית:
   ```powershell
   railway config apply
   ```
7. ב-Railway, הפק Domain ציבורי לשירות דרך Settings/Networking אם צריך גישה חיצונית. Domain שנוצר על ידי Railway מנוהל דרך הממשק, לא על ידי קובץ ה-IaC.

הקובץ מציב `DATA_DIR=/app/data`, `POLL_SECONDS=60`, סריקה של עד 400 שווקים פעילים ו-20 שווקים מנוטרים. הוא **לא** מפעיל מסחר אמיתי. לא נקבע סף 0.005, משום שה-backtest מצא שכל הספים שנבדקו הפסידו בתיק המדומה. ברירת המחדל המחקרית נשארת 0.03.

## אימות אחרי הפריסה

לאחר שהשירות פעיל, פתח את ה-Domain ובדוק:
- `/health` — בדיקת חיות בסיסית של HTTP.
- `/status` — מצב worker, זמן הסריקה האחרונה, כמות שווקים/תוצאות, שגיאה אחרונה וסיכום הסימולציה.
- `/paper` — יתרת הסימולציה, מדדי ביצועים ופוזיציות מדומות פתוחות.

`/health` אינו מוכיח שה-API החיצוני נגיש. ב-`/status` בדוק ש-`last_success_at` מתעדכן וש-`last_error` הוא `null`. אם מופיעה שגיאת DNS, timeout או rate limit, השירות עדיין עשוי להגיש HTTP אך לא לקבל נתוני שוק חדשים.

## בדיקת API ציבורי מתוך GitHub Actions

ה-workflow `Public API smoke check` מבצע קריאת read-only ל-Gamma ללא חשבון או מפתחות. הריצה האחרונה שנבדקה: [Public API smoke check #6](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40111586766). זו בדיקת API נקודתית, לא הוכחה שה-worker בענן פועל לאורך זמן.

## ריצת backtest היסטורית

```bash
python backtest.py --markets 20 --days 30 --fidelity 5 --train-ratio 0.7 --sensitivity --output backtest_report.json
```

הדוח מפריד כרונולוגית בין אימון לבדיקה, בוחן ארבעה ספים קבועים, ומודד תשואות קדימה לאחר אות מול baseline. לפי הריצה ההיסטורית שנבדקה, כל ארבעת הספים הפסידו בסימולציית התיק. ראו [דוח המחקר בעברית](research/2026-10-24-backtest-summary-he.md).

## ניהול נפח הנתונים

קובצי snapshots/signals/events מוגבלים באמצעות רוטציה אוטומטית. ברירות מחדל: snapshots ‏25MB ו-4 גיבויים; signals/events ‏5MB ו-3 גיבויים. ניתן לשנות דרך משתני `SNAPSHOT_MAX_MB`, `SNAPSHOT_BACKUPS`, `SIGNAL_LOG_MAX_MB`, `SIGNAL_LOG_BACKUPS`, `PAPER_EVENTS_MAX_MB`, `PAPER_EVENTS_BACKUPS`. יש עדיין לנטר את גודל ה-Volume.

## מגבלות בטיחות ומחקר
- השירות קורא מידע ציבורי בלבד ואינו שולח פקודות מסחר.
- האותות הם מחקריים ואינם אסטרטגיה מוכחת.
- המדגם ההיסטורי כולל רק שווקים שנבחרו מהמדגם הפעיל הנוכחי ועלול לסבול מהטיית שרידות/בחירה.
- העמלות בפועל ו-spread/slippage אמפיריים לא אומתו.
- אל תוסיף סיסמאות, API tokens, מפתחות פרטיים או פרטי ארנק למאגר או למשתני הסביבה.
- אל תטען שהשירות עובד 24/7 עד שנבדקו Domain חי, `last_success_at` מתעדכן והתמדה אחרי restart.
