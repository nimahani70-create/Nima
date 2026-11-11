# وضعیت و راهنمای GitHub

مقصد درخواستی: `nimahani70-create/cafe-nima-management`، با visibility خصوصی.

در اسپرینت اول، اتصال GitHub فعال و حساب `nimahani70-create` شناسایی شد، ولی هم connector و هم `gh repo view` مخزن را پیدا نکردند. تلاش مجاز برای ساخت با این دستور:

```bash
gh repo create nimahani70-create/cafe-nima-management --private --source=. --remote=origin
```

با خطای GitHub `Resource not accessible by integration (createRepository)` رد شد. بنابراین مخزن راه دور ساخته نشده، push انجام نشده و اجرای GitHub Actions قابل تأیید نیست. خطای ۴۰۴ به تنهایی اثبات نمی‌کند که مخزن خصوصی در حساب وجود ندارد؛ ممکن است دسترسی برنامه به آن داده نشده باشد.

## اقدام لازم مدیر فنی

۱. اگر مخزن وجود ندارد، در حساب خود یک مخزن **Private** به نام `cafe-nima-management` ایجاد کنید؛ برای انتقال تاریخچه محلی، آن را بدون README و commit اولیه بسازید.

۲. در تنظیمات برنامه GitHub متصل به Codex، این مخزن را به repository access اضافه کنید و مجوز نوشتن محتوا بدهید. به علت وجود `.github/workflows/test.yml`، انتشار workflow نیز باید مجاز باشد. برای CLI محلی می‌توانید با احراز هویت خودتان `gh auth login` را انجام دهید؛ توکن را در چت یا فایل سورس نفرستید.

۳. از همین checkout، پس از تأیید خالی‌بودن remote:

```bash
git remote add origin https://github.com/nimahani70-create/cafe-nima-management.git
git push -u origin main
```

اگر remote قبلاً history دارد، ابتدا `git fetch origin` و مقایسه تاریخچه انجام شود؛ از force push استفاده نشود. اگر تنها بسته bundle را منتقل کرده‌اید، `git clone cafeyar360-sprint1.bundle cafe-nima-management` تاریخچه را بازیابی می‌کند؛ remote آن clone به فایل bundle اشاره می‌کند و باید آگاهانه به URL بالا تغییر کند.

پس از push، وضعیت Private و نتیجه Actions بررسی شود. دیتابیس، `.env`، بکاپ و توکن نباید در Git باشند؛ `.gitignore` آن‌ها را کنار می‌گذارد. دیتابیس نمونه از `DEMO_MODE=1 python manage.py init` ساخته می‌شود؛ در Windows متغیر محیطی را با `set` تنظیم کنید.
