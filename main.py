"""Yaver - main launcher (baslatici).

llama-server + skills/brain/mouth/ear'i TEK komuttan baslatir - her biri hala
kendi AYRI isletim sistemi surecinde calisir, aralarinda hicbir dogrudan
fonksiyon cagrisi yok (bkz. CLAUDE.md "Yapilmayacaklar"). main.py sadece
elle ayri terminallerde girilen komutlari tek yerden calistirip loglarini
[isim] etiketiyle tek konsolda birlestirir. Modullerin bagimsizligi bozulmaz:
Ctrl+C ile main.py kapanirsa hepsini kapatir, ama tek bir modulu ayri ayri
`python brain.py` ile calistirmak yine de calisir (bkz. Commands.txt).

Kullanim:
    python main.py
    Ctrl+C: tum surecleri kapatir.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

# Alt surecler artik UTF-8 yayinliyor (bkz. start()'taki PYTHONIOENCODING),
# ama main.py'nin KENDI stdout'u hala Windows konsol kod sayfasini (orn.
# cp1254) kullaniyordu - relay_output() bir emoji iceren satiri print
# ederken UnicodeEncodeError firlatip o modulun log thread'ini sessizce
# olduruyordu (yasanmis: brain/mouth loglari emoji gelince kayboldu, ama
# alt surecler kendileri calismaya devam etti). Ayni cozum: bkz. brain.py.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
LAUNCHER = CONFIG["launcher"]

processes = []   # [(isim, Popen)]
reported_dead = set()


def relay_output(name, proc):
    """Bir surecin stdout'unu okuyup isim etiketiyle konsola basar."""
    for line in proc.stdout:
        print(f"[{name}] {line.rstrip()}")


def start(name, cmd, cwd):
    # PYTHONIOENCODING: alt surecin kendi stdout'u UTF-8 olsun - Windows
    # konsol kod sayfasi (orn. cp1254) LLM'in urettigi emoji gibi karakterleri
    # yazdiramayip surecin coktugu yasandi (bkz. brain.py stdout.reconfigure
    # yorumu) - burada TUM alt surecler icin ayni korumayi merkezi olarak saglar.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # PYTHONUNBUFFERED: stdout bir pipe'a (main.py'ye) yonlendirildiginde
    # Python'un kendisi bunu bir TTY sanmayip TAMAMEN arabelleğe alıyor -
    # yani alt surecin ciktisi kendi arabellegi dolana/surec kapanana kadar
    # buraya hic akmiyor (skills.py'yi tek basima test ederken de yasandi).
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
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
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("[main] hepsi kapandi.")


def main():
    llm = LAUNCHER["llama_server"]
    llm_cmd = [
        llm["binary"], "-m", llm["model"],
        "--ctx-size", str(llm["ctx_size"]),
        "--n-gpu-layers", str(llm["n_gpu_layers"]),
        "--port", str(llm["port"]),
        "--parallel", str(llm["parallel"]),
        *llm.get("extra_args", []),
    ]
    # try/finally BASLATMA asamasini da (llama-server modelini yuklerken
    # birkaç saniye surebiliyor) kapsar - onceden sadece asagidaki bekleme
    # dongusu Ctrl+C yakaliyordu, baslangicta kesilirse (orn. kullanici
    # sabirsizlanip erken Ctrl+C basarsa, ya da bir start() cagrisi hata
    # verirse) stop_all() hic calismadan siddetle baslatilmis surecler
    # (llama-server.exe dahil) arkada calisir kaliyordu (yasanmis sorun,
    # docstring'deki "Ctrl+C hepsini kapatir" sozunu bozuyordu).
    try:
        start("llama", llm_cmd, cwd=str(Path(llm["binary"]).parent))

        for module in LAUNCHER["modules"]:
            start(module.replace(".py", ""), [sys.executable, module], cwd=str(ROOT))

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
