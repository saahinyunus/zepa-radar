# Kaynak uyum kaydı (source compliance register)

Bu belge, sistemin hangi kaynaklara nasıl eriştiğini ve her kaynağın kullanım şartlarında **gerçekte ne
doğrulandığını** listeler. Hukuki görüş değildir. "Doğrulanmadı" yazan her satır, canlı satıştan önce
bir insan tarafından şartların kendisinden okunmalıdır.

Bir kaynağı kodu değiştirmeden kapatmak için `DISABLED_SOURCES` ortam değişkenini kullan
(GitHub Actions'ta workflow dosyasındaki `env` bölümüne ya da `.env`'e):

```
DISABLED_SOURCES=freelancer          # tek kaynak
DISABLED_SOURCES=freelancer,reddit   # birden fazla
ENABLED_JOB_BOARDS=remoteok,jobicy   # iş panolarından hangileri çekilsin
```

Kaynak kapalıyken diğer kaynaklar çalışmaya devam eder (`tests/test_sources.py` bunu doğrular).

| Kaynak | Nasıl erişiliyor | Şartların durumu | Durum |
|---|---|---|---|
| **Freelancer.com** | Herkese açık projeler API'si (`/api/projects/0.1/projects/active/`), anahtarsız, her çalıştırmada 1 istek | Kullanım şartları, "robot, örümcek, scraper veya başka otomatik yollarla" erişimi ve ilanları başka kaynaklarla birlikte toplamayı **yazılı izin olmadan yasaklıyor** (Bölüm 33 ve Bölüm 4, madde 11; özet üzerinden okundu, tam metin doğrulanmadı). İzin alınmadı. | **Riskli, açık.** İşletmeci bilerek açık tuttu; canlı satıştan önce yeniden karar verilecek. Kapatmak için `DISABLED_SOURCES=freelancer`. |
| **Jobicy** | Herkese açık JSON API | Ticari kullanım ve abonelik hizmetleri **açıkça serbest**; kaynağa atıf ve saatte en fazla bir otomatik sorgu şart. | Uyumlu. Sorgu saatte bir ile sınırlı (`HOURLY_BOARD_MINUTE_WINDOW`); ilan bağlantısı Jobicy sayfasına gidiyor. |
| **RemoteOK** | Herkese açık JSON API | API şartları: bağlantıyla geri yönlendirme ve "Remote OK" kaynak olarak anılmalı. Logo izinsiz kullanılamaz. | Mesajda kaynak adı ve ilan bağlantısı var. Bağlantının `nofollow` olmaması gerekiyor mu: Telegram'da geçerli değil, doğrulanmadı. |
| **We Work Remotely** | Herkese açık RSS | Doğrulanmadı. | Şartlar okunmalı. |
| **Reddit** | RSS (`/new.rss`); JSON uç noktaları datacenter IP'lerine 403 veriyor | Doğrulanmadı. Reddit'in ticari veri kullanımı kuralları genelde sıkıdır. | Şartlar okunmalı. Tarayıcı kimliği rastgele; bu ileride dürüst bir kimlikle değiştirilebilir. |
| **Hacker News** (Algolia API) | Herkese açık arama API'si, yalnızca "Who is hiring" / "Freelancer? Seeking freelancer?" konularındaki yorumlar | Doğrulanmadı. | Şartlar okunmalı. |
| **Remotive** | Kaldırıldı | Günde en fazla 4 sorgu; ilanlar 24 saat gecikmeli; ücretli abonelik kullanımı için ayrı erişim isteniyor. | Kapalı (kodu silindi). |
| **Arbeitnow** | Varsayılan olarak kapalı | Doğrulanmadı. İçerik Almanya/Hollanda'ya bağlı kadrolu iş, ürüne uymuyor. | Kapalı (`ENABLED_JOB_BOARDS`'ta yok). |

## Kaynak kapanırsa ürüne etkisi

Son ölçüme göre VIP'ye giden ilanların yaklaşık %88'i Freelancer'dan geliyordu (puanlama v2'de bu pay
düşecek). Freelancer kapatılırsa günlük hacim belirgin şekilde azalır ve Reddit ile HN ağırlık kazanır.
Bu yüzden Freelancer kararı verilmeden önce ikame kaynaklar (yeni Reddit alt grupları, ayın HN konuları)
hazır tutulmalı.

## Canlı satıştan önce insan tarafından yapılacaklar

1. Freelancer şartlarının tam metnini oku (Bölüm 4 ve 33); izin e-postasını gönder ya da kaynağı kapat.
2. Reddit, Hacker News ve We Work Remotely şartlarını oku; ticari kullanıma izin verip vermediklerini not et.
3. Şartları doğruladığında bu tabloyu güncelle (tarih ile).
