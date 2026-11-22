# Polymarket Perps — Paper Research (עברית)

## מה נכנס לענף הזה

הקוד מפריד בין שוקי התחזיות הבינאריים לבין חוזים תמידיים (Perps). מנוע Perps משתמש בנתוני REST ציבוריים בלבד, בכתובת https://api.perpetuals.polymarket.com, וקורא את רשימת המכשירים, ה־tickers וספרי הפקודות. אין שימוש ב־wallet, במפתח פרטי, ב־authenticated trading session או ב־order endpoint.

המודולים:
- perps_market_data.py — קריאת מכשירים, Mark/Index, מחיר אחרון, Funding, רמות סיכון, ספר פקודות, חותמות זמן וסינון נתונים ישנים/לא תקינים.
- perps_risk.py — שער סיכון עצמאי לפני פתיחת פוזיציית Paper: מינוף, Risk Tiers, Margin ראשוני ותחזוקתי משוער, חשיפה, מגבלת הפסד יומית, Drawdown, מרווח ומלאי בטחונות.
- perps_paper_engine.py — חשבון SQLite נפרד לשוק Perps, מילוי מדומה לפי ספר הפקודות, שמירת Margin, עמלות, Funding, Stop-Loss ו־Take-Profit.
- perps_paper_worker.py — worker רציף לקריאת נתוני שוק, עדכון Mark, ניהול יציאות/מימון, heartbeat, retry ו־backup.

## הפעלה מקומית של Paper worker

הגדר משתני סביבה מתוך .env.example או במנהל הסודות של סביבת ההרצה. אין להזין מפתחות API או Wallet — worker זה לא זקוק להם.

הפעל באמצעות:
    python perps_paper_worker.py

הגדרות ברירת מחדל שמרניות:
- מינוף חשבונאי מדומה מרבי: 2x. זהו cap של מנוע המחקר ולא מינוף מומלץ.
- הון Paper: 100 USD.
- גודל פוזיציה מרבי: 10 USD.
- חשיפה כוללת מרבית: 20 USD.
- לכל היותר 3 פוזיציות פתוחות.
- Risk budget לעסקה: 0.5% מההון; מרחק Stop התחלתי: 1%.
- הפסד יומי מרבי: 2%; Drawdown כולל מרבי: 5%.
- תקרת מרווח: 250 bps; תקרת החלקת כניסה: 100 bps.
- מקסימום גיל נתוני Ticker/Book: 10 שניות.
- עמלת taker ברירת מחדל לצורכי Paper: 0.05% לכל צד, כקלט מחקרי שניתן לשנות לפי לוח העמלות בפועל.

## מגבלות שחשוב להבין

1. ה-worker עדיין לא מייצר כניסות. הוא אוסף שוק, מעדכן פוזיציות שכבר נפתחו ב־Paper, מטפל ב־Funding וביציאות. אסטרטגיית כניסה אוטונומית תתווסף רק לאחר פיתוח ובדיקת נתוני Candles, Funding history, עלויות וזיהוי משטרי שוק.
2. מודל Funding כרגע הוא קירוב: הוא מזהה מעבר של מועד ה־Funding, משתמש בשיעור הקודם וב־Mark האחרון שנצפה. מנגנון Funding בפועל מבוסס על חישוב/מיצוע לאורך זמן ועל מצב פוזיציה בזמן settlement. יש לאמת את פרטי החיוב המדויקים מול היסטוריית Funding לפני כל מסקנה על רווחיות.
3. Maintenance Margin מוצג כאומדן לפי ה־Risk Tier; אין כאן עדיין סימולציה מלאה של Liquidation Price, exchange margin engine, ביטוח/ADL או מנגנון settlement. שער הסיכון אינו תחליף למנוע הסיכון של הבורסה.
4. ה־Paper fills צורכים עומק גלוי בלבד ואינם משחזרים Queue Priority, Latency, hidden liquidity, שינויי ספר בין שליחת פקודה למילוי, או Partial Fills אמיתיים.
5. Stop-Loss/Take-Profit נבחנים בכל snapshot. אין הבטחה ליציאה במחיר ה־Stop, ב־gap או בזמן שבו מקור הנתונים/השירות אינו זמין.
6. גיבוי SQLite נשמר לפי התיקייה שהוגדרה. גיבוי באותו Volume אינו הגנה מפני אובדן ה־Volume; עדיין נדרשים גיבוי מחוץ לשרת, הצפנה ובדיקת שחזור.
7. תוצאות Paper אינן הוכחה ליתרון מסחרי או לאפשרות מסחר חי. אין לאפשר ביצוע אמיתי לפני בדיקות Walk-forward עצמאיות, אימות עמלות ו־Funding, בדיקת Liquidation מול הבורסה, סקירת אבטחה, eligibility/geo, ניטור והתאוששות, ואישור מפורש של מפעיל.

## בטיחות

הקוד בענף זה מסומן research_only=true, execution=none, ו־live_execution_eligible=false. אין לפתוח מסחר חי דרך worker זה. אל תכניס מפתחות פרטיים, seed phrases או פרטי חשבון לקובצי Git, ל־issue, ל־PR או לשיחה.
