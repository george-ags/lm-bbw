import logging
import math
import os
import time
import sys
import traceback
import pandas as pd
from datetime import datetime
from enum import Enum
from multiprocessing import Process, Queue
from queue import Empty
from typing import Optional

from PIL import Image, ImageFont, ImageDraw

# Font Loading
try:
    label_font = ImageFont.truetype("lib/font/LiberationMono-Regular.ttf", 16)
    label_font_mid = ImageFont.truetype("lib/font/LiberationMono-Regular.ttf", 20)
    label_font_lg = ImageFont.truetype("lib/font/LiberationMono-Regular.ttf", 24)
    value_font = ImageFont.truetype("lib/font/Quicksand-Regular.ttf", 24)
    # New Medium Font for Landscape Header (approx 20% smaller than 36)
    value_font_med = ImageFont.truetype("lib/font/Quicksand-Regular.ttf", 28) 
    value_font_lg = ImageFont.truetype("lib/font/Quicksand-Regular.ttf", 36)
    value_font_lg_bold = ImageFont.truetype("lib/font/Quicksand-Bold.ttf", 36)
except Exception as e:
    logging.error(f"Error loading fonts: {e}")

bg_color = "BLACK"
light_bg_color = "DIMGREY"
fg_color = "WHITE"

# --- HELPER: Draw Battery Icon ---
def draw_battery(draw, xy, level, scale=1.0):
    x, y = xy
    w = int(24 * scale)
    h = int(12 * scale)
    terminal_w = int(3 * scale)
    padding = 2
    
    if level < 20: fill_color = "RED"
    elif level < 50: fill_color = "YELLOW"
    else: fill_color = "GREEN"

    draw.rectangle((x, y, x + w, y + h), outline=fill_color, fill=bg_color, width=2)
    
    term_y_start = y + int(h * 0.25)
    term_y_end = y + int(h * 0.75)
    draw.rectangle((x + w, term_y_start, x + w + terminal_w, term_y_end), fill=fill_color)
    
    max_fill_w = w - (padding * 2)
    current_fill_w = int(max_fill_w * (level / 100.0))
    if level > 0 and current_fill_w < 1: current_fill_w = 1
        
    if current_fill_w > 0:
        draw.rectangle((x + padding, y + padding, x + padding + current_fill_w, y + h - padding), fill=fill_color)

# --- HELPER: Draw Paddle/Toggle Switch ---
def draw_paddle_switch(draw, xy, is_on, color, scale=1.0):
    """
    Draws a toggle switch with dynamic color.
    """
    x, y = xy
    w = int(36 * scale)
    h = int(18 * scale)
    padding = 2
    knob_dia = h - (padding * 2)
    
    # Draw Track Outline
    draw.rectangle((x, y, x + w, y + h), outline=fg_color, fill=bg_color, width=2)

    if is_on:
        # ON: Fill track with color, knob is black (contrast)
        knob_color = color
        knob_x = x + padding
    else:
        # OFF: Empty track, knob is outline or solid color
        knob_color = color
        knob_x = x + w - padding - knob_dia

    # Draw Knob
    knob_y = y + padding
    draw.ellipse((knob_x, knob_y, knob_x + knob_dia, knob_y + knob_dia), fill=knob_color, outline=fg_color, width=1)

# ---------------------------------------------

class FlowGraph:
    def __init__(self, flow_data: list, series_color="BLUE", label_color="#c7c7c7", line_color="#5a5a5a", max_value=8,
                 width_pixels=240, height_pixels=160):
        self.flow_data = flow_data
        self.max_value = max_value
        self.series_color = series_color
        self.label_color = label_color
        self.line_color = line_color
        self.y_pix = height_pixels
        self.x_pix = width_pixels
        self.y_pix_interval = height_pixels / max_value
        if len(flow_data) > 0:
            self.x_pix_interval = width_pixels / len(flow_data)
        else:
            self.x_pix_interval = width_pixels / 1

    def generate_graph(self) -> Image:
        points = list()
        i = 0
        for y in self.flow_data:
            x_coord = i * self.x_pix_interval if i * self.x_pix_interval < self.x_pix else self.x_pix
            y_coord = y * self.y_pix_interval + 2 if y * self.y_pix_interval < self.y_pix else self.y_pix
            y_coord = abs(y_coord - self.y_pix)
            points.append((x_coord, y_coord))
            i += 1
        img = Image.new("RGBA", (self.x_pix, self.y_pix), "BLACK")
        draw = ImageDraw.Draw(img)

        self.__draw_y_line(draw, 0, self.label_color)
        self.__draw_y_line(draw, self.y_pix * .33 - 2, self.line_color)
        self.__draw_y_line(draw, self.y_pix * .66 - 2, self.line_color)
        self.__draw_y_line(draw, self.y_pix - 2, self.line_color)

        draw.line(points, fill=self.series_color, width=2)

        draw.text((2, 0), "6", self.label_color, label_font)
        draw.text((2, self.y_pix * .33), "4", self.label_color, label_font)
        draw.text((2, self.y_pix * .66), "2", self.label_color, label_font)

        last_flow_rate = self.flow_data[-1] if len(self.flow_data) > 0 else 0
        fmt_flow = "{:0.1f}".format(last_flow_rate)
        fmt_flow_label = "g/s"
        w = draw.textlength(fmt_flow, value_font)
        wl = draw.textlength(fmt_flow_label, label_font)
        draw.text(((self.x_pix - 4 - w - wl), (self.y_pix * .25) - value_font.size - 4), fmt_flow, fg_color, value_font)
        draw.text(((self.x_pix - wl), (self.y_pix * .25) - label_font.size - 4), fmt_flow_label, fg_color, label_font)
        return img

    def __draw_y_line(self, draw: ImageDraw, y, color):
        draw.line((0, y, self.x_pix, y), fill=color, width=1)


class DisplayData:
    def __init__(self, weight: float, sample_rate: float, memory, flow_data: list, battery: int,
                 paddle_on: bool, shot_time_elapsed: float, save_image: bool = False,
                 flow_smooth_factor: int = 8):
        self.weight = weight
        self.sample_rate = sample_rate
        self.memory = memory
        self.flow_data = flow_data
        self.battery = battery
        self.paddle_on = paddle_on
        self.shot_time_elapsed = shot_time_elapsed
        self.save_image = save_image
        self.flow_smooth_factor = flow_smooth_factor

    def flow_rate_moving_avg(self) -> list:
        flow_data_series = pd.Series(self.flow_data)
        flow_data_windows = flow_data_series.rolling(self.flow_smooth_factor)
        return flow_data_windows.mean().dropna().to_list()


class DisplaySize(Enum):
    SIZE_2_4 = 1
    SIZE_2_0 = 2

class DisplayOrientation(str, Enum):
    PORTRAIT = "PORTRAIT"
    LANDSCAPE = "LANDSCAPE"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            value_upper = value.upper()
            for member in cls:
                if member.value == value_upper:
                    return member
        return None

class Display:
    def __init__(self, data_queue: Queue, display_size: DisplaySize = DisplaySize.SIZE_2_0, image_save_dir: str = None):
        self.data_queue: Queue[DisplayData] = data_queue
        self.display_size = display_size
        self.image_save_dir = image_save_dir
        self.display_orientation = DisplayOrientation(os.environ.get('DISPLAY_ORIENTATION', DisplayOrientation.PORTRAIT))
        
        self.lcd = None
        self.on = True
        self.process = None

    def start(self):
        self.process = Process(target=self.__update_display)
        self.process.start()

    def stop(self):
        if self.process is not None:
            self.process.kill()
        self.display_off()

    def display_off(self):
        pass

    def display_on(self):
        pass

    def put_data(self, data: DisplayData):
        self.data_queue.put_nowait(data)

    def save_image(self, img: Image):
        if self.image_save_dir is None:
            logging.info("no directory set to save image")
            return
        if not os.path.exists(self.image_save_dir):
            return
        date = datetime.now().strftime("%Y-%m-%d_%I:%M:%S_%p")
        absolute_path = "{basedir}/{date}.png".format(basedir=self.image_save_dir, date=date)
        try:
            img.save(absolute_path)
        except Exception as ex:
            logging.error("Failed to save image: %s", str(ex))

    def __update_display(self):
        # Hardware init
        try:
            from lib import LCD_2inch4, LCD_2inch
            if self.display_size == DisplaySize.SIZE_2_4:
                self.lcd = LCD_2inch4.LCD_2inch4()
            elif self.display_size == DisplaySize.SIZE_2_0:
                self.lcd = LCD_2inch.LCD_2inch()
            else:
                raise Exception("unknown display size configured: %s" % self.display_size.name)
            
            self.lcd.Init()
            self.lcd.clear()
            
            # --- CLEAN STARTUP: Force Black Frame ---
            w, h = (self.lcd.width, self.lcd.height) if self.display_orientation == DisplayOrientation.PORTRAIT else (self.lcd.height, self.lcd.width)
            img = Image.new("RGBA", (w, h), "BLACK")
            self.lcd.ShowImage(img, 0, 0)
            
            logging.info("Display Hardware Initialized (Clean Start)")
        except Exception as e:
            logging.error(f"Display Hardware Init Failed: {e}")
            return

        screen_is_on = False

        while True:
            try:
                # Wait for data for 2 seconds. If none comes, raise Empty exception.
                data = self.data_queue.get(timeout=2.0)
                
                # Wake up logic
                if not screen_is_on:
                    self.lcd.bl_DutyCycle(50)
                    screen_is_on = True

                if data.battery is None:
                    data.battery = 0
                if data.weight is None:
                    data.weight = 0.0

                w, h = (self.lcd.width, self.lcd.height) if self.display_orientation == DisplayOrientation.PORTRAIT else (self.lcd.height, self.lcd.width)
                
                img = draw_frame(w, h, data, self.display_orientation)

                if data.save_image and img is not None:
                    self.save_image(img)
                self.lcd.ShowImage(img, 0, 0)
                
            except Empty:
                # --- AUTO-SLEEP LOGIC ---
                if screen_is_on:
                    self.lcd.bl_DutyCycle(0)
                    # Force Draw BLACK to wipe video memory
                    w, h = (self.lcd.width, self.lcd.height) if self.display_orientation == DisplayOrientation.PORTRAIT else (self.lcd.height, self.lcd.width)
                    black_img = Image.new("RGBA", (w, h), "BLACK")
                    self.lcd.ShowImage(black_img, 0, 0)
                    screen_is_on = False

            except Exception as e:
                logging.error(f"CRASH IN DISPLAY LOOP: {e}")
                traceback.print_exc()
                time.sleep(1)


def draw_frame(width: int, height: int, data: DisplayData, orientation: DisplayOrientation) -> Image:
    # --- 1. CONFIGURATION ---
    is_landscape = (orientation == DisplayOrientation.LANDSCAPE)
    
    if is_landscape:
        # Smaller header to fit graph + footer
        header_h = 60 
        col_w = 106
        graph_y = 60
        ready_y = 110 # Adjusted for new layout
        has_header_batt = False # Battery moved to footer
        footer_line_y = height - 35
        # Use slightly smaller font for Landscape Header
        header_val_font = value_font_med 
    else:
        # Portrait Defaults
        header_h = 96
        col_w = 120
        graph_y = 98
        ready_y = 164
        has_header_batt = False
        footer_line_y = 285 # Original Portrait logic (285 is effectively "Footer Start" on 320h)
        header_val_font = value_font_lg
        
    background = bg_color
    if data.paddle_on:
        background = light_bg_color
        
    img = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(img)

    # --- 2. GRID LINES ---
    draw.line([(0, header_h), (width, header_h)], fill=fg_color, width=2)
    draw.line([(col_w, 0), (col_w, header_h)], fill=fg_color, width=2)
    if is_landscape:
        draw.line([(col_w * 2, 0), (col_w * 2, header_h)], fill=fg_color, width=2)
        # Footer Line for Landscape
        draw.line([(0, footer_line_y), (width, footer_line_y)], fill=fg_color, width=2)
    else:
        # Footer Line for Portrait
        draw.line([(0, 285), (240, 285)], fill=fg_color, width=2)

    # --- 3. HEADER LABELS & VALUES ---
    # Common Offsets
    lbl_x_1 = 10 if is_landscape else 16
    lbl_x_2 = 118 if is_landscape else 130
    lbl_y = 8 if is_landscape else 16
    
    # Column 1: Weight
    draw.text((lbl_x_1, lbl_y), "weight(g)", fg_color, label_font)
    fmt_weight = "{:0.1f}".format(data.weight)
    w = draw.textlength(fmt_weight, header_val_font)
    h = header_val_font.size
    draw.text(((col_w - w) / 2, (header_h + 12 - h) / 2), fmt_weight, fg_color, header_val_font)
    
    # Column 2: Target
    draw.text((lbl_x_2, lbl_y), "tgt %s(g)" % data.memory.name, fg_color, label_font)
    fmt_target = "{:0.1f}".format(data.memory.target)
    # Use same font size for target
    w = draw.textlength(fmt_target, header_val_font)
    draw.text(((col_w - w) / 2 + col_w, (header_h + 12 - h) / 2), fmt_target, fg_color, header_val_font)

    # Column 3: Logic varies
    if is_landscape:
        # LANDSCAPE HEADER COL 3: BREW TIMER
        timer_label = "timer"
        w_label = draw.textlength(timer_label, label_font)
        # Centered label: Use same offset logic as value
        draw.text(((col_w - w_label)/2 + (col_w * 2) + 2, 8), timer_label, fg_color, label_font)
        
        fmt_timer = "{:0.1f}".format(data.shot_time_elapsed)
        w = draw.textlength(fmt_timer, header_val_font)
        # Center in 3rd col (start ~212)
        draw.text(((col_w - w)/2 + (col_w * 2) + 2, (header_h + 12 - h) / 2), fmt_timer, fg_color, header_val_font)
    
    # --- 4. FOOTER (Battery & Paddle) ---
    # Determine Paddle Color/Text
    p_text = ""
    if data.paddle_on:
        p_color = "BLUE" 
    else:
        p_color = "RED"
        
    fmt_batt = "%d%%" % data.battery
    w_batt_text = draw.textlength(fmt_batt, label_font)
    
    # Calculate Footer Y Position
    # For Landscape, footer starts at footer_line_y (~205) + padding
    # For Portrait, it's ~294
    
    if is_landscape:
        footer_icon_y = footer_line_y + 11 # Vertically centered in 35px footer
        footer_text_y = footer_line_y + 9
        
        # Right Side: Battery
        padding_right = 8
        icon_width = 27 
        batt_icon_x = width - padding_right - icon_width
        batt_text_x = batt_icon_x - 4 - w_batt_text
        
        draw_battery(draw, (batt_icon_x, footer_icon_y), data.battery, scale=1.0)
        draw.text((batt_text_x, footer_text_y), fmt_batt, fg_color, label_font)
        
        # Left Side: Paddle
        paddle_icon_x = 8
        draw_paddle_switch(draw, (paddle_icon_x, footer_icon_y), data.paddle_on, color=p_color, scale=1.0)
        draw.text((paddle_icon_x + 42, footer_text_y), p_text, p_color, label_font)
        
    else:
        # PORTRAIT FOOTER
        # Right Side: Battery
        padding_right = 8
        icon_width = 27 
        batt_icon_x = width - padding_right - icon_width
        batt_text_x = batt_icon_x - 4 - w_batt_text
        
        draw_battery(draw, (batt_icon_x, 296), data.battery, scale=1.0)
        draw.text((batt_text_x, 294), fmt_batt, fg_color, label_font)
        
        # Left Side: Paddle
        draw_paddle_switch(draw, (8, 294), data.paddle_on, color=p_color, scale=1.0)
        draw.text((50, 294), p_text, p_color, label_font)

    # --- 5. READY BOX ---
    fmt_ready = "Ready"
    # Use main font for Ready text
    w = draw.textlength(fmt_ready, value_font_lg)
    h = value_font_lg.size + value_font_lg.size // 2
    center_x = width // 2
    
    # Don't draw ready box if shot is running and we have data (Graph mode)
    # But current logic is to always draw it if not graph? No, original logic was specific.
    # We will stick to drawing it if data says so or relying on layers.
    # The original code drew it unconditionally at the specific Y.
    
    draw.rectangle((center_x - w / 2 - 4, ready_y, center_x + w / 2 + 4, ready_y + h), bg_color, data.memory.color, 4)
    draw.text((center_x - w / 2, ready_y), fmt_ready, fg_color, value_font_lg)

    # --- 6. GRAPH ---
    if data.flow_data is not None and len(data.flow_data) > 0:
        flow_rate_data = data.flow_rate_moving_avg()
        
        if is_landscape:
            # Graph Height = Height (240) - Header (60) - Footer (35) = 145
            g_w, g_h = 320, 145 
            # We don't draw the timer in the graph area anymore for landscape (it's in header)
            # Just draw the graph
            flow_image = FlowGraph(flow_rate_data, data.memory.color, width_pixels=g_w, height_pixels=g_h).generate_graph()
            img.paste(flow_image, (0, header_h))
            
            # Draw Time Axis Labels (bottom of graph area)
            last_sample_time = data.sample_rate * float(len(data.flow_data))
            axis_y = footer_line_y - 20 # Sits just above footer line
            draw.text((4, axis_y), "%ds" % math.ceil(last_sample_time), fg_color, label_font)
            draw.text((width - 22, axis_y), "0s", fg_color, label_font)
            
        else:
            # Portrait Graph
            g_w, g_h = 240, 160
            timer_y = 262
            timer_font = label_font
            
            flow_image = FlowGraph(flow_rate_data, data.memory.color, width_pixels=g_w, height_pixels=g_h).generate_graph()
            img.paste(flow_image, (0, graph_y))
            
            last_sample_time = data.sample_rate * float(len(data.flow_data))
            axis_y = timer_y 
            draw.text((4, axis_y), "%ds" % math.ceil(last_sample_time), fg_color, label_font)
            draw.text((width - 22, axis_y), "0s", fg_color, label_font)

            fmt_shot_time = "timer:{:0.1f}s".format(data.shot_time_elapsed)
            w = draw.textlength(fmt_shot_time, timer_font)
            draw.text(((width - w) / 2, timer_y), fmt_shot_time, fg_color, timer_font)

    return img