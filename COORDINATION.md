# תיאום עבודה בין סוכנים (ChatGPT ↔ Claude)

מסמך זה נועד למנוע עבודה כפולה וקונפליקטים בין סוכני ה-AI שעובדים במאגר הזה.
**כל סוכן קורא אותו לפני כל משימה.** שינוי במסמך עצמו נעשה רק ב-PR, ורק בהסכמת שני הצדדים (הבעלים מאשר).

> הערה: כל ה-commits במאגר נכתבים תחת חשבון הבעלים. לכן ההבחנה בין הסוכנים נעשית לפי שם ענף וטריילר ב-commit (ראו "כללים").

## 1. סוכנים ותחומי אחריות

| תחום | בעלים | קבצים (כפי שנצפו בענפים הפתוחים) |
|---|---|---|
| מחקר, חדשות, תחזיות, backtest, הערכת מודלים, למידה | ChatGPT | `news_monitor.py`, `global_news.py`, `forecast_evaluation.py`, `backtest.py`, `independent_model_evaluation.py`, `learning_journal.py`, `model_governance.py`, `research/`, `ai/` |
| מסחר מדומה (פוליסי ופרפס), נתוני שוק, replay ו-walk-forward | ChatGPT | `paper_*.py`, `perps_paper_*.py`, `perps_market_*.py`, `perps_historical_replay.py`, `perps_walk_forward_evaluation.py`, `market_*.py` |
| יומן פקודות, מצב-מכונה, preflight, שערי השקה ו-readiness | ChatGPT | `order_*.py`, `execution_readiness.py`, `live_order_preflight.py`, `live_execution_gateway.py`, `perps_live_*.py`, `perps_auto_cancel_guard.py`, `perps_sdk_*.py`, `account_risk_*.py` |
| פריסה, CI, Railway, `STATUS_HE.md`, `README.md` | ChatGPT | `.github/`, `.railway/`, `Dockerfile`, `DEPLOY_RAILWAY_HE.md`, `STATUS_HE.md`, `README.md`, `app.py`, `main.py` |
| מתאם ביצוע חי לפרפס (stops בצד הבורסה, מרחק מחיסול, חיי סשן), מערך בדיקות תקלות (chaos) | Claude | `live_bot/**`, `tests/live_bot/**`, `STATUS_CLAUDE.md` |
| סקירות עצמאיות של PRs של הצד השני | שניהם | `reviews/<תאריך>-<נושא>-<סוכן>.md` |
| מסמך זה, `AGENTS.md` | משותף | שינוי רק ב-PR שהצד השני מאשר |

כלל ברזל: **לא עורכים קובץ שבבעלות הצד השני.** צריך שינוי אצלו? פותחים בקשה בקובץ `coordination/requests/<מי>-<נושא>.md` או מגיבים ב-PR שלו.

## 2. נוהל תחילת משימה (חובה)

1. לקרוא `COORDINATION.md`, `STATUS_HE.md`, `STATUS_CLAUDE.md` ואת `coordination/claims/`.
2. לבדוק PRs פתוחים ואילו קבצים הם נוגעים בהם, ולא לפתוח עבודה על אותם קבצים. פקודות מועילות:
   `gh api "repos/<owner>/<repo>/pulls?state=open&per_page=100"` ואז `.../pulls/<n>/files`.
3. לרשום **claim** — קובץ חדש `coordination/claims/<סוכן>--<נושא>.md` (קובץ לכל משימה, כדי שלא יהיו קונפליקטי מיזוג). תוכן: מטרה, קבצים צפויים, ענף, בסיס (main או PR תלוי), סטטוס, תאריך.
4. לעבוד בענף משלך. תחילית ענפים: Claude = `claude/…`. ChatGPT ממשיך בתחיליות הקיימות (`research/`, `fix/`, `hardening/` …).
5. בסיום: PR, עדכון קובץ הסטטוס של הסוכן, ומחיקת/סימון ה-claim כ-`done` ב-PR עצמו.
6. claim שלא עודכן 14 יום נחשב נטוש; כל סוכן רשאי לשאול את הבעלים לפני שמשתלטים עליו.

## 3. כללי עבודה

- אין push ישיר ל-`main`. הכול דרך PR. מיזוג — אחרי אישור הבעלים.
- PR שתלוי ב-PR אחר שלא מוזג: לציין במפורש בתיאור וב-claim (ברירת מחדל: בסיס = `main`).
- ב-commit של Claude: טריילר `Co-Authored-By: Claude …`; ב-commit של ChatGPT: טריילר `Co-Authored-By: ChatGPT`. כך אפשר לראות מי עשה מה.
- כל סוכן מעדכן רק את קובץ הסטטוס שלו (`STATUS_HE.md` / `STATUS_CLAUDE.md`) כדי למנוע קונפליקטים.
- סודות: מפתח פרטי ו-webhooks לעולם לא בקבצים, בצ'אט או ב-CI. רק משתני סביבה / מנהל סודות.

## 4. כללי בטיחות משותפים (לא ניתנים לשינוי ללא אישור מפורש של הבעלים)

- מסחר חי **נעול**. אף סוכן לא הופך שער השקה (`RELEASE_GATES`) ל-`True` ולא מפעיל משתני הפעלה בשם הבעלים.
- אף סוכן לא מרפה אוטומטית מגבלת סיכון (הידוק בלבד).
- כל טענה על ביצועים מתויגת ברמת ראיות: `unit/fake-exchange` · `public-API` · `paper` · `live-account`. עד שלא רץ מול חשבון אמיתי, לא כותבים ש"זה עובד חי" ולא מבטיחים רווח.
- חוזים תמידיים ממונפים מסוכנים בהרבה מהשוק הרגיל; הבעלים אחראי לבדוק זכאות וחוקיות באזורו.

## 5. החלטות פתוחות (דורשות הכרעה — לא להניח)

**D1 — dead-man auto-cancel מול stops בצד הבורסה.**
`perps_auto_cancel_guard.py` מתבסס על `arm_auto_cancel`. לפי התיעוד שקראתי (Claude, 27–28.11), הפעולה מבטלת את כל הפקודות הפתוחות, ופקודת stop היא פקודה — כלומר איבוד תהליך עם פוזיציה פתוחה עלול להסיר את ה-stop ולהשאיר פוזיציה חשופה. בצד של Claude ה-stop הוא חובה והמתג לא מופעל. **לא נבדק מול בורסה חיה.** נדרש: אימות עצמאי של ההתנהגות ואז הכרעה אם לאמץ מנגנון אחד או שילוב (למשל: מתג רק כשאין פוזיציה פתוחה).

**D2 — אסטרטגיות פרפס כפולות.**
בשני הצדדים יש אסטרטגיית מגמה (EMA). כדי לא לבנות שתי ערימות מחקר: `perps_paper_engine` + `perps_walk_forward_evaluation` של ChatGPT הם מסגרת ההערכה היחידה; האסטרטגיה של Claude תוגש להם כמועמדת, ולא תוערך בנפרד.

**D3 — נקודת כניסה יחידה לשליחת פקודות.**
המלצה: `PerpsLiveExecutionGateway` (יומן, נעילה, שערים) נשאר השער היחיד; `live_bot/` מספק מאחוריו רק ניהול stops, מרחק מחיסול וסשן. ממשק מדויק ייקבע ב-PR נפרד לאחר אישור D1.

**D4 — סדר מיזוג.**
`main` מפגר אחרי ענפי הפרפס וה-Event-aware. נדרש סדר מיזוג מוסכם (בבעלות הבעלים) לפני שנפתחים ענפים חדשים מעליהם.
