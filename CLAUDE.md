# Yaver — offline sesli asistan

## Proje amacı

İnternetsiz çalışan, uyandırma kelimesiyle tetiklenen, Türkçe konuşan modüler bir
sesli asistan. Jarvis benzeri ama kademeli: önce basit soru-cevap, sonra ev
otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

**Şu anki aşama:** Tam zincir (duyuyor → anlıyor → cevap veriyor → konuşuyor)
ve ilk yetenek (tool calling) katmanı Windows'ta çalıştı: Yaver bir Pico W
kartındaki LED'i sesle yakıp söndürebiliyor (demo). Geliştirme Linux'a
(Fedora) taşındı, LLM artık arka planda çalışan bir servis (Ollama) - kod
platformdan ve LLM sunucusundan bağımsız hale getiriliyor. Sıradaki iş: skill
altyapısı (uzun işler, cihaz onayı), daha fazla yetenek (saat/tarih, hafıza
yazma), söz kesme.

## Değişmez kısıtlar

Bunlar projenin varlık sebebi, tartışmaya açık değil:

1. **İnternetsiz çalışacak.** Modeller bir kez indirilir, sonra ağ gerekmez.
   Hiçbir modül bulut API'sine bağımlı olmayacak. Tek istisna kullanıcının
   bilinçli seçimi: `config.yaml` → `llm.provider` ileride bir internet
   servisine (örn. `claude`) çevrilebilecek - o zaman SADECE o kullanılır.
   Varsayılan her zaman yerel (`local`); internet servisi asla otomatik
   devreye girmez, yerel LLM'in yedeği de değildir.
2. **Modüler olacak.** Her modül ayrı bir işlem (process). Yeni yetenek eklemek
   çekirdek koda dokunmayı gerektirmeyecek.
3. **Taşınabilir olacak.** Kod Linux, Windows ve ileride Raspberry Pi 5'te
   (ARM64, Linux) aynen çalışacak. Bu yüzden **platforma özel hiçbir şey Python
   koduna yazılmaz** — dosya yolu, cihaz numarası, model adı, IP adresi: hepsi
   `config.yaml` içindedir. Yollar `config.yaml`'da proje köküne göre göreli
   yazılır (`voices/...`), `common.resolve_path()` çözer. Platforma göre
   değişmesi kaçınılmaz olan nadir kod (örn. CUDA kütüphanesi yükleme) tek bir
   fonksiyonda, `sys.platform` ile ayrılır ve iki platformu da destekler.

## Donanım

| Aşama | Donanım | Durum |
|---|---|---|
| Şimdi | Masaüstü PC, Linux (Fedora 44, Wayland), GTX 1660 6GB | aktif geliştirme (önceden Windows) |
| Sonra | Raspberry Pi 5 8GB | hedef |
| Elde ayrıca | Pi 3B+, Pi Zero 2W, BeagleBone Black, STM32MP157D-DK1 | ileride uç birim |

STM32MP157D-DK1'in M4 çekirdeği ileride gerçek zamanlı donanım kontrolü
(röle, PWM, motor) için düşünülüyor. LLM çalıştıramaz, o iş için değil.

## Mimari

```
mikrofon → [ear] → MQTT → [brain] → MQTT → [mouth] → hoparlör
                              ↕     ↘ HTTP
                          [skills]   LLM sunucusu (Ollama vb., arka plan servisi)
                              ↓
                            MQTT → fiziksel cihazlar (örn. Pico W)

ear/brain/mouth üçü de yaver/status'a durumlarini yayinlar -> [face] bunu
dinleyip masaustunun kösesinde saydam bir widget'ta gösterir (bkz. asagida).
```

LLM, Yaver'in bir modülü değil, arka planda zaten çalışan bir servistir
(varsayılan Ollama). `brain.py` ona OpenAI uyumlu `/v1/chat/completions`
üzerinden bağlanır; sunucu, model ve isteğe eklenecek alanlar `config.yaml` →
`llm` bölümündedir (`provider: local`). Ollama, llama.cpp `llama-server`,
LM Studio... hepsi aynı kodla çalışır, Yaver hiçbirini başlatmaz.

Modüller birbirinin varlığını bilmez, sadece MQTT konularını bilir.
`common.py` modül değil, ortak bir kütüphanedir (config okuma, MQTT istemcisi,
güvenli yük çözme) - kendisi hiçbir şey yayınlamaz. Bir modül
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
| `yaver/status` | her yöne | `{"module": "ear\|brain\|mouth", "status": "..."}` |

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
  girmesine yol açar). mouth'un MQTT son vasiyeti (LWT) de budur: mouth
  konuşurken çökerse broker "done"u onun adına yayınlar, ear sağır kalmaz.

`ear`/`brain`/`thinking`/`listening`/`idle` durumlarının tek tüketicisi
`face.py` (masaüstü widget'ı) - ses/LLM hattına hiçbir etkisi yok, sadece
dinler. `face.py` kapalıyken bu yayınlar hiçbir aboneye gitmez, zararsız.

Araç (skill) konuları:

| Konu | Yön | Yük |
|---|---|---|
| `yaver/skill/request` | brain → skills | `{"id": "...", "name": "...", "params": {...}}` |
| `yaver/skill/result` | skills → brain | `{"id": "...", "name": "...", "status": "ok\|error", "data": ...}` |
| `yaver/skill/list` | skills → brain (retained) | OpenAI uyumlu tool-schema listesi (JSON dizisi), her `skills.py` bağlantısında yayınlanır. **Boş yük** = skills kapandı (LWT ile broker yayınlar), brain araçları LLM'e tanıtmayı bırakır |

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
yaver/                     (proje kökü - konumu önemsiz, yollar buna göre çözülür)
├── config.yaml            TÜM ayarlar. Platforma özel her şey burada.
├── common.py              ortak kütüphane (config, MQTT istemcisi, güvenli yük
│                          çözme) - modül değil, süreç olarak çalışmaz
├── main.py                skills/brain/mouth/ear/face'i tek komuttan baslatan
│                          launcher - her biri yine ayri bir surec, dogrudan
│                          cagrilmiyorlar. LLM ve broker'i baslatmaz, kontrol eder
├── ear.py                 mikrofon + uyandırma/tetik + konuşma tanıma
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
├── voices/                Piper ses modeli (.onnx + .onnx.json) - git'te yok
├── tests/                 saf fonksiyon testleri (unittest, MQTT/mikrofon/LLM gerekmez)
├── requirements.txt       Python bağımlılıkları
├── requirements-gpu.txt   sadece NVIDIA GPU'lu makinede (Whisper için CUDA 12 cuBLAS)
├── boards/                Her fiziksel kart icin ayri bir alt klasor
│   └── pico_w/            Pico W MicroPython kodu (main.py, secrets_example.py)
│                          secrets.py .gitignore'da (boards/*/secrets.py) -
│                          Thonny ile karta yuklenir, bu PC'de calismaz
└── venv/                  Python 3.11 sanal ortamı - git'te yok
```

## Çalıştırma

`venv` aktifken tek komut, tek terminal:

```bash
python main.py
```

Önce arka planda iki servis çalışıyor olmalı (Yaver bunları başlatmaz):
**MQTT broker** (Mosquitto) ve **LLM sunucusu** (varsayılan Ollama,
`ollama pull qwen3:4b-instruct`). `main.py`, `config.yaml`'daki `launcher`
bolumunu okuyup skills/brain/mouth/ear/face'i AYNI ANDA, her biri kendi ayri
surecinde baslatir - modullerin birbirini tanimasi/dogrudan cagirmasi ilkesi
bozulmaz. Açılışta broker'a ulaşamazsa durur (hiçbir modül bağlanamaz), LLM'e
ya da modele ulaşamazsa sadece uyarır (brain hata cümlesi söyler). Her surecin
ciktisi `[isim]` etiketiyle tek konsolda birlesir. `Ctrl+C` hepsini kapatir.

Tek bir modulu izole test etmek/hata aramak icin ayri ayri da calistirilabilir
(skills olmadan da her sey calisir, sadece arac cagirma devre disi kalir):

```bash
python brain.py
python mouth.py
python ear.py
python skills.py
```

Fiziksel cihazlar (örn. Pico W, `boards/pico_w/main.py`) ayrı, kendi başına çalışır —
bir terminal değil, Thonny ile yüklenip karta kaydedilir, açılışta otomatik
başlar. Mosquitto'nun aynı ağdaki cihazlardan bağlantı kabul etmesi için
`mosquitto.conf`'a `listener 1883 0.0.0.0` + `allow_anonymous true` eklenmeli
ve güvenlik duvarında 1883 açılmalı. Windows'ta yapılmıştı; **Linux makinesinde
henüz yapılmadı** (şu an broker sadece 127.0.0.1'i dinliyor - Pico bağlanamaz).
Fedora'da: `sudo firewall-cmd --add-port=1883/tcp --permanent && sudo firewall-cmd --reload`.
Windows notu: Wi-Fi ağı "Public" sınıflandırılırsa Private-only kural uygulanmaz
(`Set-NetConnectionProfile -NetworkCategory Private`).

### LLM sunucusu notları (yaşanmış, ölçülmüş)

- **Model:** Ollama'daki düz `qwen3:4b` "düşünen" (thinking) sürüm - ölçüldü:
  sohbet sorusunda 900 token düşünüp hiç cevap vermedi (26 sn), araç çağrısı
  16 sn sürdü; düşünme kapatılınca (`reasoning_effort: none`) İngilizce
  muhakemesi ("Okay, let's see...") cevabın İÇİNE sızdı - hoparlörden okunurdu.
  Bu yüzden `qwen3:4b-instruct` (Windows'taki Qwen3-4B-Instruct-2507 ile aynı).
- **Önek önbelleği:** Ollama tek konuşmada ortak öneki (sistem promptu +
  geçmiş) önbellekte tutuyor - ölçüldü: aynı 881 token'lık istekte istem
  işleme 4192 ms → 23 ms. Bu yüzden sistem promptu ve geçmiş turlar arası
  sabit kalmalı (bkz. Kod konvansiyonları).
- **VRAM:** `qwen3:4b` + 8192 bağlam GPU'da 3,9 GB tuttu; Whisper `small` ile
  aynı 6 GB'lık GPU'yu paylaşıyor.
- **llama.cpp kullanılacaksa** (Windows'ta böyleydi): `--parallel 1` şart -
  varsayılan 4 slot isteği önbelleği olmayan bir slota yönlendirip tüm
  bağlamı (1000+ token) sıfırdan işletiyordu (GTX 1660'ta ~5 sn). Ayrıca
  `--jinja` (araç çağırma için) ve `--cache-reuse 256` (geçmiş penceresi
  kayınca önbelleği korur). Bu GPU'nun sürücüsü CUDA 13.3 build'ini
  çalıştıramıyordu, CUDA 12.4 build'i gerekti.

Tek modül test modları (MQTT gerektirmez, hata ararken kullan):

```bash
python ear.py --textonly        # tanıdığını ekrana yazar, yayınlamaz
python ear.py --cuda            # Whisper CUDA kütüphane teşhisi, sonra çıkar
python brain.py --ask "saat kac"
python mouth.py --say "merhaba"
python find_microphone.py       # cihazları listele
```

## Teknoloji yığını

| Katman | Araç | Not |
|---|---|---|
| Uyandırma | openWakeWord, `hey_jarvis` modeli | özel "Yaver" modeli henüz eğitilmedi |
| Konuşma tanıma | faster-whisper, `small` model | GPU/int8_float16, ~0.5 sn |
| LLM | Ollama (arka plan servisi) + `qwen3:4b-instruct` | OpenAI uyumlu `/v1/chat/completions`, port 11434; `config.yaml` → `llm`. Sunucu değiştirilebilir (llama.cpp ile Windows'ta ~55 tok/sn ölçülmüştü) |
| Seslendirme | Piper, `tr_TR-dfki-medium` | onay cümleleri açılışta önceden sentezlenir, `mouth.synthesis` (config.yaml) ile ayarlanmış (bkz. asağıdaki not) |
| Mesajlaşma | Mosquitto (MQTT), 1883 | LAN'a açık olmalı (kimlik doğrulamasız, ev ağı) - fiziksel cihazlar da bağlanabilsin diye. Linux'ta henüz sadece 127.0.0.1 |
| Araç çağırma | OpenAI uyumlu `tools` (Ollama / llama.cpp) | `brain.py`'de karar turu da stream'li (bkz. `stream_decision`) - arac gerekmezse ilk cumle hemen soylenir |
| Gömülü kart | Raspberry Pi Pico W, MicroPython + `umqtt.simple` | `boards/pico_w/main.py`, ilk demo: dahili LED aç/kapat |
| Masaüstü widget | `pywebview` (yerel HTML/CSS/JS penceresi) | `face.py`, saydam/her seyin ustunde/kosede - sadece `yaver/status` dinler |

## Kod konvansiyonları

- **Dosya/klasör adları, değişken ve fonksiyon isimleri İngilizce; yorumlar
  Türkçe.** (`record_speech`, `threshold`, `pre_buffer` ama yorum satırı
  Türkçe kalır) Mevcut koda uy, karıştırma. Modül isimleri kulak/beyin/agiz
  metaforunu İngilizce karşılığıyla sürdürür: `ear.py`, `brain.py`, `mouth.py`.
- **Yorumlarda Türkçe karakter kullanma** (ASCII), Windows konsol kodlaması
  bozuyor. Kullanıcıya gösterilen `print` metinleri Türkçe ama ASCII.
  (`config.yaml`'daki veri - sistem promptu, `junk_phrases` - Türkçe karakterli
  olabilir, o yorum değil içerik.)
- **Sabit değer yazma.** Yeni bir eşik, yol veya süre gerekiyorsa `config.yaml`'a
  İngilizce anahtar adıyla ekle ve oradan oku. Yollar göreli yazılır
  (`common.resolve_path()`), cihazlar `null` (sistem varsayılanı) ya da ad
  parçasıyla - makineden makineye değişen numara/mutlak yol yazılmaz.
- **MQTT callback'leri `common.parse_payload()` + `@common.guard` kullanır.**
  paho, callback içindeki istisnayı yeniden fırlatıp ağ döngüsünü öldürüyor;
  broker LAN'a kimlik doğrulamasız açık olduğundan tek bir bozuk mesaj
  (`123`, geçersiz UTF-8, `{"text": null}`) bir modülü devirebiliyordu.
- **Test:** `python -m unittest discover tests` - saf fonksiyonlar (cümle
  bölme, çöp filtresi, yük çözme, skill registry). Yeni saf mantık eklersen
  testini de ekle.
- **Kütüphaneleri fonksiyon içinde import et** (paho, faster_whisper, piper).
  Böylece bir modül eksikken diğer test modları yine çalışır.
- **Hata durumunda çök, kapan değil.** Bir bileşen açılmazsa daha basit bir
  varyanta düş (örn. GPU→CPU) ve nedenini ekrana yaz.
- Bağımlılıklar: `requirements.txt`. Python 3.11 (Fedora'nın sistem Python'u
  3.14 - `uv venv --python 3.11 venv` ile kuruldu).
- Whisper'ın GPU'da çalışması için `requirements-gpu.txt` (`nvidia-cublas-cu12`).
  CTranslate2 `libcublas.so.12` / `cublas64_12.dll` istiyor; sistemde farklı
  CUDA olabilir (bu Linux makinesinde sadece `.so.13` var). `ear.py`'nin
  `prepare_cuda_libraries()` pip paketinin klasörünü Python'a sorup bulur:
  Windows'ta DLL arama yoluna ekler, Linux'ta (LD_LIBRARY_PATH süreç
  başladıktan sonra değiştirilemediği için) kütüphaneleri `ctypes` ile
  önceden yükler. Teşhis: `python ear.py --cuda`.
- `face.py`'nin `pywebview`'i gercek pencere saydamligi icin Qt arka ucuyla
  (`config.yaml` → `face.gui: qt`) baslatilir (Windows'un WinForms/WebView2
  arka ucu saydamligi desteklemiyor) - bu yuzden `pywebview[pyside6]` gerekir.
  Qt'ye ozel ortam degiskenleri (orn. Wayland'de `QT_QPA_PLATFORM`) kodda degil
  `face.env`'de.
- **`brain.py`'nin sistem promptu ve `question` metni turlar arası birebir
  sabit kalmalı** (saat, hava durumu gibi değişken içerik YOK). LLM sunucusu
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
  doğrudan çelişiyor. Karar: kullanılmadı. Bağımlılıklar (`torch`,
  `torchaudio`, `coqui-tts`, `transformers<5`) Windows'taki venv'de
  bırakılmıştı (~4.5GB); Linux venv'inde YOK - tekrar denemek istenirse kurulmalı.
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

1. **Skill altyapısı** — bunlar yeni yeteneklerden ÖNCE gerekli:
   - `skills.py` araçları MQTT ağ thread'inde, senkron çalıştırıyor ve brain
     en fazla `skills.timeout_sec` (5 sn) bekliyor - uzun süren bir iş
     (zamanlayıcı, derleme, drone) hem diğer istekleri kilitler hem zaman
     aşımına düşer. Araçlar ayrı bir thread'e taşınmalı, uzun işler için
     "başladım → bitince haber ver" deseni kurulmalı.
   - `led_control` mesajı yayınlar yayınlamaz "ok" dönüyor, cihazdan onay
     beklemiyor - Pico kapalıyken de Yaver "yaktım" der. Cihazlar bir onay
     (ack) konusu yayınlamalı, skill onu beklemeli.
   - Pico W kodu broker kapalıyken (açılışta ya da yeniden bağlanırken) çöküyor,
     kart elle yeniden başlatılana kadar ölü kalıyor.
   - brain sadece İLK araç çağrısını işliyor, ardından ikinci bir araç turu
     yok ("ikisini de kapat" denince biri sessizce atlanır).
2. **Daha fazla yetenek (skill)** — saat/tarih, basit hesap, zamanlayıcı, drone
   kontrolü, proje derleme. Her biri yeni bir `@skill` fonksiyonu. **Saat/tarih
   eklenirken sistem promptundaki "tarih/saat hakkında HİÇBİR bilgin yok"
   kuralı da araca yönlendirecek şekilde değişmeli** (yoksa model aracı
   çağırmak yerine bilmediğini söyler).
3. **Hafıza yazma** — "Yaver, adımın Mehmet olduğunu hatırla" → `memory.json`.
   Bunu da bir yetenek (skill) olarak yap, beyne gömme.
4. **Söz kesme** (yukarıdaki 1. sorun).
5. **Gerçek dünya etkisi olan yetenekler için onay adımı** — drone kontrolü ya
   da proje derleme gibi geri alınamaz/riskli eylemlerden önce sesli onay
   ("Emin misin?") istemek. Broker kimlik doğrulamasız olduğu sürece ağdaki
   herkes `yaver/skill/request`'e doğrudan yazıp bu onayı atlayabilir - riskli
   yeteneklerden önce Mosquitto'ya kimlik doğrulama + ACL eklenmeli.
6. **İnternet LLM sağlayıcısı (isteğe bağlı)** — `llm.provider: claude` (ya da
   başka bir servis) seçilince brain SADECE onu kullansın. Yerel `local`
   varsayılan kalır; internet servisi otomatik devreye girmez.
7. **Özel "Yaver" uyandırma modeli** — düşük öncelik, gerekmedikçe ertelendi
   (bkz. yukarıdaki "Uyandırma modu" bölümü).
8. **Pi 5'e taşıma** — `config.yaml`'da cihaz/model değişiklikleri,
   `whisper.device: cpu`, daha küçük LLM. Kod değişmemeli; değişmesi gerekiyorsa
   o bir tasarım hatasıdır, önce onu düzelt. Mosquitto'nun LAN listener +
   firewall ayarları da yeni ortamda tekrarlanmalı.

## Yapılmayacaklar

- LLM'i bir modülün içine gömme. Her zaman HTTP API'sinin arkasında dursun.
- LLM sunucusunu ya da broker'ı Yaver'in içinden başlatma - ikisi de arka
  plan servisi; `main.py` sadece kontrol eder.
- Modüller arasında doğrudan fonksiyon çağrısı kurma. Tek iletişim yolu MQTT.
- Bulut servisini varsayılan yapma ya da kullanıcı seçmeden devreye sokma
  (bkz. Değişmez kısıt 1). STT/TTS/MQTT her zaman yerel.
- `config.yaml` dışında ayar tutma.
