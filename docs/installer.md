# Installer guide — bus install (README 14/15)

- [ ] Front Pi 4B at front sign, ribbon <50 cm, buffered HAT seated, panels daisy-chained.
- [ ] Side Pi 3B at side sign, ribbon <50 cm, chain=2. Rear Pi 3B, chain=1.
- [ ] Ethernet each Pi to front switch; `ping 10.9.0.2/3` + state sync.
- [ ] Bucks at each sign, per-panel fuses (5–7.5 A), star grounds, Pi taps fused separately.
- [ ] USB keypad at cab, USB audio to PA/amp, `dtparam=audio=off`.
- [ ] Full-white load per sign: drop ≤100 mV, throttled flags clear, brightness 70 for service.
- [ ] AP password + local UI login changed, hotspot SSID saved.
- [ ] Pairing (§15): power on → 6-char code on boards + 192.168.4.1 → hotspot → portal pair → upload program → select code → verify all signs + audio.

Power: step 12/24 V down to 5 V AT each sign; long run carries high voltage, 5 V runs <1 m.
Front buck ≥15 A, side ≥10 A, rear ≥5 A. Pi 4: 5 V/3 A, Pi 3B: 5 V/2.5 A, separate taps.
