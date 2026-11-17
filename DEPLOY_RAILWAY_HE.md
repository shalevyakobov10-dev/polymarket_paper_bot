# פריסה ב-Railway — Paper Research Bot

## מצב הפרויקט
המאגר כולל שירות Python, Dockerfile, בדיקות יחידה, backtest היסטורי והגדרת Railway Infrastructure as Code ב-`.railway/railway.ts`. **השירות עדיין לא נפרס בענן**: יש להתחבר לחשבון Railway, לבדוק את תוכנית הפריסה ולאשר את יצירת השירות וה-Volume.

## פריסה עם תצורת התשתית שבמאגר

הקובץ `.railway/railway.ts` מגדיר שירות אחד בשם `polymarket-paper-bot` ממאגר GitHub הזה ומ-branch `main`, Volume בנפח 1GB, חיבור Volume אל `/app/data`, health check, וערכי סיכון קבועים ל-Paper Trading. הוא מציב את השירות ואת ה-Volume באזור `europe-west4`. התצורה כוללת גם איסוף GDELT בארבעה מסלולי נושא בכל מחזור (חלון שעה), רישום/אימון מחזורי של מודל כל 15 דקות והערכת holdout/walk-forward כל 6 שעות.

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

הקובץ מציב `DATA_DIR=/app/data`, `POLL_SECONDS=60`, סריקה של עד 400 שווקים פעילים ו-20 שווקים מנוטרים, ומגדיר במפורש `ENABLE_USER_ORDER_MONITOR=false`. המאזין המאומת לא יופעל אלא לאחר opt-in מפורש. הוא **לא** מפעיל מסחר אמיתי. לא נקבע סף 0.005, משום שה-backtest מצא שכל הספים שנבדקו הפסידו בתיק המדומה. ברירת המחדל המחקרית נשארת 0.03.


## Supervisor ומאזין חשבון אופציונלי

תמונת Docker מפעילה את `service_supervisor.py` כ-PID 1. התהליך מפעיל את שרת המחקר ומפקח עליו. כברירת מחדל `ENABLE_USER_ORDER_MONITOR=false`, ולכן אין התחברות לחשבון.

להפעלה יזומה של מאזין החשבון לקריאה בלבד באותו שירות:
1. הוסף ב-Railway את `POLYMARKET_PRIVATE_KEY` כ-Secret Variable דרך ממשק Railway בלבד; אין להוסיף אותו ל-IaC, לקובץ `.env`, ל-Git או לשיחה.
2. שנה את `ENABLE_USER_ORDER_MONITOR` ל-`true` דרך משתני השירות.
3. ודא שהשירות משתמש ב-Volume המתמשך שמחובר ל-`/app/data`, ושנשארת replica אחת בלבד עבור קובץ SQLite.
4. בדוק בלוגים שהמאזין התחיל ושהוא מתאושש מניתוק; health check של HTTP אינו מוכיח סנכרון של החשבון.

ה-supervisor מפעיל מחדש את מאזין החשבון לאחר יציאה/ניתוק עם backoff מוגבל עד 60 שניות. אם שרת ה-HTTP הראשי יוצא, כל הקונטיינר יוצא כדי שהפלטפורמה תפעיל אותו מחדש. גם במצב זה המאזין נשאר read-only: לא נוצרת, נחתמת או נשלחת פקודת מסחר. הגדרת Railway במאגר משאירה את המאזין כבוי במכוון עד שמפעיל הפריסה בוחר להדליקו.

## תקינות וגיבוי SQLite

בעת פתיחה, יומן SQLite דורש WAL ומבצע `quick_check` ו-`foreign_key_check` לפני שינוי schema. אם WAL אינו זמין או שבדיקת התקינות נכשלת, היומן מסרב לעלות.

ליצירת snapshot עקבי ידני בזמן שהשירות עובד:
```bash
python backup_order_journal.py --destination /path/on/independent-backup-storage/order-journal.sqlite3
```

הכלי מעתיק דרך SQLite online backup API, כולל נתונים מחויבים שעדיין נמצאים ב-WAL, מאמת את קובץ היעד ורק אז מחליף אותו אטומית. יש להריץ רק מול יומן קיים ולהשתמש ביעד נפרד ממסד הנתונים החי.

לבדיקת שחזור אל נתיב חדש שאינו קיים:
```bash
python restore_order_journal.py --source /path/to/verified-backup.sqlite3 --destination /path/to/restore-staging/order-journal.sqlite3
```

כלי השחזור פותח את הגיבוי לקריאה בלבד, בודק integrity, קשרי foreign keys, מבנה טבלאות, כל מסמכי JSON שנשמרו ביומן, רצף מעברי המצבים והאירועים לפי `order_lifecycle.py`, רציפות תנאי הפקודה והמונים, עקביות של venue-order index, אריתמטיקה מלאה של צילומי סיכון היסטוריים והתאמת timestamp של מצב הסנכרון לצילום הקיים. הוא בונה קובץ זמני, מאמת אותו שוב ומשווה ספירות רשומות לפני פרסום אטומי שאינו דורס יעד קיים. הוא מסרב לגיבוי שיש לצדו קובצי WAL/SHM/journal. תחילה יש לבדוק את השחזור בסביבת staging, לפתוח אותו בגרסת הקוד התואמת ולהשוות את מספר הרשומות. אם נמצאות רשומות `submission_pending`, יש לשמר אותן ולבצע `recover_interrupted_submissions()` רק לאחר הבטחת בעלות בלעדית על היומן; הפעולה הופכת אותן ל-`submission_unknown` ואינה מאפשרת שליחה חוזרת. **אין להצביע על מסד הנתונים הפעיל או להחליף אותו כשהשירות עדיין פועל**: יש לעצור את השירות, לבצע גיבוי נוסף, לוודא שאין writer פעיל, ואז לבצע מעבר מתוכנן עם אפשרות rollback. כלי השחזור אינו עוצר שירות, אינו משנה משתני Railway ואינו מחליף אוטומטית את מסד הנתונים של היישום.

**חשוב:** תצורת Railway הנוכחית אינה מגדירה scheduler, יעד off-host, הצפנה או retention לגיבויים. שמירת עותק נוסף באותו Volume אינה הגנה מפני אובדן ה-Volume. לפני שימוש פרודקשני יש להגדיר תהליך מתוזמן ליעד אחסון עצמאי, לשמור מספר דורות, להגביל הרשאות ולתרגל שחזור. הגיבוי מכיל מידע על חשבון והזמנות ויש להגן עליו כמו על מסד המקור.

## בדיקת מנהל התהליכים

פתח את `/service-supervisor` כדי לראות אם ה-container רץ תחת supervisor, האם מאזין החשבון מופעל, האם הוא במצב `running` או `restarting`, כמה הפעלות מחדש בוצעו ומתי צפויה ההפעלה הבאה. ה-supervisor מרענן heartbeat כל 15 שניות, גם בזמן המתנה להפעלה מחדש; `status_fresh` יסומן false כאשר קובץ הסטטוס לא עודכן במשך יותר מ-45 שניות. אם מתקבל `supervised=false`, האפליקציה רצה ללא supervisor או שקובץ הסטטוס חסר; אין להסיק מכך שמאזין החשבון מסונכרן. ה-endpoint אינו מציג מפתחות, משתני סביבה או כתובות ארנק, ואינו מחליף בדיקת טריות של צילום החשבון.

## אימות אחרי הפריסה

לאחר שהשירות פעיל, פתח את ה-Domain ובדוק:
- `/health` — בדיקת חיות בסיסית של HTTP.
- `/status` — מצב worker, זמן הסריקה האחרונה, כמות שווקים/תוצאות, שגיאה אחרונה וסיכום הסימולציה.
- `/paper` — יתרת הסימולציה, מדדי ביצועים ופוזיציות מדומות פתוחות.
- `/learning` — גרסת המודל, כמות שווקים באימון, מספר תחזיות שהבשילו, Brier/Log Loss מול baseline ותוצאות לפי גרסת מודל.
- `/news` — מצב RSS/GDELT, מסלולי הנושא שהצליחו/נכשלו, פריטים עדכניים וטריות המידע.

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
