# פריסה ב-Railway — Paper Research Bot

## מצב הפרויקט
המאגר כולל שירות Python, Dockerfile, בדיקות יחידה, backtest היסטורי והגדרת Railway Infrastructure as Code ב-`.railway/railway.ts`. **השירות עדיין לא נפרס בענן**: יש להתחבר לחשבון Railway, לבדוק את תוכנית הפריסה ולאשר את יצירת השירות וה-Volume.

## פריסה עם תצורת התשתית שבמאגר

הקובץ `.railway/railway.ts` מגדיר שירות אחד בשם `polymarket-paper-bot` ממאגר GitHub הזה ומ-branch `main`, Volume בנפח 1GB, חיבור Volume אל `/app/data`, health check, וערכי סיכון קבועים ל-Paper Trading. הוא מציב את השירות ואת ה-Volume באזור `europe-west4`. התצורה מפעילה גם את `event_paper_worker.py` באופן רציף (Paper בלבד), קוראת שוק ציבורי דרך Gamma/CLOB, שומרת את תיק ה-Paper ואת ארכיון השוק ב-SQLite, ומבצעת גיבוי עקבי לשני המסדים כל שש שעות.

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

התצורה מגדירה `DATA_DIR=/app/data`, `ENABLE_EVENT_PAPER_WORKER=true`, `PAPER_MARKET_DATA_SOURCE=live_clob`, ארכיון שוק ותיק Paper נפרדים, גיבויים כל שש שעות, `DRY_RUN=true`, `LIVE_EXECUTION_ENABLED=false`, וגם `ENABLE_USER_ORDER_MONITOR=false`. מנוע ה-Paper משתמש בסף ראיות אירוע 0.75, הסכמה מינימלית של שני מפרסמים, עמלות סימולציה 1% ומגבלת השפעת מחיר 100 bps. השירות עדיין **לא נפרס**; יש למזג/לשחרר את קוד ה-worker לענף `main` לפני שה-IaC יוכל להפעיל אותו. התצורה אינה מפעילה מסחר אמיתי.

ה-healthcheck מוגדר עם timeout של 300 שניות, וחלון graceful shutdown של 30 שניות מוגדר כדי לאפשר כיבוי מסודר. לפי Railway, ה-healthcheck מאשר שהפריסה החדשה מוכנה לפני ניתוב תעבורה; הוא אינו ניטור רציף אחרי שהפריסה פעילה. לאחר פריסה יש להוסיף uptime monitor חיצוני ל-`/health`, ולנטר גם heartbeat ו-`/event-paper`.


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

**חשוב:** מנוע ה-Paper מגבה כעת את שני מסדי הנתונים ל-`/app/data/backups` כל שש שעות, עם בדיקת SQLite ו-manifest SHA-256 ושמירת 30 דורות. אבל העותקים נשמרים באותו Railway Volume. התצורה עדיין אינה מגדירה יעד off-host, הצפנה או שחזור אוטומטי. לפני שימוש פרודקשני יש להגדיר יעד אחסון עצמאי, להגביל הרשאות ולתרגל שחזור. הגיבוי מכיל מידע רגיש על פעילות החשבון והעסקאות ויש להגן עליו כמו על מסד המקור.

## בדיקת מנהל התהליכים

פתח את `/service-supervisor` כדי לראות אם ה-container רץ תחת supervisor, האם מאזין החשבון מופעל, האם הוא במצב `running` או `restarting`, כמה הפעלות מחדש בוצעו ומתי צפויה ההפעלה הבאה. ה-supervisor מרענן heartbeat כל 15 שניות, גם בזמן המתנה להפעלה מחדש; `status_fresh` יסומן false כאשר קובץ הסטטוס לא עודכן במשך יותר מ-45 שניות. אם מתקבל `supervised=false`, האפליקציה רצה ללא supervisor או שקובץ הסטטוס חסר; אין להסיק מכך שמאזין החשבון מסונכרן. ה-endpoint אינו מציג מפתחות, משתני סביבה או כתובות ארנק, ואינו מחליף בדיקת טריות של צילום החשבון.

## אימות אחרי הפריסה

לאחר שהשירות פעיל, פתח את ה-Domain ובדוק:
- `/health` — בדיקת readiness; כאשר `ENABLE_EVENT_PAPER_WORKER=true`, היא דורשת גם worker פעיל ו-heartbeat טרי.
- `/service-supervisor` — מצב PID-1, תהליכי worker, ומדדי restart.
- `/event-paper` — מחזורי event-paper, תיק מדומה, מדדי ביצועים, מצב גיבוי ו-heartbeat.
- `/status` — מצב worker השוק הוותיק, זמן הסריקה האחרונה, כמות שווקים/תוצאות וסיכום הסימולציה.
- `/paper` — תיק הסימולציה של מנוע השירות הוותיק; אינו אותו תיק כמו `/event-paper`.
- `/learning` — גרסת המודל, כמות שווקים באימון, מספר תחזיות שהבשילו, Brier/Log Loss מול baseline ותוצאות לפי גרסת מודל.
- `/news` — מצב RSS/GDELT, מסלולי הנושא שהצליחו/נכשלו, פריטים עדכניים וטריות המידע.

`/health` אינו מוכיח שה-API החיצוני נגיש. ב-`/status` בדוק ש-`last_success_at` מתעדכן וש-`last_error` הוא `null`. אם מופיעה שגיאת DNS, timeout או rate limit, השירות עדיין עשוי להגיש HTTP אך לא לקבל נתוני שוק חדשים.

## בדיקת API ציבורי מתוך GitHub Actions

ה-workflow `Public API smoke check` מבצע קריאות read-only ל-Gamma ולספרי CLOB ציבוריים, דרך ה-provider האמיתי, ללא חשבון או מפתחות. הוא דורש לפחות snapshot בינארי טרי אחד שבו ספרי YES ו-NO דו-צדדיים ושימושיים. זו בדיקת API נקודתית בלבד: היא אינה מוכיחה זמינות רציפה, עומק היסטורי, או שה-worker פועל בענן לאורך זמן.

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
