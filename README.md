## Pico-Vga-Python

VGA output library for Raspberry Pi Pico using PIO state machines and DMA.

## Hardware Setup

### Pinout
- **GP0-GP2**: RGB output (1 bit per channel)
- **GP4**: H-Sync
- **GP5**: V-Sync
- **GP16-GP21**: General purpose GPIO (controllable via serial)

### Display
- **Resolution**: 640x480 @ 60Hz
- **Colors**: 8 colors (3-bit RGB)

## Installation

1. Flash MicroPython to your Pico
2. Upload `VGA.py` to the Pico
3. Run the script

## Usage

## Schematic
<img width="1362" height="661" alt="Ekran görüntüsü 2025-10-23 221937" src="https://github.com/user-attachments/assets/09aea598-941e-4abe-b7b5-8a2d751268f6" />



https://github.com/user-attachments/assets/1c7bba68-1e3c-47c9-b2b4-7759eed3f4d5


https://github.com/user-attachments/assets/eb876ddb-2cf8-4164-84c0-fa674099fab2


### Serial Commands

Connect via USB serial terminal (115200 baud):

```
DEMO          - 3D rotating cube demo
TEXT          - Text terminal mode
BLUE/RED/GREEN - Solid color backgrounds
MULTICOLOUR   - Color pattern
HELP          - Show all commands
CLEAR         - Clear terminal
STATUS        - GPIO status
```

### GPIO Control

```
GPIO 16 ON or 1   - Turn on GP16
GPIO 17 OFF or 0   - Turn off GP17
```

Supported pins: GP16, GP17, GP18, GP19, GP20, GP21

## Drawing Functions

```python
fill_screen(color)                    # Fill entire screen
draw_pix(x, y, color)                 # Draw single pixel
draw_rect(x1, y1, x2, y2, color)      # Draw rectangle outline
fill_rect(x1, y1, x2, y2, color)      # Draw filled rectangle
draw_circle(x, y, radius, color)      # Draw circle outline
fill_disk(x, y, radius, color)        # Draw filled circle
draw_text(x, y, "text", color, scale) # Draw text
draw_line(x1, y1, x2, y2, color)      # Draw line
```

## Colors

```python
BLACK, WHITE, RED, GREEN, BLUE, YELLOW, CYAN, MAGENTA
```

## Memory

Uses ~31KB for framebuffer (640x480x3 bits)

## Overclocking

Set `OVCLK = True` for 250MHz operation (I don't recommend it, but if you wish, you may try it.)
