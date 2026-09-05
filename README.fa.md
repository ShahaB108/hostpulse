# HostPulse

[English](README.md) | فارسی

تشخیص کاربران پرمصرف روی سرورهای DirectAdmin + CloudLinux با اجرای مجموعه‌ای
از کالکتورها (شمارش خطاهای LVE، اسنپ‌شات لحظه‌ای CPU/حافظه/پردازه‌ها) و
نوشتن گزارش JSON تلفیقی، به‌همراه خروجی اختیاری Prometheus برای node_exporter.

کالکتور فقط از کتابخانه استاندارد پایتون استفاده می‌کند (بدون هیچ وابستگی pip)
و برای اجرا با دسترسی root از طریق cron یا تایمر systemd طراحی شده است.

تمام فایل‌های موردنیاز HostPulse زیر دایرکتوری نصب (`/opt/hostpulse`)
نگه‌داری می‌شوند: خود کد، venv، فایل `config/htpasswd`، خروجی
`output/users.json`، لاگ‌ها و state. فقط فایل‌های یونیت systemd به
`/etc/systemd/system/` می‌روند.

## نصب

```bash
sudo mkdir -p /opt/hostpulse
sudo cp -r hostpulse/* /opt/hostpulse/
cd /opt/hostpulse

# نام فایل کانفیگ hostpulse.env است، نه .env
sudo cp hostpulse.env.sample hostpulse.env
sudo nano hostpulse.env
```

قبل از اجرای واقعی روی سرور، این موارد را در `hostpulse.env` بررسی/ویرایش کنید:

- `HOSTPULSE_LVEINFO_COMMAND` — مطمئن شوید سینتکس `lveinfo` با نسخه
  CloudLinux این سرور مطابقت دارد (`lveinfo --help`)
- `HOSTPULSE_PS_COMMAND` — اگر ترتیب ستون‌ها را تغییر دادید، پارس ترتیبی در
  `collectors/live_stats.py` را هم باید به‌روز کنید
- تمام آستانه‌های `HOSTPULSE_*_WARNING` / `HOSTPULSE_*_CRITICAL` — مقادیر
  پیش‌فرض فقط حدس اولیه‌اند، نه اعدادی که برای سرورهای شما تنظیم شده باشند
- `HOSTPULSE_IGNORED_USERS` — حساب‌های سرویسِ مخصوص همین سرور را به لیست
  پیش‌فرض داخلی اضافه کنید

## اجرای دستی (قبل از اتکا به cron/تایمر تست کنید)

```bash
cd /opt/hostpulse
python3 hostpulse.py
cat output/users.json
```

برای اطمینان از خروجی هر کالکتور روی سرور واقعی، هر کدام را جداگانه اجرا کنید:

```bash
python3 -m collectors.lve_faults
python3 -m collectors.live_stats
```

## اجرای خودکار

نمونه cron (هر ۱۵ دقیقه):

```
*/15 * * * * root /usr/bin/python3 /opt/hostpulse/hostpulse.py
```

یا یک سرویس + تایمر systemd که به همین دستور اشاره کند — یک سرویس oneshot که
با تایمر فعال می‌شود؛ جز `WorkingDirectory=/opt/hostpulse` تنظیم خاصی لازم نیست.

## وب‌سرویس (FastAPI، پورت 35707)

به‌جای خواندن مستقیم `users.json` از دیسک، گزارش را می‌توان با
`hostpulse_web.py` — یک اپ کوچک FastAPI محافظت‌شده با HTTP Basic Auth مبتنی
بر فایل htpasswd — روی HTTP سرو کرد (وب‌سرویس تنها بخشی از HostPulse است که
وابستگی pip دارد؛ خود کالکتور همچنان stdlib-only می‌ماند).

نصب یک‌مرحله‌ای (ساخت venv، تولید htpasswd، نصب یونیت‌های systemd و
فعال‌سازی وب‌سرویس + تایمر ساعتی):

```bash
sudo bash deploy/install-web.sh
```

اسکریپت تشخیص می‌دهد که از داخل خود دایرکتوری نصب اجرا شده (مثلاً بعد از
کپی ریپو در سرور: `cd /opt/hostpulse && bash deploy/install-web.sh`) و در
آن حالت با خیال راحت کپی فایل‌ها را رد می‌کند.

نصب دستی:

```bash
cd /opt/hostpulse
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# ساخت فایل htpasswd (هش apr1، همان فرمت `htpasswd -m`).
# مسیر پیش‌فرض /opt/hostpulse/config/htpasswd است:
.venv/bin/python hostpulse_web.py --add-user admin \
  --htpasswd /opt/hostpulse/config/htpasswd
```

Endpoint ها — همه به‌جز `/health` نیاز به Basic Auth دارند:

| Endpoint          | محتوا                                                          |
|-------------------|----------------------------------------------------------------|
| `GET /`           | گزارش JSON (همان خروجی `/users.json`)                          |
| `GET /users.json` | گزارش JSON تولیدشده توسط hostpulse.py                          |
| `GET /metrics`    | خروجی Prometheus (اگر `HOSTPULSE_PROM_OUTPUT` خالی باشد 404)   |
| `GET /health`     | probe سلامت، بدون احراز هویت                                   |


```bash
curl -u admin http://SERVER:35707/users.json
curl -u admin http://SERVER:35707/metrics
```

فایل htpasswd با هر تغییری خودکار reload می‌شود — افزودن/حذف کاربر نیازی به
ری‌استارت ندارد. تنظیمات مخصوص وب (متغیر محیطی یا drop-in سرویس در systemd):
`HOSTPULSE_WEB_HOST` (پیش‌فرض `0.0.0.0`)، `HOSTPULSE_WEB_PORT` (پیش‌فرض
`35707`)، `HOSTPULSE_HTPASSWD_FILE` (پیش‌فرض `/opt/hostpulse/config/htpasswd`).
مسیرهای JSON/Prometheus از همان متغیرهای `HOSTPULSE_JSON_OUTPUT` /
`HOSTPULSE_PROM_OUTPUT` که کالکتور استفاده می‌کند خوانده می‌شوند، پس API
همیشه همان چیزی را سرو می‌کند که کالکتورها نوشته‌اند.

### یونیت‌های systemd (وب‌سرویس + تایمر ساعتی)

پوشه `deploy/` شامل سه یونیت است — یا مستقیم فعالشان کنید یا از اسکریپت نصب
بالا استفاده کنید:

```bash
sudo cp deploy/hostpulse-web.service deploy/hostpulse-collect.service \
     deploy/hostpulse.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hostpulse-web.service  # API همیشه‌روشن روی :35707
sudo systemctl enable --now hostpulse.timer        # تازه‌سازی گزارش هر ساعت
systemctl list-timers hostpulse.timer
```

- `hostpulse-web.service` — پردازه uvicorn همیشه‌روشن؛ چون API باید همیشه در
  دسترس باشد این یونیت بالا می‌ماند و در صورت خطا خودکار ری‌استارت می‌شود.
- `hostpulse-collect.service` + `hostpulse.timer` — تایمر هر ساعت
  (`OnBootSec=5min`، `OnUnitActiveSec=1h`) کالکتور را با دسترسی root اجرا
  می‌کند تا `users.json` بازنویسی شود؛ سپس وب‌سرویس داده تازه را سرو می‌کند.
  بررسی لاگ‌ها: `journalctl -u hostpulse-collect.service`.

## خروجی

در مسیر `HOSTPULSE_JSON_OUTPUT` نوشته می‌شود (پیش‌فرض
`/opt/hostpulse/output/users.json`):

```json
{
  "generated_at": "2026-08-16T10:00:00+00:00",
  "server": "lh675.irandns.com",
  "users": [
    {
      "username": "someuser",
      "metrics": {"pmemf": 62, "nprocf": 0, "cpuf": 0, "cpu_percent": 12.3, "rss_mb": 340.1, "nproc": 4},
      "causes": [
        {"source": "lveinfo", "metric": "pmemf", "value": 62, "status": "warning"}
      ],
      "score": 5,
      "status": "warning"
    }
  ],
  "collector_stats": {
    "lveinfo": {"status": "ok", "users_found": 3, "error": null},
    "live_stats": {"status": "ok", "users_found": 40, "error": null}
  }
}
```

فقط کاربرانی که حداقل از یک آستانه warning/critical عبور کرده باشند در گزارش
می‌آیند — این گزارش «کاربران پرچم‌شده» است، نه فهرست کامل کاربران.

امتیازدهی از نسخه 2.1.0 به‌ازای هر علت (cause) است: هر متریک که از آستانه
warning/critical عبور کرده باشد یک‌بار وزن دسته خودش را به score اضافه
می‌کند؛ مثلاً کاربری که در دو متریک LVE پرچم شده باشد، دو برابر وزن `lve`
امتیاز می‌گیرد.

## ارسال به Grafana

`HOSTPULSE_PROM_OUTPUT` را به مسیری داخل دایرکتوری textfile collector
node_exporter تنظیم کنید (مثلاً
`/var/lib/node_exporter/textfile_collector/hostpulse.prom`) تا
`hostpulse_user_score`، `hostpulse_user_status` و gauge های هر متریک مستقیم
در Prometheus/Grafana ظاهر شوند — در کنار گزارش JSON برای مواردی که جزئیات
کامل لازم دارند (causes، وضعیت هر کالکتور).

## نکات طراحی (چرا این ساختار)

- **نام آستانه‌ها فقط در یک جا زندگی می‌کنند**: جدول `THRESHOLD_SPEC` در
  `hostpulse.py`. هم `build_config()` و هم `evaluate_user()` از همین جدول
  می‌خوانند تا فهرست env-var ها جداگانه و دوباره در کد تکرار نشود — همین
  تکرار در نسخه قبلی باعث عدم تطابق نام env/کد شده بود؛ افزودن متریک جدید
  یعنی افزودن یک سطر به جدول، نه ویرایش سه جای جداگانه که باید دستی همگام
  می‌ماندند.
- **`HOSTPULSE_IGNORED_USERS` در فایل env جمع‌شونده (additive) است**، نه
  جایگزین لیست داخلی `DEFAULT_IGNORED_USERS` در `hostpulse.py`. فقط
  حساب‌های اضافی مخصوص همین سرور را در آن بنویسید.
- **ارجاع‌های `$HOSTNAME` و سایر `$VAR` در `hostpulse.env`** نسبت به محیط
  واقعی سیستم expand می‌شوند (شل نیست — هیچ دستوری اجرا نمی‌شود). اگر متغیر
  در محیطی که اسکریپت در آن اجرا می‌شود ست نشده باشد (کرون/systemd معمولاً
  `HOSTNAME` را export نمی‌کنند)، HostPulse وجود `$VAR` حل‌نشده را تشخیص
  می‌دهد و با یک هشدار لاگ‌شده به `os.uname().nodename` برمی‌گردد، به‌جای
  آنکه بی‌سروصدا رشته literal `"$HOSTNAME"` را نام سرور قرار دهد.
- **`lve_faults.py` اول `lveinfo --json` را امتحان می‌کند** و اگر خروجی JSON
  معتبر نبود، خودکار به پارس جدول متنی برمی‌گردد (نسخه‌های قدیمی‌تر lveinfo
  بدون پشتیبانی `--json`، یا هر خروجی غیرمنتظره). تطبیق‌داده‌شده با خروجی
  واقعی `--json` در CloudLinux: مپ فیلدها `ID`→username، `PMemF`→pmemf،
  `NprocF`→nprocf. توجه: در این build از lveinfo اصلاً فیلد خطای CPU وجود
  ندارد (کلید `CPUf` نیست) — متریک `cpuf` روی چنین سرورهایی صفر می‌ماند؛
  حذف نشده چون برای نسخه‌هایی که آن را گزارش می‌کنند نگه داشته شده است.

## مواردی که قبل از اعتماد در پروداکشن باید بررسی شوند

- `collectors/lve_faults.py`: پارسر جدول چند سبک جداکننده را مدیریت می‌کند و
  هدر را با نام ستون پیدا می‌کند نه با موقعیت، اما در برابر خروجی واقعی هر
  نسخه CloudLinux اعتبارسنجی نشده — قبل از اتکا، روی هر نوع سرور مستقل
  اجرایش کنید (`python3 -m collectors.lve_faults`).
- `collectors/live_stats.py`: خروجی `ps` را ترتیبی پارس می‌کند
  (`user, pcpu, pmem, rss, pid`) — اگر ترتیب ستون‌های `HOSTPULSE_PS_COMMAND`
  را تغییر دادید، پارس را مطابق به‌روز کنید.
- آستانه‌ها و وزن‌های امتیازدهی در `hostpulse.env.sample` نقطه شروع هستند،
  نه اعدادی مشتق از داده واقعی سرور — بعد از چند روز مشاهده خروجی واقعی،
  تنظیمشان کنید.
