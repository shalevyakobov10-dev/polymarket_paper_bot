# פריסה ב-Railway — Paper Research Bot

## מצב הפרויקט
המאגר כולל שירות Python, Dockerfile, בדיקות יחידה ו-workflow לבדיקות. **השירות עדיין לא נפרס בענן**, וצריך לבדוק בפועל את חיבור ה-API ואת סטטוס ה-build לאחר הפריסה.

## יצירת שירות
1. היכנס ל-[Railway](https://railway.com/) והתחבר.
2. צור פרויקט חדש ובחר Deploy from GitHub repo.
3. בחר `shalevyakobov10-dev/polymarket_paper_bot` ואת הענף `main`.
4. ודא ש-Railway בונה מה-Dockerfile בשורש המאגר.
5. הגדר משתני סביבה:
   - `DATA_DIR=/app/data`
   - `POLL_SECONDS=60`
   Railway בדרך כלל מגדיר בעצמו את `PORT`; אין צורך להגדיר אותו ידנית אלא אם פלטפורמת הפריסה דורשת זאת.
6. הוסף Volume לשירות ומפה אותו ל-`/app/data`, כדי שקובצי ה-JSONL ומצב הסימולציה יישמרו אחרי הפעלה מחדש.
7. ברירות המחדל של Paper Trading הן יתרת $100, פוזיציה עד $2, חשיפה פתוחה עד $10, עד 5 פוזיציות, stop-loss/take-profit מדומים של 10% וזמן החזקה מרבי של 24 שעות. ניתן לשנות הגדרות דרך משתני `PAPER_*` המתועדים ב-README.
8. בדוק את העלויות והגדרות השינה/זמינות של התוכנית לפני הפעלת שירות רציף. פריסה 24/7 עשויה להיות בתשלום.
9. בצע Deploy והמתן לתוצאת build וללוגים.

## אימות
לאחר שהשירות עלה, בדוק את:
- `/health` — בדיקת חיות בסיסית של HTTP.
- `/status` — מצב worker, זמן הסריקה האחרונה, כמות שווקים/תוצאות, שגיאה אחרונה וסיכום הסימולציה.
- `/paper` — יתרת הסימולציה, מדדי ביצועים ופוזיציות מדומות פתוחות; אין בו חשבון אמיתי או סודות.

`/health` אינו מוכיח שה-API החיצוני נגיש. ב-`/status` בדוק ש-`last_success_at` מתעדכן וש-`last_error` הוא `null`. אם מופיעה שגיאת DNS, timeout או rate limit, השירות עדיין עשוי להגיש HTTP אך לא לקבל נתוני שוק חדשים.

## מגבלות בטיחות ומחקר
- השירות קורא מידע ציבורי בלבד ואינו שולח פקודות מסחר.
- אות מומנטום הוא מועמד מחקרי ניסיוני, לא אסטרטגיה מוכחת.
- נתונים נשמרים בקובצי JSONL ועלולים לגדול; יש לעקוב אחר נפח ה-Volume.
- אל תוסיף סיסמאות, API tokens, מפתחות פרטיים או פרטי ארנק למאגר או למשתני הסביבה.
- אל תטען שהשירות עובד 24/7 עד שבוצעה פריסה ונבדק שהנתונים מתעדכנים לאורך זמן.


## בדיקת API ציבורי מתוך GitHub Actions
במאגר נוסף workflow בשם `Public API smoke check` שמבצע בדיקת קריאה בלבד מול Gamma API ללא התחברות או מפתחות, ומתוזמן מדי יום. כדי להפעילו ידנית: פתח GitHub → Actions → Public API smoke check → Run workflow. ה-workflow עבר בהצלחה בריצה [Public API smoke check #1](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40110178400): GitHub Actions קיבל 5 שווקים ציבוריים ובדק שדות בסיסיים. זו בדיקת Gamma נקודתית בלבד, לא הוכחה שה-worker בענן פועל לאורך זמן או ש-Docker נבנה בהצלחה.


## ריצת backtest היסטורית
כלי `backtest.py` מאפשר להריץ בדיקה היסטורית מול endpoint ציבורי של CLOB:
```bash
python backtest.py --markets 5 --days 30 --fidelity 60 --train-ratio 0.7 --output backtest_report.json
```
הדוח מפריד כרונולוגית בין in-sample ל-out-of-sample, מריץ תיק מדומה נפרד לכל חלון, ומציג P&L, שיעור הצלחה, מספר אותות ומספר פוזיציות. אין להתייחס לזה כהוכחת רווחיות: המודל אינו משחזר עומק ספר פקודות או מחיר ביצוע מובטח.


## ניהול נפח הנתונים
קובצי snapshots/signals/events מוגבלים באמצעות רוטציה אוטומטית. ברירות מחדל: snapshots ‏25MB ו-4 גיבויים; signals/events ‏5MB ו-3 גיבויים. ניתן לשנות דרך משתני `SNAPSHOT_MAX_MB`, `SNAPSHOT_BACKUPS`, `SIGNAL_LOG_MAX_MB`, `SIGNAL_LOG_BACKUPS`, `PAPER_EVENTS_MAX_MB`, `PAPER_EVENTS_BACKUPS`. יש עדיין לנטר את גודל ה-Volume.
