# Yaver

İnternetsiz çalışan, Türkçe konuşan, uyandırma kelimesiyle tetiklenen modüler bir sesli asistan.

## Ne yapıyor

- Mikrofonu dinler, cümlede "yaver" geçince konuşmayı metne çevirir (Whisper)
- Yerel bir LLM'e sorar (llama.cpp + Qwen3-4B), cevabı üretirken aynı anda cümle cümle seslendirir (Piper)
- Basit araçlar (skill) çağırabilir — şu an demo olarak bir Raspberry Pi Pico W kartındaki LED'i sesle açıp kapatabiliyor
- Masaüstünün köşesinde küçük, saydam bir widget ile o an ne yaptığını (dinliyor / düşünüyor / konuşuyor) gösterir

Hepsi tamamen yerelde çalışır, hiçbir bulut servisine bağımlı değil.

## Neden

Jarvis benzeri ama kademeli bir hedef: önce basit soru-cevap, sonra ev otomasyonu, en sonunda drone/derleme gibi gerçek görevler.

## Mimari

Her modül ayrı bir işletim sistemi süreci — birbirlerinin varlığını bilmezler, sadece MQTT üzerinden konuşurlar:

```
mikrofon → ear → MQTT → brain → MQTT → mouth → hoparlör
                            ↕
                        skills → MQTT → fiziksel cihazlar (Pico W vb.)

ear / brain / mouth → yaver/status → face → masaüstü widget
```

Bir modül kapalıyken diğerleri çalışmaya devam eder (örn. `skills.py` yoksa `brain.py` hiç araç tanıtmaz, normal sohbet eder). Yeni bir yetenek eklemek çekirdek koda dokunmayı gerektirmez.

## Teknoloji

| Katman | Araç |
|---|---|
| Uyandırma | metin-içi tetik kelime (openWakeWord'e de geçilebilir) |
| Konuşma tanıma | faster-whisper |
| LLM | llama.cpp server + Qwen3-4B-Instruct-2507 (yerel, GPU) |
| Seslendirme | Piper (`tr_TR-dfki-medium`) |
| Mesajlaşma | MQTT (Mosquitto) |
| Araç çağırma | llama.cpp `tools` (OpenAI uyumlu) |
| Masaüstü widget | pywebview |
| Gömülü kart | Raspberry Pi Pico W (MicroPython) |

## Kurulum

Boyut/lisans nedeniyle repoda **bulunmayan**, elle indirilmesi gereken dosyalar:

- Bir GGUF LLM dosyası → `models/` (örn. Qwen3-4B-Instruct-2507 Q4_K_M)
- llama.cpp binary'leri → `llama/` (GPU'na uygun CUDA sürümünü seç)
- Piper ses modeli (`tr_TR-dfki-medium.onnx` + `.onnx.json`) → `voices/`
- [Mosquitto](https://mosquitto.org/) kurulu ve çalışıyor olmalı

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install paho-mqtt sounddevice numpy pyyaml openwakeword faster-whisper ^
    piper-tts httpx mss "pywebview[pyside6]"
```

GPU'da Whisper için ayrıca: `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12`

Tüm ayarlar `config.yaml`'da toplanır — platforma özel hiçbir şey (dosya yolu, model adı, IP) kod içine yazılmaz.

## Çalıştırma

```bash
python main.py
```

Tek komut, tek terminal: `llama-server` + `skills` / `brain` / `mouth` / `ear` / `face` hepsini başlatır, loglarını `[isim]` etiketiyle birleştirir. `Ctrl+C` hepsini kapatır.

Tek bir modülü izole test etmek için (bkz. `Commands.txt`):

```bash
python ear.py --textonly        # tanıdığını ekrana yazar, yayınlamaz
python brain.py --ask "saat kaç"
python mouth.py --say "merhaba"
```

## Durum

**Çalışıyor:** metin-içi tetik kelime, Türkçe konuşma tanıma, akışlı LLM cevabı + seslendirme, araç çağırma (Pico W LED demosu uçtan uca), masaüstü durum widget'ı.

**Bilinen sınırlamalar:** sözü kesilemiyor (ağız konuşurken kulak tamamen sağır), özel bir "Yaver" uyandırma modeli henüz eğitilmedi, tetik kelime niyet ayrımı yapmıyor (üçüncü şahıs olarak söylense de tetiklenir), küçük 4B model bazen bir aracı çağırmadan "yaptım" diyebiliyor.

## Donanım

Şu an: masaüstü PC (Windows, GTX 1660 6GB). Hedef: Raspberry Pi 5 (8GB). Kod platforma özel hiçbir şey içermeyecek şekilde tasarlandı — taşıma sırasında sadece `config.yaml` değişmesi bekleniyor.

## Daha fazlası

Mimari detaylar, MQTT sözleşmesi, kod konvansiyonları ve yol haritası için: [CLAUDE.md](CLAUDE.md)
