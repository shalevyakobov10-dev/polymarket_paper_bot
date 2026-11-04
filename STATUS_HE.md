## חיבור preflight ליומן intents — ללא ביצוע
- נוסף `order_staging.py` שמריץ את כל שערי `live_order_preflight.py` על תנאי ההוראה ומצב הסיכון.
- רק אם כל הבדיקות עוברות נשמר intent במצב `prepared` עם דוח ה-preflight בתוך אירוע audit ראשון; כשל בשער לא יוצר intent.
- גם במקרה של preflight תקין, הפלט נשאר `order_submission_allowed=false`, `order_signing_allowed=false` ו-`execution=none`. אין כאן SDK לכתיבה, חתימה או submit.

## קליטת אירועי חשבון מאומתים — read-only בלבד
- נוסף `user_order_stream.py`, המשתמש ב-`AsyncSecureClient` וב-`UserSpec` של `polymarket-client==0.16.0` כדי לצרוך אירועי פקודות בחשבון.
- אירועי Order ממופים לפי מזהה הפקודה של CLOB לאינדקס המקומי ב-SQLite; לפני עדכון נבדקים מזהה נכס, צד, מחיר וגודל מקורי מול ה-intent המקומי.
- כמות `size_matched` נשמרת ככמות מצטברת. אירועי Trade אינם משנים מילויים, כדי שלא לספור את אותה התאמה גם מאירוע Order וגם מאירוע Trade.
- נוסף CLI להפעלה מבודדת: `python user_order_stream.py`; מסד הנתונים נשמר כברירת מחדל תחת `DATA_DIR/order_journal.sqlite3` או בנתיב `ORDER_JOURNAL_DB`. התהליך מיועד להפעלה תחת supervisor עם volume מתמשך; הוא אינו מתחיל אוטומטית עם שרת ה-HTTP.
- לאחר פתיחת המנוי מתבצע snapshot REST מלא של כל דפי הפקודות הפתוחות; מילויים מצטברים מעודכנים מהתשובה. אותו snapshot מתבצע שוב כל 300 שניות כברירת מחדל, וניתן לשנות את התדירות ב-`USER_ORDER_SNAPSHOT_INTERVAL_SECONDS` או ב-CLI. פקודה מקומית שאינה מופיעה ברשימת הפקודות הפתוחות נשארת לא פתורה ומדווחת בנפרד — אין להסיק שהיא בוטלה או מולאה רק מהיעדרה.
- אם ה-SDK מדווח על אירועים שנפלו מהתור, הצרכן נעצר עם `UserEventGapError`; אין להציג את היומן כמסונכרן. נדרש בהמשך REST trade-history reconciliation כדי לסגור פקודות שנעלמו מהרשימה ולתקן פערים לאחר reconnect.
- ההאזנה אינה מופעלת אוטומטית עם האפליקציה ואינה כוללת יצירת/חתימת/שליחת/ביטול פקודות. מפתח פרטי וכתובת ארנק, אם נדרשת, נקראים רק בעת הפעלה יזומה מתוך secret manager / משתני סביבה — לעולם לא מתוך קוד, commit או שיחה.

[object Object]