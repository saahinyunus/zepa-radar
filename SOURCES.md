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
| **Reddit** | RSS (`/new.rss`); JSON uç noktaları datacenter IP'lerine 403 veriyor. 4 grupta 48 topluluk, her grup tek birleşik istek (`direct_jobs` her çalıştırmada, 18 topluluk) | Doğrulanmadı. Reddit'in ticari veri kullanımı kuralları genelde sıkıdır. | Şartlar okunmalı. Tarayıcı kimliği rastgele; bu ileride dürüst bir kimlikle değiştirilebilir. |
| **Himalayas** | Herkese açık JSON API (`himalayas.app/jobs/api`), anahtarsız, imleç sayfalama, 3 saatte bir | API sayfası: kendi iş panonuzda/sitenizde/uygulamanızda **sormadan gösterebilirsiniz**; kaynağa görünür bağlantı ve değiştirilmemiş `applicationLink` şart; veri günlük yenileniyor; ilanları **başka iş panolarına, toplayıcılara, Google Jobs/LinkedIn'e aktarmak yasak**; süresi dolan ilanlar kaldırılmalı (sayfa üzerinden okundu, tam şartlar doğrulanmadı). | Büyük ölçüde uyumlu. Açık soru: özel bir Telegram grubunun "kendi uygulamamız" sayılıp sayılmayacağı belirsiz, tam şartlar okunmalı. Yalnızca sözleşmeli/yarı zamanlı/geçici roller alınıyor. |
| **Hacker News** (Algolia API) | Herkese açık arama API'si, yalnızca "Who is hiring" / "Freelancer? Seeking freelancer?" konularındaki yorumlar | Doğrulanmadı. | Şartlar okunmalı. |
| **Remotive** | Kaldırıldı | Günde en fazla 4 sorgu; ilanlar 24 saat gecikmeli; ücretli abonelik kullanımı için ayrı erişim isteniyor. | Kapalı (kodu silindi). |
| **Arbeitnow** | Varsayılan olarak kapalı | Doğrulanmadı. İçerik Almanya/Hollanda'ya bağlı kadrolu iş, ürüne uymuyor. | Kapalı (`ENABLED_JOB_BOARDS`'ta yok). |

## Kaynak kapanırsa ürüne etkisi

Son ölçüme göre VIP'ye giden ilanların yaklaşık %87'si Freelancer'dan geliyordu (puanlama v2'de bu pay
düşecek). Freelancer kapatılırsa günlük hacim belirgin şekilde azalır ve Reddit ile HN ağırlık kazanır.
Bu yüzden Freelancer kararı verilmeden önce ikame kaynaklar (yeni Reddit alt grupları, ayın HN konuları)
hazır tutulmalı.

## Canlı satıştan önce insan tarafından yapılacaklar

1. Freelancer şartlarının tam metnini oku (Bölüm 4 ve 33); izin e-postasını gönder ya da kaynağı kapat.
2. Reddit, Hacker News ve We Work Remotely şartlarını oku; ticari kullanıma izin verip vermediklerini not et.
3. Şartları doğruladığında bu tabloyu güncelle (tarih ile).

## Kaynak araştırması (31 Ekim 2026)

Yeni kaynaklar için aday uç noktalar canlı denendi. Sonuç: iş panoları (RemoteOK, Jobicy, We Work Remotely,
Jobspresso, Dribbble Jobs, NoDesk, Working Nomads) neredeyse tamamen **kadrolu iş** yayınlıyor; "freelance /
contract / part-time" geçen ilan oranı %0-10. Bu yüzden serbest çalışma ilanları için en değerli kaynaklar
topluluklar (Reddit) ve serbest çalışma pazaryerleri (Freelancer.com). Eklenenler ve elenenler:

| Aday | Karar | Neden |
|---|---|---|
| Himalayas API | **Eklendi** | Resmî, ücretsiz; `employmentType` ile sözleşmeli roller kesin ayrılıyor. Hacim küçük, çoğu rol "uzman danışman" tipi. |
| r/hiring, r/gameDevJobs, r/gamedevclassifieds, r/RemoteJobs, r/SEOjobs, r/PythonJobs, r/WebDevJobs, r/HireaWriter | **Eklendi** | Gerçek işe alım gönderileri; birleşik istekte ek maliyet yok. |
| r/artcommissions, r/commissions | Elendi | Gönderilerin tamamı sanatçı ilanı (müşteri değil). |
| GitHub "bounty" ilanları | Elendi | Sonuçlar bot spam'i ve escrow dolandırıcılığı. |
| Mastodon #hiring, #jobs | Elendi | Çoğu iş panosu botu (13 hesap 40 gönderi), kadrolu işler. |
| Real Work From Anywhere RSS | Elendi | RSS dosyası geçersiz XML; kadrolu işler. |
| We Work Remotely ek kategoriler, Jobspresso, Dribbble Jobs, NoDesk | Elendi | Kadrolu iş, serbest çalışma oranı ~%0-5. |
| DEV.to #hiring, Lobsters, Authentic Jobs, FreelanceWritingJobs | Elendi | Aylardır güncellenmiyor. |
| ProBlogger, JustRemote, Dynamite Jobs, Remote.co, 4dayweek, Remoteleaf | Elendi | Boş, 404 veya bozuk RSS. |
| Upwork, Fiverr, PeoplePerHour, Guru, Truelancer, Workana | Alınmadı | Şartları tek tek okunmadı. Kamuya açık, anahtarsız bir ilan beslemesi bulunamadı; büyük pazaryerlerinin çoğu otomatik erişimi şartlarında yasaklar. Resmî API yolu için aşağıya bak. |

## Gemini kaynak listesi incelemesi (2 Kasım 2026)

"70+ kaynak" listesinin büyük kısmı zaten sistemdeydi ya da 31 Ekim'de denenip elenmişti. Her aday canlı
ölçüldü (Reddit: her alt dizin tek tek RSS ile, gönderi sıklığı ve ön filtreyi geçen oran; diğerleri:
erişim, tazelik, serbest çalışma oranı).

| Aday | Karar | Neden |
|---|---|---|
| r/thumbnails | **Eklendi** (`direct_jobs`) | "[hiring] thumbnail designer $30 per thumbnail" türü gerçek ilanlar; gönderilerin ~%6'sı ön filtreyi geçiyor. |
| r/freelance_forhire, r/hireafreelancer | Elendi | Bu adlarda topluluk yok (404). |
| r/logorequests | Elendi | 403 (erişilemiyor). |
| r/logodesign, r/UI_Design, r/web_design, r/Design, r/reactjs, r/python, r/androiddev, r/developersIndia, r/AppDevelopers, r/Freelancers, r/freelance, r/freelancewriters, r/ProgrammingBuddies | Elendi | Geri bildirim isteyen, öğrenen ya da serbest çalışanların kendi sohbeti; müşteri ilanı değil. Ön filtreyi geçenlerin neredeyse tamamı gürültü, LLM kotasını yer. |
| r/artcommissions | Elendi (yine) | Gönderilerin ~%94'ü sanatçı ilanı; müşteri ilanları hobi siparişi (evcil hayvan çizimi, DnD karakteri, $15-75). |
| Remotive | Kapalı kalıyor | Günde 4 sorgu ve 24 saat gecikme şartı; şimdi `limit=100` bile 12 ilan döndürüyor. |
| Authentic Jobs | Elendi | Freelance beslemesi boş (0 ilan), uzaktan beslemesinin en yeni ilanı 18 günlük. |
| Working Nomads | Elendi | RSS geçersiz XML; JSON API'de 35 ilan, en yenisi 3 günlük, başlık/etiketinde serbest çalışma geçen 1 ilan. |
| WWR DevOps, Jobspresso | Elendi | Kadrolu iş (serbest çalışma oranı ~%0-5). |
| Himalayas `/user-api/jobs` | Gerekmiyor | Bu adres 404; sistem zaten çalışan `himalayas.app/jobs/api`'yi kullanıyor. |
| Jobicy `jobType=contract` | Gerekmiyor | Parametre artık geçersiz (400); mevcut sorgu çalışıyor ve filtrelemeyi biz yapıyoruz. |
| HN `"looking for" OR "hiring" OR "freelancer"` | Yanlış | Algolia'da `OR` yok; sorgu sadece 52 anlamsız sonuç buluyor. Mevcut üç ayrı istek doğru yöntem. |
| HN "Freelancer? Seeking freelancer?" | Bilgi | Bu aylık başlık Ekim 2025'ten beri açılmıyor; yalnızca "Who is hiring?" sürüyor. Ekim 2026 başlığında 261 ilandan 162'sini sorgularımız yakalıyor, kaçırdıkları arasında sözleşmeli/freelance geçen en fazla ayda ~9 ilan var; ayrı bir tarayıcıya değmez. |

**Ön filtre iyileştirmesi.** Ölçüm sırasında iş arayan serbest çalışanların gönderileri ("looking for immediate
work", "[ FOR HIRE ]", "Title: [For Hire] ...") ön filtreyi geçip LLM kotasını harcıyordu: `direct_jobs`
akışında ön filtreyi geçen 28 gönderinin 4'ü bu türdendi. Başlıktaki "[for hire]" etiketi (boşluklu ya da
"Title:" önekli hâlleri dahil) ve "looking/seeking for work, jobs, gigs, clients, opportunities" kalıpları
artık eleniyor. Veritabanındaki 1.639 ilana uygulanınca elenecek 22 başlığın hepsi zaten 10 ve altında puan
almıştı, hiçbiri VIP'ye gitmemişti. İş panosu ilanlarına bu kural uygulanmıyor.

**Kota uyarısı.** Radar her çalıştırmada en yeni ilanlardan başlayarak en fazla 40 (fiilen Groq sınırıyla
~18-20) ilanı LLM'e gönderiyor. Gürültülü bir kaynak eklemek iyi ilanları kota dışında bırakır; bu yüzden
yukarıdaki listede gönderi sayısı değil, ön filtreyi geçen **müşteri** gönderisi oranı ölçüt alındı.

**Resmî yoldan erişilebilecek ama başvuru/anahtar gerektirenler** (işletmecinin yapması gerekir):
Upwork API (onaylı geliştirici başvurusu), Freelancer.com resmî geliştirici API'si (uygulama kaydı; yazılı
izin sorununu da çözebilir), Adzuna / Jooble (ücretsiz anahtar, ağırlıklı kadrolu iş).
