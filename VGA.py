from machine import Pin
from rp2 import PIO, StateMachine, asm_pio
from micropython import const
from array import array
from uctypes import addressof
from gc import mem_free, collect

AVAILABLE_GPIOS = {16, 17, 18, 19, 20, 21}
gpio_pins = {}
OVCLK = False

def init_gpio(pin_num):
    if pin_num in AVAILABLE_GPIOS and pin_num not in gpio_pins:
        gpio_pins[pin_num] = Pin(pin_num, Pin.OUT)
        gpio_pins[pin_num].off()
        return True
    return False

def gpio_control(pin_num, state):
    if pin_num not in AVAILABLE_GPIOS:
        return False, f"GP{pin_num} not available"
    
    if pin_num not in gpio_pins:
        init_gpio(pin_num)
    
    gpio_pins[pin_num].value(state)
    return True, f"GP{pin_num} {'ON' if state else 'OFF'}"

@micropython.viper
def set_freq(fclock:int)->int:
    if fclock < 100000000 or fclock > 250000000:
        print(f"Clock {fclock//1000000}MHz out of range (100-250MHz)")
        return
    
    if fclock <= 130000000:
        FBDIV = fclock // 1000000
        POSTDIV1, POSTDIV2 = 6, 2
    else:
        FBDIV = fclock // 2000000
        POSTDIV1, POSTDIV2 = 3, 2
    
    ptr32(0x4002800c)[0] = (POSTDIV1 << 16) | (POSTDIV2 << 12)
    ptr32(0x40028008)[0] = FBDIV
    print(f"Clock: {FBDIV * 12 // (POSTDIV1 * POSTDIV2)}MHz")

H_res = const(640)
V_res = const(480)
bit_per_pix = const(3)
pixel_bitmask = const(0b111)
usable_bits = const(30)
pix_per_words = const(10)

if OVCLK:
    set_freq(250000000)
    SM0_FREQ, SM1_FREQ, SM2_FREQ = 12587500, 125000000, 113287500
else:
    SM0_FREQ, SM1_FREQ, SM2_FREQ = 25175000, 125000000, 100700000

@asm_pio(set_init=PIO.OUT_HIGH, autopull=True, pull_thresh=32)
def paral_Hsync():
    wrap_target()
    mov(x, osr)
    label("activeporch")
    jmp(x_dec, "activeporch")
    set(pins, 0) [31]
    set(pins, 0) [31]
    set(pins, 0) [31]
    set(pins, 1) [31]
    set(pins, 1) [13]
    irq(0)
    wrap()
     
paral_write_Hsync = StateMachine(0, paral_Hsync, freq=SM0_FREQ, set_base=Pin(4))

@asm_pio(sideset_init=(PIO.OUT_HIGH,) * 1, autopull=True, pull_thresh=32)
def paral_Vsync():
    pull(block)
    wrap_target()
    mov(x, osr)
    label("active")
    wait(1, irq, 0)
    irq(1)
    jmp(x_dec, "active")
    set(y, 9)
    label("frontporch")
    wait(1, irq, 0)
    jmp(y_dec, "frontporch")
    wait(1, irq, 0)              .side(0)
    wait(1, irq, 0)
    set(y, 31)
    label("backporch")
    wait(1, irq, 0)              .side(1)
    jmp(y_dec, "backporch")
    wait(1, irq, 0)
    wrap()
 
paral_write_Vsync = StateMachine(1, paral_Vsync, freq=SM1_FREQ, sideset_base=Pin(5))

@asm_pio(out_init=(PIO.OUT_LOW,) * 3, out_shiftdir=PIO.SHIFT_RIGHT, sideset_init=(PIO.OUT_LOW,) * 3, autopull=True, pull_thresh=usable_bits)
def paral_RGB():
    pull(block)
    mov(y, osr)
    wrap_target()
    mov(x, y)                  .side(0)
    wait(1, irq, 1)
    label("colorout")
    out(pins, 3)
    nop()                      [1]
    jmp(x_dec, "colorout")
    wrap()                   
    
paral_write_RGB = StateMachine(2, paral_RGB, freq=SM2_FREQ, out_base=Pin(0), sideset_base=Pin(0))

@micropython.viper
def configure_DMAs(nword:int, H_buffer_line_add:ptr32):
    TREQ_SEL = 2
    DMA_ctrl = (0 << 21) | (TREQ_SEL << 15) | (0 << 11) | (0 << 10) | \
               (0 << 9) | (0 << 5) | (1 << 4) | (2 << 2) | (1 << 1) | (1 << 0)
    
    ptr32(0x50000040)[0] = 0
    ptr32(0x50000044)[0] = uint(0x50200018)
    ptr32(0x50000048)[0] = nword
    ptr32(0x50000060)[0] = DMA_ctrl
    
    TREQ_SEL = 0x3f
    DMA_ctrl = (0 << 21) | (TREQ_SEL << 15) | (0 << 11) | (0 << 10) | \
               (0 << 9) | (0 << 5) | (0 << 4) | (2 << 2) | (1 << 1) | (1 << 0)
    
    ptr32(0x50000000)[0] = uint(H_buffer_line_add)
    ptr32(0x50000004)[0] = uint(0x5000007c)
    ptr32(0x50000008)[0] = 1
    ptr32(0x50000010)[0] = DMA_ctrl

@micropython.viper
def startsync():
    V=int(ptr16(V_res))
    H=int(ptr16(H_res))
    paral_write_Hsync.put(655)
    paral_write_Vsync.put(int(V-1))
    paral_write_RGB.put(int(H-1))
    ptr32(0x50000430)[0] |= 0b00001
    ptr32(0x50200000)[0] |= 0b111

@micropython.viper
def stopsync():
    ptr32(0x50000444)[0] |= 0b000011
    ptr32(0x50200000)[0] &= 0b111111111000

@micropython.viper
def draw_pix(x:int, y:int, col:int):
    Data = ptr32(H_buffer_line)
    n = int(y * (int(H_res) * int(bit_per_pix)) + x * int(bit_per_pix))
    k = (n // int(usable_bits) - 1) if (n // int(usable_bits) > 0) else (int(len(H_buffer_line)) - 1)
    p = n % int(usable_bits)
    mask = ((int(pixel_bitmask) << p) ^ 0x3FFFFFFF)
    Data[k] = (Data[k] & mask) | (col << p)

@micropython.viper
def fill_screen(col:int):
    Data = ptr32(H_buffer_line)
    mask = 0
    for i in range(int(pix_per_words)):
        mask |= col << (int(bit_per_pix) * i)
    for i in range(int(len(H_buffer_line))):
        Data[i] = mask

@micropython.viper
def clear_region(x1:int, y1:int, x2:int, y2:int, col:int):
    if y1 > y2:
        y1, y2 = y2, y1
    for y in range(y1, y2):
        draw_fastHline(x1, x2, y, col)

@micropython.viper
def draw_fastHline(x1:int,x2:int,y:int,col:int):
    if (x1<0):x1=0
    if (x1>(int(H_res)-1)):x1=(int(H_res)-1)
    if (x2<0):x2=0
    if (x2>(int(H_res)-1)):x2=(int(H_res)-1)
    if (y<0):y=0
    if (y>(int(V_res)-1)):y=(int(V_res)-1)
    if (x2<x1):
        temp = x1
        x1 = x2
        x2 = temp
    Data=ptr32(H_buffer_line)
    n1=int((y)*(int(H_res)*int(bit_per_pix))+ (x1)*int(bit_per_pix))
    n2=int((y)*(int(H_res)*int(bit_per_pix))+ (x2)*int(bit_per_pix))
    k1=(n1//int(usable_bits)-1) if (n1//int(usable_bits)>0)  else (int(len(H_buffer_line))-1)
    k2=(n2//int(usable_bits)-1) if (n2//int(usable_bits)>0)  else (int(len(H_buffer_line))-1)
    if (k2==k1):
        for i in range(x1,x2):
            draw_pix(i,y,col)
        return
    p1=n1%int(usable_bits)
    p2=n2%int(usable_bits)
    mask1off=0
    mask1col=0
    mask2off=0
    mask2col=0
    for i in range(p1//int(bit_per_pix),int(pix_per_words)):
        mask1off|=(int(pixel_bitmask))<<(int(bit_per_pix)*i)
        mask1col|=col<<(int(bit_per_pix)*i)
    mask1off^=int(0x3FFFFFFF)
    for i in range(0,p2//int(bit_per_pix)):
        mask2off|=(int(pixel_bitmask))<<(int(bit_per_pix)*i)
        mask2col|=col<<(int(bit_per_pix)*i)
    mask2off^=0x3FFFFFFF
    Data[k1]=(Data[k1] & mask1off) | mask1col
    Data[k2]=(Data[k2] & mask2off) | mask2col
    mask=0
    for i in range(0,int(pix_per_words)):
        mask|=col<<(int(bit_per_pix)*i)
    i=k1+1
    if (i>(int(len(H_buffer_line))-1)):i=0
    while i < k2:
        Data[i]=mask
        i+=1

@micropython.viper
def draw_fastVline(x:int,y1:int,y2:int,col:int):
    if (x<0):x=0
    if (x>(int(H_res)-1)):x=(int(H_res)-1)
    if (y1<0):y1=0
    if (y1>(int(V_res)-1)):y1=(int(V_res)-1)
    if (y2<0):y2=0
    if (y2>(int(V_res)-1)):y2=(int(V_res)-1)
    if (y2<y1):
        temp = y1
        y1 = y2
        y2 = temp
    Data=ptr32(H_buffer_line)
    n1=int((y1)*(int(H_res)*int(bit_per_pix))+ (x)*int(bit_per_pix))
    k1=(n1//int(usable_bits)-1) if (n1//int(usable_bits)>0)  else (int(len(H_buffer_line))-1)
    p1=n1%int(usable_bits)
    nword=(int(len(H_buffer_line))//int(V_res))
    mask= ((int(pixel_bitmask) << p1)^0x3FFFFFFF)
    for i in range(y2-y1):
        Data[k1+i*nword]=(Data[k1+i*nword] & mask) | (col << p1)

@micropython.viper
def fill_rect(x1:int,y1:int,x2:int,y2:int,col:int):
    j=int(min(y1,y2))
    while (j<int(max(y1,y2))):
        draw_fastHline(x1,x2,j,col)
        j+=1

@micropython.viper
def draw_rect(x1:int,y1:int,x2:int,y2:int,col:int):
    draw_fastHline(x1,x2,y1,col)
    draw_fastHline(x1,x2,y2,col)
    draw_fastVline(x1,y1,y2,col)
    draw_fastVline(x2,y1,y2,col)

@micropython.viper
def draw_circle(x:int, y:int, r:int , color:int):
    if (x < 0 or y < 0 or x >= int(H_res) or y >= int(V_res)):
        return
    x_pos = 0-r
    y_pos = 0
    err = 2 - 2 * r
    while 1:
        draw_pix(x-x_pos, y+y_pos,color)
        draw_pix(x-x_pos, y-y_pos,color)
        draw_pix(x+x_pos, y+y_pos,color)
        draw_pix(x+x_pos, y-y_pos,color)
        e2 = err
        if (e2 <= y_pos):
            y_pos += 1
            err += y_pos * 2 + 1
            if((0-x_pos) == y_pos and e2 <= x_pos):
                e2 = 0
        if (e2 > x_pos):
            x_pos += 1
            err += x_pos * 2 + 1
        if x_pos > 0:
            break

@micropython.viper
def fill_disk(x:int, y:int, r:int , color:int):
    if (x < 0 or y < 0 or x >= int(H_res) or y >= int(V_res)):
        return
    x_pos = 0-r
    y_pos = 0
    err = 2 - 2 * r
    while 1:
        draw_fastHline(x-x_pos,x+x_pos,y+y_pos,color)
        draw_fastHline(x-x_pos,x+x_pos,y-y_pos,color)
        e2 = err
        if (e2 <= y_pos):
            y_pos += 1
            err += y_pos * 2 + 1
            if((0-x_pos) == y_pos and e2 <= x_pos):
                e2 = 0
        if (e2 > x_pos):
            x_pos += 1
            err += x_pos * 2 + 1
        if x_pos > 0:
            break

collect()
mem_before = mem_free()
visible_pix = int((H_res) * V_res * bit_per_pix / usable_bits)
H_buffer_line = array('L', (0 for _ in range(visible_pix)))
H_buffer_line_address = array('L', [addressof(H_buffer_line)])

print(f"Buffer: {(mem_before - mem_free()) / 1024:.1f}KB | RAM: {mem_free() / 1024:.1f}KB")
collect()


RED, GREEN, BLUE = 0b001, 0b010, 0b100
YELLOW, CYAN, MAGENTA = 0b011, 0b110, 0b101
BLACK, WHITE = 0, 0b111

FONT_5X7 = {
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00],
    '!': [0x00, 0x00, 0x5F, 0x00, 0x00],
    '"': [0x00, 0x07, 0x00, 0x07, 0x00],
    '#': [0x14, 0x7F, 0x14, 0x7F, 0x14],
    '$': [0x24, 0x2A, 0x7F, 0x2A, 0x12],
    '%': [0x23, 0x13, 0x08, 0x64, 0x62],
    '&': [0x36, 0x49, 0x55, 0x22, 0x50],
    "'": [0x00, 0x05, 0x03, 0x00, 0x00],
    '(': [0x00, 0x1C, 0x22, 0x41, 0x00],
    ')': [0x00, 0x41, 0x22, 0x1C, 0x00],
    '*': [0x14, 0x08, 0x3E, 0x08, 0x14],
    '+': [0x08, 0x08, 0x3E, 0x08, 0x08],
    ',': [0x00, 0x50, 0x30, 0x00, 0x00],
    '-': [0x08, 0x08, 0x08, 0x08, 0x08],
    '.': [0x00, 0x60, 0x60, 0x00, 0x00],
    '/': [0x20, 0x10, 0x08, 0x04, 0x02],
    '0': [0x3E, 0x51, 0x49, 0x45, 0x3E],
    '1': [0x00, 0x42, 0x7F, 0x40, 0x00],
    '2': [0x42, 0x61, 0x51, 0x49, 0x46],
    '3': [0x21, 0x41, 0x45, 0x4B, 0x31],
    '4': [0x18, 0x14, 0x12, 0x7F, 0x10],
    '5': [0x27, 0x45, 0x45, 0x45, 0x39],
    '6': [0x3C, 0x4A, 0x49, 0x49, 0x30],
    '7': [0x01, 0x71, 0x09, 0x05, 0x03],
    '8': [0x36, 0x49, 0x49, 0x49, 0x36],
    '9': [0x06, 0x49, 0x49, 0x29, 0x1E],
    ':': [0x00, 0x36, 0x36, 0x00, 0x00],
    ';': [0x00, 0x56, 0x36, 0x00, 0x00],
    '<': [0x08, 0x14, 0x22, 0x41, 0x00],
    '=': [0x14, 0x14, 0x14, 0x14, 0x14],
    '>': [0x00, 0x41, 0x22, 0x14, 0x08],
    '?': [0x02, 0x01, 0x51, 0x09, 0x06],
    '@': [0x32, 0x49, 0x79, 0x41, 0x3E],
    'A': [0x7E, 0x11, 0x11, 0x11, 0x7E],
    'B': [0x7F, 0x49, 0x49, 0x49, 0x36],
    'C': [0x3E, 0x41, 0x41, 0x41, 0x22],
    'D': [0x7F, 0x41, 0x41, 0x22, 0x1C],
    'E': [0x7F, 0x49, 0x49, 0x49, 0x41],
    'F': [0x7F, 0x09, 0x09, 0x09, 0x01],
    'G': [0x3E, 0x41, 0x49, 0x49, 0x7A],
    'H': [0x7F, 0x08, 0x08, 0x08, 0x7F],
    'I': [0x00, 0x41, 0x7F, 0x41, 0x00],
    'J': [0x20, 0x40, 0x41, 0x3F, 0x01],
    'K': [0x7F, 0x08, 0x14, 0x22, 0x41],
    'L': [0x7F, 0x40, 0x40, 0x40, 0x40],
    'M': [0x7F, 0x02, 0x0C, 0x02, 0x7F],
    'N': [0x7F, 0x04, 0x08, 0x10, 0x7F],
    'O': [0x3E, 0x41, 0x41, 0x41, 0x3E],
    'P': [0x7F, 0x09, 0x09, 0x09, 0x06],
    'Q': [0x3E, 0x41, 0x51, 0x21, 0x5E],
    'R': [0x7F, 0x09, 0x19, 0x29, 0x46],
    'S': [0x46, 0x49, 0x49, 0x49, 0x31],
    'T': [0x01, 0x01, 0x7F, 0x01, 0x01],
    'U': [0x3F, 0x40, 0x40, 0x40, 0x3F],
    'V': [0x1F, 0x20, 0x40, 0x20, 0x1F],
    'W': [0x3F, 0x40, 0x38, 0x40, 0x3F],
    'X': [0x63, 0x14, 0x08, 0x14, 0x63],
    'Y': [0x07, 0x08, 0x70, 0x08, 0x07],
    'Z': [0x61, 0x51, 0x49, 0x45, 0x43],
    '[': [0x00, 0x7F, 0x41, 0x41, 0x00],
    '\\': [0x02, 0x04, 0x08, 0x10, 0x20],
    ']': [0x00, 0x41, 0x41, 0x7F, 0x00],
    '^': [0x04, 0x02, 0x01, 0x02, 0x04],
    '_': [0x40, 0x40, 0x40, 0x40, 0x40],
    '`': [0x00, 0x01, 0x02, 0x04, 0x00],
    'a': [0x20, 0x54, 0x54, 0x54, 0x78],
    'b': [0x7F, 0x48, 0x44, 0x44, 0x38],
    'c': [0x38, 0x44, 0x44, 0x44, 0x20],
    'd': [0x38, 0x44, 0x44, 0x48, 0x7F],
    'e': [0x38, 0x54, 0x54, 0x54, 0x18],
    'f': [0x08, 0x7E, 0x09, 0x01, 0x02],
    'g': [0x0C, 0x52, 0x52, 0x52, 0x3E],
    'h': [0x7F, 0x08, 0x04, 0x04, 0x78],
    'i': [0x00, 0x44, 0x7D, 0x40, 0x00],
    'j': [0x20, 0x40, 0x44, 0x3D, 0x00],
    'k': [0x7F, 0x10, 0x28, 0x44, 0x00],
    'l': [0x00, 0x41, 0x7F, 0x40, 0x00],
    'm': [0x7C, 0x04, 0x18, 0x04, 0x78],
    'n': [0x7C, 0x08, 0x04, 0x04, 0x78],
    'o': [0x38, 0x44, 0x44, 0x44, 0x38],
    'p': [0x7C, 0x14, 0x14, 0x14, 0x08],
    'q': [0x08, 0x14, 0x14, 0x18, 0x7C],
    'r': [0x7C, 0x08, 0x04, 0x04, 0x08],
    's': [0x48, 0x54, 0x54, 0x54, 0x20],
    't': [0x04, 0x3F, 0x44, 0x40, 0x20],
    'u': [0x3C, 0x40, 0x40, 0x20, 0x7C],
    'v': [0x1C, 0x20, 0x40, 0x20, 0x1C],
    'w': [0x3C, 0x40, 0x30, 0x40, 0x3C],
    'x': [0x44, 0x28, 0x10, 0x28, 0x44],
    'y': [0x0C, 0x50, 0x50, 0x50, 0x3C],
    'z': [0x44, 0x64, 0x54, 0x4C, 0x44],
    '{': [0x00, 0x08, 0x36, 0x41, 0x00],
    '|': [0x00, 0x00, 0x7F, 0x00, 0x00],
    '}': [0x00, 0x41, 0x36, 0x08, 0x00],
    '~': [0x08, 0x04, 0x08, 0x10, 0x08],
}

def draw_char(x, y, char, color, scale=1):
    if char not in FONT_5X7:
        return
    bitmap = FONT_5X7[char]
    for col in range(5):
        for row in range(8):
            if bitmap[col] & (1 << row):
                for sx in range(scale):
                    for sy in range(scale):
                        px = x + col * scale + sx
                        py = y + row * scale + sy
                        if 0 <= px < H_res and 0 <= py < V_res:
                            draw_pix(px, py, color)

def draw_text(x, y, text, color, scale=1):
    cx = x
    for char in text:
        if char == '\n':
            y += 8 * scale + 2 * scale
            cx = x
        else:
            draw_char(cx, y, char, color, scale)
            cx += 6 * scale

from math import sin, cos, pi

class Matrix3D:
    @staticmethod
    def rotate_x(angle):
        c = cos(angle)
        s = sin(angle)
        return [[1, 0, 0], [0, c, -s], [0, s, c]]
    
    @staticmethod
    def rotate_y(angle):
        c = cos(angle)
        s = sin(angle)
        return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    
    @staticmethod
    def rotate_z(angle):
        c = cos(angle)
        s = sin(angle)
        return [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    
    @staticmethod
    def multiply(m1, m2):
        result = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
        for i in range(3):
            for j in range(3):
                for k in range(3):
                    result[i][j] += m1[i][k] * m2[k][j]
        return result
    
    @staticmethod
    def transform(matrix, point):
        x, y, z = point
        return [
            matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
            matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z
        ]

def project_3d(x, y, z, distance=4):
    factor = distance / (distance + z)
    screen_x = int((H_res * 0.5) + x * factor * 80)
    screen_y = int(V_res // 2 - y * factor * 80)
    return screen_x, screen_y

def draw_line(x1, y1, x2, y2, color):
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx - dy
    
    while True:
        if 0 <= x1 < H_res and 0 <= y1 < V_res:
            draw_pix(x1, y1, color)
        
        if x1 == x2 and y1 == y2:
            break
        
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x1 += sx
        if e2 < dx:
            err += dx
            y1 += sy

def fill_triangle(x1, y1, x2, y2, x3, y3, color):
    if y1 > y2:
        x1, y1, x2, y2 = x2, y2, x1, y1
    if y1 > y3:
        x1, y1, x3, y3 = x3, y3, x1, y1
    if y2 > y3:
        x2, y2, x3, y3 = x3, y3, x2, y2
    
    if y3 < 0 or y1 >= V_res:
        return
    
    y_start = max(0, y1)
    y_end = min(V_res - 1, y3)
    
    for y in range(y_start, y_end + 1):
        if y < y2:
            if y2 != y1:
                xa = x1 + (x2 - x1) * (y - y1) // (y2 - y1)
            else:
                xa = x1
            if y3 != y1:
                xb = x1 + (x3 - x1) * (y - y1) // (y3 - y1)
            else:
                xb = x1
        else:
            if y3 != y2:
                xa = x2 + (x3 - x2) * (y - y2) // (y3 - y2)
            else:
                xa = x2
            if y3 != y1:
                xb = x1 + (x3 - x1) * (y - y1) // (y3 - y1)
            else:
                xb = x1
        
        if xa > xb:
            xa, xb = xb, xa
        
        xa = max(0, min(H_res - 1, xa))
        xb = max(0, min(H_res - 1, xb))
        draw_fastHline(xa, xb, y, color)

class Cube3D:
    def __init__(self):
        self.vertices = [
            [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
            [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]
        ]
        
        self.faces = [
            ([0, 1, 2, 3], RED),
            ([4, 5, 6, 7], GREEN),
            ([0, 1, 5, 4], BLUE),
            ([2, 3, 7, 6], YELLOW),
            ([0, 3, 7, 4], MAGENTA),
            ([1, 2, 6, 5], CYAN)
        ]
        
        self.edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7)
        ]
        
        self.angle_x = 0
        self.angle_y = 0
        self.angle_z = 0
    
    def rotate(self, dx, dy, dz):
        self.angle_x += dx
        self.angle_y += dy
        self.angle_z += dz
    
    def draw(self, filled=True):
        rx = Matrix3D.rotate_x(self.angle_x)
        ry = Matrix3D.rotate_y(self.angle_y)
        rz = Matrix3D.rotate_z(self.angle_z)
        rotation = Matrix3D.multiply(Matrix3D.multiply(rx, ry), rz)
        
        transformed = []
        for vertex in self.vertices:
            rotated = Matrix3D.transform(rotation, vertex)
            transformed.append(rotated)
        
        if filled:
            face_depths = []
            for face_indices, color in self.faces:
                avg_z = sum(transformed[i][2] for i in face_indices) / len(face_indices)
                face_depths.append((avg_z, face_indices, color))
            
            face_depths.sort(key=lambda x: x[0])
            
            for _, face_indices, color in face_depths:
                projected = [project_3d(*transformed[i]) for i in face_indices]
                
                if len(projected) == 4:
                    p0, p1, p2, p3 = projected
                    fill_triangle(p0[0], p0[1], p1[0], p1[1], p2[0], p2[1], color)
                    fill_triangle(p0[0], p0[1], p2[0], p2[1], p3[0], p3[1], color)
        else:
            projected = [project_3d(*v) for v in transformed]
            for edge in self.edges:
                p1 = projected[edge[0]]
                p2 = projected[edge[1]]
                draw_line(p1[0], p1[1], p2[0], p2[1], WHITE)

import sys
import select

text_buffer = []
command_history = []
current_mode = "demo"
previous_mode = "demo"
cube = Cube3D()
mode_changed = False
show_terminal = True

TERM_X = 440
TERM_Y = 10
TERM_WIDTH = 190
TERM_HEIGHT = 460
TERM_MAX_LINES = 28

def add_to_terminal(message, color=WHITE):
    global command_history
    command_history.append((message, color))
    if len(command_history) > TERM_MAX_LINES:
        command_history.pop(0)

def draw_terminal():
    if not show_terminal:
        return
    
    fill_rect(TERM_X - 5, TERM_Y - 5, H_res - 5, V_res - 5, BLACK)
    draw_rect(TERM_X - 5, TERM_Y - 5, H_res - 5, V_res - 5, WHITE)
    draw_text(TERM_X, TERM_Y, "Terminal", GREEN, 1)
    draw_text(TERM_X, TERM_Y + 12, "GP16-GP21", CYAN, 1)
    
    y_pos = TERM_Y + 30
    for message, color in command_history:
        if y_pos > TERM_Y + TERM_HEIGHT - 20:
            break
        msg = message[:28] if len(message) > 28 else message
        draw_text(TERM_X, y_pos, msg, color, 1)
        y_pos += 15

def process_command(cmd):
    global current_mode, text_buffer, mode_changed, previous_mode, show_terminal
    
    original_cmd = cmd.strip()
    cmd_upper = cmd.strip().upper()
    
    if cmd_upper.startswith("GPIO ") or cmd_upper.startswith("GP"):
        parts = cmd_upper.replace("GPIO", "GP").split()
        
        if len(parts) >= 2:
            try:
                pin_str = parts[0].replace("GP", "")
                if not pin_str and len(parts) >= 3:
                    pin_str = parts[1]
                    state_str = parts[2] if len(parts) > 2 else ""
                else:
                    state_str = parts[1] if len(parts) > 1 else ""
                
                pin_num = int(pin_str)
                
                if state_str in ["ON", "1", "HIGH"]:
                    success, msg = gpio_control(pin_num, True)
                    if current_mode == "text":
                        add_to_terminal(f"> {original_cmd}", CYAN)
                        add_to_terminal(msg, GREEN if success else RED)
                    return msg
                elif state_str in ["OFF", "0", "LOW"]:
                    success, msg = gpio_control(pin_num, False)
                    if current_mode == "text":
                        add_to_terminal(f"> {original_cmd}", CYAN)
                        add_to_terminal(msg, YELLOW if success else RED)
                    return msg
                else:
                    msg = "Use: GPIO 16 ON/OFF"
                    if current_mode == "text":
                        add_to_terminal(f"> {original_cmd}", CYAN)
                        add_to_terminal(msg, RED)
                    return msg
            except (ValueError, IndexError):
                msg = "Invalid GPIO command"
                if current_mode == "text":
                    add_to_terminal(f"> {original_cmd}", CYAN)
                    add_to_terminal(msg, RED)
                return msg
    
    elif cmd_upper == "BLUE":
        previous_mode = current_mode
        current_mode = "static"
        mode_changed = True
        fill_screen(BLUE)
        draw_text(10, 10, "BLUE MODE", WHITE, 2)
        draw_text(10, 40, "Type DEMO to return", CYAN, 1)
        return "Switched to BLUE background"
    
    elif cmd_upper == "RED":
        previous_mode = current_mode
        current_mode = "static"
        mode_changed = True
        fill_screen(RED)
        draw_text(10, 10, "RED MODE", WHITE, 2)
        draw_text(10, 40, "Type DEMO to return", CYAN, 1)
        return "Switched to RED background"
    
    elif cmd_upper == "GREEN":
        previous_mode = current_mode
        current_mode = "static"
        mode_changed = True
        fill_screen(GREEN)
        draw_text(10, 10, "GREEN MODE", WHITE, 2)
        draw_text(10, 40, "Type DEMO to return", CYAN, 1)
        return "Switched to GREEN background"
    
    elif cmd_upper == "MULTICOLOUR" or cmd_upper == "MULTICOLOR":
        previous_mode = current_mode
        current_mode = "static"
        mode_changed = True
        for h in range(8):
            for i in range(0, 60):
                for k in range(8):
                    col = (h + k) % 8
                    draw_fastHline(k * 80, k * 80 + 80, h * 60 + i, col)
        draw_text(10, 10, "MULTICOLOUR MODE", BLACK, 2)
        draw_text(10, 40, "Type DEMO to return", WHITE, 1)
        return "Switched to MULTICOLOUR pattern"
    
    elif cmd_upper == "DEMO":
        previous_mode = current_mode
        current_mode = "demo"
        mode_changed = True
        fill_screen(BLACK)
        return "Starting 3D cube demo"
    
    elif cmd_upper == "TEXT":
        previous_mode = current_mode
        current_mode = "text"
        mode_changed = True
        command_history.clear()
        text_buffer = []
        fill_screen(BLACK)
        add_to_terminal("TEXT MODE", GREEN)
        add_to_terminal("Type anything", CYAN)
        add_to_terminal("GPIO commands work", CYAN)
        return "TEXT mode - command prompt style"
    
    elif cmd_upper == "CLEAR":
        if current_mode == "text":
            command_history.clear()
            text_buffer = []
            add_to_terminal("Terminal cleared", GREEN)
        return "Terminal cleared"
    
    elif cmd_upper == "HELP":
        if current_mode == "text":
            add_to_terminal(f"> {original_cmd}", CYAN)
            add_to_terminal("Commands:", WHITE)
            add_to_terminal("GPIO <16-21> ON/OFF", GREEN)
            add_to_terminal("DEMO, BLUE, RED", GREEN)
            add_to_terminal("GREEN, MULTICOLOUR", GREEN)
            add_to_terminal("CLEAR, STATUS, HELP", GREEN)
        else:
            previous_mode = current_mode
            current_mode = "static"
            mode_changed = True
            fill_screen(BLACK)
            draw_text(10, 10, "Commands:", WHITE, 2)
            draw_text(10, 40, "BLUE, RED, GREEN", CYAN, 1)
            draw_text(10, 55, "MULTICOLOUR", CYAN, 1)
            draw_text(10, 70, "DEMO - 3D cube", CYAN, 1)
            draw_text(10, 85, "TEXT - cmd prompt", CYAN, 1)
            draw_text(10, 100, "GPIO <16-21> ON/OFF", CYAN, 1)
            draw_text(10, 115, "STATUS, CLEAR, HELP", CYAN, 1)
        return "Help displayed"
    
    elif cmd_upper == "STATUS":
        if current_mode == "text":
            add_to_terminal(f"> {original_cmd}", CYAN)
            add_to_terminal("GPIO Status:", WHITE)
            for pin in sorted(AVAILABLE_GPIOS):
                if pin in gpio_pins:
                    state = "ON" if gpio_pins[pin].value() else "OFF"
                    color = GREEN if gpio_pins[pin].value() else YELLOW
                    add_to_terminal(f"GP{pin}: {state}", color)
                else:
                    add_to_terminal(f"GP{pin}: INIT", WHITE)
        return "Status displayed"
    
    else:
        if current_mode == "text" and original_cmd:
            add_to_terminal(f"> {original_cmd}", WHITE)
            return None
        return None

def read_serial_input():
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline().strip()
        return line
    return None

def main_loop():
    global current_mode, text_buffer, mode_changed
    from time import ticks_ms, ticks_diff, sleep_ms
    
    print("VGA Ready | GPIO: 16-21 | Type HELP")
    fill_screen(BLACK)
    
    while True:
        user_input = read_serial_input()
        if user_input:
            result = process_command(user_input)
            if result:
                print(result)
        
        if current_mode == "demo":
            t = ticks_ms()
            
            if mode_changed:
                fill_screen(BLACK)
                mode_changed = False
            else:
                clear_region(80, 60, 560, 420, BLACK)
            
            cube.rotate(0.05, 0.07, 0.03)
            cube.draw(filled=True)
            
            elapsed = ticks_diff(ticks_ms(), t)
            if elapsed < 33:
                sleep_ms(33 - elapsed)
        
        elif current_mode == "text":
            if mode_changed:
                fill_screen(BLACK)
                mode_changed = False
            
            draw_terminal()
            sleep_ms(50)
        
        elif current_mode == "static":
            sleep_ms(100)

configure_DMAs(len(H_buffer_line), H_buffer_line_address)
startsync()
fill_screen(BLACK)
main_loop()