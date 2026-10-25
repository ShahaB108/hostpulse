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

## نصب یک‌کلیکی

`deploy/install.sh` همه‌چیز را به‌صورت خودکار نصب می‌کند. رفتار پیش‌فرض دقیقاً
همان چیزی است که برای تغذیه Prometheus/Grafana لازم است: کالکتور + تایمر ساعتی
systemd + خروجی Prometheus — بدون venv و بدون pip (کالکتور stdlib-only است).
پنل وب اختیاری است و با `--web` نصب می‌شود (تنها بخشی که به پکیج‌های pip نیاز
دارد).

از داخل checkout ریپو:

```bash
sudo bash deploy/install.sh             # کالکتور + تایمر ساعتی + خروجی .prom
sudo bash deploy/install.sh --web       # + پنل FastAPI با Basic Auth روی :35707
```

بوت‌استرپ از راه دور (یک خط، بدون نیاز به clone — لینک RAW همین اسکریپت؛
نکته مهاجرت به GitLab پایین‌تر):

```bash
curl -fsSL https://raw.githubusercontent.com/ShahaB108/hostpulse/main/deploy/install.sh | sudo bash
# همراه با پنل وب:
curl -fsSL https://raw.githubusercontent.com/ShahaB108/hostpulse/main/deploy/install.sh | sudo bash -s -- --web
```

اسکریپت به این ترتیب کار می‌کند:

1. تعیین منبع کد: اگر از داخل خود checkout اجرا شود همان فایل‌ها استفاده می‌شوند
   (هیچ‌وقت دانلود نمی‌کند)؛ وگرنه tarball را از `--source`/`HOSTPULSE_SOURCE`
   دانلود می‌کند.
2. پیش‌چک: بررسی root،`python3 >= 3.6`، وجود systemd (در نبود systemd به
   `/etc/cron.d/hostpulse` برمی‌گردد).
3. نصب فایل‌های برنامه در دایرکتوری نصب (پیش‌فرض `/opt/hostpulse`)؛ ساخت
   `hostpulse.env` از نمونه فقط در نصب اول.
4. تنظیم `HOSTPULSE_PROM_OUTPUT` (پیش‌فرض روشن:
   `/var/lib/node_exporter/textfile_collector/hostpulse.prom`) و در صورت نیاز
   `HOSTPULSE_IGNORED_USERS`.
5. یک‌بار اجرای کالکتور تا `users.json` و فایل `.prom` بلافاصله بعد از نصب
   موجود باشند (بدون 503 / متریک خالی در اولین scrape).
6. با `--web`: ساخت venv، نصب `requirements.txt`، تولید یک رمز تصادفی برای
   ورود پنل و نمایش آن در خلاصه پایانی (همان لحظه ذخیره‌اش کنید — هیچ‌جای
   دیگر نوشته نمی‌شود).
7. نصب یونیت‌های systemd (با رعایت `--install-dir`، `--web-port`، `--interval`
   و مسیر واقعی `python3`) و فعال‌سازی تایمر — و با `--web` سرویس
   `hostpulse-web.service` هم.

آپشن‌ها (هر کدام معادل env با پیشوند `HOSTPULSE_*` دارند تا از طریق
`curl | bash -s --` بدون کوتیشن‌باز گرفتگی پاس شوند):

| آپشن | پیش‌فرض | کار |
|---|---|---|
| `--source URL` | پایین‌تر | آدرس tarball کد (`HOSTPULSE_SOURCE`)؛ فقط برای نصب از راه دور |
| `--install-dir DIR` | `/opt/hostpulse` | محل نصب (`HOSTPULSE_INSTALL_DIR`) |
| `--prom PATH` | `/var/lib/node_exporter/textfile_collector/hostpulse.prom` | مسیر خروجی Prometheus (`HOSTPULSE_PROM_PATH`) |
| `--no-prom` | — | غیرفعال‌کردن خروجی Prometheus |
| `--web` | خاموش | نصب پنل وب نیز (`HOSTPULSE_INSTALL_WEB=1`) |
| `--web-port N` | `35707` | پورت پنل (`HOSTPULSE_WEB_PORT`) |
| `--admin USER` | `admin` | نام کاربری پنل (`HOSTPULSE_ADMIN_USER`) |
| `--interval T` | `1h` | فاصله جمع‌آوری، مثلاً `15min`، `2h` (`HOSTPULSE_INTERVAL`) |
| `--ignore-users A,B` | — | حساب‌های اضافی به‌حاشیه (`HOSTPULSE_IGNORED_USERS`) |
| `--uninstall` | — | توقف و حذف سرویس‌ها/یونیت‌ها (فایل‌ها نگه داشته می‌شوند) |
| `--purge` | — | همراه `--uninstall`: حذف کامل دایرکتوری نصب شامل `hostpulse.env`، گزارش‌ها، state و htpasswd |

نکته‌ها:

- **مهاجرت میزبان کد (مثلاً GitHub شخصی → GitLab سازمان)**: آدرس دانلود دقیقاً
  در یک نقطه زندگی می‌کند — ثابت `DEFAULT_SOURCE` در ابتدای `deploy/install.sh`
  (شکل tarball گیت‌هاب و گیت‌لب کنارش مستند شده). پس از مهاجرت فقط همان خط و
  لینک curl در همین README را به‌روز کنید. نصب‌هایی که از داخل checkout اجرا
  شده‌اند اصلاً نیاز به دانلود ندارند و بدون تغییر کار می‌کنند.
- **Idempotent و مناسب آپگرید**: اجرای مجدد installer کد و یونیت‌ها را به‌روز
  می‌کند، ولی همیشه `hostpulse.env` موجود، htpasswd، گزارش‌ها و state را نگه
  می‌دارد.
- **بازیابی رمز پنل** (فراموشی یا تغییر):
  ```bash
  printf '%s\n' 'NEWPASS' | /opt/hostpulse/.venv/bin/python \
    /opt/hostpulse/hostpulse_web.py --add-user admin \
    --htpasswd /opt/hostpulse/config/htpasswd --password-stdin
  ```

## نصب دستی

موارد زیر همان کاری است که installer یک‌کلیکی به‌صورت خودکار انجام می‌دهد.

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

نصب یک‌فرمانی (توصیه‌شده؛ علاوه بر وب‌سرویس، خود کالکتور را هم یک‌جا نصب
می‌کند — venv، htpasswd، یونیت‌های systemd و فعال‌سازی وب‌سرویس + تایمر):

```bash
sudo bash deploy/install.sh --web
```

اسکریپت قدیمی‌تر `deploy/install-web.sh` همچنان کار می‌کند (وقتی از داخل خود
دایرکتوری نصب اجرا شود، مثلاً پس از کپی ریپو: `cd /opt/hostpulse && bash
deploy/install-web.sh`؛ در این حالت با خیال راحت کپی فایل‌ها را رد می‌کند)،
اما installer کامل بالا جایگزین آن است.

نصب دستی:

```bash
cd /opt/hostpulse
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# ساخت فایل htpasswd (هش apr1، همان فرمت `htpasswd -m`).
# مسیر پیش‌فرض /opt/hostpulse/config/htpasswd است:
.venv/bin/python hostpulse_web.py --add-user admin \
  --htpasswd /opt/hostpulse/config/htpasswd

# حالت غیرتعاملی (installer/CI)؛ رمز از stdin خوانده می‌شود:
printf '%s\n' 'YOURPASSWORD' | .venv/bin/python hostpulse_web.py \
  --add-user admin --htpasswd /opt/hostpulse/config/htpasswd --password-stdin
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
`hostpulse_user_score`، `hostpulse_user_status`، gauge های هر متریک و
جزئیات علل (causes) هر کاربر مستقیم در Prometheus/Grafana ظاهر شوند.
از نسخه 2.2.0 هر علت (لیست `causes` در گزارش JSON) به‌صورت یک سری جداگانه
export می‌شود، پس فایل `.prom` به‌تنهایی پاسخ «چرا این کاربر پرچم شده» را
می‌دهد:

```text
hostpulse_user_cause{username="u",source="lveinfo",metric="pmemf",status="critical"} 62
hostpulse_user_cause_threshold{username="u",source="lveinfo",metric="pmemf",level="warning"} 15
hostpulse_user_cause_threshold{username="u",source="lveinfo",metric="pmemf",level="critical"} 30
hostpulse_user_cause_count{username="u"} 1
```

سری آستانه‌ها (`level`) به‌ازای هر علت export می‌شود، چون برای متریک‌های
درصدی lCPU (`acpu`، `cpu_percent`) و سقف ایمیل مبتنی بر hostname آستانه‌ها
برای هر کاربر متفاوت‌اند. تنها وضعیت هر کالکتور (per-collector status)
همچنان به گزارش JSON نیاز دارد.

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
