# Polymarket Perps — Paper Engine (מחקר בלבד)

## ארכיטקטורה

המערכת משתמשת ב־collector הקיים של Polymarket Perps ואינה יוצרת ספק נתונים או worker נוסף. ה־collector קורא נתוני REST ציבוריים, מאמת Instrument/Ticker/Book, שומר היסטוריה ב־SQLite ומנוטר על ידי ה־PID-1 supervisor. שכבת ה־Paper מקבלת את אותו snapshot מקורי וממירה אותו לפורמט פנימי.

מודולים:
- perps_market_data.py — נתוני Instrument, Ticker, Mark/Index, Funding, Risk Tiers וספר פקודות ציבורי.
- perps_market_history_store.py — היסטוריית שוק append-only עם retention.
- perps_market_worker.py — worker קיים יחיד; אוסף נתוני שוק, מריץ את מנוע ה־Paper כשהוא מופעל, מייצר heartbeat, מנסה שוב לאחר תקלות ומגבה את בסיסי הנתונים.
- perps_risk.py — שער סיכון עצמאי: מינוף, Risk Tiers, initial/maintenance margin משוער, collateral, סיכון לעסקה, חשיפה, הפסד יומי ו־drawdown.
- perps_paper_engine.py — חשבון SQLite נפרד ל־Paper, סימולציית מילוי לפי עומק, עמלות, Funding, Stop-Loss ו־Take-Profit.
- /perps-market — מצב collector ונתוני היסטוריה.
- /perps-paper — מצב חשבון ה־Paper, מספר פוזיציות, Equity, הפסד יומי, גיבויים והצהרת execution.

## הפעלה

ה־Paper engine מופעל באמצעות משתנה סביבה:
    ENABLE_PERPS_PAPER_ENGINE=true

ברירת המחדל בקובץ .env.example היא false. תצורת Railway IaC בענף זה מפעילה אותו במצב Paper בלבד. ה־source של Railway עדיין נשאר main; קובצי IaC אינם מפעילים פריסה אוטומטית.

## מגבלות מסחר מדומה וסיכון

ברירות מחדל שמרניות בקוד:
- תקרת מינוף: 2x, ללא תלות במקסימום המינוף הגבוה יותר של המכשיר.
- גודל פוזיציה מרבי: 20 USD; חשיפה כוללת מרבית: 20 USD.
- עד 3 פוזיציות פתוחות במקביל.
- Stop distance התחלתי: 1%; Take-Profit: 2%.
- Risk budget לעסקה: 0.5% מההון.
- הפסד יומי מרבי: 2%; drawdown מרבי: 5%.
- תקרת spread: 250 bps; תקרת impact לכניסה: 100 bps.
- גיל Ticker/Book מרבי: 10 שניות.
- היסטוריית Paper snapshots נשמרת עד 14 ימים כברירת מחדל.
- עמלות Paper: 0.05% לכל צד כברירת מחדל — הנחת סימולציה בלבד, לא הצהרה שזהו תעריף Polymarket הנוכחי.

## אסטרטגיית אותות

נוסף מודול מחקר EMA 8/21 עם מומנטום 5 נרות וסינון תנודתיות 14 נרות. הוא משתמש רק בנרות דקה שהושלמו, דורש לפחות 60 נרות רציפים, ומחשב Stop בטווח 0.5%-1.5% ויעד התחלתי של 2R. זהו baseline ניסיוני בלבד; אין תוצאות OOS או הוכחה ליתרון מסחרי.

`PERPS_SIGNAL_EVALUATION_ENABLED=true` מאפשר להעריך ולרשום אותות. `PERPS_AUTO_ENTRY_ENABLED=false` נשאר כבוי ב-Railway. פתיחת כניסות Paper אפשרית רק אחרי הפעלה מפורשת של דגל זה, לעולם לא פקודות live.

כל החלטה נרשמת ב-SQLite לפי `(instrument_id, bar_close_ms)`, עם claim אטומי ומנגנון reclaim מוגבל לנעילה ישנה. כך worker restart אינו יוצר כפילות של אותו נר. ב-`/perps-paper` מוצגים מספר אותות, כניסות שנפתחו, כניסות שנחסמו ומצב האסטרטגיה.

## מה כבר אוטומטי

- collector רציף יחיד שומר נתונים ציבוריים עם חותמות זמן.
- worker מפעיל את מנוע ה־Paper לאחר כל מחזור נתונים.
- אם קיימת פוזיציית Paper, מנוע ה־Paper מעדכן Mark, עוקב אחרי Funding ומנסה לכבד Stop-Loss/Take-Profit.
- נתוני Ticker/Book ישנים, Instrument לא מתאים, סתירת מזהי instrument, ספר פקודות לא תקין או Funding חסר מונעים פתיחת כניסה חדשה.
- חוסר Funding מסומן כ־gap וחוסם כניסות חדשות; עם זאת, הוא אינו חוסם יציאה מגינה קיימת.
- SQLite משתמש ב־WAL ו־FULL synchronous; גיבוי מתבצע באמצעות SQLite online backup, integrity_check, foreign_key_check, SHA-256 ושמות קבצים ייחודיים.
- ה־supervisor מנטר את ה־worker ומפעיל אותו מחדש לאחר יציאה; ה־HTTP health endpoint מסמן worker שאינו בריא.
- worker וסביבתו אינם מקבלים מפתחות Wallet/Polymarket/Perps; כל דגלי live execution נשארים false.

## מה עדיין לא קיים — ואסור להציג כאילו קיים

1. אין אסטרטגיית כניסה אוטונומית פעילה. ה־Paper engine מקבל פקודות open_position מקוד מחקר נפרד, אבל ה־worker הנוכחי אינו מייצר אותות LONG/SHORT ולא פותח פוזיציות חדשות.
2. Funding הוא קירוב: המנוע מבחין במעבר next_funding_ms ומשתמש בשיעור הקודם וב־Mark האחרון שנצפה. הוא אינו משחזר את חישוב ה־time-weighted funding המלא של הבורסה.
3. Maintenance Margin מחושב כאומדן מ־Risk Tier. אין כאן עדיין חיקוי מלא של Liquidation Price, exchange margin engine, liquidation fee, insurance/ADL או settlement.
4. Stop/Take-Profit נבחנים רק כאשר מתקבל snapshot חדש. זו אינה יציאה רציפה בזמן אמת ואין הבטחה לביצוע במחיר ה־Stop במקרה של gap, השבתת API או נזילות חסרה.
5. Paper fills מבוססים על עומק גלוי ואינם מדמים Queue Priority, hidden liquidity, latency או את מלוא מנגנון matching של הבורסה.
6. גיבויים נשמרים באותו Volume שהוגדר. זה אינו תחליף לגיבוי off-site מוצפן ולתרגיל שחזור בפועל.
7. אין תוצאות Walk-forward / OOS שמוכיחות יתרון מסחרי ל־Perps. אין להפעיל מסחר חי על בסיס תוצאות Paper.

## Release gates למסחר חי

לפני חיבור order submission נדרשים: אסטרטגיית כניסה עם נתוני היסטוריה point-in-time, walk-forward ו־out-of-sample; אימות מלא של Funding ועמלות עדכניות; סימולציית liquidation מול מנוע הבורסה; בדיקת partial fills/reconciliation; circuit breakers נפרדים ל־Perps; בדיקות restart/recovery/backup restore; אבטחת מפתחות ב־secret manager; ובדיקת eligibility/region. גם לאחר מכן נדרש אישור מפורש של המפעיל.

בענף הזה:
- PERPS_SIGNAL_EVALUATION_ENABLED = true ב-Railway
- PERPS_AUTO_ENTRY_ENABLED = false ב-Railway
- research_only = true
- execution = "none"
- live_execution_enabled = false
- live_execution_eligible = false
