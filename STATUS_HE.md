# מצב הפרויקט — 30 באוקטובר 2026

## מה נמצא במאגר
- שירות Python לקריאת נתונים ציבוריים מ-Gamma; נקודות HTTP: `/`, `/status`, `/health`, `/paper`, `/news`, `/matches`.
- רכיב `news_monitor.py` אוסף כותרות/תקצירים וקישורים מ-15 פידי RSS ציבוריים של BBC, The Guardian, Al Jazeera English, NPR, Deutsche Welle, Sky News ו-France 24; משתמש בעד 5 בקשות מקבילות, מבצע deduplication גם בין פידים, מסדר פריטים לפי זמן הפרסום, שומר JSONL עם רוטציה ומבודד כשלים בין מקורות. תדירות ברירת מחדל: 5 דקות.
- רכיב התאמה מחקרי `keyword_overlap_v1` מקשר כותרות לשאלות של השווקים המנוטרים לפי חפיפה במילות מפתח. נקודת `/matches` ו-`data/news_market_candidates.jsonl` מיועדות למחקר בלבד; ההתאמה אינה חיזוי, אינה הסתברות ואינה מפעילה את מנוע המסחר.
- סימולציית תיק בלבד: יתרת פתיחה $100, פוזיציה עד $2, חשיפה עד $10, עד 5 פוזיציות, TP/SL של 10%, החזקה מרבית 24 שעות והחלקה מדומה 0.5% לכל צד.
- שמירת מצב ואירועים, רוטציה מוגבלת של קובצי JSONL ו-backtest שרץ בזיכרון בלי כתיבה לדיסק בכל נר.
- כלי `backtest.py`: היסטוריית CLOB ציבורית, split כרונולוגי, ארבעה ספי רגישות, ותשואות קדימה של שעה/6 שעות/24 שעות מול baseline.
- תצורת Railway Infrastructure as Code ב-`.railway/railway.ts`: שירות יחיד ב-`main`, Volume מתמשך 1GB אל `/app/data`, בדיקת חיות ומשתני סביבה של Paper Trading.
- GitHub Actions לבדיקות Python + Docker + בדיקת HTTP בתוך הקונטיינר, בדיקת Gamma ציבורי, בדיקת RSS מתוזמנת, backtest היסטורי ואימות תצורת Railway.

## עדכון אחרון — 30 באוקטובר 2026
- [CI #111](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/41047539325): **הצליח** — קומפילציה, בדיקות יחידה, בניית Docker ובדיקת HTTP לקונטיינר.
- [CI #112](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/41048019756): **הצליח** על commit `3727df8` — 45 בדיקות יחידה, קומפילציה, בניית Docker ובדיקת HTTP של `/news`, `/status`, `/paper` ו-`/matches`.
- גילוי השווקים ב-Gamma עבר לעימוד מוגבל: עד 5 עמודים כברירת מחדל, הסרת כפילויות לפי מזהה שוק, דירוג לפי נפח 24 שעות ומעקב אחרי 20 שווקים מובילים כברירת מחדל. ניתן לכוונן דרך `GAMMA_DISCOVERY_LIMIT`, `GAMMA_DISCOVERY_PAGES` ו-`MARKETS_TO_TRACK`.
- נקודת `/matches` מציגה כעת סיכום גיוון מקורות לכל שוק: מספר מפרסמים שונים, שמות מפרסמים ומספר כותרות מותאמות. מדורי חדשות של אותו מפרסם מאוחדים תחת שם מפרסם אחד. זהו מדד לגיוון מקורות בלבד, לא אימות אירוע ולא הסתברות.
- נוספו בדיקות ל-pagination/deduplication ולסיכום גיוון המפרסמים. בדיקות ה-RSS הציבוריות עברו בריצה [#41048010816](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/41048010816).
- ריצות CI כפולות נוצרו משום שכל קובץ עודכן ב-commit נפרד; ריצת הבדיקה של ה-commit האחרון עברה.

## עדכון אחרון — 25 באוקטובר 2026
- [CI #107](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40184898249): **הצליח** — קומפילציה, 42 בדיקות יחידה, בניית Docker, והרצת קונטיינר שבדקה את `/news`, `/status`, `/paper` ו-`/matches`.
- [Public news RSS smoke check #5](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40184898217): **הצליח** — כל 15/15 פידי ה-RSS החזירו כותרות תקינות בזמן הבדיקה.
- [Railway IaC validation #3](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40184110153): **הצליח** — תצורת Railway עברה אימות.
- נקודת `/health` מחזירה מצב לא בריא (HTTP 503) אם ה-worker לא רץ או אם אין סריקת שווקים מוצלחת ועדכנית.
- בדיקת HTTP אמיתית לקונטיינר הצליחה; הופיעה הודעת חיבור שהתאפס בזמן ההפעלה הראשונית, והבדיקה ביצעה retry ואז עברה. זו בדיקת עשן ולא מבחן עומס או יציבות של 24/7.
- רכיב RSS פועל בשרשור נפרד. כשל במקור חדשות אחד אינו עוצר את איסוף מחירי Polymarket. נקודת `/news` מציגה פריטים אחרונים, מצב מקורות וזמן עדכון.
- פידים של חדשות הם כיסוי חלקי, לא כל אירוע בעולם; כותרת אינה אימות עצמאי. חפיפת מילות מפתח היא התאמה בסיסית בלבד, עלולה לפספס קשרים או לייצר התאמות שגויות ואינה אות מסחר.
- הצלחת CI מאמתת קוד/בדיקות/בנייה בלבד; היא אינה הוכחה שהשירות נפרס או פועל בענן.

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
