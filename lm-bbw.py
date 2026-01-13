#!/usr/bin/env python3
import logging
import os
import signal
import sys
import time

from concurrent.futures import ThreadPoolExecutor
from logging import handlers
from multiprocessing import Queue
from timeit import default_timer as timer
from typing import Optional

from lib import control
from lib.control import ControlManager
from lib.display import Display, DisplayData, DisplaySize
from lib.pyacaia import AcaiaScale
from lib.webserver import WebServer

WEB_PORT = 80
WEB_DIR = '/opt/lm-bbw/web'
MIN_GOOD_SHOT_DURATION = 10
MAC_SAVE_FILE = '/opt/lm-bbw/mac.save'

stop = False
overshoot_update_executor = ThreadPoolExecutor(max_workers=1)

logLevel = os.environ.get('LOGLEVEL', 'INFO').upper()
logPath = os.environ.get('LOGFILE', '/var/log/lm-bbw.log')

refreshRate = float(os.environ.get('REFRESH_RATE', '0.1'))
smoothing = round(1 / refreshRate)

stdout_handler = logging.StreamHandler(stream=sys.stdout)
stdout_handler.setLevel(logging.INFO)
file_handler = handlers.TimedRotatingFileHandler(filename=logPath, when='midnight', backupCount=4)
file_handler.setLevel(logLevel)
handlers = [stdout_handler, file_handler]
logging.basicConfig(
    level=logLevel,
    format='[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s',
    handlers=handlers
)

def save_mac_address(mac):
    try:
        with open(MAC_SAVE_FILE, 'w') as f:
            f.write(mac)
            logging.info(f"Saved Scale MAC {mac} to disk")
    except Exception as e:
        logging.error(f"Failed to save MAC: {e}")

def load_last_mac():
    try:
        if os.path.exists(MAC_SAVE_FILE):
            with open(MAC_SAVE_FILE, 'r') as f:
                mac = f.read().strip()
                if len(mac) > 10:
                    logging.info(f"Loaded Last Known MAC: {mac}")
                    return mac
    except Exception as e:
        logging.error(f"Failed to load MAC: {e}")
    return None

def update_overshoot(scale: AcaiaScale, mgr: ControlManager):
    if mgr.shot_time_elapsed() < MIN_GOOD_SHOT_DURATION:
        logging.info("Declining to consider short shot as a good shot. Not updating overshoot value or saving image")
        return
    time.sleep(3)
    logging.debug("over scale weight is %.2f, target was %.2f" % (scale.weight, mgr.current_memory().target))
    mgr.current_memory().update_overshoot(scale.weight)
    mgr.image_needs_save = True
    logging.info("new overshoot on memory %s is %.2f" %(mgr.current_memory().name, mgr.current_memory().overshoot))


def check_target_disable_relay(scale: AcaiaScale, mgr: ControlManager):
    if mgr.shot_time_elapsed() < 1.5:
        return

    if mgr.relay_on() and scale.weight > mgr.current_memory().target_minus_overshoot():
        mgr.disable_relay()
        overshoot_update_executor.submit(update_overshoot, scale, mgr)
        logging.debug("Scheduling overshoot check and update")


def main():
    web_server = WebServer(WEB_DIR, WEB_PORT)
    web_server.start()
    logging.info("Started web server")

    display_data_queue: Queue[DisplayData] = Queue()
    display = Display(display_data_queue, display_size=DisplaySize.SIZE_2_0, image_save_dir=WEB_DIR)
    display.start()

    mgr = ControlManager(max_flow_points=round(60 / refreshRate))
    scale = AcaiaScale(mac='')

    last_mac = load_last_mac()
    if last_mac:
        mgr.discovered_mac = last_mac

    mgr.add_tare_handler(lambda: scale.tare())

    last_sample_time: Optional[float] = None
    last_weight: Optional[float] = None
    
    last_relay_state = False
    shot_started_with_scale = False

    while not stop:
        # --- REMOVED: Old Sleep/Wake Logic ---
        # The system now scans continuously (with slow intervals).
        # We rely on ControlManager's built-in "Suicide" protection for D-Bus leaks.
        
        is_connected = control.try_connect_scale(scale, mgr)
        
        if is_connected and scale.mac and scale.mac != last_mac:
            save_mac_address(scale.mac)
            last_mac = scale.mac

        relay_is_on = mgr.relay_on()

        if relay_is_on and not last_relay_state:
            shot_started_with_scale = is_connected
            if shot_started_with_scale:
                logging.info("Shot Started in GRAVIMETRIC Mode")
            else:
                logging.info("Shot Started in MANUAL Mode (Scale not ready)")

        if is_connected:
            check_target_disable_relay(scale, mgr)
        
        elif relay_is_on:
            if shot_started_with_scale:
                logging.warning("LOST SCALE CONNECTION DURING SHOT - EMERGENCY STOP")
                mgr.disable_relay()
            else:
                pass 
        
        last_relay_state = relay_is_on

        if scale is not None and scale.connected:
            (last_sample_time, last_weight) = update_display(scale, mgr, display, last_sample_time, last_weight)
        else:
            display.display_off()
        time.sleep(refreshRate)
        
    if scale.connected:
        try:
            scale.disconnect()
        except Exception as ex:
            logging.error("Error during shutdown: %s" % str(ex))
    if display is not None:
        display.stop()
    logging.info("Exiting on stop")


def update_display(scale: AcaiaScale, mgr: ControlManager, display: Display, last_time: float, last_weight: float) -> (float, float):
    now = timer()
    weight = scale.weight
    sample_rate = 0.0
    if last_time is not None and last_weight is not None:
        sample_rate = now - last_time
        changed = weight - last_weight
        g_per_s = round(1 / sample_rate * changed, 1)
        mgr.add_flow_rate_data(g_per_s)
    data = DisplayData(weight, sample_rate, mgr.current_memory(), mgr.flow_rate_data,
                       scale.battery, mgr.relay_on(), mgr.shot_time_elapsed(),
                       mgr.image_needs_save, smoothing)
    display.display_on()
    display.put_data(data)
    mgr.image_needs_save = False
    return now, weight


def shutdown(sig, frame):
    global stop
    stop = True


if __name__ == '__main__':
    signal.signal(signal.SIGINT, shutdown)
    main()