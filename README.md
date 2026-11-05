# autoclicker

Autoclicker pour Wayland / Hyprland. Les clics sont injectés au niveau noyau via
`/dev/uinput` : ils fonctionnent partout — Wayland natif, XWayland, jeux Proton —
puisqu'aucune API de compositeur n'est utilisée.

Interface GTK4 flottante (layer-shell), plus un mode ligne de commande.

## Fonctionnalités

- Intervalle réglable de 0,01 s à 30 s, bouton gauche / droit / milieu,
  durée d'appui, nombre de clics (0 = infini) et décompte avant démarrage.
- Trois façons de viser :
  - **position actuelle de la souris** (éventuellement épinglée) ;
  - **coordonnées fixes**, capturées en cliquant sur l'écran avec une loupe
    (« Sélectionner une position ») ;
  - **fenêtre cible** choisie parmi les fenêtres ouvertes, avec réactivation et
    vérification avant chaque clic : si la cible n'est pas sûre, aucun clic n'est
    envoyé.
- Coordonnées **absolues** (écran) ou **relatives à la fenêtre cible**,
  recalculées à chaque clic : la position suit les déplacements de la fenêtre.
- Bouton « Tester la position » : un clic unique pour vérifier avant de lancer.
- Raccourci global : `--toggle` démarre ou arrête l'instance déjà ouverte
  (l'interface reste lancée en arrière-plan).

## Dépendances

- Python 3 et `python-evdev` (`from evdev import UInput`)
- GTK 4 + PyGObject (`python-gobject`)
- `gtk4-layer-shell` (préchargé automatiquement via `LD_PRELOAD` pour que la
  fenêtre soit un layer-shell ; le chemin est `/usr/lib/libgtk4-layer-shell.so`)
- Hyprland (IPC) pour la sélection de fenêtre cible — facultatif, le reste
  fonctionne sans.

Arch :

```sh
sudo pacman -S python-evdev python-gobject gtk4 gtk4-layer-shell
```

## Autoriser `/dev/uinput`

Une règle udev suffit (à faire une fois, puis se reconnecter) :

```sh
echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-uinput.rules
sudo udevadm control --reload && sudo modprobe uinput
```

Sans cette règle, l'application démarre mais affiche l'erreur
« Accès à /dev/uinput impossible » et n'envoie aucun clic.

## Installation

```sh
mkdir -p ~/.local/bin ~/.local/share/applications
install -m755 autoclicker.py ~/.local/bin/autoclicker
install -m644 autoclicker.desktop ~/.local/share/applications/
```

## Utilisation

Interface graphique :

```sh
autoclicker
```

Ligne de commande (sans interface, pratique pour un script ou un cron) :

```sh
autoclicker --run --interval 0.1 --button left --count 100 --x 960 --y 540
```

| Option | Effet |
|---|---|
| `--toggle` | alterne marche/arrêt sur l'instance déjà lancée |
| `--stop` | arrête l'instance déjà lancée |
| `--run` | mode sans interface |
| `--interval` | délai entre clics en secondes (0.01 – 30) |
| `--button` | `left`, `right` ou `middle` |
| `--press` | durée d'appui en ms |
| `--count` | nombre de clics (0 = infini) |
| `--delay` | décompte avant démarrage, en secondes |
| `--pin` | épingle la position du curseur au démarrage |
| `--x`, `--y` | coordonnées fixes |
| `--coord-mode` | `screen` (absolu) ou `window` (relatif à la fenêtre cible) |
| `--target-class` | classe WM de la fenêtre cible (ex. `vesktop`) |
| `--no-focus-check` | ne pas réactiver la fenêtre cible si elle perd le focus |

Raccourci global sous Hyprland (`~/.config/hypr/config/keybinds.lua`) :

```lua
hl.bind("SUPER + X", hl.dsp.exec_cmd("autoclicker --toggle"))
```

## Configuration

Les réglages de l'interface sont mémorisés dans
`~/.config/autoclick/settings.json`. Le PID de l'instance tourne dans
`$XDG_RUNTIME_DIR/autoclicker.pid`.

## Limites

- Hyprland uniquement pour la sélection de fenêtre et l'épinglage du curseur
  (IPC) ; le mode coordonnées fixes ne dépend pas du compositeur.
- Le chemin de `gtk4-layer-shell` est codé pour Arch (`/usr/lib/libgtk4-layer-shell.so`) ;
  sur une autre distribution, adapter `LAYER_SHELL_LIB` dans le script.
