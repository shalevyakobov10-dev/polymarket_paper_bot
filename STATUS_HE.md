# מצב הפרויקט — 24 באוקטובר 2026

## מה נמצא במאגר
- שירות Python שמושך שווקים ציבוריים דרך Gamma API.
- סימולציית תיק מתמשכת עם יתרת פתיחה $100, פוזיציה עד $2, חשיפה פתוחה עד $10, ועד 5 פוזיציות.
- מועמד לכניסה נוצר כאשר המחיר הנצפה עולה ב-0.03 לפחות בין תצפיות; זהו כלל ניסיוני ולא מודל חיזוי מוכח.
- יציאה מדומה לפי take-profit/stop-loss של 10% או מגבלת החזקה של 24 שעות.
- החלקה מדומה 0.5% בכל צד; העמלה ברירת מחדל 0 כי שיעור העמלה בשוק הרלוונטי לא אומת.
- שמירת מצב ב-`paper_state.json` ולוג אירועים ב-`paper_events.jsonl`.
- נקודות HTTP: `/`, `/status`, `/health`, `/paper`.
- Dockerfile, GitHub Actions לבדיקות ול-build, ו-workflow נפרד לבדיקת API ציבורי.

## בדיקות מאומתות
- `python -m py_compile app.py paper_trading.py`: עבר.
- `python -m pytest -q` בסביבת העבודה: 15 בדיקות עברו.
- GitHub Actions: 15 בדיקות עברו בריצה [Tests #20](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40110178415).
- Gamma API ציבורי: ריצה [Public API smoke check #1](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/actions/runs/40110178400) קיבלה 5 שווקים ובדקה שדות בסיסיים. מדובר בבדיקת קריאה קצרה מתוך GitHub-hosted runner.
- CI עודכן כך שיבנה גם Docker image; תוצאת ה-build החדש עדיין ממתינה לאימות.

## עדיין לא אומת
- Docker build בריצה הכוללת את שלב הבנייה החדש.
- פריסה ב-Railway ופעולה 24/7: טרם בוצעו. נדרשת יצירת שירות ו-Volume בחשבון Railway והבדיקה של URL חי ונתוני `/status`.
- רווחיות: אין עדיין תוצאות בדיקה היסטורית או forward-test מספקות כדי לטעון שיש יתרון אסטרטגי.

## גבולות
Paper/research בלבד. אין ארנק, הזמנות מסחר אמיתיות, התחברות לחשבון, הפקדות או משיכות. אל תפרש תוצאות מדומות כהבטחה לרווח. מצב המאגר והגדרותיו אינם הוכחה שה-worker פרוס או רץ בענן.
