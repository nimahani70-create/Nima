# انتشار کافه یار ۳۶۰ در Nima

مرجع: https://github.com/nimahani70-create/Nima ، پروژه `cafeyar360/`، شاخه اصلی `main`. مخزن عمومی است؛ فقط سورس بررسی‌شده منتشر شود. مخزن مستقل قدیمی دیگر مقصد انتشار نیست.

قبل از کار، آخرین remote main و وضعیت فایل‌های محلی بررسی شود. SHA تاریخی `b8cde863a763594ae36a072bb59aa347004971b3` را جای بررسی آخرین وضعیت قرار ندهید. تاریخچه و checkout مستقل نسخه ۱.۴ حفظ شده و تغییرات آن به زیرپوشه پروژه منتقل می‌شود؛ ریشه سایت Mintlify تغییر نمی‌کند.

شاخه نامزد جاری `feature/sprint-2` است. برای هر اسپرینت از نام `feature/sprint-N` استفاده کنید. تغییر بیرون پروژه فقط workflow تأییدشده `.github/workflows/cafeyar360-tests.yml` است. فایل workflow قدیمی داخل زیرپوشه به‌تنهایی در GitHub اجرا نمی‌شود؛ workflow ریشه، با working-directory پروژه و فیلتر مسیر، مرجع CI است.

```bash
git fetch origin
git status --short --branch
git push -u origin feature/sprint-2
```

پس از بررسی امنیت و موفقیت تست‌ها، PR به main ایجاد و نتیجه Actions روی HEAD واقعی آن بررسی شود. Force Push یا reset مخرب ممنوع است. اگر main پس از انتشار شاخه تغییر کرد، آن را با commit ادغام عادی هماهنگ کنید. هیچ بخش دیگر مخزن در PR نباید تغییر کند.

PR بدون تأیید صریح مالک ادغام نمی‌شود. اسپرینت سوم بعد از تأیید و ادغام PR دوم از main به‌روز شروع می‌شود. قبل از تأیید، خروجی‌ها فقط با برچسب candidate در Drive/review قرار می‌گیرند؛ نسخه رسمی پس از تأیید و ادغام از SHA نهایی ساخته می‌شود.

برای انتقال bundle، شاخه آن را صریح انتخاب کنید؛ origin حاصل از clone bundle به فایل محلی اشاره می‌کند و باید آگاهانه به URL مخزن Nima تنظیم شود. هیچ history از checkout قبلی حذف نمی‌شود و main بدون PR بازنویسی نمی‌شود.

اگر Push یا ایجاد/تغییر workflow رد شد، خطای واقعی گزارش و مجوز نوشتن contents و workflow برای اتصال GitHub درخواست شود؛ توکن در چت یا سورس نوشته نشود. دسترسی خواندن یا metadata دارای push به‌تنهایی شاهد موفقیت Push نیست. لینک Commit/PR فقط پس از ایجاد و بازخوانی آن‌ها گزارش شود.

پوشه سورس و پشتیبان نهایی: https://drive.google.com/drive/folders/1NArzX1JX6zGgmn3oxnisgBdX-yPSbR0S

پوشه نامزد و دمو/review: https://drive.google.com/drive/folders/1XQacIaCup98FmarhKyJZjrow_A_Fr9Md

ZIP پروژه و workflow مجاز باید مستقیماً از commit اعلام‌شده ساخته شوند. فایل‌های اضافه، .env و DB عملیاتی داخل ZIP سورس نیستند. DB نمونه جدا و فقط ساختگی است. Bundle، گزارش، نتایج تست و SHA256 همراه نسخه‌اند. پس از آپلود، ZIP دوباره خوانده و hash و محتوایش با همان commit تطبیق داده شوند؛ در صورت نبود اتصال، فایل‌های محلی آماده و خطا شفاف گزارش شوند.
