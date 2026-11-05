#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Autoclicker pour Wayland / Hyprland.

Clics souris synthétiques via /dev/uinput : fonctionne partout (Wayland, XWayland,
jeux Proton) car les événements sont injectés au niveau noyau, pas via une API
de compositeur.

Nouveautés :
  * mode « coordonnées fixes » (X/Y mémorisés, jamais dépendants de la souris) ;
  * capture d'une position par clic sur l'écran (« Sélectionner une position ») ;
  * fenêtre cible choisie parmi les fenêtres ouvertes, avec activation/vérification
    avant chaque clic (aucun clic n'est envoyé si la cible n'est pas sûre) ;
  * coordonnées absolues (écran) ou relatives au coin supérieur gauche de la fenêtre
    cible, recalculées à chaque clic (la position suit les déplacements de la fenêtre).

Délai entre clics réglable de 0.01 s à 30 s.

Usage :
    autoclicker                 ouvre l'interface graphique
    autoclicker --toggle        démarre / arrête l'instance déjà lancée (raccourci global)
    autoclicker --stop          arrête l'instance déjà lancée
    autoclicker --run ...       mode sans interface (script / cron)
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time

MIN_IV, MAX_IV = 0.01, 30.0
BUTTONS = [("Gauche", "left"), ("Droite", "right"), ("Milieu", "middle")]
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "autoclicker.pid")

CONF_DIR = os.path.expanduser("~/.config/autoclick")
SETTINGS = os.path.join(CONF_DIR, "settings.json")
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"

COORD_MODES = [("Écran (absolu)", "screen"), ("Relatif à la fenêtre", "window")]
CLICK_MODES = [("Position actuelle de la souris", "pointer"),
               ("Coordonnées fixes", "fixed")]


# --------------------------------------------------------------------------- IPC Hyprland
class Hypr:
    """Accès direct aux sockets IPC de Hyprland (≈0,03 ms par appel)."""

    def __init__(self):
        self.dir = None
        self.lock = threading.RLock()
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if sig:
            cand = os.path.join(rt, "hypr", sig)
            if os.path.isdir(cand):
                self.dir = cand
        self.available = bool(self.dir and os.path.exists(
            os.path.join(self.dir, ".socket.sock")))

    # -- transport
    def _talk(self, msg, timeout=2.0):
        path = os.path.join(self.dir, ".socket.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(msg.encode())
            chunks = []
            while True:
                try:
                    b = s.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not b:
                    break
                chunks.append(b)
        return b"".join(chunks).decode(errors="ignore")

    def dispatch(self, expr):
        """Évalue une expression Lua de dispatcher, ex. hl.dsp.cursor.move{...}."""
        if not self.available:
            try:
                subprocess.run(["hyprctl", "dispatch", expr], capture_output=True,
                               timeout=2)
                return "ok"
            except Exception:                                   # noqa: BLE001
                return None
        try:
            with self.lock:
                return self._talk("dispatch " + expr, 1.5)
        except Exception:                                       # noqa: BLE001
            return None

    def query(self, cmd):
        """Requête JSON (ex. 'j/clients')."""
        if self.available:
            try:
                with self.lock:
                    out = self._talk(cmd, 2.0)
                return json.loads(out)
            except Exception:                                   # noqa: BLE001
                pass
        try:
            res = subprocess.run(["hyprctl", "-j", cmd.removeprefix("j/")],
                                 capture_output=True, text=True, timeout=3)
            return json.loads(res.stdout or "{}")
        except Exception:                                       # noqa: BLE001
            return None

    # -- requêtes typées
    def cursor_pos(self):
        d = self.query("j/cursorpos")
        if isinstance(d, dict):
            return int(d.get("x", 0)), int(d.get("y", 0))
        return None

    def clients(self):
        d = self.query("j/clients")
        return d if isinstance(d, list) else []

    def active_window(self):
        d = self.query("j/activewindow")
        return d if isinstance(d, dict) else {}

    def monitors(self):
        d = self.query("j/monitors")
        return d if isinstance(d, list) else []


HYPR = Hypr()


# --------------------------------------------------------------------------- utilitaires
def cursor_pos():
    """Position du curseur dans l'espace de travail Hyprland, ou None."""
    pos = HYPR.cursor_pos()
    if pos:
        return pos
    try:
        out = subprocess.run(["hyprctl", "cursorpos"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        x, y = out.split(",")
        return int(x), int(y)
    except Exception:                                           # noqa: BLE001
        return None


def move_cursor(x, y):
    """Déplace le curseur (coordonnées absolues de l'espace Hyprland)."""
    return HYPR.dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})") is not None


def focus_window(address):
    return HYPR.dispatch(f'hl.dsp.focus({{ window = "address:{address}" }})')


def notify(msg):
    try:
        subprocess.Popen(["notify-send", "-a", "Autoclicker", msg])
    except Exception:                                           # noqa: BLE001
        pass


def read_pidfile():
    try:
        pid = int(open(PIDFILE).read().strip())
        if os.path.exists(f"/proc/{pid}") and "autoclicker" in open(
                f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore"):
            return pid
    except Exception:                                           # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- fenêtres
def _rect(w):
    at = w.get("at") or [0, 0]
    size = w.get("size") or [0, 0]
    try:
        return (int(at[0]), int(at[1]), int(size[0]), int(size[1]))
    except (TypeError, ValueError, IndexError):
        return (0, 0, 0, 0)


def normalize(w):
    """Fenêtre Hyprland → dict stable utilisé partout dans l'application."""
    x, y, ww, hh = _rect(w)
    ws = w.get("workspace") or {}
    return {
        "address": str(w.get("address") or ""),
        "title": (w.get("title") or "").strip(),
        "class": (w.get("class") or w.get("initialClass") or "").strip(),
        "at": (x, y),
        "size": (ww, hh),
        "workspace_id": int(ws.get("id", 0) or 0),
        "workspace_name": str(ws.get("name") or ""),
        "monitor": int(w.get("monitor", -1) if w.get("monitor") is not None else -1),
        "floating": bool(w.get("floating")),
        "fullscreen": int(w.get("fullscreen") or 0),
        "hidden": bool(w.get("hidden")),
        "mapped": bool(w.get("mapped", True)),
        "pid": int(w.get("pid") or 0),
        "xwayland": bool(w.get("xwayland")),
    }


def list_windows():
    out = []
    for raw in HYPR.clients():
        w = normalize(raw)
        if not w["mapped"] or w["size"][0] <= 1 or w["size"][1] <= 1:
            continue
        if w["pid"] == os.getpid():          # nos propres fenêtres
            continue
        out.append(w)
    out.sort(key=lambda v: (v["workspace_id"], v["class"].lower(), v["title"].lower()))
    return out


def label_of(w, limit=58):
    name = w["title"] or w["class"] or "(sans titre)"
    if len(name) > limit:
        name = name[:limit - 1] + "…"
    cls = w["class"]
    if cls and cls.lower() not in name.lower():
        return f"{name}  ·  {cls}"
    return name


# --------------------------------------------------------------------------- icônes
_ICON_INDEX = None
_ICON_PATH_CACHE: dict[str, str | None] = {}


def _parse_desktop(path):
    name = icon = wm = exec_ = None
    in_main = False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("["):
                    in_main = (line == "[Desktop Entry]")
                    continue
                if not in_main or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key == "Name" and name is None:
                    name = val
                elif key == "Icon" and icon is None:
                    icon = val
                elif key == "StartupWMClass" and wm is None:
                    wm = val
                elif key == "Exec" and exec_ is None:
                    exec_ = val
    except Exception:                                           # noqa: BLE001
        return None
    return name, icon, wm, exec_


def icon_index():
    global _ICON_INDEX
    if _ICON_INDEX is not None:
        return _ICON_INDEX
    idx = {}
    dirs = ["/usr/local/share/applications", "/usr/share/applications",
            os.path.expanduser("~/.local/share/applications"),
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/flatpak/exports/share/applications"]
    for d in dirs:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            parsed = _parse_desktop(path)
            if not parsed:
                continue
            _name, icon, wm, exec_ = parsed
            if not icon:
                continue
            stem = os.path.basename(path)[:-len(".desktop")].lower()
            keys = {stem}
            if wm:
                keys.add(wm.lower())
            if exec_:
                first = exec_.strip().split()[0]
                keys.add(os.path.basename(first).lower())
            for k in keys:
                idx.setdefault(k, icon)
    _ICON_INDEX = idx
    return idx


_ICON_FOR_CACHE = {}


def icon_for_class(cls):
    """(chemin|nom, est_un_chemin) de l'icône associée à une classe WM."""
    if not cls:
        return None
    if cls in _ICON_FOR_CACHE:
        return _ICON_FOR_CACHE[cls]
    res = _icon_for_class(cls)
    _ICON_FOR_CACHE[cls] = res
    return res


def _icon_for_class(cls):
    idx = icon_index()
    key = cls.lower()
    icon = idx.get(key)
    if not icon:
        for k, v in idx.items():
            if k and (k in key or key in k) and len(k) > 3:
                icon = v
                break
    if not icon:
        return None
    if icon.startswith("/"):
        return (icon, True) if os.path.exists(icon) else None
    if icon in _ICON_PATH_CACHE:
        return (_ICON_PATH_CACHE[icon], True) if _ICON_PATH_CACHE[icon] else None
    found = None
    for base in ("/usr/share/icons/hicolor", os.path.expanduser("~/.local/share/icons")):
        for size in ("scalable", "128x128", "64x64", "48x48", "256x256"):
            for ext in ("svg", "png", "xpm"):
                cand = os.path.join(base, size, "apps", f"{icon}.{ext}")
                if os.path.exists(cand):
                    found = cand
                    break
            if found:
                break
        if found:
            break
    if not found:
        for ext in ("svg", "png"):
            cand = f"/usr/share/pixmaps/{icon}.{ext}"
            if os.path.exists(cand):
                found = cand
                break
    _ICON_PATH_CACHE[icon] = found
    if found:
        return (found, True)
    return (icon, False)


# --------------------------------------------------------------------------- moteur
class ClickError(Exception):
    """Erreur de ciblage : aucun clic ne doit être envoyé."""


_DEVICE = None
_DEVICE_LOCK = threading.Lock()


def acquire_device():
    """Ouvre le périphérique uinput virtuel, une seule fois par processus.

    Le device reste ouvert : le compositeur n'a plus à réenregistrer un
    périphérique à chaque démarrage (0,5 à 2,5 s d'attente) et les clics
    suivants partent immédiatement.
    """
    global _DEVICE
    with _DEVICE_LOCK:
        if _DEVICE is not None:
            return _DEVICE
        from evdev import UInput, ecodes
        _DEVICE = UInput(
            {ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
             ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y]},
            name="Autoclicker virtuel", vendor=0x1234, product=0x5678, version=1,
            bustype=ecodes.BUS_USB,
        )
        # le compositeur (libinput) met quelques centaines de ms à enregistrer
        # le tout nouveau device : sans cette attente, les premiers clics partent
        # dans le vide (~450 ms mesuré sur cette machine).
        time.sleep(0.5)
        return _DEVICE


class Clicker:
    """Moteur de clics : un thread émet press/release sur /dev/uinput."""

    def __init__(self):
        self.dev = None
        self.thread = None
        self.pin_thread = None
        self.stop_ev = threading.Event()
        self.running = False
        self.clicks = 0
        self.t_start = 0.0
        self.t_end = 0.0
        self.error = None
        self.pin = None            # (x, y) si épinglage actif
        self.effective_iv = 0.0    # délai réellement utilisé
        # --- ciblage
        self.fixed = None          # (x, y) dans l'espace de coordonnées choisi
        self.coord_mode = "screen"  # "screen" | "window"
        self.target = None         # dict fenêtre normalisée
        self.keep_focus = True
        self.point = None          # dernier point écran réellement cliqué
        self._clients_cache = []
        self._clients_ts = 0.0
        self._mons_cache = []
        self._mons_ts = 0.0
        self._focus_ts = 0.0

    # -- appareil virtuel
    def _open(self):
        return acquire_device()

    def _nudge(self, px=1):
        """Micro-deplacement : Hyprland ne recalcule le focus pointeur que sur
        mouvement. Sans ce va-et-vient, une fenetre apparue sous un curseur
        immobile ne recoit ni enter ni clics."""
        from evdev import ecodes
        if not self.dev:
            return
        try:
            for delta in (px, -px):
                self.dev.write(ecodes.EV_REL, ecodes.REL_X, delta)
                self.dev.write(ecodes.EV_REL, ecodes.REL_Y, delta)
                self.dev.syn()
                time.sleep(0.02)
        except Exception:                                       # noqa: BLE001
            pass

    @property
    def device_name(self):
        return "Autoclicker virtuel"

    @property
    def elapsed(self):
        if not self.t_start:
            return 0.0
        return (self.t_end or time.perf_counter()) - self.t_start

    @property
    def rate(self):
        e = self.elapsed
        return self.clicks / e if e > 0 and self.clicks else 0.0

    # -- état des fenêtres
    def _refresh_clients(self, max_age=0.12):
        now = time.perf_counter()
        if now - self._clients_ts >= max_age:
            self._clients_cache = [normalize(w) for w in HYPR.clients()]
            self._clients_ts = now
        return self._clients_cache

    def _win_now(self):
        if not self.target:
            return None
        addr = self.target["address"].lower()
        for w in self._refresh_clients(max_age=0.1):
            if w["address"].lower() == addr:
                return w
        return None

    def _monitors(self, max_age=0.5):
        now = time.perf_counter()
        if now - self._mons_ts >= max_age or not self._mons_cache:
            self._mons_cache = HYPR.monitors()
            self._mons_ts = now
        return self._mons_cache

    def _visible_monitor(self, win):
        """Moniteur qui affiche réellement la fenêtre (None sinon)."""
        self._mons_ts = 0.0
        for m in self._monitors(max_age=0.12):
            active = int((m.get("activeWorkspace") or {}).get("id", 0))
            special = int((m.get("specialWorkspace") or {}).get("id", 0) or 0)
            if win["workspace_id"] in (active, special):
                return m
        return None

    def _check_visible(self, win):
        if win["hidden"]:
            raise ClickError("La fenêtre cible est masquée (espace spécial) : "
                             "aucun clic n’a été envoyé.")
        if self._visible_monitor(win) is None:
            raise ClickError(
                f"La fenêtre cible est sur l’espace de travail « {win['workspace_name']} » "
                f"qui n’est pas affiché : aucun clic n’a été envoyé. "
                f"Activez la fenêtre (ou cochez « Activer la fenêtre cible »).")

    def _blocker_at(self, gx, gy, target):
        """Fenêtre flottante/plein écran recouvrant le point et masquant la cible."""
        for w in self._refresh_clients(max_age=0.15):
            if w["address"] == target["address"] or not w["mapped"] or w["hidden"]:
                continue
            if w["workspace_id"] != target["workspace_id"]:
                continue
            if not (w["floating"] or w["fullscreen"]):
                continue                          # les fenêtres carrelées ne se recouvrent pas
            x, y, ww, hh = w["at"][0], w["at"][1], w["size"][0], w["size"][1]
            if x <= gx < x + ww and y <= gy < y + hh:
                return w
        return None

    def _activate_target(self):
        """Active la fenêtre cible et vérifie que le focus a bien suivi."""
        if not self.target:
            return
        win = self._win_now()
        if win is None:
            raise ClickError("Fenêtre cible introuvable (fermée entre-temps) : "
                             "aucun clic n’a été envoyé.")
        focus_window(win["address"])
        # le basculement d’espace de travail n’est pas instantané : on attend
        # que le focus ET l’espace affiché correspondent à la cible.
        deadline = time.perf_counter() + 1.5
        focused = False
        while time.perf_counter() < deadline:
            time.sleep(0.06)
            aw = HYPR.active_window() or {}
            if str(aw.get("address", "")).lower() == win["address"].lower():
                focused = True
                if self._visible_monitor(win) is not None:
                    break
        self._mons_ts = 0.0
        if not focused:
            aw = HYPR.active_window() or {}
            raise ClickError(
                f"Impossible d’activer la fenêtre cible « {label_of(win, 40)} » "
                f"(focus actuel : {aw.get('class') or 'aucune'}). Aucun clic n’a été envoyé.")
        self.target = win

    def _compute_point(self):
        """Point écran réel du clic, recalculé (fenêtre déplacée, redimensionnée…)."""
        if self.fixed is None:
            return None
        x, y = self.fixed
        if not self.target:
            if self.coord_mode == "window":
                raise ClickError("Mode « relatif à la fenêtre » sans fenêtre cible.")
            return int(x), int(y)

        win = self._win_now()
        if win is None:
            raise ClickError("Fenêtre cible introuvable (fermée ?) : clics interrompus.")
        self.target = win
        if self.coord_mode == "window":
            gx, gy = win["at"][0] + int(x), win["at"][1] + int(y)
        else:
            gx, gy = int(x), int(y)

        wx, wy, ww, hh = win["at"][0], win["at"][1], win["size"][0], win["size"][1]
        if not (wx <= gx < wx + ww and wy <= gy < wy + hh):
            raise ClickError(
                f"Le point {gx},{gy} est hors de la fenêtre cible "
                f"« {label_of(win, 40)} » ({wx},{wy} → {wx + ww},{wy + hh}) : "
                f"aucun clic envoyé.")
        self._check_visible(win)
        blocker = self._blocker_at(gx, gy, win)
        if blocker:
            raise ClickError(
                f"La fenêtre « {label_of(blocker, 40)} » recouvre le point {gx},{gy} : "
                f"le clic toucherait une autre application. Déplacez-la ou changez de cible.")
        return gx, gy

    def _verify_focus(self, every=0.25):
        """Contrôle périodique : la cible doit rester la fenêtre active."""
        if not self.target:
            return
        now = time.perf_counter()
        if now - self._focus_ts < every:
            return
        self._focus_ts = now
        aw = HYPR.active_window() or {}
        if str(aw.get("address", "")).lower() == self.target["address"].lower():
            return
        if not self.keep_focus:
            raise ClickError(
                f"La fenêtre cible « {label_of(self.target, 40)} » n’est plus active "
                f"(focus : {aw.get('class') or 'aucune'}) : clics interrompus pour "
                f"éviter de cliquer ailleurs.")
        self._activate_target()

    # -- démarrage / arrêt
    def start(self, button="left", interval=0.1, press_ms=20, count=0, pin=False,
              fixed=None, target=None, coord_mode="screen", keep_focus=True):
        if self.running:
            return False
        from evdev import ecodes
        btn = {"left": ecodes.BTN_LEFT, "right": ecodes.BTN_RIGHT,
               "middle": ecodes.BTN_MIDDLE}[button]
        press = max(0.0, press_ms / 1000.0)
        # un appui ne peut pas être plus long que le délai entre deux clics
        self.effective_iv = max(interval, press + 0.001)

        self.error = None
        self.fixed = (int(fixed[0]), int(fixed[1])) if fixed is not None else None
        self.coord_mode = coord_mode
        self.target = dict(target) if target else None
        self.keep_focus = keep_focus
        self.point = None

        # une fenêtre cible n'a de sens qu'avec des coordonnées fixes
        if self.target and self.fixed is None:
            self.error = ("Une fenêtre cible est sélectionnée : le mode « coordonnées "
                          "fixes » est obligatoire pour cliquer dedans.")
            self.target = None
            return False

        # validation avant tout clic (aucun effet de bord si la cible est douteuse)
        try:
            if self.fixed is not None:
                self._refresh_clients(max_age=0)
                if self.target and self.keep_focus:
                    self._activate_target()      # bascule aussi sur son espace de travail
                self._compute_point()
        except ClickError as exc:
            self.error = str(exc)
            self.fixed = None
            self.target = None
            return False

        try:
            self.dev = self._open()
        except Exception as exc:                                # noqa: BLE001
            self.error = (f"Accès à /dev/uinput impossible : {exc}\n"
                          "Créer la règle udev :\n"
                          '  echo \'KERNEL=="uinput", TAG+="uaccess"\' | '
                          "sudo tee /etc/udev/rules.d/60-uinput.rules\n"
                          "  sudo udevadm control --reload && sudo modprobe uinput")
            self.fixed = None
            self.target = None
            return False

        self.clicks = 0
        self.t_start = time.perf_counter()
        self.t_end = 0.0
        self.running = True
        self._focus_ts = 0.0
        self.stop_ev.clear()

        if self.fixed is None:
            self._nudge()                  # force le focus pointeur sur la cible
            self.pin = cursor_pos() if pin else None
        else:
            self.pin = None

        if self.pin:
            self.pin_thread = threading.Thread(target=self._pin_loop, args=self.pin,
                                               daemon=True)
            self.pin_thread.start()

        self.thread = threading.Thread(
            target=self._loop, args=(btn, self.effective_iv, press, count), daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if not self.running:
            self.dev = None
            return
        self.stop_ev.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.pin_thread:
            self.pin_thread.join(timeout=1.0)
        self.t_end = time.perf_counter()
        self.running = False
        self.pin_thread = None
        self.dev = None                 # périphérique partagé, voir acquire_device()

    # -- boucles
    def _click_once(self, btn, press):
        from evdev import ecodes
        self.dev.write(ecodes.EV_KEY, btn, 1)
        self.dev.syn()
        if press > 0:
            time.sleep(press)
        self.dev.write(ecodes.EV_KEY, btn, 0)
        self.dev.syn()

    def _loop(self, btn, interval, press, count):
        """Cadence : rdv absolu + sommeil hybride, précision ~0.2 ms."""
        next_t = time.perf_counter()
        while not self.stop_ev.is_set():
            try:
                if self.fixed is not None:
                    pt = self._compute_point()
                    self.point = pt
                    move_cursor(*pt)
                    self._verify_focus()
            except ClickError as exc:
                self.error = str(exc)
                break
            self._click_once(btn, press)
            self.clicks += 1
            if count and self.clicks >= count:
                break
            next_t += interval
            while not self.stop_ev.is_set():
                rem = next_t - time.perf_counter()
                if rem <= 0.0:
                    break
                if rem > 0.0025:
                    time.sleep(rem - 0.0015)   # rend la main au scheduler
                else:
                    time.sleep(0.0002)         # fin de créneau : attente active douce
        self.t_end = time.perf_counter()
        self.running = False

    def _pin_loop(self, x, y):
        """Réimpose la position du curseur (si l'utilisateur bouscule la souris)."""
        while not self.stop_ev.is_set():
            move_cursor(x, y)
            self.stop_ev.wait(0.15)


def test_click(button="left", press_ms=20, fixed=None, target=None,
               coord_mode="screen", keep_focus=True):
    """Un seul clic de test. Retourne (ok, message)."""
    c = Clicker()
    ok = c.start(button=button, interval=0.05, press_ms=press_ms, count=1,
                 fixed=fixed, target=target, coord_mode=coord_mode,
                 keep_focus=keep_focus)
    if not ok:
        return False, c.error or "démarrage impossible"
    if c.thread:
        c.thread.join(timeout=5.0)
    point = c.point
    c.stop()
    if c.error:
        return False, c.error
    return True, (f"clic de test envoyé en {point[0]},{point[1]}" if point
                  else "clic de test envoyé à la position du curseur")


# --------------------------------------------------------------------------- mode CLI
def cli_run(args):
    c = Clicker()
    target = None
    if args.target_class:
        for w in list_windows():
            if w["class"].lower() == args.target_class.lower():
                target = w
                break
        if target is None:
            print(f"Fenêtre de classe {args.target_class!r} introuvable", file=sys.stderr)
            return 2
    fixed = (args.x, args.y) if args.x is not None and args.y is not None else None
    if args.delay:
        print(f"démarrage dans {args.delay:.1f} s…", flush=True)
        time.sleep(args.delay)
    if not c.start(args.button, args.interval, args.press, args.count, args.pin,
                   fixed=fixed, target=target, coord_mode=args.coord_mode,
                   keep_focus=not args.no_focus_check):
        print(c.error, file=sys.stderr)
        return 1
    try:
        while c.running:
            time.sleep(0.1)
    except KeyboardInterrupt:
        c.stop()
    if c.error:
        print(c.error, file=sys.stderr)
    print(f"clics : {c.clicks} | durée : {c.elapsed:.2f} s | "
          f"cadence mesurée : {c.rate:.2f} clics/s | délai : {c.effective_iv:.3f} s")
    return 0 if not c.error else 1


def send_signal(sig):
    pid = read_pidfile()
    if not pid:
        return None
    try:
        os.kill(pid, sig)
        return pid
    except Exception:                                           # noqa: BLE001
        return None


# --------------------------------------------------------------------------- paramètres
DEFAULTS = {
    "interval": 0.1, "button": 0, "press": 20, "count": 0, "countdown": 2,
    "pin": False, "click_mode": "pointer", "fixed_x": 168, "fixed_y": 65,
    "coord_mode": "screen", "target_class": "", "target_title": "",
    "target_address": "", "keep_focus": True, "pick_target": True,
}


def load_settings():
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for k, v in stored.items():
                if k in DEFAULTS:
                    data[k] = v
    except Exception:                                           # noqa: BLE001
        pass
    return data


def save_settings(data):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS)
    except Exception:                                           # noqa: BLE001
        pass


# --------------------------------------------------------------------------- GUI
def build_gui():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib, Gio, Gdk, Pango

    CSS = """
    .card { background: alpha(currentColor, 0.06); border-radius: 12px; padding: 12px; }
    .pill { border-radius: 99px; padding: 2px 10px; font-size: 0.85em;
            background: alpha(currentColor, 0.12); }
    .pill.active { background: #3584e4; color: #ffffff; }
    .pill.warn { background: #e5a50a; color: #1a1a1a; }
    .hint { font-size: 0.85em; opacity: 0.75; }
    .err { color: #e01b24; }
    """

    class Picker(Gtk.Window):
        """Surface plein écran (layer-shell) qui capture UN clic."""

        def __init__(self, app, monitor, on_done, on_cancel):
            super().__init__(application=app)
            self.app = app
            self.on_done = on_done
            self.on_cancel = on_cancel
            self.monitor = monitor
            self.geo = monitor.get_geometry()
            self.pos = None
            self.set_decorated(False)
            self.area = Gtk.DrawingArea()
            self.area.set_draw_func(self._draw)
            self.set_child(self.area)

            from gi.repository import Gdk as _Gdk
            try:
                self.set_cursor(_Gdk.Cursor.new_from_name("crosshair", None))
            except Exception:                                   # noqa: BLE001
                pass

            import gi as _gi
            _gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell as LS
            self.layer_ok = False
            try:
                LS.init_for_window(self)
                LS.set_namespace(self, "autoclicker-select")
                LS.set_monitor(self, monitor)
                LS.set_layer(self, LS.Layer.OVERLAY)
                for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
                    LS.set_anchor(self, edge, True)
                LS.set_exclusive_zone(self, -1)
                LS.set_keyboard_mode(self, LS.KeyboardMode.EXCLUSIVE)
                self.layer_ok = LS.is_layer_window(self)
            except Exception:                                   # noqa: BLE001
                self.layer_ok = False

            click = Gtk.GestureClick()
            click.connect("pressed", self._on_pressed)
            self.add_controller(click)
            mot = Gtk.EventControllerMotion()
            mot.connect("motion", self._on_motion)
            self.add_controller(mot)
            key = Gtk.EventControllerKey()
            key.connect("key-pressed", self._on_key)
            self.add_controller(key)

        def _draw(self, _area, cr, width, height):
            cr.set_source_rgba(0.03, 0.04, 0.07, 0.45)
            cr.paint()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(20)
            txt = "Cliquez sur la position à capturer   ·   Échap pour annuler"
            ext = cr.text_extents(txt)
            cr.move_to((width - ext.width) / 2, 48)
            cr.show_text(txt)
            if self.pos:
                x, y = self.pos
                gx, gy = self.geo.x + x, self.geo.y + y
                cr.set_source_rgba(1, 0.35, 0.3, 0.9)
                cr.set_line_width(1)
                cr.move_to(0, y + .5)
                cr.line_to(width, y + .5)
                cr.move_to(x + .5, 0)
                cr.line_to(x + .5, height)
                cr.stroke()
                cr.set_font_size(16)
                label = f"{gx}, {gy}"
                cr.set_source_rgba(0, 0, 0, 0.75)
                bx, by = min(x + 16, width - 140), min(max(y + 16, 30), height - 30)
                cr.rectangle(bx, by - 20, 130, 30)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.move_to(bx + 10, by)
                cr.show_text(label)

        def _on_motion(self, _c, x, y):
            self.pos = (x, y)
            self.queue_draw()

        def _on_pressed(self, _g, _n, x, y):
            gx, gy = int(self.geo.x + x), int(self.geo.y + y)
            GLib.idle_add(self.on_done, gx, gy)
            return True

        def _on_key(self, _c, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                GLib.idle_add(self.on_cancel)
                return True
            return False

    class Win(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Autoclicker")
            self.clicker = Clicker()
            self.countdown_left = 0
            self.countdown_timer = 0
            self.pending = {"toggle": False, "stop": False}
            self.pickers = []
            self.windows = []              # index 0 = « toutes les fenêtres »
            self.settings = load_settings()
            self.set_default_size(470, 760)

            provider = Gtk.CssProvider()
            provider.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            self.set_child(root)

            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(14)
            box.set_margin_bottom(14)
            box.set_margin_start(14)
            box.set_margin_end(14)
            scroll.set_child(box)
            root.append(scroll)

            # ---- badge de mode en tête
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.badge = Gtk.Label(label="Position actuelle")
            self.badge.add_css_class("pill")
            head.append(self.badge)
            head.append(Gtk.Label(label="  clics synthétiques uinput · Hyprland",
                                  xalign=0, hexpand=True))
            box.append(head)

            # ---- cadence
            card = self._card("Délai entre les clics")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.iv_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["interval"], lower=MIN_IV, upper=MAX_IV,
                step_increment=0.01, page_increment=0.5))
            self.iv_spin.set_digits(2)
            self.iv_spin.set_numeric(True)
            self.iv_spin.set_width_chars(7)
            row.append(self.iv_spin)
            row.append(Gtk.Label(label="secondes  (0.01 – 30)"))
            card.append(row)

            self.iv_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, math.log10(MIN_IV), math.log10(MAX_IV), 0.001)
            self.iv_scale.set_draw_value(False)
            self.iv_scale.set_hexpand(True)
            card.append(self.iv_scale)

            pres = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for v in (0.01, 0.05, 0.1, 0.5, 1, 5, 30):
                b = Gtk.Button(label=f"{v:g}")
                b.connect("clicked", lambda _b, val=v: self.set_interval(val))
                pres.append(b)
            card.append(pres)
            self.warn = Gtk.Label(xalign=0)
            self.warn.add_css_class("hint")
            card.append(self.warn)
            box.append(card)

            # ---- bouton & options
            card = self._card("Bouton et cadence")
            grid = Gtk.Grid(column_spacing=10, row_spacing=8)
            card.append(grid)
            grid.attach(Gtk.Label(label="Bouton de souris", xalign=0), 0, 0, 1, 1)
            self.btn_dd = Gtk.DropDown.new_from_strings([b[0] for b in BUTTONS])
            self.btn_dd.set_selected(int(self.settings["button"]) % len(BUTTONS))
            grid.attach(self.btn_dd, 1, 0, 1, 1)

            grid.attach(Gtk.Label(label="Durée d'appui (ms)", xalign=0), 0, 1, 1, 1)
            self.press_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["press"], lower=0, upper=500, step_increment=5))
            grid.attach(self.press_spin, 1, 1, 1, 1)

            grid.attach(Gtk.Label(label="Nombre de clics (0 = infini)", xalign=0), 0, 2, 1, 1)
            self.count_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["count"], lower=0, upper=1000000, step_increment=10))
            grid.attach(self.count_spin, 1, 2, 1, 1)

            grid.attach(Gtk.Label(label="Décompte avant démarrage (s)", xalign=0), 0, 3, 1, 1)
            self.cd_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["countdown"], lower=0, upper=10, step_increment=1))
            grid.attach(self.cd_spin, 1, 3, 1, 1)

            self.pin_check = Gtk.CheckButton(
                label="Épingler la position du curseur pendant les clics")
            self.pin_check.set_active(bool(self.settings["pin"]))
            self.pin_check.set_tooltip_text(
                "Uniquement en mode « position actuelle » : la position est mémorisée au "
                "démarrage puis réimposée toutes les 150 ms.")
            card.append(self.pin_check)
            box.append(card)

            # ---- mode de clic
            card = self._card("Position des clics")
            self.mode_dd = Gtk.DropDown.new_from_strings([m[0] for m in CLICK_MODES])
            card.append(self.mode_dd)

            self.fixed_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
            card.append(self.fixed_grid)
            self.fixed_grid.attach(Gtk.Label(label="Coordonnée X", xalign=0), 0, 0, 1, 1)
            self.x_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["fixed_x"], lower=-32768, upper=32768, step_increment=1))
            self.x_spin.set_numeric(True)
            self.x_spin.set_width_chars(8)
            self.fixed_grid.attach(self.x_spin, 1, 0, 1, 1)

            self.fixed_grid.attach(Gtk.Label(label="Coordonnée Y", xalign=0), 0, 1, 1, 1)
            self.y_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(
                value=self.settings["fixed_y"], lower=-32768, upper=32768, step_increment=1))
            self.y_spin.set_numeric(True)
            self.y_spin.set_width_chars(8)
            self.fixed_grid.attach(self.y_spin, 1, 1, 1, 1)

            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.pick_btn = Gtk.Button(label="Sélectionner une position")
            self.pick_btn.set_icon_name("crosshairs-symbolic")
            self.pick_btn.add_css_class("suggested-action")
            self.pick_btn.connect("clicked", lambda _b: self.start_pick())
            btns.append(self.pick_btn)

            self.test_btn = Gtk.Button(label="Tester la position")
            self.test_btn.set_icon_name("media-playback-start-symbolic")
            self.test_btn.connect("clicked", lambda _b: self.do_test())
            btns.append(self.test_btn)
            self.fixed_grid.attach(btns, 0, 2, 2, 1)

            self.pick_hint = Gtk.Label(xalign=0)
            self.pick_hint.add_css_class("hint")
            self.pick_hint.set_wrap(True)
            self.pick_hint.set_text("Aucune position capturée pour l’instant.")
            self.fixed_grid.attach(self.pick_hint, 0, 3, 2, 1)

            self.preview = Gtk.DrawingArea()
            self.preview.set_content_height(110)
            self.preview.set_draw_func(self._draw_preview)
            self.fixed_grid.attach(self.preview, 0, 4, 2, 1)
            box.append(card)

            # ---- fenêtre cible
            card = self._card("Fenêtre cible")
            self.target_dd = Gtk.DropDown(model=Gtk.StringList(strings=["Chargement…"]))
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._fact_setup)
            factory.connect("bind", self._fact_bind)
            self.target_dd.set_factory(factory)
            self.target_dd.set_list_factory(factory)
            self.target_dd.set_hexpand(True)
            card.append(self.target_dd)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.refresh_btn = Gtk.Button(label="Actualiser les fenêtres")
            self.refresh_btn.set_icon_name("view-refresh-symbolic")
            self.refresh_btn.connect("clicked", lambda _b: self.refresh_windows(notify_user=True))
            row.append(self.refresh_btn)
            self.pick_target_check = Gtk.CheckButton(
                label="La sélection capture aussi la fenêtre cliquée")
            self.pick_target_check.set_active(bool(self.settings["pick_target"]))
            row.append(self.pick_target_check)
            card.append(row)

            self.target_info = Gtk.Label(xalign=0)
            self.target_info.set_wrap(True)
            self.target_info.add_css_class("hint")
            card.append(self.target_info)

            grid2 = Gtk.Grid(column_spacing=10, row_spacing=8)
            card.append(grid2)
            grid2.attach(Gtk.Label(label="Mode de coordonnées", xalign=0), 0, 0, 1, 1)
            self.coord_dd = Gtk.DropDown.new_from_strings([c[0] for c in COORD_MODES])
            grid2.attach(self.coord_dd, 1, 0, 1, 1)
            self.keep_focus_check = Gtk.CheckButton(
                label="Activer la fenêtre cible si elle perd le focus")
            self.keep_focus_check.set_active(bool(self.settings["keep_focus"]))
            self.keep_focus_check.set_tooltip_text(
                "Wayland interdit d’envoyer un clic à une fenêtre en arrière-plan : "
                "l’autoclicker active donc la cible et interrompt les clics si le focus "
                "ne peut pas être obtenu.")
            grid2.attach(self.keep_focus_check, 0, 1, 2, 1)
            box.append(card)

            # ---- pied de page
            footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            footer.set_margin_top(8)
            footer.set_margin_bottom(12)
            footer.set_margin_start(14)
            footer.set_margin_end(14)
            cmd = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.start_btn = Gtk.Button(label="Démarrer")
            self.start_btn.add_css_class("suggested-action")
            self.start_btn.set_hexpand(True)
            self.start_btn.connect("clicked", lambda _b: self.do_start())
            self.stop_btn = Gtk.Button(label="Arrêter")
            self.stop_btn.add_css_class("destructive-action")
            self.stop_btn.set_hexpand(True)
            self.stop_btn.connect("clicked", lambda _b: self.do_stop())
            self.stop_btn.set_sensitive(False)
            cmd.append(self.start_btn)
            cmd.append(self.stop_btn)
            footer.append(cmd)
            self.status = Gtk.Label(xalign=0)
            self.status.set_wrap(True)
            self.stats = Gtk.Label(xalign=0)
            self.stats.add_css_class("hint")
            self.stats.set_wrap(True)
            footer.append(self.status)
            footer.append(self.stats)
            root.append(footer)

            # ---- Échap = arrêt
            kc = Gtk.EventControllerKey()
            kc.connect("key-pressed", self._on_key)
            self.add_controller(kc)

            # ---- signaux
            self.iv_spin.connect("value-changed", self._from_spin)
            self.iv_scale.connect("value-changed", self._from_scale)
            self.press_spin.connect("value-changed", lambda *_a: (self._update_warn(),
                                                                  self.save()))
            self.count_spin.connect("value-changed", lambda *_a: self.save())
            self.cd_spin.connect("value-changed", lambda *_a: self.save())
            self.btn_dd.connect("notify::selected", lambda *_a: self.save())
            self.pin_check.connect("toggled", lambda *_a: self.save())
            self.mode_dd.connect("notify::selected", lambda *_a: self._on_mode_changed())
            self.coord_dd.connect("notify::selected", lambda *_a: self._on_coord_changed())
            self.x_spin.connect("value-changed", lambda *_a: self._on_xy_changed())
            self.y_spin.connect("value-changed", lambda *_a: self._on_xy_changed())
            self.keep_focus_check.connect("toggled", lambda *_a: self.save())
            self.pick_target_check.connect("toggled", lambda *_a: self.save())
            self._target_hid = self.target_dd.connect("notify::selected",
                                                      self._on_target_changed)

            self._sync(float(self.settings["interval"]), source=None)
            self.mode_dd.set_selected(0 if self.settings["click_mode"] == "pointer" else 1)
            self.coord_dd.set_selected(0 if self.settings["coord_mode"] == "screen" else 1)
            self._update_warn()
            self._refresh_buttons()
            self.refresh_windows()
            self._update_badge()

            GLib.timeout_add(80, self._pump)
            self.connect("close-request", self._on_close)

        # ---- helpers UI
        @staticmethod
        def _card(title):
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            outer.add_css_class("card")
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup(f"<b>{title}</b>")
            outer.append(lbl)
            return outer

        def _on_key(self, _c, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                if self.pickers:
                    self.cancel_pick()
                else:
                    self.do_stop()
                return True
            return False

        # ---- cadence
        def set_interval(self, v):
            self._sync(v, source="spin")

        def _from_spin(self, *_a):
            self._sync(self.iv_spin.get_value(), source="spin")

        def _from_scale(self, *_a):
            self._sync(10 ** self.iv_scale.get_value(), source="scale")

        def _sync(self, value, source):
            value = min(max(value, MIN_IV), MAX_IV)
            if source != "spin":
                self.iv_spin.set_value(round(value, 2))
            if source != "scale":
                self.iv_scale.set_value(math.log10(value))
            self._update_warn()
            self.save()

        def _interval(self):
            return max(self.iv_spin.get_value(), MIN_IV)

        def _update_warn(self):
            iv = self._interval()
            press = self.press_spin.get_value() / 1000.0
            if press >= iv:
                self.warn.set_text(
                    f"Appui {self.press_spin.get_value():g} ms ≥ délai : cadence réelle "
                    f"limitée à {1 / (press + 0.001):.0f} clics/s")
            else:
                self.warn.set_text(f"≈ {1 / iv:.2f} clics/s")

        # ---- mode / coordonnées
        def _mode(self):
            return CLICK_MODES[self.mode_dd.get_selected()][1]

        def _coord_mode(self):
            return COORD_MODES[self.coord_dd.get_selected()][1]

        def _fixed_point(self):
            return (int(self.x_spin.get_value()), int(self.y_spin.get_value()))

        def _current_target(self):
            i = self.target_dd.get_selected()
            if i is None or i <= 0 or i >= len(self.windows):
                return None
            return self.windows[i]

        def _on_mode_changed(self, *_a):
            fixed = self._mode() == "fixed"
            self.fixed_grid.set_sensitive(fixed and not self._busy())
            self.pin_check.set_sensitive(not fixed)
            self._update_badge()
            self._update_target_info()
            self.preview.queue_draw()
            self.save()

        def _on_coord_changed(self, *_a):
            self._update_target_info()
            self._update_badge()
            self.preview.queue_draw()
            self.save()

        def _on_xy_changed(self, *_a):
            self.preview.queue_draw()
            self._update_badge()
            self.save()

        def _on_target_changed(self, *_a):
            self._update_target_info()
            self._update_badge()
            self.preview.queue_draw()
            self.save()

        def _update_badge(self):
            self.badge.remove_css_class("active")
            self.badge.remove_css_class("warn")
            if self._mode() == "fixed":
                x, y = self._fixed_point()
                tgt = self._current_target()
                if tgt and self._coord_mode() == "window":
                    self.badge.set_text(f"Coordonnées fixes {x},{y} · relatif {tgt['class'] or 'fenêtre'}")
                    self.badge.add_css_class("active")
                elif tgt:
                    self.badge.set_text(f"Coordonnées fixes {x},{y} · écran → {tgt['class'] or 'fenêtre'}")
                    self.badge.add_css_class("active")
                else:
                    self.badge.set_text(f"Coordonnées fixes {x},{y} · écran entier")
                    self.badge.add_css_class("warn")
            else:
                tgt = self._current_target()
                if tgt:
                    self.badge.set_text("Position actuelle — cible ignorée")
                    self.badge.add_css_class("warn")
                else:
                    self.badge.set_text("Position actuelle de la souris")

        def _update_target_info(self):
            tgt = self._current_target()
            if not tgt:
                self.target_info.set_text(
                    "Toutes les fenêtres / écran entier — les clics vont à la fenêtre "
                    "sous le curseur.")
                return
            x, y, ww, hh = tgt["at"][0], tgt["at"][1], tgt["size"][0], tgt["size"][1]
            mode = ("relatives au coin supérieur gauche de la fenêtre"
                    if self._coord_mode() == "window" else "absolues (écran)")
            self.target_info.set_text(
                f"Cible : {label_of(tgt, 70)}\n"
                f"Géométrie {x},{y} · {ww}×{hh} · espace « {tgt['workspace_name']} » · "
                f"écran {tgt['monitor']}\n"
                f"Les coordonnées sont {mode}.")

        # ---- liste des fenêtres
        def _fact_setup(self, _f, item):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            img = Gtk.Image()
            lbl = Gtk.Label(xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(img)
            box.append(lbl)
            item.set_child(box)

        def _fact_bind(self, _f, item):
            box = item.get_child()
            img = box.get_first_child()
            lbl = img.get_next_sibling()
            i = item.get_position()
            lbl.set_text(item.get_item().get_string())
            if i <= 0 or i >= len(self.windows):
                img.set_from_icon_name("video-display-symbolic")
                return
            ic = icon_for_class(self.windows[i]["class"])
            if ic and ic[1]:
                img.set_from_file(ic[0])
            elif ic:
                img.set_from_icon_name(ic[0])
            else:
                img.set_from_icon_name("application-x-executable-symbolic")

        def refresh_windows(self, notify_user=False):
            keep_addr = ""
            keep_cls = ""
            cur = self._current_target()
            if cur:
                keep_addr, keep_cls = cur["address"], cur["class"]
            elif self.settings.get("target_class"):
                keep_cls = self.settings.get("target_class", "")

            wins = list_windows()
            labels = ["Toutes les fenêtres / Écran entier"] + [label_of(w) for w in wins]
            self.windows = [None] + wins

            self.target_dd.handler_block(self._target_hid)
            model = Gtk.StringList(strings=labels)
            self.target_dd.set_model(model)
            idx = 0
            if keep_addr:
                for i, w in enumerate(self.windows):
                    if w and w["address"] == keep_addr:
                        idx = i
                        break
            if idx == 0 and keep_cls:
                for i, w in enumerate(self.windows):
                    if w and w["class"].lower() == keep_cls.lower():
                        idx = i
                        break
            self.target_dd.set_selected(idx)
            self.target_dd.handler_unblock(self._target_hid)

            self._update_target_info()
            self._update_badge()
            self.preview.queue_draw()
            if notify_user:
                self.pick_hint.set_text(f"{len(wins)} fenêtre(s) détectée(s).")

        def _select_window_by_address(self, address):
            for i, w in enumerate(self.windows):
                if w and w["address"].lower() == address.lower():
                    self.target_dd.set_selected(i)
                    return w
            return None

        # ---- aperçu
        def _preview_point(self):
            if self._mode() != "fixed":
                return cursor_pos()
            x, y = self._fixed_point()
            tgt = self._current_target()
            if tgt and self._coord_mode() == "window":
                return tgt["at"][0] + x, tgt["at"][1] + y
            return x, y

        def _draw_preview(self, _area, cr, width, height):
            mons = HYPR.monitors()
            if not mons:
                return
            xs = [int(m.get("x", 0)) for m in mons]
            ys = [int(m.get("y", 0)) for m in mons]
            x2 = [int(m.get("x", 0)) + int(m.get("width", 0)) for m in mons]
            y2 = [int(m.get("y", 0)) + int(m.get("height", 0)) for m in mons]
            X0, Y0, X1, Y1 = min(xs), min(ys), max(x2), max(y2)
            W, H = max(X1 - X0, 1), max(Y1 - Y0, 1)
            s = min((width - 10) / W, (height - 10) / H)
            ox = (width - W * s) / 2 - X0 * s
            oy = (height - H * s) / 2 - Y0 * s

            def T(x, y):
                return ox + x * s, oy + y * s

            for m in mons:
                mx, my = T(int(m.get("x", 0)), int(m.get("y", 0)))
                cr.rectangle(mx, my, int(m.get("width", 0)) * s,
                             int(m.get("height", 0)) * s)
                cr.set_source_rgba(1, 1, 1, 0.05)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, 0.22)
                cr.set_line_width(1)
                cr.stroke()
                cr.set_source_rgba(1, 1, 1, 0.45)
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(9)
                cr.move_to(mx + 4, my + 12)
                cr.show_text(str(m.get("name", "")))

            tgt = self._current_target()
            if tgt:
                tx, ty = T(tgt["at"][0], tgt["at"][1])
                cr.rectangle(tx, ty, tgt["size"][0] * s, tgt["size"][1] * s)
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.30)
                cr.fill_preserve()
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.9)
                cr.stroke()

            pt = self._preview_point()
            if pt:
                px, py = T(pt[0], pt[1])
                cr.set_source_rgba(1, 0.25, 0.25, 0.95)
                cr.set_line_width(1.2)
                cr.move_to(px - 9, py)
                cr.line_to(px + 9, py)
                cr.move_to(px, py - 9)
                cr.line_to(px, py + 9)
                cr.stroke()
                cr.arc(px, py, 3.2, 0, 2 * math.pi)
                cr.fill()

        # ---- sélection
        def start_pick(self):
            if self.pickers or self._busy():
                return
            self.pick_btn.set_sensitive(False)
            self.pick_hint.set_text("Sélection en cours : cliquez sur la position voulue "
                                    "(Échap pour annuler)…")
            self.hide()
            GLib.timeout_add(120, self._open_pickers)

        def _open_pickers(self):
            disp = Gdk.Display.get_default()
            mons = disp.get_monitors()
            n = mons.get_n_items()
            if n == 0:
                self.cancel_pick()
                return False
            for i in range(n):
                p = Picker(self.get_application(), mons.get_item(i),
                           self._picked, self.cancel_pick)
                p.present()
                self.pickers.append(p)
            return False

        def _close_pickers(self):
            for p in self.pickers:
                p.destroy()
            self.pickers = []
            self.pick_btn.set_sensitive(True)

        def _picked(self, gx, gy):
            if not self.pickers:
                return
            self._close_pickers()
            self.present()
            tgt_before = self._current_target()
            self.x_spin.set_value(gx)
            self.y_spin.set_value(gy)

            picked_win = None
            if self.pick_target_check.get_active():
                for w in list_windows():
                    x, y, ww, hh = w["at"][0], w["at"][1], w["size"][0], w["size"][1]
                    if x <= gx < x + ww and y <= gy < y + hh:
                        picked_win = w
                        break
                if picked_win and (tgt_before is None
                                   or picked_win["address"] != tgt_before["address"]):
                    self.refresh_windows()
                    self._select_window_by_address(picked_win["address"])
                self.refresh_windows()

            tgt = self._current_target()
            if tgt and self._coord_mode() == "window":
                rx, ry = gx - tgt["at"][0], gy - tgt["at"][1]
                self.x_spin.set_value(rx)
                self.y_spin.set_value(ry)
                self.pick_hint.set_text(
                    f"Position capturée : écran {gx},{gy} → relatif {rx},{ry} "
                    f"dans « {tgt['class'] or tgt['title']} ».")
            else:
                self.pick_hint.set_text(f"Position capturée : X = {gx}, Y = {gy} (écran).")
            self.mode_dd.set_selected(1)
            self.preview.queue_draw()
            self.save()

        def cancel_pick(self):
            if not self.pickers:
                return
            self._close_pickers()
            self.present()
            self.pick_hint.set_text("Sélection annulée.")

        # ---- test
        def do_test(self):
            if self._busy():
                return
            if self._mode() != "fixed":
                self.status.set_text("Le test nécessite le mode « Coordonnées fixes ».")
                return
            fixed = self._fixed_point()
            target = self._current_target()
            btn = BUTTONS[self.btn_dd.get_selected()][1]
            press = self.press_spin.get_value()
            mode = self._coord_mode()
            keep = self.keep_focus_check.get_active()
            self.status.set_text("Test : envoi d’un clic…")
            self.test_btn.set_sensitive(False)

            def work():
                ok, msg = test_click(btn, press, fixed, target, mode, keep)
                GLib.idle_add(self._test_done, ok, msg)

            threading.Thread(target=work, daemon=True).start()

        def _test_done(self, ok, msg):
            self.test_btn.set_sensitive(True)
            self.status.set_text(("Test réussi — " if ok else "Test échoué — ") + msg)
            self.pick_hint.set_text(msg)
            if not ok:
                self.pick_hint.add_css_class("err")
            else:
                self.pick_hint.remove_css_class("err")
            return False

        # ---- démarrage
        def _busy(self):
            return self.clicker.running or bool(self.countdown_left) or bool(self.pickers)

        def do_start(self):
            if self._busy():
                return
            if self._mode() == "pointer" and self._current_target():
                self.status.set_text(
                    "Une fenêtre cible est sélectionnée : passez en mode « Coordonnées "
                    "fixes » pour cliquer dans cette fenêtre.")
                return
            self.countdown_left = int(self.cd_spin.get_value())
            if self.pin_check.get_active() and self.countdown_left == 0 and self._mode() != "fixed":
                self.countdown_left = 2
                self.cd_spin.set_value(2)
            if self.countdown_left:
                self.status.set_text(f"Démarrage dans {self.countdown_left} s… "
                                     "ne déplacez plus la souris")
                self._refresh_buttons()
                self.countdown_timer = GLib.timeout_add(1000, self._tick)
            else:
                self._begin()

        def _tick(self):
            self.countdown_left -= 1
            if self.countdown_left > 0:
                self.status.set_text(f"Démarrage dans {self.countdown_left} s…")
                return True
            self.countdown_timer = 0
            self._begin()
            return False

        def _begin(self):
            name = BUTTONS[self.btn_dd.get_selected()][1]
            fixed = self._fixed_point() if self._mode() == "fixed" else None
            ok = self.clicker.start(
                button=name, interval=self._interval(),
                press_ms=self.press_spin.get_value(), count=int(self.count_spin.get_value()),
                pin=self.pin_check.get_active() and fixed is None,
                fixed=fixed, target=self._current_target(),
                coord_mode=self._coord_mode(),
                keep_focus=self.keep_focus_check.get_active())
            if not ok:
                self.status.set_text(self.clicker.error or "démarrage impossible")
            self._refresh_buttons()

        def do_stop(self):
            if self.countdown_timer:
                GLib.source_remove(self.countdown_timer)
                self.countdown_timer = 0
                self.countdown_left = 0
            self.clicker.stop()
            self._refresh_buttons()

        def _refresh_buttons(self):
            busy = self._busy()
            self.start_btn.set_sensitive(not busy)
            self.stop_btn.set_sensitive(busy and not self.pickers)
            for w in (self.iv_spin, self.btn_dd, self.press_spin, self.count_spin,
                      self.cd_spin, self.target_dd, self.refresh_btn, self.mode_dd,
                      self.coord_dd, self.test_btn):
                w.set_sensitive(not busy)
            self.pick_btn.set_sensitive(not busy)
            if not busy:
                self._on_mode_changed()

        # ---- boucle d'entretien
        def _pump(self):
            if self.pending["stop"]:
                self.pending["stop"] = False
                self.pending["toggle"] = False
                self.do_stop()
            elif self.pending["toggle"]:
                self.pending["toggle"] = False
                if self._busy():
                    self.do_stop()
                else:
                    self.do_start()
            c = self.clicker
            if c.running:
                extra = ""
                if c.point:
                    extra = f" · point {c.point[0]},{c.point[1]}"
                elif c.pin:
                    extra = f" · épinglé en {c.pin[0]},{c.pin[1]}"
                tgt = ""
                if c.target:
                    tgt = f" · cible {c.target['class'] or c.target['title']}"
                self.status.set_text(
                    f"En cours — clic {BUTTONS[self.btn_dd.get_selected()][0].lower()}, "
                    f"délai {c.effective_iv * 1000:.0f} ms{extra}{tgt}")
                self.stats.set_text(f"{c.clicks} clics | {c.elapsed:.1f} s | "
                                    f"{c.rate:.2f} clics/s mesurés")
            elif c.error:
                self.status.set_text("Interrompu — " + c.error)
                self.stats.set_text(f"{c.clicks} clics | {c.elapsed:.1f} s")
            elif c.clicks:
                self.status.set_text("Arrêté")
                self.stats.set_text(f"{c.clicks} clics | {c.elapsed:.1f} s | "
                                    f"{c.rate:.2f} clics/s mesurés")
            elif not self.countdown_left:
                self.status.set_text("Prêt")
            if not c.running and self.stop_btn.get_sensitive() and not self.countdown_left:
                self._refresh_buttons()
            return True

        # ---- persistance
        def save(self, *_a):
            try:
                data = {
                    "interval": round(float(self.iv_spin.get_value()), 3),
                    "button": int(self.btn_dd.get_selected()),
                    "press": int(self.press_spin.get_value()),
                    "count": int(self.count_spin.get_value()),
                    "countdown": int(self.cd_spin.get_value()),
                    "pin": bool(self.pin_check.get_active()),
                    "click_mode": self._mode(),
                    "fixed_x": int(self.x_spin.get_value()),
                    "fixed_y": int(self.y_spin.get_value()),
                    "coord_mode": self._coord_mode(),
                    "keep_focus": bool(self.keep_focus_check.get_active()),
                    "pick_target": bool(self.pick_target_check.get_active()),
                }
                tgt = self._current_target()
                data["target_class"] = tgt["class"] if tgt else ""
                data["target_title"] = tgt["title"] if tgt else ""
                data["target_address"] = tgt["address"] if tgt else ""
                self.settings.update(data)
                save_settings(self.settings)
            except Exception:                                   # noqa: BLE001
                pass

        # ---- fermeture
        def _on_close(self, *_a):
            self._close_pickers()
            self.do_stop()
            try:
                if os.path.exists(PIDFILE):
                    os.remove(PIDFILE)
            except Exception:                                   # noqa: BLE001
                pass
            return False

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="org.local.Autoclicker",
                             flags=Gio.ApplicationFlags.FLAGS_NONE)

        def do_activate(self):
            win = self.props.active_window or Win(self)
            win.present()

        def do_shutdown(self, *_a):
            try:
                if os.path.exists(PIDFILE):
                    os.remove(PIDFILE)
            except Exception:                                   # noqa: BLE001
                pass
            Gtk.Application.do_shutdown(self)

    return App


# --------------------------------------------------------------------------- main
def ensure_layer_shell():
    """gtk4-layer-shell doit être préchargé avant libwayland (sinon pas de layer)."""
    if os.environ.get("AUTOCLICK_PRELOADED") == "1":
        return
    if not os.path.exists(LAYER_SHELL_LIB):
        return
    env = dict(os.environ)
    preload = env.get("LD_PRELOAD", "")
    if LAYER_SHELL_LIB in preload:
        env["AUTOCLICK_PRELOADED"] = "1"
        return
    env["LD_PRELOAD"] = (preload + " " + LAYER_SHELL_LIB).strip()
    env["AUTOCLICK_PRELOADED"] = "1"
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)]
              + sys.argv[1:], env)


def main(argv=None):
    p = argparse.ArgumentParser(description="Autoclicker /dev/uinput (0.01–30 s)")
    p.add_argument("--toggle", action="store_true", help="alterner marche/arrêt sur l'instance active")
    p.add_argument("--stop", action="store_true", help="arrêter l'instance active")
    p.add_argument("--run", action="store_true", help="mode sans interface")
    p.add_argument("--interval", type=float, default=0.1, help="délai entre clics en secondes")
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--press", type=float, default=20.0, help="durée d'appui en ms")
    p.add_argument("--count", type=int, default=0, help="nombre de clics (0 = infini)")
    p.add_argument("--delay", type=float, default=0.0, help="décompte avant démarrage (s)")
    p.add_argument("--pin", action="store_true", help="épingler la position du curseur")
    p.add_argument("--x", type=int, help="coordonnée X fixe")
    p.add_argument("--y", type=int, help="coordonnée Y fixe")
    p.add_argument("--coord-mode", choices=["screen", "window"], default="screen",
                   help="espace des coordonnées fixes")
    p.add_argument("--target-class", help="classe WM de la fenêtre cible (ex. vesktop)")
    p.add_argument("--no-focus-check", action="store_true",
                   help="ne pas réactiver la fenêtre cible si elle perd le focus")
    args = p.parse_args(argv)

    if args.interval < MIN_IV or args.interval > MAX_IV:
        p.error(f"--interval doit être entre {MIN_IV} et {MAX_IV}")

    if args.run:
        return cli_run(args)

    if args.toggle or args.stop:
        sig = signal.SIGUSR2 if args.stop else signal.SIGUSR1
        if send_signal(sig):
            return 0
        if args.stop:
            return 0
        # pas d'instance : on lance l'interface et on y applique le toggle
        args.pending_toggle = True
    else:
        args.pending_toggle = False

    pid = read_pidfile()
    if pid and not args.toggle and not args.stop:
        notify("Autoclicker est déjà lancé (raccourci global pour démarrer/arrêter).")
        return 0

    ensure_layer_shell()

    App = build_gui()
    app = App()

    # signaux → drapeaux lus par la boucle GTK
    state = {"toggle": args.pending_toggle, "stop": False}
    signal.signal(signal.SIGUSR1, lambda *_: state.__setitem__("toggle", True))
    signal.signal(signal.SIGUSR2, lambda *_: state.__setitem__("stop", True))

    # branche les drapeaux dans la fenêtre au moment où elle est créée
    app.connect("activate", lambda a: GLib_pump(a, state))

    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        return app.run([])
    finally:
        try:
            if read_pidfile() == os.getpid():
                os.remove(PIDFILE)
        except Exception:                                       # noqa: BLE001
            pass


def GLib_pump(app, state):
    """Recopie les drapeaux signal → fenêtre GTK."""
    from gi.repository import GLib

    def tick():
        win = app.props.active_window
        for k in ("toggle", "stop"):
            if state[k]:
                state[k] = False
                if win is not None and hasattr(win, "pending"):
                    win.pending[k] = True
        return True

    GLib.timeout_add(80, tick)


if __name__ == "__main__":
    sys.exit(main())
