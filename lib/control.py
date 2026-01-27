import math
import logging
import pickle
import time
import copy
import threading
import os
import sys
from collections import deque
from timeit import default_timer as timer
from typing import Optional, Callable

from gpiozero import Button, DigitalOutputDevice

import lib.pyacaia as pyacaia
from lib.pyacaia import AcaiaScale

default_target = 36.0
default_overshoot = 1.0
memory_save_file = "memory.save"

class TargetMemory:
    def __init__(self, name: str, color="#ff1303"):
        self.name: str = name
        self.target: float = default_target
        self.overshoot: float = default_overshoot
        self.color: str = color

    def target_minus_overshoot(self) -> float:
        return self.target - self.overshoot

    def update_overshoot(self, weight: float):
        new_overshoot = self.overshoot + (weight - self.target)
        if new_overshoot > 10 or new_overshoot < -10:
            logging.error("New overshoot out of safe range, ignoring")
        else:
            self.overshoot = new_overshoot
            logging.debug("set new overshoot to %.2f" % self.overshoot)


class ControlManager:
    TARE_GPIO = 4
    MEM_GPIO = 21
    SCALE_CONNECT_GPIO = 5
    TGT_INC_GPIO = 12
    TGT_DEC_GPIO = 16
    PADDLE_GPIO = 20
    RELAY_GPIO = 26

    def __init__(self, max_flow_points=500):
        self.flow_rate_data = deque([])
        self.flow_rate_max_points = max_flow_points
        self.relay_off_time = timer()
        self.shot_timer_start: Optional[float] = None
        self.image_needs_save = False
        self.running = True
        
        # --- AUTO-SLEEP CONFIG ---
        self.idle_timeout = int(os.environ.get('IDLE_TIMEOUT', 300))
        self.sleep_pause = int(os.environ.get('SLEEP_PAUSE', 360))
        
        self.last_activity = timer()
        self.is_sleeping = False
        self.sleep_end_time = 0.0
        self.last_weight_check = 0.0
        # -------------------------

        # ASYNC SCANNER VARIABLES
        self.discovered_mac: Optional[str] = None
        self.scale_is_connected_flag = False 
        
        self.load_memory()

        self.relay = DigitalOutputDevice(ControlManager.RELAY_GPIO)

        # TARGET BUTTONS
        self.tgt_inc_button = Button(ControlManager.TGT_INC_GPIO, hold_time=0.5, hold_repeat=True, pull_up=True, bounce_time=0.02)
        self.tgt_inc_button.when_released = lambda: (self._activity_detected(), self.__change_target(0.1))
        self.tgt_inc_button.when_held = lambda: (self._activity_detected(), self.__change_target_held(1))

        self.tgt_dec_button = Button(ControlManager.TGT_DEC_GPIO, hold_time=0.5, hold_repeat=True, pull_up=True, bounce_time=0.02)
        self.tgt_dec_button.when_released = lambda: (self._activity_detected(), self.__change_target(-0.1))
        self.tgt_dec_button.when_held = lambda: (self._activity_detected(), self.__change_target_held(-1))

        # PADDLE SWITCH
        self.paddle_switch = Button(ControlManager.PADDLE_GPIO, pull_up=True, bounce_time=0.05)
        self.paddle_switch.when_pressed = lambda: (self._activity_detected(), self.__start_shot())
        
        self.tare_button = Button(ControlManager.TARE_GPIO, pull_up=True) 

        self.memory_button = Button(ControlManager.MEM_GPIO, pull_up=True)
        self.memory_button.when_pressed = lambda: (self._activity_detected(), self.__rotate_memory())

        self.scale_connect_button = Button(ControlManager.SCALE_CONNECT_GPIO, pull_up=True)
        self.scale_connect_button.when_pressed = lambda: self._activity_detected()
        
        self.tgt_button_was_held = False

        # START THREADS
        self.wd_thread = threading.Thread(target=self._watchdog_loop)
        self.wd_thread.daemon = True
        self.wd_thread.start()

        self.scan_thread = threading.Thread(target=self._bg_scan_loop)
        self.scan_thread.daemon = True
        self.scan_thread.start()

    # --- AUTO-SLEEP LOGIC ---
    def _activity_detected(self):
        self.last_activity = timer()
        if self.is_sleeping:
            logging.info("Activity Detected -> Waking Up from Sleep Mode")
            self.is_sleeping = False

    def check_auto_sleep(self, scale: AcaiaScale):
        now = timer()
        
        # Check for weight change (Activity)
        if scale.connected:
            if abs(scale.weight - self.last_weight_check) > 0.3: # 0.3g threshold for activity
                self._activity_detected()
            self.last_weight_check = scale.weight

            # Logic: Enter Sleep
            if not self.is_sleeping:
                if (now - self.last_activity) > self.idle_timeout:
                    logging.info(f"No activity for {self.idle_timeout}s -> Sleep Mode Active (Scanner Paused)")
                    self.is_sleeping = True
                    self.sleep_end_time = now + self.sleep_pause
                
                    # Disconnect scale if connected
                    logging.info("Disconnecting scale for sleep...")
                    scale.disconnect()

        # Logic: Auto-Wake after pause
        elif self.is_sleeping:
            if now > self.sleep_end_time:
                logging.info("Sleep Pause Timeout Reached -> Auto-Waking System")
                self._activity_detected() # Resets flags and timers
    # ------------------------

    def _watchdog_loop(self):
        logging.info("Paddle Watchdog Started")
        while self.running:
            if self.relay_on() and not self.paddle_switch.is_pressed:
                time.sleep(0.05) 
                if not self.paddle_switch.is_pressed:
                    logging.info("Watchdog detected paddle OPEN - Stopping shot")
                    self.disable_relay()
            time.sleep(0.05)

    def _bg_scan_loop(self):
        logging.info("Bluetooth Background Scanner Started")
        while self.running:
            
            # --- PAUSE SCANNING IF SLEEPING ---
            if self.is_sleeping:
                time.sleep(1)
                continue
            # ----------------------------------

            if self.should_scale_connect() and not self.scale_is_connected_flag and self.discovered_mac is None:
                try:
                    devices = pyacaia.find_acaia_devices(timeout=1)
                    if devices:
                        self.discovered_mac = devices[0]
                        logging.info("Scanner found Scale: %s (Handing over to Main Thread)" % self.discovered_mac)
                        time.sleep(1) 
                    else:
                        time.sleep(6)
                except Exception as e:
                    # --- SAFETY NET: AUTO-RESTART IF D-BUS IS DEAD ---
                    err_str = str(e)
                    if "AccessDenied" in err_str or "registered" in err_str or "Hello" in err_str:
                        logging.fatal(f"CRITICAL: D-Bus Connection Limit Reached. Restarting Service... Error: {err_str}")
                        os._exit(1) # Kill Process. Systemd will restart it.
                    # -------------------------------------------------
                    
                    logging.error("Scanner Error: %s" % e)
                    time.sleep(10)
            else:
                time.sleep(1)

    def save_memory(self):
        self._save_worker(self.memories)

    def _save_worker(self, data_to_save):
        try:
            with open(memory_save_file, 'wb') as savefile:
                pickle.dump(data_to_save, savefile)
                logging.info("Saved shot data to memory")
        except Exception as e:
            logging.error("Error persisting memory: %s" % e)

    def load_memory(self):
        try:
            with open(memory_save_file, 'rb') as savefile:
                self.memories = pickle.load(savefile)
        except Exception as e:
            logging.warn("Not able to load memory from save, resetting memory to defaults. Error was: %s" % e)
            self.memories = deque([TargetMemory("A"), TargetMemory("B", "#25a602"), TargetMemory("C", "#376efa")])

    def add_tare_handler(self, callback: Callable):
        # Modified to trigger activity
        self.tare_button.when_pressed = lambda: (self._activity_detected(), callback())

    def should_scale_connect(self) -> bool:
        return self.scale_connect_button.value

    def relay_on(self) -> bool:
        return self.relay.value

    def add_flow_rate_data(self, data_point: float):
        if self.relay_on() or self.relay_off_time + 3.0 > timer():
            self.flow_rate_data.append(data_point)
            if len(self.flow_rate_data) > self.flow_rate_max_points:
                self.flow_rate_data.popleft()
            
    def disable_relay(self):
        if self.relay_on():
            logging.info("disable relay")
            self.relay_off_time = timer()
            self.relay.off()
            
            if self.scale_is_connected_flag:
                memories_snapshot = copy.deepcopy(self.memories)
                save_thread = threading.Thread(target=self._save_worker, args=(memories_snapshot,))
                save_thread.start()
            else:
                logging.info("Scale disconnected - Skipping memory save")

    def current_memory(self):
        return self.memories[0]

    def shot_time_elapsed(self):
        if self.shot_timer_start is None:
            return 0.0
        elif self.relay_on():
            return timer() - self.shot_timer_start
        else:
            return self.relay_off_time - self.shot_timer_start

    def __change_target(self, amount):
        if not self.tgt_button_was_held:
            self.memories[0].target += amount
        else:
            self.tgt_button_was_held = False

    def __change_target_held(self, amount):
        self.tgt_button_was_held = True
        if amount > 0:
            self.memories[0].target = math.floor(self.memories[0].target) + math.floor(amount)
        if amount < 0:
            self.memories[0].target = math.ceil(self.memories[0].target) + math.ceil(amount)

    def __rotate_memory(self):
        self.memories.rotate(-1)

    def __start_shot(self):
        if self.relay_on():
            return
            
        logging.info("Start shot")
        self.flow_rate_data = deque([])
        
        # --- FIX: Priority to Relay (Coffee First) ---
        self.shot_timer_start = timer()
        self.relay.on()
        # ---------------------------------------------
        
        # Then try to Tare (Best Effort)
        try:
            if self.tare_button.when_pressed:
                logging.info("Auto-Taring Scale...")
                self.tare_button.when_pressed()
        except Exception as e:
            logging.error(f"Auto-Tare failed (ignoring to keep shot running): {e}")

def try_connect_scale(scale: AcaiaScale, mgr: ControlManager) -> bool:
    try:
        mgr.scale_is_connected_flag = scale.connected

        if not mgr.should_scale_connect():
            if scale.connected:
                logging.debug("Scale connect switch off, disconnecting")
                scale.disconnect()
            return False

        if scale.connected:
            return True

        if mgr.discovered_mac:
            logging.info("Main Thread connecting to found MAC: %s" % mgr.discovered_mac)
            
            # --- FIX 1: Reset Idle Timer immediately ---
            # Prevents Auto-Sleep from killing the connection instantly
            mgr._activity_detected() 
            # -------------------------------------------

            scale.mac = mgr.discovered_mac
            
            logging.info("Clearing old shot data (Preparing to Connect)")
            mgr.flow_rate_data.clear()
            
            # --- FIX 2: Only reset timer if we are NOT mid-shot ---
            if not mgr.relay_on():
                mgr.shot_timer_start = None
            # ------------------------------------------------------

            # Check for ghost relay state (Safety)
            if mgr.relay_on() and not mgr.paddle_switch.is_pressed:
                logging.warning("Ghost Start detected during connection. Forcing Relay OFF.")
                mgr.relay.off()

            scale.connect()
            
            mgr.discovered_mac = None 
            return True

        return False

    except Exception as ex:
        logging.error("Failed to connect to found device:%s" % str(ex))
        mgr.discovered_mac = None
        return False