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
mikrofon → [ear] → MQTT → [brain] → MQTT → [mouth] → hoparlör
                              ↕
                          [skills]   (henüz yazılmadı)
```

Modüller birbirinin varlığını bilmez, sadece MQTT konularını bilir. Bir modül
kapalıyken diğerleri çalışmaya devam eder (ear, mouth yoksa onay beklemez;
brain, LLM kapalıysa hata cümlesi yayınlar).

### MQTT sözleşmesi

Bu şema projenin omurgası. Değiştirilecekse tüm modüller birlikte güncellenmeli.

| Konu | Yön | Yük |
|---|---|---|
| `yaver/speech/text` | ear → brain | `{"text": "...", "source": "ear-pc", "timestamp": 0.0}` |
| `yaver/reply/text` | brain → mouth | `{"text": "...", "timestamp": 0.0}` |
| `yaver/status` | her yöne | `{"module": "ear\|mouth", "status": "..."}` |

`yaver/status` değerleri:
- `ear` → `woke` : uyandırma kelimesi duyuldu, mouth onay çalsın
- `mouth` → `speaking` : hoparlör aktif, ear sussun
- `mouth` → `ready` : onay cümlesi bitti, ear kayda başlayabilir
- `mouth` → `done` : kuyruk boşaldı

Henüz yazılmayan, tasarımı belli olan konular:
- `yaver/skill/request` : brain → skills, `{"name": "...", "params": {...}}`
- `yaver/skill/result` : skills → brain, `{"name": "...", "status": "ok", "data": ...}`

## Dosya düzeni

```
C:\yaver\
├── config.yaml          TÜM ayarlar. Platforma özel her şey burada.
├── ear.py                mikrofon + uyandırma + konuşma tanıma
├── brain.py               LLM istemcisi + hafıza + cümle akışı
├── mouth.py               seslendirme + çalma kuyruğu
├── find_microphone.py     yardımcı: ses cihazlarını listeler/test eder
├── memory.json            kalıcı bilgiler (otomatik oluşur)
├── history.json           son konuşma turları (otomatik oluşur)
├── voices/                Piper ses modeli (.onnx + .onnx.json)
├── models/                LLM gguf dosyası
├── llama/                 llama.cpp binary'leri
└── venv/                  Python 3.11 sanal ortamı
```

## Çalıştırma

Dört ayrı terminal, hepsinde `venv` aktif:

```bash
# 1. LLM sunucusu (bkz. Commands.txt)
cd C:\yaver\llama
llama-server.exe -m C:\yaver\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf ^
  --ctx-size 8128 --n-gpu-layers 99 --port 8080 --jinja

# 2, 3, 4
python brain.py
python mouth.py
python ear.py
```

`llama/` klasöründeki ikili dosyalar llama.cpp'nin resmi GitHub sürümünden
CUDA 12.4 build'i — bu GPU'nun sürücüsü (CUDA 13.1'e kadar destekliyor) CUDA
13.3 ile derlenmiş varsayılan build'i çalıştıramadığı için özellikle seçildi
("PTX compiled with unsupported toolchain" hatası verir). Build'i güncellersen
GPU'nun hâlâ çalıştığını `llama-server.exe --list-devices` ile doğrula.

Tek modül test modları (MQTT gerektirmez, hata ararken kullan):

```bash
python ear.py --textonly        # tanıdığını ekrana yazar, yayınlamaz
python ear.py --dll             # CUDA DLL teşhisi, sonra çıkar
python brain.py --ask "saat kac"
python mouth.py --say "merhaba"
python find_microphone.py       # cihazları listele
```

## Teknoloji yığını

| Katman | Araç | Not |
|---|---|---|
| Uyandırma | openWakeWord, `hey_jarvis` modeli | özel "Yaver" modeli henüz eğitilmedi |
| Konuşma tanıma | faster-whisper, `small` model | GPU/int8_float16, ~0.5 sn |
| LLM | llama.cpp server (CUDA 12.4 build) + Qwen3-4B-Instruct-2507 Q4_K_M | OpenAI uyumlu API, port 8080, GPU'da ~45 tok/sn |
| Seslendirme | Piper, `tr_TR-dfki-medium` | onay cümleleri açılışta önceden sentezlenir |
| Mesajlaşma | Mosquitto (MQTT), localhost:1883 | |

## Kod konvansiyonları

- **Dosya/klasör adları, değişken ve fonksiyon isimleri İngilizce; yorumlar
  Türkçe.** (`record_speech`, `threshold`, `pre_buffer` ama yorum satırı
  Türkçe kalır) Mevcut koda uy, karıştırma. Modül isimleri kulak/beyin/agiz
  metaforunu İngilizce karşılığıyla sürdürür: `ear.py`, `brain.py`, `mouth.py`.
- **Yorumlarda Türkçe karakter kullanma** (ASCII), Windows konsol kodlaması
  bozuyor. Kullanıcıya gösterilen `print` metinleri Türkçe ama ASCII.
- **Sabit değer yazma.** Yeni bir eşik, yol veya süre gerekiyorsa `config.yaml`'a
  İngilizce anahtar adıyla ekle ve oradan oku.
- **Kütüphaneleri fonksiyon içinde import et** (paho, faster_whisper, piper).
  Böylece bir modül eksikken diğer test modları yine çalışır.
- **Hata durumunda çök, kapan değil.** Bir bileşen açılmazsa daha basit bir
  varyanta düş (örn. GPU→CPU) ve nedenini ekrana yaz.
- Bağımlılıklar: `paho-mqtt sounddevice numpy pyyaml openwakeword faster-whisper
  piper-tts httpx`
- Whisper'ın GPU'da çalışması için ek olarak `nvidia-cublas-cu12 nvidia-cudnn-cu12
  nvidia-cuda-runtime-cu12` kurulu olmalı (ear.py bunların DLL yollarını
  otomatik bulup PATH'e ekler).
- **`brain.py`'nin sistem promptu ve `question` metni turlar arası birebir
  sabit kalmalı** (saat, hava durumu gibi değişken içerik YOK). llama-server
  isteğin ortak önekini (system prompt + geçmiş) önbelleğe alıyor; içeriklerden
  biri her turda değişirse (örn. canlı saat damgası) önbellek daha ilk
  farktan sonra tamamen geçersiz kalıyor ve tüm bağlam sıfırdan işleniyor —
  yaşanmış ve tekrar tekrar hata ayıklanmış bir performans/doğruluk sorunu.

## Şu an çalışan / çalışmayan

**Çalışıyor:** uyandırma (skor ~0.94), Türkçe tanıma (GPU'da, isabetli, ~0.5 sn),
LLM cevabı (GPU'da, ~45 tok/sn), cümle cümle akışlı seslendirme, onay cümlesi
("Efendim"), ağız konuşurken kulağın susması, kalıcı konuşma geçmişi.

**Bilinen sorunlar:**

1. **Sözü kesilemiyor.** Mouth konuşurken ear tamamen sağır. Çözüm: mouth
   konuşurken ear sadece uyandırma kelimesini dinlesin, duyarsa `sd.stop()`.
2. **Uyandırma kelimesi İngilizce.** "hey jarvis" kullanılıyor. Özel "Yaver"
   modeli openWakeWord'ün eğitim hattıyla, Piper'ın Türkçe sesiyle sentetik
   örnek üreterek eğitilebilir.

## Sıradaki işler (öncelik sırasıyla)

1. **`skills.py`** — LLM'i araç çağırıcı olarak kullan. llama.cpp `--jinja`
   ile OpenAI uyumlu tool calling destekliyor. İlk araçlar: saat/tarih, basit
   hesap, zamanlayıcı. Her araç ayrı bir fonksiyon, kayıt (registry) üzerinden.
2. **Hafıza yazma** — "Yaver, adımın Mehmet olduğunu hatırla" → `memory.json`.
   Bunu da bir yetenek olarak yap, beyne gömme.
3. **Söz kesme** (yukarıdaki 1. sorun).
4. **Özel "Yaver" uyandırma modeli.**
5. **Pi 5'e taşıma** — `config.yaml`'da yol/cihaz/model değişiklikleri,
   `whisper.device: cpu`, daha küçük LLM. Kod değişmemeli; değişmesi gerekiyorsa
   o bir tasarım hatasıdır, önce onu düzelt.

## Yapılmayacaklar

- LLM'i bir modülün içine gömme. Her zaman HTTP API'sinin arkasında dursun.
- Modüller arasında doğrudan fonksiyon çağrısı kurma. Tek iletişim yolu MQTT.
- Bulut servisi (OpenAI, Google STT vb.) ekleme. Proje offline olacak.
- `config.yaml` dışında ayar tutma.
