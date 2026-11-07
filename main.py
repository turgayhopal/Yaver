"""Yaver - main launcher (baslatici).

config.yaml -> launcher.modules'deki modulleri TEK komuttan baslatir - her biri
hala kendi AYRI isletim sistemi surecinde calisir, aralarinda hicbir dogrudan
fonksiyon cagrisi yok (bkz. CLAUDE.md "Yapilmayacaklar"). main.py sadece
elle ayri terminallerde girilen komutlari tek yerden calistirip loglarini
[isim] etiketiyle tek konsolda birlestirir. Tek bir modulu ayri ayri
`python brain.py` ile calistirmak yine de calisir.

LLM sunucusunu (Ollama, llama.cpp vb.) ve MQTT broker'ini BASLATMAZ - ikisi
de arka planda zaten calisan servisler olarak beklenir. main.py acilista
sadece ulasilabilir olduklarini kontrol edip net bir uyari verir.

Kullanim:
    python main.py
    Ctrl+C: tum surecleri kapatir.
"""

import os
import socket
import subprocess
import sys
import threading
import time

from common import ROOT, load_config

# Alt surecler UTF-8 yayinliyor (bkz. start()'taki PYTHONIOENCODING), ama
# main.py'nin KENDI stdout'u konsolun kod sayfasini (orn. Windows cp1254)
# kullanabilir - relay_output() emoji iceren bir satiri print ederken
# UnicodeEncodeError firlatip o modulun log thread'ini sessizce olduruyordu
# (yasanmis). Ayni cozum: bkz. brain.py.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG = load_config()
LAUNCHER = CONFIG["launcher"]

processes = []   # [(isim, Popen)]
reported_dead = set()


def check_broker():
    """Broker kapaliysa hicbir modul baglanamaz - baslatmanin anlami yok."""
    server, port = CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"]
    try:
        with socket.create_connection((server, port), timeout=LAUNCHER["check_timeout_sec"]):
            pass
    except OSError as error:
        raise SystemExit(
            f"[main] MQTT broker'a ulasilamiyor ({server}:{port}): {error}\n"
            "       Mosquitto calisiyor mu? (orn. 'systemctl start mosquitto' ya da 'mosquitto -v')"
        )
    print(f"[main] MQTT broker hazir: {server}:{port}")


def check_llm():
    """LLM kapali olsa bile diger moduller calisir (brain hata cumlesi
    soyler) - bu yuzden sadece uyarir, durdurmaz."""
    import httpx

    llm = CONFIG["llm"]
    if llm["provider"] != "local":
        return
    local = llm["local"]
    try:
        r = httpx.get(f"{local['server']}/v1/models", timeout=LAUNCHER["check_timeout_sec"])
        r.raise_for_status()
        models = [m.get("id") for m in r.json().get("data", [])]
    except Exception as error:
        print(f"[main] UYARI: LLM sunucusuna ulasilamiyor ({local['server']}): {error}")
        print("[main]        Yaver duyar ama cevap veremez. LLM sunucusunu (orn. Ollama) baslat.")
        return
    if local["model"] not in models:
        print(f"[main] UYARI: '{local['model']}' modeli sunucuda yok. Mevcut: {', '.join(models) or '-'}")
        print(f"[main]        Ollama icin: ollama pull {local['model']}")
        return
    print(f"[main] LLM hazir: {local['model']} @ {local['server']}")


def relay_output(name, proc):
    """Bir surecin stdout'unu okuyup isim etiketiyle konsola basar."""
    for line in proc.stdout:
        print(f"[{name}] {line.rstrip()}")


def start(name, cmd):
    # PYTHONIOENCODING: alt surecin kendi stdout'u UTF-8 olsun - konsol kod
    # sayfasi LLM'in urettigi emoji gibi karakterleri yazdiramayip surecin
    # coktugu yasandi (bkz. brain.py stdout.reconfigure yorumu).
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # PYTHONUNBUFFERED: stdout bir pipe'a (main.py'ye) yonlendirildiginde
    # Python ciktiyi tamamen arabellege aliyor - alt surecin loglari arabellek
    # dolana/surec kapanana kadar buraya hic akmiyordu (yasanmis).
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    processes.append((name, proc))
    threading.Thread(target=relay_output, args=(name, proc), daemon=True).start()
    print(f"[main] {name} baslatildi (pid {proc.pid})")


def stop_all():
    print("\n[main] kapatiliyor...")
    for name, proc in processes:
        proc.terminate()
    for name, proc in processes:
        try:
            proc.wait(timeout=LAUNCHER["stop_timeout_sec"])
        except subprocess.TimeoutExpired:
            proc.kill()
    print("[main] hepsi kapandi.")


def main():
    check_broker()
    check_llm()
    # try/finally BASLATMA asamasini da kapsar - baslatma sirasinda Ctrl+C
    # basilirsa ya da bir start() hata verirse, o ana kadar baslatilmis
    # surecler arkada calisir kalmasin (yasanmis sorun).
    try:
        for module in LAUNCHER["modules"]:
            start(module.replace(".py", ""), [sys.executable, module])

        print("[main] hepsi baslatildi. Kapatmak icin Ctrl+C.")
        while True:
            time.sleep(1)
            for name, proc in processes:
                if proc.poll() is not None and name not in reported_dead:
                    reported_dead.add(name)
                    print(f"[main] UYARI: {name} kapandi (kod {proc.returncode})")
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()


if __name__ == "__main__":
    main()
