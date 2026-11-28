# סקירה עצמאית — PR #49: תקציב ה-heartbeat של Railway Perps

תאריך: 28 בנובמבר 2026 · נסקר על ידי: מודל רביעי (DeepAI Code, GLM) · סטטוס: ללא חוסמים, מוכן למיזוג

## אימות עקביות תקציב ה-watchdog
- `PERPS_WORKER_HEARTBEAT_TIMEOUT_SECONDS: 330` ב-`.railway/railway.ts` תואם עכשיו למינימום שהקוד מחשב הן ב-`perps_market_worker.py` והן ב-`service_supervisor.py`:
  `max(180, interval*3 + (market_limit + 10) * 8 + 80) = max(180, 30*3 + 20*8 + 80) = 330`
  עם ברירות המחדל `PERPS_POLL_INTERVAL_SECONDS=30` ו-`PERPS_MARKET_LIMIT=10`.
- קודם לכן 250 שניות היו מתחת לרצפה, ולכן ה-supervisor היה מעלה את הערך בזמן ריצה בכל מקרה — היישור מסיר סטייה בין ה-IaC לקוד.

## כלי אימות ושחזור גיבוי (backup_paper_data.py)
- מתנהג fail-closed כפי שמתועד: מסרב ל-manifest חלקי, מסרב לדרוס קובץ יעד קיים, השחזור יוצא ל-staging בלבד.
- תהליך ההפעלה ב-`DEPLOY_RAILWAY_HE.md` תואם ליישום.

## סמוק-טסט של היסטוריית Funding
- טענת סדר יורד של חותמות הזמן עם סובלנות של ±1 שנייה לגבולות החלון — סביר; לא נמצאו בעיות.

## המלצה (לא חוסמת)
- מכיוון ש-`service_supervisor.py` מעלה כל ערך מוגדר למינימום המחושב, ניתן להשמיט את הערך הסטטי מ-`railway.ts` ולתת לרצפה בקוד לחול אוטומטית — מפחית עוד מקום אחד לסטייה בעתיד בשינוי `PERPS_POLL_INTERVAL_SECONDS` או `PERPS_MARKET_LIMIT`.

## סיכום
נסקרו: workflows, IaC, כלי גיבוי, תקציב supervisor/worker. לא נמצאו חוסמים.
