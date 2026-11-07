# Yaver

İnternetsiz çalışan, Türkçe konuşan, uyandırma kelimesiyle tetiklenen modüler bir sesli asistan.

## Ne yapıyor

- Mikrofonu dinler, cümlede "yaver" geçince konuşmayı metne çevirir (Whisper)
- Yerel bir LLM'e sorar (Ollama, llama.cpp ya da OpenAI uyumlu başka bir sunucu), cevabı üretirken aynı anda cümle cümle seslendirir (Piper)
- Basit araçlar (skill) çağırabilir — şu an demo olarak bir Raspberry Pi Pico W kartındaki LED'i sesle açıp kapatabiliyor
- Masaüstünün köşesinde küçük, saydam bir widget ile o an ne yaptığını (dinliyor / düşünüyor / konuşuyor) gösterir

Hepsi tamamen yerelde çalışır, hiçbir bulut servisine bağımlı değil.

## Neden

Jarvis benzeri ama kademeli bir hedef: önce basit soru-cevap, sonra ev otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

## Mimari

Her modül ayrı bir işletim sistemi süreci — birbirlerinin varlığını bilmezler, sadece MQTT üzerinden konuşurlar:

```
mikrofon → ear → MQTT → brain → MQTT → mouth → hoparlör
                          ↕   ↘
                     skills    LLM sunucusu (HTTP, arka planda çalışan servis)
                          ↓
                 fiziksel cihazlar (Pico W vb.)

ear / brain / mouth → yaver/status → face → masaüstü widget
```

Bir modül kapalıyken diğerleri çalışmaya devam eder (örn. `skills.py` yoksa `brain.py` hiç araç tanıtmaz, normal sohbet eder). Yeni bir yetenek eklemek çekirdek koda dokunmayı gerektirmez.

## Gereksinimler

Belirli bir işletim sistemi gerekmez (Linux ve Windows, hedef Raspberry Pi 5). Gerekenler:

- Python 3.11
- Bir MQTT broker'ı (Mosquitto), arka planda çalışıyor
- OpenAI uyumlu `/v1/chat/completions` sunan yerel bir LLM sunucusu, arka planda çalışıyor — varsayılan: [Ollama](https://ollama.com/) + `qwen3:4b-instruct`
- Mikrofon ve hoparlör

Hangi LLM'in kullanılacağı `config.yaml` → `llm` bölümünden seçilir; kodda model ya da sunucu adı yoktur.

> **Model notu:** Ollama'daki düz `qwen3:4b` "düşünen" (thinking) sürümdür — cevap vermeden önce yüzlerce token düşünür, düşünme kapatılınca da İngilizce muhakemeyi cevabın içine sızdırır (hoparlörden okunur). Sesli asistan için `qwen3:4b-instruct` kullan.

## Kurulum

```bash
# LLM (Ollama kuruluysa)
ollama pull qwen3:4b-instruct

# Python ortamı
python3.11 -m venv venv              # ya da: uv venv --python 3.11 venv
source venv/bin/activate             # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-gpu.txt  # sadece NVIDIA GPU varsa (Whisper'ı GPU'da çalıştırır)
```

Piper ses modeli (`tr_TR-dfki-medium.onnx` + `.onnx.json`) → `voices/` klasörüne, [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0/tr/tr_TR/dfki/medium) üzerinden.

**GPU'da Whisper (NVIDIA):** `requirements-gpu.txt` içindeki `nvidia-cublas-cu12`, Whisper motorunun (CTranslate2) istediği CUDA 12 kütüphanesini getirir — sistemde başka bir CUDA sürümü kurulu olsa bile. `ear.py` bunu her platformda kendisi bulur; sorun olursa `python ear.py --cuda` ile teşhis et. GPU yoksa `config.yaml` → `whisper.device: cpu`.

**Mosquitto:** Pico W gibi ağdaki cihazların bağlanabilmesi için broker'ın LAN'a açık olması gerekir (`listener 1883 0.0.0.0` + `allow_anonymous true`) ve güvenlik duvarında 1883 açılmalı. Sadece bu bilgisayardaki modüller için varsayılan (localhost) ayar yeterli.

## Çalıştırma

```bash
python main.py
```

Tek komut, tek terminal: `skills` / `brain` / `mouth` / `ear` / `face` modüllerini başlatır, loglarını `[isim]` etiketiyle birleştirir. `Ctrl+C` hepsini kapatır. LLM sunucusunu ve MQTT broker'ını başlatmaz — açılışta ulaşılabilir olduklarını kontrol edip uyarır.

Tek bir modülü izole test etmek için:

```bash
python skills.py                 # her modül tek başına da çalışır
python ear.py --textonly         # tanıdığını ekrana yazar, yayınlamaz
python ear.py --cuda             # Whisper için CUDA kütüphane teşhisi
python brain.py --ask "saat kaç"
python mouth.py --say "merhaba"
python find_microphone.py        # ses cihazlarını listele/test et
python -m unittest discover tests
```

## Durum

**Çalışıyor:** metin-içi tetik kelime, Türkçe konuşma tanıma, akışlı LLM cevabı + seslendirme, araç çağırma (Pico W LED demosu uçtan uca), masaüstü durum widget'ı.

**Bilinen sınırlamalar:** sözü kesilemiyor (ağız konuşurken kulak tamamen sağır), özel bir "Yaver" uyandırma modeli henüz eğitilmedi, tetik kelime niyet ayrımı yapmıyor (üçüncü şahıs olarak söylense de tetiklenir), küçük 4B model bazen bir aracı çağırmadan "yaptım" diyebiliyor.

## Daha fazlası

Mimari detaylar, MQTT sözleşmesi, kod konvansiyonları ve yol haritası için: [CLAUDE.md](CLAUDE.md)
