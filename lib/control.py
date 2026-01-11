import math
import logging
import pickle
import time
import copy
import threading
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
        
        # ACTIVITY TRACKING
        self.last_activity_time = timer()
        self.is_sleeping = False
        self.sleep_start_time = 0 
        
        # ASYNC SCANNER VARIABLES
        self.discovered_mac: Optional[str] = None
        self.scale_is_connected_flag = False 
        
        self.load_memory()

        self.relay = DigitalOutputDevice(ControlManager.RELAY_GPIO)

        # TARGET BUTTONS
        self.tgt_inc_button = Button(ControlManager.TGT_INC_GPIO, hold_time=0.5, hold_repeat=True, pull_up=True, bounce_time=0.02)
        self.tgt_inc_button.when_released = lambda: self.register_activity() or self.__change_target(0.1)
        self.tgt_inc_button.when_held = lambda: self.register_activity() or self.__change_target_held(1)

        self.tgt_dec_button = Button(ControlManager.TGT_DEC_GPIO, hold_time=0.5, hold_repeat=True, pull_up=True, bounce_time=0.02)
        self.tgt_dec_button.when_released = lambda: self.register_activity() or self.__change_target(-0.1)
        self.tgt_dec_button.when_held = lambda: self.register_activity() or self.__change_target_held(-1)

        # PADDLE SWITCH
        self.paddle_switch = Button(ControlManager.PADDLE_GPIO, pull_up=True, bounce_time=0.05)
        self.paddle_switch.when_pressed = lambda: self.register_activity() or self.__start_shot()
        
        self.tare_button = Button(ControlManager.TARE_GPIO, pull_up=True) 

        self.memory_button = Button(ControlManager.MEM_GPIO, pull_up=True)
        self.memory_button.when_pressed = lambda: self.register_activity() or self.__rotate_memory()

        self.scale_connect_button = Button(ControlManager.SCALE_CONNECT_GPIO, pull_up=True)
        self.tgt_button_was_held = False

        # START THREADS
        self.wd_thread = threading.Thread(target=self._watchdog_loop)
        self.wd_thread.daemon = True
        self.wd_thread.start()

        self.scan_thread = threading.Thread(target=self._bg_scan_loop)
        self.scan_thread.daemon = True
        self.scan_thread.start()

    # --- ACTIVITY HELPERS ---
    def register_activity(self):
        """Updates timestamp and wakes system if sleeping."""
        self.last_activity_time = timer()
        if self.is_sleeping:
            logging.info("Activity Detected -> Waking Up from Sleep Mode")
            self.is_sleeping = False
            self.discovered_mac = None 

    def check_auto_sleep(self, timeout_seconds):
        """Checks if inactivity timeout is reached."""
        if timeout_seconds <= 0: return False 
        
        if self.relay_on():
            self.last_activity_time = timer()
            return False

        if not self.is_sleeping and (timer() - self.last_activity_time > timeout_seconds):
            logging.info(f"No activity for {timeout_seconds}s -> Sleep Mode Active (Scanner Paused)")
            self.is_sleeping = True
            self.sleep_start_time = timer()
            return True
        return False
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
            if self.is_sleeping:
                time.sleep(1)
                continue

            if self.should_scale_connect() and not self.scale_is_connected_flag and self.discovered_mac is None:
                try:
                    devices = pyacaia.find_acaia_devices(timeout=1)
                    if devices:
                        self.discovered_mac = devices[0]
                        logging.debug("Background Scanner found: %s" % self.discovered_mac)
                        # --- FIX: Finding a device resets the sleep timer ---
                        self.register_activity() 
                        # --------------------------------------------------
                        time.sleep(1) 
                    else:
                        time.sleep(10) 
                except Exception as e:
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
        def wrapped_callback():
            self.register_activity()
            callback()
        self.tare_button.when_pressed = wrapped_callback

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
        if self.tare_button.when_pressed is not None:
            self.tare_button.when_pressed()
            logging.info("Sent tare to scale")
        self.shot_timer_start = timer()
        self.relay.on()


def try_connect_scale(scale: AcaiaScale, mgr: ControlManager) -> bool:
    try:
        mgr.scale_is_connected_flag = scale.connected

        # DISCONNECT ON SLEEP
        if mgr.is_sleeping:
            if scale.connected:
                logging.info("Sleep Mode Active -> Disconnecting Scale")
                scale.disconnect()
            return False

        if not mgr.should_scale_connect():
            if scale.connected:
                logging.debug("Scale connect switch off, disconnecting")
                scale.disconnect()
            return False

        if scale.connected:
            return True

        if mgr.discovered_mac:
            logging.info("Main Thread connecting to found MAC: %s" % mgr.discovered_mac)
            scale.mac = mgr.discovered_mac
            scale.connect()
            
            if scale.connected:
                logging.info("Scale connected - Clearing old shot data")
                mgr.flow_rate_data.clear()
                
                # --- FIX: Connection success resets the sleep timer ---
                mgr.register_activity()
                # ----------------------------------------------------
                
                if mgr.relay_on() and not mgr.paddle_switch.is_pressed:
                    logging.warning("Ghost Start detected during connection. Forcing Relay OFF.")
                    mgr.relay.off()

            mgr.discovered_mac = None 
            return True

        return False

    except Exception as ex:
        logging.error("Failed to connect to found device:%s" % str(ex))
        mgr.discovered_mac = None
        return False