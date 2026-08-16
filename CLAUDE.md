# Yaver — offline sesli asistan

## Proje amacı

İnternetsiz çalışan, uyandırma kelimesiyle tetiklenen, Türkçe konuşan modüler bir
sesli asistan. Jarvis benzeri ama kademeli: önce basit soru-cevap, sonra ev
otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

**Şu anki aşama:** PC üzerinde tam zincir çalışıyor (duyuyor → anlıyor → cevap
veriyor → konuşuyor), ve ilk yetenek (tool calling) katmanı canlı: Yaver bir
Pico W kartındaki LED'i sesle yakıp söndürebiliyor (demo). Sıradaki iş: daha
fazla yetenek (hafıza yazma, saat/tarih) ve söz kesme.

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
                          [skills] → MQTT → fiziksel cihazlar (örn. Pico W)

ear/brain/mouth üçü de yaver/status'a durumlarini yayinlar -> [face] bunu
dinleyip masaustunun kösesinde saydam bir widget'ta gösterir (bkz. asagida).
```

Modüller birbirinin varlığını bilmez, sadece MQTT konularını bilir. Bir modül
kapalıyken diğerleri çalışmaya devam eder (ear, mouth yoksa onay beklemez;
brain, LLM kapalıysa hata cümlesi yayınlar; skills kapalıysa brain hiç arac
tanıtmaz, LLM normal sohbet eder).

`skills.py` de aynı kurala uyar: kendi arac (tool) fonksiyonlarını bir
registry'de tutar (`@skill` dekoratörü), her yeni arac ayrı bir fonksiyon,
`skills.py`'nin çekirdeğine (MQTT akışı, `run_skill`) dokunmaz. `brain.py`
mevcut araçları **dinamik olarak** öğrenir (`yaver/skill/list`, retained
mesaj) — yeni bir arac eklemek `brain.py`'de kod değişikliği gerektirmez.

**Cihazlar da aynı şekilde genel bir kayıt defterinden (`config.yaml` →
`devices`) okunur.** Her cihaz bir `type` (örn. `led`), bir `label` (LLM'e
gösterilen, "hangi cihaz" ayrımını yapmasını sağlayan açıklama) ve bir MQTT
konusuyla tanımlanır. Bir skill (örn. `led_control`) kendi türündeki TÜM
cihazları `skills.py`'deki `devices_by_type()` ile sorgular ve LLM'e "hangi
cihaz" parametresini bir enum olarak sunar (`device_param_schema()`). **Yeni
bir fiziksel LED/kart eklemek = `config.yaml`'a yeni bir `devices` girdisi +
o cihazın kartına (bkz. `boards/`) küçük bir MicroPython dosyası — skill
kodu değişmez.** Aynı desen ileride drone, başka kart türleri, vb. için de
geçerli: yeni bir `type` + o türü sorgulayan yeni bir `@skill` fonksiyonu.

### MQTT sözleşmesi

Bu şema projenin omurgası. Değiştirilecekse tüm modüller birlikte güncellenmeli.

| Konu | Yön | Yük |
|---|---|---|
| `yaver/speech/text` | ear → brain | `{"text": "...", "source": "ear-pc", "timestamp": 0.0}` |
| `yaver/reply/text` | brain → mouth | `{"text": "...", "timestamp": 0.0}` |
| `yaver/status` | her yöne | `{"module": "ear\|mouth", "status": "..."}` |

`yaver/status` değerleri:
- `ear` → `woke` : uyandırma kelimesi duyuldu, mouth onay çalsın
- `ear` → `listening` : ses algılandı, aktif olarak kaydediliyor
- `ear` → `idle` : kayıt bitti (transkripsiyon/brain'e devrediliyor)
- `brain` → `thinking` : soru kuyruktan alındı, LLM'den cevap bekleniyor
- `brain` → `done` : bu turun cevabı (ya da hatası) tamamlandı
- `mouth` → `speaking` : hoparlör aktif, ear sussun
- `mouth` → `ready` : onay cümlesi bitti, ear kayda başlayabilir
- `mouth` → `done` : kuyruk `mouth.reply_gap_sec` boyunca boş kaldı (anlık
  boşluk yetmez - brain cümle cümle yayınlarken sıradaki cümle gecikebilir,
  erken "done" ear'ın mouth'un kendi sesini dinleyip geri besleme döngüsüne
  girmesine yol açar)

`ear`/`brain`/`thinking`/`listening`/`idle` durumlarının tek tüketicisi
`face.py` (masaüstü widget'ı) - ses/LLM hattına hiçbir etkisi yok, sadece
dinler. `face.py` kapalıyken bu yayınlar hiçbir aboneye gitmez, zararsız.

Araç (skill) konuları:

| Konu | Yön | Yük |
|---|---|---|
| `yaver/skill/request` | brain → skills | `{"id": "...", "name": "...", "params": {...}}` |
| `yaver/skill/result` | skills → brain | `{"id": "...", "name": "...", "status": "ok\|error", "data": ...}` |
| `yaver/skill/list` | skills → brain (retained) | OpenAI uyumlu tool-schema listesi, açılışta ve her `skills.py` başlangıcında yayınlanır |

`id`, brain'in eşzamanlı bekleyen istekleri sonuçla eşleştirmesi için
(`call_skill()` bir `threading.Event` ile MQTT ağ thread'inden ayrı bir
worker thread'de bekler — aksi halde ağ thread'i kendi cevabını işlemek
için kilitlenirdi).

Cihaz konuları (her cihazın kendi konusu, `config.yaml`'ın `devices`
bölümünde tanımlı — bkz. yukarıdaki genel cihaz kayıt defteri açıklaması):

| Konu | Yön | Yük |
|---|---|---|
| `yaver/device/pico_led_masa` | skills → Pico W (`boards/pico_w`) | düz metin: `"on"` / `"off"` |

## Dosya düzeni

```
C:\yaver\
├── config.yaml          TÜM ayarlar. Platforma özel her şey burada.
├── main.py                tum surecleri (llama-server + skills/brain/mouth/ear/face)
│                          tek komuttan baslatan launcher - her biri yine ayri
│                          bir surec, dogrudan cagrilmiyorlar (bkz. Calistirma)
├── ear.py                mikrofon + uyandırma/tetik + konuşma tanıma
├── brain.py               LLM istemcisi + hafıza + cümle akışı + arac cagirma
├── mouth.py               seslendirme + çalma kuyruğu
├── skills.py              arac (tool) registry + fiziksel cihaz kontrolu
├── face.py                masaustu widget'i - yaver/status'u dinleyip durumu
│                          (bosta/dinliyor/dusunuyor/konusuyor) gosterir
├── face/
│   └── widget.html        widget'in gorseli (pywebview ile acilir)
├── find_microphone.py     yardımcı: ses cihazlarını listeler/test eder
├── memory.json            kalıcı bilgiler (otomatik oluşur)
├── history.json           son konuşma turları (otomatik oluşur)
├── voices/                Piper ses modeli (.onnx + .onnx.json)
├── models/                LLM gguf dosyası
├── llama/                 llama.cpp binary'leri
├── boards/                Her fiziksel kart icin ayri bir alt klasor
│   └── pico_w/            Pico W MicroPython kodu (main.py, secrets_example.py)
│                          secrets.py .gitignore'da (boards/*/secrets.py) -
│                          Thonny ile karta yuklenir, bu PC'de calismaz
└── venv/                  Python 3.11 sanal ortamı
```

## Çalıştırma

`venv` aktifken tek komut, tek terminal:

```bash
python main.py
```

`main.py`, `config.yaml`'daki `launcher` bolumunu okuyup llama-server'i ve
skills/brain/mouth/ear'i AYNI ANDA, her biri kendi ayri surecinde baslatir -
modullerin birbirini tanimasi/dogrudan cagirmasi ilkesi bozulmaz (main.py
sadece elle ayri terminallerde girilen komutlari tek yerden calistirir,
iletisim yine sadece MQTT). Her surecin ciktisi `[isim]` etiketiyle tek
konsolda birlesir. `Ctrl+C` hepsini kapatir.

Tek bir modulu izole test etmek/hata aramak icin eskisi gibi ayri ayri da
calistirilabilir (bkz. Commands.txt), `venv` aktif her terminalde:

```bash
# 1. LLM sunucusu (bkz. Commands.txt - bu bayraklar config.yaml -> launcher ile
#    birebir ayni tutulmali, main.py da bunlari oradan okuyup calistiriyor)
cd C:\yaver\llama
llama-server.exe -m C:\yaver\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf ^
  --ctx-size 8128 --n-gpu-layers 99 --port 8080 --jinja --parallel 1 ^
  --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on --cache-reuse 256

# 2, 3, 4, 5 (skills olmadan da her sey calisir, sadece arac cagirma devre disi kalir)
python brain.py
python mouth.py
python ear.py
python skills.py
```

Fiziksel cihazlar (örn. Pico W, `boards/pico_w/main.py`) ayrı, kendi başına çalışır —
bir terminal değil, Thonny ile yüklenip karta kaydedilir, açılışta otomatik
başlar. Mosquitto'nun aynı ağdaki cihazlardan bağlantı kabul etmesi için
`mosquitto.conf`'a `listener 1883 0.0.0.0` + `allow_anonymous true` eklenmiş
ve Windows Güvenlik Duvarı'nda 1883 için gelen kural açılmış olmalı — bu PC'de
zaten yapıldı (bkz. Bilinen sorunlar altında Pi 5 notu, aynı ayarlar orada da
gerekecek). **Dikkat:** Windows bir Wi-Fi ağını "Public" olarak sınıflandırırsa
Private-only güvenlik duvarı kuralı uygulanmaz — `Get-NetConnectionProfile` ile
kontrol et, gerekirse `Set-NetConnectionProfile -NetworkCategory Private`.

`llama/` klasöründeki ikili dosyalar llama.cpp'nin resmi GitHub sürümünden
CUDA 12.4 build'i — bu GPU'nun sürücüsü (CUDA 13.1'e kadar destekliyor) CUDA
13.3 ile derlenmiş varsayılan build'i çalıştıramadığı için özellikle seçildi
("PTX compiled with unsupported toolchain" hatası verir). Build'i güncellersen
GPU'nun hâlâ çalıştığını `llama-server.exe --list-devices` ile doğrula.

`--parallel 1` bilinçli bir secim: llama-server varsayılan olarak 4 paralel
slot (n_slots=4) acar, her biri kendi KV-cache'ini tutar. Yaver hep TEK ve
sıralı bir konusma yürüttügü icin (aynı anda iki kullanici yok) 4 slot sadece
zarar veriyor - sunucu yeni bir istegi rastgele/LRU ile bos bir slota
yönlendirebiliyor, bu da sistem promptu + gecmisin o slotta hic önbellekte
olmaması, yani TÜM baglamin (1000+ token) sıfırdan islenmesi anlamına
geliyor (GTX 1660'ta Tensor Core olmadigi icin bu ~5 saniye sürebiliyor -
yasanmis ve ölcülmüs bir gecikme kaynagi). `--parallel 1` ile her istek hep
AYNI slotu kullanir, ortak önek önbellegi hicbir zaman kaybolmaz - sadece
son eklenen mesaj islenir. Yan fayda: 4 yerine 1 slotluk KV-cache ayrılır,
VRAM'de ear.py'nin Whisper modeliyle paylasilan GPU icin yer acilir.

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
| LLM | llama.cpp server (CUDA 12.4 build) + Qwen3-4B-Instruct-2507 Q4_K_M | OpenAI uyumlu API, port 8080, `--parallel 1` + kuantali KV-cache + flash-attn ile GPU'da ~55 tok/sn |
| Seslendirme | Piper, `tr_TR-dfki-medium` | onay cümleleri açılışta önceden sentezlenir, `mouth.synthesis` (config.yaml) ile ayarlanmış (bkz. asağıdaki not) |
| Mesajlaşma | Mosquitto (MQTT), 0.0.0.0:1883 | LAN'a açık (kimlik doğrulamasız, ev ağı) - fiziksel cihazlar da bağlanabilsin diye |
| Araç çağırma | llama.cpp `tools` (OpenAI uyumlu) | `brain.py`'de karar turu da stream'li (bkz. `stream_decision`) - arac gerekmezse ilk cumle hemen soylenir |
| Gömülü kart | Raspberry Pi Pico W, MicroPython + `umqtt.simple` | `boards/pico_w/main.py`, ilk demo: dahili LED aç/kapat |
| Masaüstü widget | `pywebview` (yerel HTML/CSS/JS penceresi) | `face.py`, saydam/her seyin ustunde/kosede - sadece `yaver/status` dinler |

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
  piper-tts httpx pywebview mss`
- Whisper'ın GPU'da çalışması için ek olarak `nvidia-cublas-cu12 nvidia-cudnn-cu12
  nvidia-cuda-runtime-cu12` kurulu olmalı (ear.py bunların DLL yollarını
  otomatik bulup PATH'e ekler).
- `face.py`'nin `pywebview`'i Windows'ta gercek pencere saydamligi icin `gui="qt"`
  ile baslatmasi gerekiyor (WinForms/WebView2 arka ucu saydamligi desteklemiyor -
  bkz. face.py'deki yorum) - bu yuzden `pip install pywebview` yetmez,
  `pip install pywebview[pyside6]` gerekir (PySide6 + QtPy'yi de kurar).
- **`brain.py`'nin sistem promptu ve `question` metni turlar arası birebir
  sabit kalmalı** (saat, hava durumu gibi değişken içerik YOK). llama-server
  isteğin ortak önekini (system prompt + geçmiş) önbelleğe alıyor; içeriklerden
  biri her turda değişirse (örn. canlı saat damgası) önbellek daha ilk
  farktan sonra tamamen geçersiz kalıyor ve tüm bağlam sıfırdan işleniyor —
  yaşanmış ve tekrar tekrar hata ayıklanmış bir performans/doğruluk sorunu.
- **Kimlik bilgisi (WiFi şifresi vb.) asla git'e girmez.** `boards/*/secrets.py`
  `.gitignore`'da; sadece `secrets_example.py` (placeholder) takip edilir.
  Yeni bir kart/entegrasyon kimlik bilgisi gerektirirse aynı deseni kullan:
  `*_example.py` commit edilir, gerçek dosya gitignore'a eklenir.
- **`brain.py`'nin `history`'si tur bazında gruplanır** (tek tek mesaj değil,
  bkz. `deque(maxlen=BRAIN["history_turns"])`) — bir arac turu 4 mesajdan
  (soru, arac çağrısı, arac sonucu, cevap) oluşur, sınır dolunca bir turun
  ortadan kesilip bozuk bir mesaj dizisi (yetim `"tool"` mesajı) bırakmaması
  için. Aracın gerçekten çağrıldığını gösteren mesajlar da geçmişe yazılır -
  sadece son cümleyi tutmak, küçük modelin bir sonraki turda aracı çağırmadan
  kendi eski onay cümlesini taklit etmesine yol açıyordu (yaşanmış sorun).

## Uyandırma modu: openWakeWord yerine metin-içi tetik kelime

`config.yaml`'da `wakeword.enabled: false` (şimdilik varsayılan). Bu modda
openWakeWord hiç çalışmaz; `ear.py` her algılanan konuşmayı (rms eşiği ile)
sürekli Whisper'a verir, çıkan metinde `wakeword.trigger_word` (varsayılan
"yaver") cümlenin herhangi bir yerinde geçiyorsa (`contains_trigger()`)
brain'e yayınlar, geçmiyorsa sessizce atar. Böylece "Yaver, bugün hava nasıl"
gibi tek nefeste, doğal cümleler çalışır — ayrı "uyandır → onay bekle → komutu
söyle" iki aşaması yok. Bedeli: odadaki her konuşma parçası Whisper'dan geçer
(GPU'da ucuz ama sürekli çalışır).

`wakeword.enabled: true` yaparsan eski iki-aşamalı akışa (openWakeWord +
"Efendim" onayı + ayrı kayıt) dönülür — ama bunun için `hey_jarvis` yerine
gerçek bir "Yaver" ses modeli eğitilmesi gerekir (aşağıdaki bilinen sorun 2).
Plan: Pi 5'e taşırken ya da sürekli Whisper yükü sorun olursa, önce ucuz bir
filtre olarak bu moda dönülür.

## Şu an çalışan / çalışmayan

**Çalışıyor:** metin-içi tetik kelime ("yaver" cümlede geçince cevap verir),
Türkçe tanıma (GPU'da, isabetli, ~0.5 sn), LLM cevabı (GPU'da, ~55 tok/sn),
cümle cümle akışlı seslendirme, ağız konuşurken kulağın susması, kalıcı
konuşma geçmişi, arac cagirma (Pico W LED demo uctan uca calisiyor), masaustu
durum widget'i (`face.py` - bosta/dinliyor/dusunuyor/konusuyor).

**Bilinen model tuhaflığı:** küçük 4B model bazen bir araci çağırmadan
"yaptım" diye uydurabiliyor (özellikle ayni araç bir önceki turda başarıyla
çağrılmışsa) - sistem promptunda bunu açıkça yasaklayan bir kural var ama
%100 güvenilir değil, izlemeye devam et.

**Seslendirme motoru değerlendirmesi (yasanmis, tekrar denemeden once oku):**
Piper'ın Türkçe telaffuzu tatmin etmeyince iki alternatif GPU'da (GTX 1660)
canlı test edildi:
- **XTTS-v2** (Coqui, `coqui-tts` paketi) — kalite gerçekten iyi (özellikle
  referans ses klonlamayla) ama çok yavaş: ısınmış haldeyken bile cümle
  başına 1.7-5 saniye sentez süresi ölçüldü (Piper'da bu neredeyse sıfır).
  Toplam "soru bitince Yaver'in konuşmaya başlaması" süresini 2-4 kat
  artırıyor - bu projenin üzerinde ayrıca uğraştığı "anlık cevap" hedefiyle
  doğrudan çelişiyor. Karar: kullanılmadı, ama bağımlılıklar (`torch`,
  `torchaudio`, `coqui-tts`, `transformers<5`) kullanıcı isteğiyle venv'de
  bırakıldı (~4.5GB) - ileride tekrar denemek istenirse hazır.
- **MMS-TTS** (Facebook, `transformers` üzerinden) hiç denenmedi, XTTS-v2'ye
  gecilirken atlandı.

Bunun yerine Piper'ın kendi `SynthesisConfig`'i (`length_scale`, `noise_scale`,
`noise_w_scale`) dinleyerek ayarlandı - `config.yaml` → `mouth.synthesis`.
Aynı motor, aynı hız, sadece daha net/temiz telaffuz. `mouth.py`'nin
`synthesize()` fonksiyonu bunu okuyup `voice.synthesize(text, syn_config=...)`
ile kullanıyor.

**Bilinen sorunlar:**

1. **Sözü kesilemiyor.** Mouth konuşurken ear tamamen sağır. Çözüm: mouth
   konuşurken ear sadece uyandırma kelimesini dinlesin, duyarsa `sd.stop()`.
2. **Özel "Yaver" uyandırma modeli henüz eğitilmedi.** Şimdilik gerekmiyor
   (yukarıdaki metin-içi tetik moduyla "Yaver" ismi zaten çalışıyor); sadece
   `wakeword.enabled: true` moduna dönülmek istenirse gerekir.
3. **Metin-içi tetik, niyet ayrımı yapmaz.** "Yaver" kelimesi asistana değil
   üçüncü şahıs olarak söylense bile (örn. birine "Yaver bugün çalışmadı"
   derken) yine tetiklenir. Hem bu modda hem openWakeWord modunda ortak,
   çözülmemiş bir sınırlama.

## Sıradaki işler (öncelik sırasıyla)

1. **Daha fazla yetenek (skill)** — `skills.py`'ye yeni `@skill` fonksiyonları
   ekle: saat/tarih, basit hesap, zamanlayıcı, drone kontrolü, proje derleme.
   Mimari hazır, her biri sadece yeni bir fonksiyon + registry kaydı.
2. **Hafıza yazma** — "Yaver, adımın Mehmet olduğunu hatırla" → `memory.json`.
   Bunu da bir yetenek (skill) olarak yap, beyne gömme.
3. **Söz kesme** (yukarıdaki 1. sorun).
4. **Gerçek dünya etkisi olan yetenekler için onay adımı** — drone kontrolü ya
   da proje derleme gibi geri alınamaz/riskli eylemlerden önce sesli onay
   ("Emin misin?") istemek. LED demosunda gerekmedi ama ölçek büyüdükçe lazım.
5. **Özel "Yaver" uyandırma modeli** — düşük öncelik, gerekmedikçe ertelendi
   (bkz. yukarıdaki "Uyandırma modu" bölümü).
6. **Pi 5'e taşıma** — `config.yaml`'da yol/cihaz/model değişiklikleri,
   `whisper.device: cpu`, daha küçük LLM. Kod değişmemeli; değişmesi gerekiyorsa
   o bir tasarım hatasıdır, önce onu düzelt. Mosquitto'nun LAN listener +
   firewall ayarları da yeni ortamda tekrarlanmalı.

## Yapılmayacaklar

- LLM'i bir modülün içine gömme. Her zaman HTTP API'sinin arkasında dursun.
- Modüller arasında doğrudan fonksiyon çağrısı kurma. Tek iletişim yolu MQTT.
- Bulut servisi (OpenAI, Google STT vb.) ekleme. Proje offline olacak.
- `config.yaml` dışında ayar tutma.
