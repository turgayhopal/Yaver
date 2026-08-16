"""Yaver - face module (yuz).

yaver/status'u dinleyip masaustunun bir kosesinde, her seyin ustunde, saydam
kucuk bir pencerede Yaver'in o anki durumunu (bosta/dinliyor/dusunuyor/
konusuyor) sinirsel bir animasyonla gosterir. Baska hicbir modulun varligini
bilmez - ses/LLM hattina hicbir etkisi yok, sadece MQTT dinler.

Gorsel: face/widget.html (pywebview ile acilan yerel HTML/CSS/JS sayfasi).

Kullanim:
    python face.py
"""

import json
import sys
import threading
import time
from pathlib import Path

import yaml

# DPI olcekleme (orn. %125) acikken bu sureci "DPI-aware" olarak isaretlemezsek
# SPI_GETWORKAREA (asagida) sanal/olceklenmis piksel dondurebiliyor, webview'in
# pencere konumlandirmasi ise gercek piksel bekliyor - ikisi uyusmayinca widget
# gorev cubugundan cok uzakta bitiyordu (yasanmis sorun). webview import
# edilmeden ONCE, en erken noktada isaretlenmeli.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
FACE = CONFIG["face"]
WIDGET_HTML = ROOT / "face" / "widget.html"


def classify_backdrop(sct, rect):
    """Verilen mss.MSS() ile rect'i (left/top/width/height) ornekleyip
    ortalama parlakliga gore 'dark'/'light' dondurur. Sorun olursa None -
    cagiran taraf onceki modu korur, widget hicbir zaman bozuk bir duruma
    dusmez."""
    try:
        import numpy as np

        shot = sct.grab(rect)
        arr = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
        luminance = float((0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]).mean())
    except Exception:
        return None
    return "light" if luminance > FACE["backdrop_light_threshold"] else "dark"


def watch_backdrop(window, sample_rect):
    """Widget'in HEMEN SOLUNDAKI gercek masaustu seridini periyodik olarak
    ornekler - widget'in kendi karanlik cemberinin ustune degil, cunku o
    zaman hep 'dark' okur (kendi kendini orneklemis olur). Ayri, dusuk
    oncelikli bir arka plan thread'i - ana ses/LLM hattina hicbir etkisi
    yok, sadece iki saniyede bir kucuk bir ekran bolgesi okur.

    mss (ve numpy) kurulu degilse ya da ekran yakalama hic acilamazsa bu
    thread bir uyari basip sessizce devre disi kalir - widget'in geri
    kalani (durum gosterimi, saydamlik) bundan etkilenmez."""
    try:
        import mss
        sct = mss.MSS()
    except Exception as error:
        print(f"  arka plan ornekleme baslatilamadi: {error}")
        return

    last = None
    interval = FACE["backdrop_sample_sec"]
    with sct:
        while True:
            mode = classify_backdrop(sct, sample_rect)
            if mode and mode != last:
                last = mode
                try:
                    window.evaluate_js(f"setBackdrop('{mode}')")
                except Exception as error:
                    print(f"  arka plan guncellenemedi: {error}")
            time.sleep(interval)


# (module, status) -> o modulun kendi ALT durumu (widget.html'deki setState()
# degeriyle ayni isimler, ama asagida DOGRUDAN kullanilmiyor - bkz. main()).
STATE_MAP = {
    ("ear", "listening"): "listening",
    ("ear", "woke"): "listening",       # eski iki-asamali uyandirma modu icin de gecerli
    ("ear", "idle"): "idle",
    ("brain", "thinking"): "thinking",
    ("brain", "done"): "idle",
    ("mouth", "speaking"): "speaking",
    ("mouth", "ready"): "idle",
    ("mouth", "done"): "idle",
}

# Her modulun SON bilinen alt durumunu ayri tutup goruntulenecek durumu bu
# oncelik sirasina gore hesapliyoruz - dogrudan "en son gelen mesaj neyse o"
# YAPMIYORUZ, cunku brain LLM'den cevabi tamamlar tamamlamaz "done" (->idle)
# yayinliyor ama mouth o cevabi hala SESLENDIRIYOR olabiliyor (ozellikle
# uzun/coklu cumleli cevaplarda) - "brain done" mesaji "mouth speaking"i
# erken ustune yazip konusma gostergesini gercekte konusma bitmeden
# kapatiyordu (yasanmis sorun). "konusuyor" gorunur olan gercek olay,
# her zaman "dusunuyor"dan onceliklidir.
PRIORITY = ["speaking", "thinking", "listening", "idle"]


def main():
    import paho.mqtt.client as mqtt
    import webview

    # SPI_GETWORKAREA (gorev cubugu haric alan) denendi ama Qt'nin kendi
    # ekran/pencere koordinat birimiyle (DPI olcegine gore mantiksal piksel)
    # uyusmadigi icin daha kötü sonuc verdi (yasanmis) - screen.width/height
    # DAHA GUVENILIR cunku ikisi de ayni (Qt) kaynaktan geliyor. Gorev
    # cubugunu atlamak icin margin_bottom'a guvenip config.yaml'dan ayarla.
    screen = webview.screens[FACE["screen_index"]]
    width, height = FACE["width"], FACE["height"]
    x = screen.x + screen.width - width - FACE["margin_right"]
    y = screen.y + screen.height - height - FACE["margin_bottom"]

    window = webview.create_window(
        "Yaver", url=str(WIDGET_HTML),
        width=width, height=height, x=x, y=y,
        frameless=True, on_top=True, transparent=True,
        resizable=False, easy_drag=False, shadow=False, focus=False,
    )

    # Kontrast icin arka plani ornekle - widget'in kendi dikdortgeninin
    # SOLUNDAKI seridi okur (kendi cemberinin ustunu degil, bkz. watch_backdrop).
    sample_width = min(90, max(0, x - screen.x))
    if sample_width > 0:
        sample_rect = {"left": x - sample_width, "top": y, "width": sample_width, "height": height}
        threading.Thread(target=watch_backdrop, args=(window, sample_rect), daemon=True).start()

    module_state = {"ear": "idle", "brain": "idle", "mouth": "idle"}

    def on_message(client_, userdata, msg):
        try:
            packet = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return
        module = packet.get("module")
        sub_state = STATE_MAP.get((module, packet.get("status")))
        if sub_state is None or module not in module_state:
            return
        module_state[module] = sub_state

        display = next((s for s in PRIORITY if s in module_state.values()), "idle")
        try:
            window.evaluate_js(f"setState('{display}')")
        except Exception as error:
            print(f"  durum guncellenemedi: {error}")

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_status"])
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_status']}")

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-face")
    except AttributeError:
        client = mqtt.Client(client_id="yaver-face")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"], 60)
    client.loop_start()  # arka planda kendi thread'i - webview.start() burayi bloklamaz

    print("Yuz hazir.")
    # gui="qt": pywebview'in Windows'taki varsayilan arka ucu (WinForms/
    # WebView2) gercek pencere saydamligini desteklemiyor - sadece HTML
    # icindeki arka planı saydam yapiyor, pencerenin kendisi opak griye
    # boyaniyordu (yasanmis ve gorulmus bir sorun). Qt arka ucu (PySide6)
    # gercek alfa saydamligi destekliyor (Qt.WA_TranslucentBackground).
    webview.start(gui="qt")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nyuz kapandi.")
