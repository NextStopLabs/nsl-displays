# Pi wiring quick reference (README 4.1) - regular mapping, parallel=1
# Pi header pin -> panel IDC. E line REQUIRED for 80x40 panels.

| Pi header | Signal | Panel IDC |
|---|---|---|
| 6 (+9/14/20) | GND | GND |
| 7 | STB/LAT | LAT |
| 11 | CLK | CLK |
| 12 (GPIO18) | OE- | OE |
| 15 | A | A |
| 16 | B | B |
| 18 | C | C |
| 22 | D | D |
| 10 | E | E (required 80x40) |
| 13 | [1] G1 | G1 |
| 19 | [1] B2 | B2 |
| 21 | [1] G2 | G2 |
| 23 | [1] R1 | R1 |
| 24 | [1] R2 | R2 |
| 26 | [1] B1 | B1 |

# Chains: front chain=3, side chain=2, rear chain=1. Ribbon <50 cm.
# Bus LAN: front 10.9.0.1, side 10.9.0.2, rear 10.9.0.3, /24, unmanaged switch.
