# מצב הפרויקט — 25 באוקטובר 2026

## מה נמצא במאגר
- שירות Python לקריאת נתונים ציבוריים מ-Gamma; נקודות HTTP: `/`, `/status`, `/health`, `/paper`.
- סימולציית תיק בלבד: יתרת פתיחה $100, פוזיציה עד $2, חשיפה עד $10, עד 5 פוזיציות, TP/SL של 10%, החזקה מרבית 24 שעות והחלקה מדומה 0.5% לכל צד.
- שמירת מצב ואירועים, רוטציה מוגבלת של קובצי JSONL ו-backtest שרץ בזיכרון בלי כתיבה לדיסק בכל נר.
- כלי `backtest.py`: היסטוריית CLOB ציבורית, split כרונולוגי, ארבעה ספי רגישות, ותשואות קדימה של שעה/6 שעות/24 שעות מול baseline.
- תצורת Railway Infrastructure as Code ב-`.railway/railway.ts`: שירות יחיד ב-`main`, Volume מתמשך 1GB אל `/app/data`, בדיקת חיות ומשתני סביבה של Paper Trading.
- GitHub Actions לבדיקות Python + Docker, בדיקת API ציבורי, backtest היסטורי ואימות תצורת Railway.

## עדכון אחרון — 25 באוקטובר 2026
- [CI #80](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40165769842): **הצליח** על commit `ae7078ce` — קומפילציה, 29 בדיקות יחידה ובניית Docker image עברו.
- הוסרה מחלקת סימולציה כפולה שנוספה במהלך העבודה; היישום ממשיך להשתמש במנוע המתמיד `paper_trading.py`, שכבר כולל מגבלות חשיפה, TP/SL, מגבלת זמן, שמירת מצב ורוטציית לוגים.
- הצלחת CI מאמתת בדיקות וקונטיינר בלבד; היא אינה אומרת שהשירות נפרס או רץ בענן.

## בדיקות מאומתות
- [Tests #71](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40112061374): **29 בדיקות עברו**, קומפילציה ובניית Docker image עברו.
- [Railway IaC validation #2](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40112061293): התקנת חבילות TypeScript ואימות הטיפוסים של הגדרת Railway עברו.
- [Public API smoke check #6](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40111586766): קריאת Gamma ציבורית עברה.
- [Historical paper backtest #14](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40111596005): משיכת ההיסטוריה, פיצול כרונולוגי, רגישות ספים והעלאת דוח עברו.
- [דוח המחקר בעברית](research/2026-10-24-backtest-summary-he.md).

## ממצאי מחקר
- חלון של 30 יום עם 168,492 תצפיות מחיר על 40 טוקנים שנבחרו מתוך 20 שווקים.
- כל ארבעת הספים שנבדקו (0.005, 0.01, 0.02, 0.03) הפיקו תשואת תיק שלילית ב-in-sample וב-out-of-sample, למעט 0.03 עם אפס עסקאות בחלון הבדיקה (0% שם משמעו שלא נפתחה פוזיציה).
- סף 0.005 הציג ממוצע חיובי באופקי שעה/6 שעות במדגם הבדיקה, אך ממוצע שלילי באופק 24 שעות; סימולציית התיק שלו עדיין הפסידה 0.4124% בחלון הבדיקה.
- יש מעט אותות, חלונות תשואה חופפים ותלות בזמן, והמדגם עלול לסבול מהטיית בחירה/שרידות. כרגע אין ראיה ליתרון מסחרי.
- ברירת המחדל לא שונתה, ואין מסחר אמיתי.

## עדיין לא הושלם
- **פריסה בענן לא בוצעה.** קובץ IaC מוכן ומאומת, אבל עדיין צריך להתחבר לחשבון Railway, לבחור/ליצור פרויקט, לבדוק את `railway config plan` ולאשר את `railway config apply`. יצירת Volume עשויה להיות כרוכה בעלות.
- לאחר הפריסה יש להפעיל Domain ולוודא ש-`last_success_at` ב-`/status` מתעדכן וש-`last_error` הוא `null`.
- עדיין אין forward paper-test לאורך זמן מחוץ לתקופה ההיסטורית.
- שיעור עמלות אמיתי ו-spread/slippage אמפיריים לא אומתו.

## גבולות
Paper/research בלבד. אין ארנק, פקודות מסחר אמיתיות, התחברות לחשבון, הפקדות או משיכות. תוצאת CI אינה הוכחה שה-worker פועל בענן.
