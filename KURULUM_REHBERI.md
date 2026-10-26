# Kurulum ve Canlıya Alma Rehberi

Sıra önemli: 1 → 2 → 3 → 4 → önce yerelde deneme → 5 → 6 → 7.

> **Güvenlik:** Depo **public** olacak. `.env` dosyan `.gitignore` ile korunuyor ama yine de hiçbir anahtarı koda, commit mesajına veya issue'ya yazma. Tüm anahtarlar yalnızca GitHub Secrets ve Render/Apify ortam değişkenlerinde durur.

---

## 1. Telegram (10 dk)

1. `@BotFather` → `/newbot` → **`TELEGRAM_BOT_TOKEN`**'ı kaydet. Botun kullanıcı adını (`@olmadan`) **`TELEGRAM_BOT_USERNAME`** olarak not al.
2. **Ücretsiz kanal (public):** Yeni kanal aç, botu **yönetici** yap (mesaj gönderme yetkisi yeterli). Kanal ID'si: `@kanal_adin` veya `-100...` ile başlayan sayı → **`TELEGRAM_FREE_CHANNEL_ID`**.
3. **VIP grup (private supergroup):**
   - Grup ayarlarından **Topics (Konular)** özelliğini aç.
   - Şu konuları oluştur: `💻 Software Dev`, `🎨 Graphic Design`, `🎬 Video Editing`, `✍️ Copywriting & SEO`.
   - Botu **yönetici** yap ve şu yetkileri ver: **Kullanıcı davet et (Invite users)**, **Kullanıcıları yasakla (Ban users)**, mesaj gönder.
   - Grup ID'si (`-100...`) → **`TELEGRAM_VIP_GROUP_ID`**. ID'yi öğrenmek için gruba geçici olarak `@RawDataBot` ekleyip ID'yi oku, sonra çıkar.
   - Her konunun bağlantısını kopyala; sondaki sayı o konunun ID'sidir (`https://t.me/c/123456789/12` → `12`):
     `TOPIC_ID_SOFTWARE`, `TOPIC_ID_DESIGN`, `TOPIC_ID_VIDEO`, `TOPIC_ID_MARKETING`.

## 2. Groq (2 dk)

1. `console.groq.com/keys` → ücretsiz **`GROQ_API_KEY`** oluştur.
2. `console.groq.com/settings/limits` sayfasında ücretsiz katman limitlerini (özellikle günlük token) kontrol et.
3. Sistem iki aşamalı çalışır: küçük model (`openai/gpt-oss-20b`) ilanı eler, `openai/gpt-oss-120b` yalnızca elemeyi geçenleri puanlayıp pitch yazar. Kota yetmezse `MAX_AI_CALLS_PER_RUN` değerini düşür.

## 3. Supabase (5 dk)

1. `supabase.com` → ücretsiz proje aç.
2. SQL Editor'a şu komutun çıktısını yapıştırıp çalıştır:
   ```
   python -m src.database
   ```
3. **Project Settings → API** sayfasından `SUPABASE_URL` ve **`service_role`** anahtarını (`SUPABASE_KEY`) al. `anon` anahtarını **kullanma**; tablolar RLS ile kilitli.

## 4. Yerelde deneme (5 dk)

```
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env             # sonra .env içini doldur
python main.py --dry-run           # yalnızca GROQ_API_KEY ister; Telegram'a hiçbir şey göndermez
```

Dry-run çıktısında puanlanmış ilanları ve örnek VIP mesajını görürsün. Memnun kalırsan `python main.py` gerçek bir döngü çalıştırır (Supabase ve Telegram ayarları gerekir).

## 5. GitHub (public depo, 10 dk)

1. Yeni hesapta **Public** bir depo aç ve dosyaları yükle (`.env` zaten yüklenmez).
2. **Settings → Secrets and variables → Actions → New repository secret** ile şunları ekle:

   | Zorunlu | İsteğe bağlı |
   | :--- | :--- |
   | `GROQ_API_KEY` | `GROQ_MODEL` |
   | `SUPABASE_URL`, `SUPABASE_KEY` | `GROQ_FALLBACK_MODEL` (`none` = kapalı) |
   | `TELEGRAM_BOT_TOKEN` | `GROQ_TRIAGE_MODEL` (`none` = ön eleme kapalı) |
   | `TELEGRAM_VIP_GROUP_ID`, `TELEGRAM_FREE_CHANNEL_ID` | |
   | `TOPIC_ID_SOFTWARE`, `TOPIC_ID_DESIGN`, `TOPIC_ID_VIDEO`, `TOPIC_ID_MARKETING` | |
   | `LEMON_SQUEEZY_CHECKOUT_URL` | |

3. **Actions** sekmesinden `Lead Radar Cron` iş akışını **Run workflow** ile elle çalıştır ve logu kontrol et. Sonrasında her 20 dakikada bir kendi çalışır.
4. Public depolarda 60 gün hiç commit/etkinlik olmazsa GitHub zamanlanmış işleri durdurur; Actions sekmesinden yeniden etkinleştir.

## 6. Lemon Squeezy + Webhook sunucusu (20 dk)

### 6.1 Ürün
1. Lemon Squeezy'de **aylık $19 abonelik** ürünü oluştur. Checkout bağlantısı → `LEMON_SQUEEZY_CHECKOUT_URL`.
2. Ürün ayarlarında satın alma sonrası yönlendirme adresini sonraki adımda alacağın `https://SUNUCUN/welcome` yap.

### 6.2 Sunucu (Render ücretsiz web servisi örneği)
Güncel ücretsiz limitleri Render'ın sitesinden kontrol et; ücretsiz servisler boştayken uyuyabilir, ilk istek yavaş olur (Lemon Squeezy başarısız webhook'ları yeniden dener).

1. Render → **New → Web Service** → GitHub deposunu seç.
2. Ayarlar:
   - Runtime: **Python 3**
   - Build Command: `pip install -r requirements-cron.txt fastapi "uvicorn[standard]"`
   - Start Command: `python -m src.lemon_webhook serve`
   - Health Check Path: `/health`
3. Environment Variables:
   `PYTHON_VERSION=3.12.7`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_VIP_GROUP_ID`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` (uzun rastgele bir metin), `LEMON_SQUEEZY_WEBHOOK_SECRET` (aşağıda belirleyeceksin), `LEMON_SQUEEZY_CHECKOUT_URL`.
4. (İsteğe bağlı) UptimeRobot gibi ücretsiz bir servisle `https://SUNUCUN/health` adresini 5 dakikada bir pingleyerek uykuyu önleyebilirsin.

### 6.3 Webhook'ları bağla
1. **Telegram:** Yerelde (aynı `.env` değerleriyle) çalıştır:
   ```
   python -m src.lemon_webhook set-webhook https://SUNUCUN
   ```
2. **Lemon Squeezy:** Settings → Webhooks → yeni webhook:
   - URL: `https://SUNUCUN/webhooks/lemonsqueezy`
   - Signing secret: Render'daki `LEMON_SQUEEZY_WEBHOOK_SECRET` ile **aynı** metin
   - Olaylar: `subscription_created`, `subscription_payment_success`, `subscription_expired`, `subscription_cancelled`, `subscription_resumed`, `subscription_unpaused`

### 6.4 Müşteri akışı
Satın alma → `/welcome` sayfası → müşteri botu açıp satın alırken kullandığı e-postayı yazar → bot tek kullanımlık VIP davet linkini gönderir → müşteri katılınca hesap aboneliğe bağlanır. Abonelik bitince (`subscription_expired`) otomatik gruptan çıkarılır. İptal edilen aboneler ödedikleri dönem bitene kadar grupta kalır.

Test için Lemon Squeezy **test modu** ile sahte bir satın alma yap.

## 7. Apify Store (10 dk)

1. `apify.com` → geliştirici hesabı → **Create Actor → Link Git repository** (aynı depo).
2. Actor ayarları → **Environment variables**: `GROQ_API_KEY` ekle ve **secret** olarak işaretle.
3. **Build**'i çalıştır; bittikten sonra Console'dan bir test çalıştırması yap.
4. **Publication → Monetization → Pay per event**: olay adı **`lead-analyzed`**, fiyat belirle ve yayınla.
   Fiyatı belirlerken Groq maliyetini ve ücretsiz kotanı unutma: her kullanıcı çalıştırması senin Groq kotanı harcar.

---

## Kontrol listesi

- [ ] `python main.py --dry-run` ilanları puanlıyor
- [ ] Actions'ta elle çalıştırılan iş akışı yeşil ve VIP grubunda doğru konuda mesaj var
- [ ] 4 saat sonra Free kanalda link içermeyen teaser görünüyor
- [ ] Lemon Squeezy test satın alımı → bot e-postayla davet linki veriyor
- [ ] Apify test çalıştırması dataset'e kayıt yazıyor ve olay ücretlendiriliyor

## Bilinen sınırlar

- Reddit veri merkezi IP'lerini kısıtlayabilir. Tarama bu yüzden her çalıştırmada 2 grubu dönüşümlü çeker ve RSS yedeği vardır; yine de bazı çalıştırmalarda Reddit sonuç vermeyebilir.
- Groq ücretsiz kotası dolarsa o çalıştırmada AI durur, işlenmeyen ilanlar bir sonraki çalıştırmada yeniden denenir.
- İş panolarındaki ilanların çoğu tam zamanlı iştir; puanları düşük tutulur.
