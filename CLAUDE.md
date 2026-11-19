# Yaver — offline sesli asistan

## Proje amacı

İnternetsiz çalışan, uyandırma kelimesiyle tetiklenen, Türkçe konuşan modüler bir
sesli asistan. Jarvis benzeri ama kademeli: önce basit soru-cevap, sonra ev
otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

**Şu anki aşama (2026-11-16):** Tam zincir Linux'ta (Fedora, Ollama) kurulu.
Büyük bir yenileme yapıldı, **gerçek mikrofon/hoparlörle henüz test
edilmedi**: Claude API sağlayıcısı (`llm.provider: claude`), çok adımlı
araç çağırma, skill altyapısı (thread havuzu, cihaz onayı, arka plan işleri),
yeni yetenekler (saat/tarih, hesap, zamanlayıcı, hafıza, tanımlı komutlar,
Claude Code'a iş devretme), sesli onay, söz kesme, takip penceresi
("yaver" demeden devam), seslendirmede sentez/çalma paralelliği. Sıradaki
iş: evde uçtan uca test, ölçüm ve ayar (bkz. "Sıradaki işler").

## Değişmez kısıtlar

Bunlar projenin varlık sebebi, tartışmaya açık değil:

1. **İnternetsiz çalışacak.** Modeller bir kez indirilir, sonra ağ gerekmez.
   Hiçbir modül bulut API'sine bağımlı olmayacak. Tek istisna kullanıcının
   bilinçli seçimi: `config.yaml` → `llm.provider: claude` (Claude API) - o
   zaman SADECE o kullanılır. Varsayılan her zaman yerel (`local`); internet
   servisi asla otomatik devreye girmez, yerel LLM'in yedeği de değildir.
   Aynı kural skill'ler için: internete çıkan bir skill (`delegate_task` →
   Claude Code) `config.yaml`'da varsayılan KAPALI durur, kullanıcı açar.
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

LLM, Yaver'in bir modülü değil, arka planda zaten çalışan bir servistir.
`config.yaml` → `llm.provider` ikisinden birini seçer:
- `local` (varsayılan): OpenAI uyumlu `/v1/chat/completions` (Ollama,
  llama.cpp `llama-server`, LM Studio... hepsi aynı kodla). Yaver hiçbirini
  başlatmaz.
- `claude`: Anthropic Claude API (`anthropic` paketi, varsayılan model
  `claude-haiku-5-5`). Anahtar `secrets.yaml` → `anthropic_api_key` ya da
  `ANTHROPIC_API_KEY`. İnternet ister, ücretlidir.

`brain.py`'nin iç mesaj formatı (ve `history.json`) OpenAI uyumludur;
Claude'a giderken `to_claude_messages()` / `to_claude_tools()` çevirir.
İki sağlayıcı da aynı `llm_turn()` arayüzünden çağrılır - geri kalan akış
(araç döngüsü, onay, cümle akışı) sağlayıcıdan bağımsızdır.

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

Skill altyapısı:
- Araçlar MQTT ağ thread'inde değil bir thread havuzunda çalışır
  (`skills.workers`) - yavaş bir araç diğerlerini kilitlemez.
- Her araç fonksiyonu ilk argüman olarak bir `SkillContext` alır:
  `publish()`, `publish_and_wait()` (cihaz onayı), `notify()` (arka plan işi
  bitti → `yaver/skill/event`).
- Uzun işler (zamanlayıcı, `delegate_task`) hemen "başladı" döner, bitince
  `notify()` eder; brain bunu LLM'e verip kullanıcıya söyletir.
- `@skill(..., confirm=True)`: riskli/geri alınamaz araç. Liste mesajında
  `"confirm": true` olarak gider, brain bu alanı LLM'e göndermeden siler.
  LLM böyle bir aracı çağırınca brain ÇALIŞTIRMAZ, `needs_confirmation`
  döner ve işlemi bekletir; bir sonraki cümle "evet" ise
  (`confirmation_answer()`, karar kodda - LLM'de değil) brain aracı kendisi
  çalıştırır, "hayır" ise iptal eder.
- Hafızanın (`memory.json`) sahibi skills'tir (`remember`/`forget`);
  brain dosyayı okumaz, `yaver/memory` (retained) mesajından öğrenir.
- `run_command`: `config.yaml` → `commands`'daki ADLANDIRILMIŞ komutlar
  (kabuksuz argüman listesi). LLM sadece adı seçer - uzaktaki kartlara ssh
  ile erişmenin güvenli yolu budur. Komut tanımlı değilse araç hiç tanıtılmaz.

Mevcut araçlar: `led_control`, `get_datetime`, `calculate`, `set_timer`,
`cancel_timer`, `remember`, `forget`, `run_command` (komut varsa),
`device_command` (`type: board` kart varsa - Yaver Saat),
`delegate_task` (açıksa - `skills.delegate`, `claude -p` çalıştırır).

brain'in bir soruyu işleyişi (`ask()`): LLM ya cevap verir (cümle cümle
hemen söylenir) ya da bir/birkaç araç çağırır → sonuçlar LLM'e verilir → LLM
yeniden karar verir. En fazla `brain.max_tool_rounds` adım; son adımda araçlar
kapatılır. Aynı (ad, parametre) çağrısı bir turda ikinci kez çalıştırılmaz
(küçük model döngüye girebiliyor). Araç `brain.tool_filler_sec`'ten uzun
sürerse bir kez "Bir saniye, bakıyorum." denir.

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
| `yaver/control` | ear → brain, mouth | `{"command": "stop", "source": "ear"}` - söz kesme: mouth anında susar ve kuyruğu boşaltır, brain cevabı yarıda bırakır |

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

mouth, `stop` sonrası gecikmeli gelen cümleleri brain'in bir sonraki
`thinking`/`done` durumuna kadar yutar (en fazla `barge_in.mute_sec`); ear,
mouth'un `done`'ından sonra `wakeword.followup_sec` boyunca tetik kelimesiz
cümle kabul eder (takip penceresi).

`ear`/`brain`/`thinking`/`listening`/`idle` durumlarının tek tüketicisi
`face.py` (masaüstü widget'ı) - ses/LLM hattına hiçbir etkisi yok, sadece
dinler. `face.py` kapalıyken bu yayınlar hiçbir aboneye gitmez, zararsız.

Araç (skill) konuları:

| Konu | Yön | Yük |
|---|---|---|
| `yaver/skill/request` | brain → skills | `{"id": "...", "name": "...", "params": {...}}` |
| `yaver/skill/result` | skills → brain | `{"id": "...", "name": "...", "status": "ok\|error", "data": ...}` |
| `yaver/skill/list` | skills → brain (retained) | OpenAI uyumlu tool-schema listesi (JSON dizisi), her `skills.py` bağlantısında yayınlanır. Riskli araçlarda ek `"confirm": true`. **Boş yük** = skills kapandı (LWT ile broker yayınlar), brain araçları LLM'e tanıtmayı bırakır |
| `yaver/skill/event` | skills → brain | `{"name": "...", "data": ..., "timestamp": 0.0}` - arka planda biten iş (zamanlayıcı doldu, devredilen iş bitti); brain LLM'e "sistem bildirimi" olarak verip kullanıcıya söyletir |
| `yaver/memory` | skills → brain (retained) | `{"anahtar": "değer", ...}` - kalıcı bilgiler, brain sistem promptuna ekler |

`id`, brain'in eşzamanlı bekleyen istekleri sonuçla eşleştirmesi için
(`call_skill()` bir `threading.Event` ile MQTT ağ thread'inden ayrı bir
worker thread'de bekler — aksi halde ağ thread'i kendi cevabını işlemek
için kilitlenirdi).

Cihaz konuları (her cihazın kendi konusu, `config.yaml`'ın `devices`
bölümünde tanımlı — bkz. yukarıdaki genel cihaz kayıt defteri açıklaması):

| Konu | Yön | Yük |
|---|---|---|
| `yaver/device/pico_led_masa` | skills → Pico W (`boards/pico_w`) | düz metin: `"on"` / `"off"` |
| `yaver/device/pico_led_masa/state` | Pico W → skills (retained) | düz metin: uygulanan durum `"on"` / `"off"` - cihaz onayı (`devices.*.ack_topic`). skill `skills.device_ack_timeout_sec` içinde göremezse hata döner, Yaver "cihaz cevap vermedi" der |
| `yaver/device/<cihaz>/command` | skills → kart (QoS1, retained değil) | `{"id": "...", "command": "bluetooth_pairing", "params": {...}}` - `type: board` kartlar (Yaver Saat), `device_command` aracı. Komutlar `config.yaml` → `devices.<cihaz>.commands`'ta ADLANDIRILMIŞ |
| `yaver/device/<cihaz>/result` | kart → skills (QoS1, retained değil) | `{"id": aynı, "command": "...", "status": "ok\|error", "data": {...}}` ya da `"message"` - skills sadece kendi `id`'sini kabul eder (`parse_device_result()`); bilinmeyen komut → `error` |
| `yaver/device/<cihaz>/online` | kart → herkes (retained, LWT) | `"1"` bağlanınca, LWT `"0"` - `"0"` ise `device_command` komutu hiç göndermez |

### Yaver Saat ses sözleşmesi (Faz 3-4, henüz kodlanmadı)

Yaver Saat (Pi Zero 2 W, ayrı repo: `~/Workspace/Hardware/yaver-saat`,
`PROJE.md`) PC'ye istemci olarak bağlanır: mikrofonu ve hoparlörü MQTT
üzerinden. Whisper/Piper/LLM PC'de kalır. Hedef: hangi cihaz duyduysa cevap
oradan çıkar. Plan (iki oturumda kullanıcıyla onaylandı, 2026-11-19):

- Faz 0: PC zinciri evde gerçek mikrofon/hoparlörle test (bu bitmeden ear/mouth ses yoluna dokunulmaz)
- Faz 1: Mosquitto LAN + kimlik doğrulama + ACL (aşağıda)
- Faz 2: saat sadece istemci - yüz (`yaver/status`'u okur) + `device_command` **← yapıldı**
- Faz 3: saat mikrofonu ek ses kaynağı (cevap hâlâ PC hoparlöründen)
- Faz 4: cevap hedefe göre saat hoparlöründen + `played` onayı
- Faz 5: iki kaynak birlikte + hakemlik

| Konu | Yön | Yük |
|---|---|---|
| `yaver/audio/<cihaz>/mic` | saat → ear (QoS0) | ikili: 4 bayt uint32 LE sıra no + PCM int16 LE mono 16 kHz, tam 1280 örnek (80 ms, 2560 bayt). Kayıp sıra no → o blok sessizlikle doldurulabilir |
| `yaver/audio/<cihaz>/speaker` | mouth → saat (QoS0) | ikili: tek satır JSON başlık + `"\n"` + PCM int16 LE mono. Başlık `{"utt": "...", "gen": 0, "seq": 0, "rate": 22050, "last": false}`, parça ~100-200 ms, son parça `last: true` (PCM boş olabilir) |
| `yaver/audio/<cihaz>/played` | saat → mouth (QoS1) | `{"utt": "...", "gen": 0, "status": "played\|stopped\|dropped"}` - `played` ALSA gerçekten boşalınca. mouth "done"u buna göre yayınlar; ses süresi + pay kadar zaman aşımı (saat düşerse ear sağır kalmasın) |

Kurallar:
- Stop: saat `yaver/control`'e kendisi abone; stop'ta çalmayı keser, o ana
  kadarki en büyük `gen`'i durdurulmuş sayar, `gen <=` olan parçaları atar.
  **mouth `gen`'i açılışta epoch milisaniye ile başlatır** (yeniden
  başlayınca 0'a dönerse saat yeni cevapların hepsini atardı), her stop'ta +1.
- Saat `yaver/status`'a yazmaz, sadece okur. `yaver/reply/text` ve mouth
  durumuna `"target"` alanı eklenecek (saat sadece kendi turunda "konuşuyor").
- Tek ear süreci, N giriş (Whisper tek kopya, kilitli). Çift tetiklemede
  karar Whisper'dan ÖNCE: ~300-500 ms içinde iki kaynakta başlayan konuşmada
  RMS'i yüksek olan. Takip penceresi kaynak bazında; mute (mouth_busy) ilk
  aşamada global. Saat mikrofonunda söz kesme ilk sürümde kapalı (hoparlör
  INMP441'e çok yakın, AEC yok).
- Saat PC'ye modemde DHCP rezervasyonuyla sabitlenmiş IP ile bağlanır (Pico gibi).
- Saat MQTT kullanıcısı `saat`, ACL: yazma `yaver/audio/saat/mic`,
  `yaver/audio/saat/played`, `yaver/device/saat/online`,
  `yaver/device/saat/result`; okuma `yaver/audio/saat/speaker`,
  `yaver/device/saat/command`, `yaver/status`, `yaver/control`,
  `yaver/reply/text`. Kimlik doğrulamasız broker'da mikrofon akışı =
  ağdaki herkes odayı dinler ve sahte sesle komut verir: Faz 1 ZORUNLU.

## Dosya düzeni

```
yaver/                     (proje kökü - konumu önemsiz, yollar buna göre çözülür)
├── config.yaml            TÜM ayarlar. Platforma özel her şey burada.
├── secrets_example.yaml   kimlik bilgisi şablonu (API anahtarı, MQTT şifresi) -
│                          secrets.yaml olarak kopyalanır, o dosya gitignore'da
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
**MQTT broker** (Mosquitto, `sudo systemctl enable --now mosquitto`) ve
**LLM sunucusu** (varsayılan Ollama, `ollama pull qwen3:4b-instruct`) -
`llm.provider: claude` seçiliyse Ollama gerekmez, internet ve `secrets.yaml`
gerekir. `main.py`, `config.yaml`'daki `launcher`
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

### Mosquitto kimlik doğrulama (riskli yeteneklerden önce yapılmalı)

Broker kimlik doğrulamasız olduğu sürece ağdaki herkes `yaver/skill/request`'e
yazıp araçları (komutlar, `delegate_task`) doğrudan çalıştırabilir - brain'in
sesli onayı bu yolu kapatmaz. Kod hazır, sistem ayarı kullanıcıda (sudo):

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd yaver     # şifre sorar
# /etc/mosquitto/mosquitto.conf:
#   listener 1883 0.0.0.0
#   allow_anonymous false
#   password_file /etc/mosquitto/passwd
sudo systemctl restart mosquitto
```

Sonra `config.yaml` → `mqtt.username: yaver`, `secrets.yaml` →
`mqtt_password: ...`, Pico'da `secrets.py` → `MQTT_USER`/`MQTT_PASSWORD`.

Kartlar için ayrı kullanıcı + ACL (`acl_file /etc/mosquitto/acl`): PC
modülleri `yaver` tam erişim; `pico` yazma `yaver/device/pico_led_masa/state`,
okuma `yaver/device/pico_led_masa`; `saat` için bkz. "Yaver Saat ses
sözleşmesi". `allow_anonymous false` olunca Pico da kullanıcı adı/şifre ister.

### LLM sunucusu notları (yaşanmış, ölçülmüş)

- **Bağlam boyutu (2026-11-16, ölçüldü):** Ollama 0.40'ın varsayılan bağlamı
  bu makinede 16384 - model 6 GB'a sığmadı, %29'u CPU'da çalıştı, ilk cümle
  1-4 sn'ye çıktı. `/v1` uç noktası `num_ctx`'i yok saydığından ayar modelin
  kendisinde: `ollama create yaver-qwen3-4b -f ollama/yaver-qwen3-4b.Modelfile`
  (qwen3:4b-instruct + `num_ctx 4096`), `config.yaml` → `llm.local.model:
  yaver-qwen3-4b`. Yeni makinede bu komut bir kez çalıştırılmalı.
- **VRAM'i masaüstü uygulamaları da kullanıyor:** ölçümde Blender, Opera,
  VS Code vb. açıkken Ollama'ya ~2,3 GB kaldı, 4096 bağlamda bile modelin
  %16'sı CPU'daydı. Sonuç (12 soruluk test): sohbet cevabında ilk cümle
  0,5-1,3 sn, araçlı cevapta 2,2-3,2 sn (araç kararı JSON'u CPU'da yavaş
  üretiliyor), hafıza değişince ilk soru 7 sn (sistem promptu değişti,
  önbellek yeniden). Araç seçimi 8/8 doğruydu. Hızlı yerel cevap için GPU'yu
  kullanan uygulamaları kapat ya da `provider: claude`.
- **Model:** Ollama'daki düz `qwen3:4b` "düşünen" (thinking) sürüm - ölçüldü:
  sohbet sorusunda 900 token düşünüp hiç cevap vermedi (26 sn), araç çağrısı
  16 sn sürdü; düşünme kapatılınca (`reasoning_effort: none`) İngilizce
  muhakemesi ("Okay, let's see...") cevabın İÇİNE sızdı - hoparlörden okunurdu.
  Bu yüzden `qwen3:4b-instruct` (Windows'taki Qwen3-4B-Instruct-2507 ile aynı).
- **Önek önbelleği:** Ollama tek konuşmada ortak öneki (sistem promptu +
  geçmiş) önbellekte tutuyor - ölçüldü: aynı 881 token'lık istekte istem
  işleme 4192 ms → 23 ms. Bu yüzden sistem promptu ve geçmiş turlar arası
  sabit kalmalı (bkz. Kod konvansiyonları).
- **VRAM:** ölçüldü - `qwen3:4b-instruct` (4096 bağlam) + Whisper `small`
  (int8_float16) birlikte 4,2 / 6,1 GB. Whisper GPU'da cümle başına ~0,3 sn.
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
| Konuşma tanıma | faster-whisper, `small` model | GPU/int8_float16. Ölçüldü (2026-11-16, Piper sesiyle 8 cümle, GTX 1660): `small` 0,30 sn/cümle 5/8 birebir; `medium` 0,73 sn 6/8 (Raspberry Pi gibi özel adlarda daha iyi); `large-v3-turbo` 1,11 sn 4/8 - kullanma. Gerçek sesle fark büyükse `medium` dene |
| LLM | Ollama (arka plan servisi) + `qwen3:4b-instruct` ya da Claude API (`claude-haiku-5-5`) | `config.yaml` → `llm.provider`. Yerel: OpenAI uyumlu `/v1/chat/completions`, port 11434. Claude: `anthropic` SDK, adaptive thinking + `effort: low` (Haiku 5.5 `temperature` kabul etmiyor - gönderilmez) |
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
- **Kimlik bilgileri `secrets.yaml`'da** (`common.secret()` okur, yoksa
  ortam değişkeni). `config.yaml` git'te - içine anahtar/şifre yazılmaz.
- **Claude'un thinking blokları geçmişe yazılmaz.** Sadece süren bir araç
  döngüsünde (`claude_content`) aynen geri gönderilir. Geçmiş blok halinde
  kırpılınca ya da sistem promptu (hafıza, araç listesi) değişince eski
  bloklar "geçmiş değişti" kontrolüne takılıp 400 verirdi.
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
- **`brain.py`'nin `history`'si sınır aşılınca BLOK halinde kırpılır** (bkz.
  `add_turn()`, `brain.history_trim_to`): her turda en eski turu atmak LLM
  önbelleğini her soruda bozup geçmişin tamamını yeniden işletiyordu (ölçüldü:
  6. turdan itibaren ilk cümle 0,5 → 1,15 sn). Aynı sebeple brain'in açılış
  ısınması araç listesini bekler ve araçlarla birlikte gönderilir - araçsız
  ısınma başka bir öneki önbelleğe alıyordu (ilk soru 1,5 sn → 0,4 sn).
- **`brain.py`'nin `history`'si tur bazında gruplanır** (tek tek mesaj
  değil) — bir arac turu 4 mesajdan
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

**2026-11-16 yenilemesinde doğrulanan (mikrofonsuz):** 40 birim testi (araç
döngüsü, onay akışı, tekrar süzgeci, Claude mesaj çevirisi, sahte HTTP
sunucusuyla Claude stream + thinking bloklarının geri gönderilmesi); skills
gerçek Mosquitto üzerinden (sahte Pico ile LED onayı ve onay gelmeyince 2 sn
sonra hata, saat, hesap, `run_command`, zamanlayıcı bildirimi, hafıza);
brain + skills + Ollama 12 soruluk uçtan uca test (yukarıdaki ölçümler).
**Doğrulanmadı:** gerçek Claude API (anahtar yoktu - sadece sahte sunucu),
söz kesme ve takip penceresi (mikrofon/hoparlör gerekir), yeni Pico kodu
(kart gerekir), mouth'un yeni sentez/çalma thread'leri hoparlörde.

**Linux'ta doğrulanan (Fedora, Ollama):** brain + skills uçtan uca (gerçek
Mosquitto + Ollama ile soru-cevap, LED aracı, bozuk mesajlar, skills kapanınca
araçların kalkması), Whisper GPU'da (Piper sesiyle beslenerek), Piper sentezi,
widget'ın açılması, birim testleri. **Henüz doğrulanmadı:** gerçek mikrofonla
tam zincir (`python main.py`), hoparlörden duyulan ses, widget'ın Wayland'de
konumu/her şeyin üstünde kalması.

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

**Sistem promptu (2026-11-16 yeniden yazıldı):** eski prompt neredeyse
tamamen yasaklardan ("ASLA", "uydurma", "bilmiyorsan söyle") oluşuyordu ve
Yaver'in ne yapabildiğini hiç söylemiyordu - küçük model "Bilmiyorum...
Lütfen daha net sor." kaçışını öğrenmişti. Yeni prompt kimliği, konuşma
tarzını, araçları ve **metnin konuşma tanımadan hatalı gelebileceğini**
(en olası anlamı tahmin et) anlatıyor. Örnek cümle olarak "Ben Yaver'im"
yazılmıyor - model her cevaba onunla başlıyordu. Ayrıca kod tarafında
tekrar süzgeci var (`is_repeat()`, `brain.repeat_window_turns`).

**history.json kötü kalıpları taşır:** model geçmişteki kendi cümlelerini
taklit ettiği için kötü bir oturumun `history.json`'u yeniden başlatmada da
etkisini sürdürür. Cevaplar tuhaflaşırsa önce bu dosyayı sil.

**Bilinen sorunlar:**

1. **Söz kesme gerçek hoparlörle denenmedi.** ear, Yaver konuşurken
   `barge_in.threshold_multiplier` kat yüksek sesi kaydedip Whisper'a verir,
   cümlede tetik kelime + dur kelimesi varsa durdurur. Yankı engelleme (AEC)
   yok: hoparlör yüksekse ve mikrofon yakınsa eşik ayarı gerekebilir. Sadece
   metin-içi tetik modunda (`wakeword.enabled: false`).
2. **Özel "Yaver" uyandırma modeli henüz eğitilmedi.** Şimdilik gerekmiyor;
   sadece `wakeword.enabled: true` moduna dönülmek istenirse gerekir.
3. **Metin-içi tetik, niyet ayrımı yapmaz.** "Yaver" üçüncü şahıs olarak
   söylense de tetiklenir. Takip penceresi (`followup_sec`) bunu biraz artırır:
   Yaver konuştuktan sonraki birkaç saniyede odadaki her cümle ona gider.
4. **Küçük yerel model (4B) sınırlı.** Türkçe sohbette ve araç seçiminde
   Claude'un belirgin gerisinde; bazen aracı çağırmadan "yaptım" diyebilir.
5. **Mosquitto hâlâ kimlik doğrulamasız ve sadece 127.0.0.1'de** (bkz.
   yukarıda "Mosquitto kimlik doğrulama"). Pico bağlanamaz.

## Sıradaki işler (öncelik sırasıyla)

1. **Evde uçtan uca test** (`python main.py`, gerçek mikrofon/hoparlör):
   akıcılık, ilk cümle gecikmesi (yerel vs Claude), söz kesme eşiği, takip
   penceresi, LED onayı (Pico yeni `boards/pico_w/main.py` ile yüklenmeli).
2. **Mosquitto kimlik doğrulama + LAN listener**, sonra `run_command`'a
   gerçek ssh komutları ve gerekirse `delegate_task`'ı açmak.
3. **Whisper modeli** - ölçüm sonucuna göre (bkz. Teknoloji yığını) `small`
   yerine daha büyük model.
4. **Daha fazla yetenek** - drone kontrolü, ev otomasyonu cihazları (yeni
   `type` + yeni `@skill`).
5. **Özel "Yaver" uyandırma modeli** - düşük öncelik.
6. **Pi 5'e taşıma** - `config.yaml`'da cihaz/model değişiklikleri,
   `whisper.device: cpu`, daha küçük LLM ya da `provider: claude`. Kod
   değişmemeli; değişmesi gerekiyorsa o bir tasarım hatasıdır.

## Yapılmayacaklar

- LLM'i bir modülün içine gömme. Her zaman HTTP API'sinin arkasında dursun.
- LLM sunucusunu ya da broker'ı Yaver'in içinden başlatma - ikisi de arka
  plan servisi; `main.py` sadece kontrol eder.
- Modüller arasında doğrudan fonksiyon çağrısı kurma. Tek iletişim yolu MQTT.
- Bulut servisini varsayılan yapma ya da kullanıcı seçmeden devreye sokma
  (bkz. Değişmez kısıt 1). STT/TTS/MQTT her zaman yerel.
- `config.yaml` dışında ayar tutma.
