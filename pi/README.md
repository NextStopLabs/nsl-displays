# Pi software (`pi/`)

One logical controller per bus. Front Pi 4B runs core + player + agent + keypad +
front display + AP. Side/rear Pis run the receiver + local display only.

| module | job |
|---|---|
| `core.py` | state, override lifetime rule, announcement composition |
| `player.py` | gapless FLAC→PCM chain → ALSA |
| `renderer.py` | shared Pillow layout renderer (portal preview identical) |
| `display_driver.py` | owns HUB75 matrix, holds last frame |
| `receiver.py` | polls `http://10.9.0.1:8090/state` @10 Hz, hold-last-frame |
| `input_keypad.py` | evdev USB keypad, 1–8 digit entry (Enter confirms, auto-select at 8) |
| `gpio.py` | optional GPIO bindings (free pins only) |
| `agent.py` | portal sync (program→audio→software), heartbeat, outbox |
| `local_ui.py` | `192.168.4.1` FastAPI app |
| `netmode.py` | hotspot client vs AP logic |
| `update.py` | signed A/B update + rollback |
| `store.py` | `controller.db` bootstrap |

Install: `sudo bash pi/install.sh front bus-1` (or `side`/`rear`).
Bench: `python3 scripts/bench_test.py`. Seed demo: `python3 scripts/seed.py`.
