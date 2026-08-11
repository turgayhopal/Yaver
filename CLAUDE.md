# Yaver — offline sesli asistan

## Proje amacı

İnternetsiz çalışan, uyandırma kelimesiyle tetiklenen, Türkçe konuşan modüler bir
sesli asistan. Jarvis benzeri ama kademeli: önce basit soru-cevap, sonra ev
otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

**Şu anki aşama:** PC üzerinde tam zincir çalışıyor (duyuyor → anlıyor → cevap
veriyor → konuşuyor). Sıradaki iş: yetenek (tool calling) katmanı.

## Değişmez kısıtlar

Bunlar projenin varlık sebebi, tartışmaya açık değil:

1. **İnternetsiz çalışacak.** Modeller bir kez indirilir, sonra ağ gerekmez.
   Hiçbir modül bulut API'sine bağımlı olmayacak.
2. **Modüler olacak.** Her modül ayrı bir işlem (process). Yeni yetenek eklemek
   çekirdek koda dokunmayı gerektirmeyecek.
3. **Taşınabilir olacak.** Kod ileride Raspberry Pi 5'e (ARM64, Linux) taşınacak.
   Bu yüzden **platforma özel hiçbir şey Python koduna yazılmaz** — dosya yolu,
   cihaz numarası, model adı, IP adresi: hepsi `config.yaml` içindedir.

## Donanım

| Aşama | Donanım | Durum |
|---|---|---|
| Şimdi | Masaüstü PC, Windows, GTX 1660 6GB | aktif geliştirme |
| Sonra | Raspberry Pi 5 8GB | hedef |
| Elde ayrıca | Pi 3B+, Pi Zero 2W, BeagleBone Black, STM32MP157D-DK1 | ileride uç birim |

STM32MP157D-DK1'in M4 çekirdeği ileride gerçek zamanlı donanım kontrolü
(röle, PWM, motor) için düşünülüyor. LLM çalıştıramaz, o iş için değil.

## Mimari

```
mikrofon → [kulak] → MQTT → [beyin] → MQTT → [agiz] → hoparlör
                                ↕
                           [yetenekler]   (henüz yazılmadı)
```

Modüller birbirinin varlığını bilmez, sadece MQTT konularını bilir. Bir modül
kapalıyken diğerleri çalışmaya devam eder (kulak, ağız yoksa onay beklemez;
beyin, LLM kapalıysa hata cümlesi yayınlar).

### MQTT sözleşmesi

Bu şema projenin omurgası. Değiştirilecekse tüm modüller birlikte güncellenmeli.

| Konu | Yön | Yük |
|---|---|---|
| `yaver/ses/metin` | kulak → beyin | `{"metin": "...", "kaynak": "kulak-pc", "zaman": 0.0}` |
| `yaver/cevap/metin` | beyin → agiz | `{"metin": "...", "zaman": 0.0}` |
| `yaver/durum` | her yöne | `{"modul": "kulak\|agiz", "durum": "..."}` |

`yaver/durum` değerleri:
- `kulak` → `uyandi` : uyandırma kelimesi duyuldu, ağız onay çalsın
- `agiz` → `konusuyor` : hoparlör aktif, kulak sussun
- `agiz` → `hazir` : onay cümlesi bitti, kulak kayda başlayabilir
- `agiz` → `sustu` : kuyruk boşaldı

Henüz yazılmayan, tasarımı belli olan konular:
- `yaver/yetenek/istek` : beyin → yetenekler, `{"ad": "...", "parametre": {...}}`
- `yaver/yetenek/sonuc` : yetenekler → beyin, `{"ad": "...", "durum": "ok", "veri": ...}`

## Dosya düzeni

```
C:\yaver\
├── config.yaml          TÜM ayarlar. Platforma özel her şey burada.
├── kulak.py             mikrofon + uyandırma + konuşma tanıma
├── beyin.py             LLM istemcisi + hafıza + cümle akışı
├── agiz.py              seslendirme + çalma kuyruğu
├── mikrofon_bul.py      yardımcı: ses cihazlarını listeler/test eder
├── hafiza.json          kalıcı bilgiler (otomatik oluşur)
├── gecmis.json          son konuşma turları (otomatik oluşur)
├── sesler/              Piper ses modeli (.onnx + .onnx.json)
├── llama/               llama.cpp binary'leri
└── venv/                Python 3.11 sanal ortamı
```

## Çalıştırma

Dört ayrı terminal, hepsinde `venv` aktif:

```bash
# 1. LLM sunucusu
llama-server.exe -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
  --ctx-size 8192 --n-gpu-layers 99 --port 8080 --jinja

# 2, 3, 4
python beyin.py
python agiz.py
python kulak.py
```

Tek modül test modları (MQTT gerektirmez, hata ararken kullan):

```bash
python kulak.py --yaziyok       # tanıdığını ekrana yazar, yayınlamaz
python kulak.py --dll           # CUDA DLL teşhisi, sonra çıkar
python beyin.py --yaz "saat kac"
python agiz.py --de "merhaba"
python mikrofon_bul.py          # cihazları listele
```

## Teknoloji yığını

| Katman | Araç | Not |
|---|---|---|
| Uyandırma | openWakeWord, `hey_jarvis` modeli | özel "Yaver" modeli henüz eğitilmedi |
| Konuşma tanıma | faster-whisper, `base` model | CPU/int8, ~0.7 sn |
| LLM | llama.cpp server + Qwen3-4B-Instruct-2507 Q4_K_M | OpenAI uyumlu API, port 8080 |
| Seslendirme | Piper, `tr_TR-dfki-medium` | onay cümleleri açılışta önceden sentezlenir |
| Mesajlaşma | Mosquitto (MQTT), localhost:1883 | |

## Kod konvansiyonları

- **Değişken, fonksiyon ve yorum isimleri Türkçe.** (`konusmayi_kaydet`, `esik`,
  `on_bellek`) Mevcut koda uy, karıştırma.
- **Yorumlarda Türkçe karakter kullanma** (ASCII), Windows konsol kodlaması
  bozuyor. Kullanıcıya gösterilen `print` metinleri de ASCII.
- **Sabit değer yazma.** Yeni bir eşik, yol veya süre gerekiyorsa `config.yaml`'a
  ekle ve oradan oku.
- **Kütüphaneleri fonksiyon içinde import et** (paho, faster_whisper, piper).
  Böylece bir modül eksikken diğer test modları yine çalışır.
- **Hata durumunda çök, kapan değil.** Bir bileşen açılmazsa daha basit bir
  varyanta düş (örn. GPU→CPU) ve nedenini ekrana yaz.
- Bağımlılıklar: `paho-mqtt sounddevice numpy pyyaml openwakeword faster-whisper
  piper-tts httpx`

## Şu an çalışan / çalışmayan

**Çalışıyor:** uyandırma (skor ~0.94), Türkçe tanıma (isabetli), LLM cevabı,
cümle cümle akışlı seslendirme, onay cümlesi ("Efendim"), ağız konuşurken kulağın
susması, kalıcı konuşma geçmişi.

**Bilinen sorunlar:**

1. **CUDA açılmıyor.** `cublas64_12.dll is not found or cannot be loaded`.
   `nvidia-cublas-cu12` ve `nvidia-cudnn-cu12` kurulu (12.9 / 9.24),
   `os.add_dll_directory` ile yollar tanıtılıyor ama bulamıyor. Şu an CPU/int8
   ile çalışıyor, kabul edilebilir hızda. Çözülürse `config.yaml`'da
   `model: small` + `cihaz: cuda` yapılabilir, süre 0.7 sn → 0.2 sn'ye iner.
   Denenecek: ctranslate2 sürüm uyumu, DLL'i doğrudan ctranslate2 klasörüne
   kopyalamak.
2. **Sözü kesilemiyor.** Ağız konuşurken kulak tamamen sağır. Çözüm: ağız
   konuşurken kulak sadece uyandırma kelimesini dinlesin, duyarsa `sd.stop()`.
3. **Uyandırma kelimesi İngilizce.** "hey jarvis" kullanılıyor. Özel "Yaver"
   modeli openWakeWord'ün eğitim hattıyla, Piper'ın Türkçe sesiyle sentetik
   örnek üreterek eğitilebilir.

## Sıradaki işler (öncelik sırasıyla)

1. **`yetenekler.py`** — LLM'i araç çağırıcı olarak kullan. llama.cpp `--jinja`
   ile OpenAI uyumlu tool calling destekliyor. İlk araçlar: saat/tarih, basit
   hesap, zamanlayıcı. Her araç ayrı bir fonksiyon, kayıt (registry) üzerinden.
2. **Hafıza yazma** — "Yaver, adımın Mehmet olduğunu hatırla" → `hafiza.json`.
   Bunu da bir yetenek olarak yap, beyne gömme.
3. **Söz kesme** (yukarıdaki 2. sorun).
4. **Özel "Yaver" uyandırma modeli.**
5. **Pi 5'e taşıma** — `config.yaml`'da yol/cihaz/model değişiklikleri,
   `whisper.cihaz: cpu`, daha küçük LLM. Kod değişmemeli; değişmesi gerekiyorsa
   o bir tasarım hatasıdır, önce onu düzelt.

## Yapılmayacaklar

- LLM'i bir modülün içine gömme. Her zaman HTTP API'sinin arkasında dursun.
- Modüller arasında doğrudan fonksiyon çağrısı kurma. Tek iletişim yolu MQTT.
- Bulut servisi (OpenAI, Google STT vb.) ekleme. Proje offline olacak.
- `config.yaml` dışında ayar tutma.
