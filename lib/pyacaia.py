#!/usr/bin/env python
# -*- coding: utf-8 -*-

__version__ = "0.7.3-simplepyble-fixed"

import logging
import time
import threading
import struct
import gc
from typing import Optional, List, Tuple

try:
    import simplepyble
except ImportError:
    logging.fatal("SimplePyBLE not installed. Run: pip3 install simplepyble")
    raise

root = logging.getLogger()
root.setLevel(logging.INFO)

HEADER1 = 0xef
HEADER2 = 0xdd
# Acaia UUIDs
OLD_CHAR_UUID = "00002a80-0000-1000-8000-00805f9b34fb"
PYXIS_CMD_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

def normalize_uuid(uuid_str):
    return uuid_str.lower().replace('-', '')

# --- SCANNING FUNCTION ---
def find_acaia_devices(timeout=1) -> List[str]:
    """
    Scans for Acaia devices using SimplePyBLE.
    Blocking call for 'timeout' seconds.
    """
    found_devs = []
    target_names = ['ACAIA', 'PYXIS', 'UMBRA', 'LUNAR', 'PROCH']
    
    try:
        adapters = simplepyble.Adapter.get_adapters()
        if not adapters:
            logging.warning("No Bluetooth Adapters found")
            return []

        adapter = adapters[0]
        
        # SimplePyBLE scan is blocking
        adapter.scan_for(timeout * 1000) 
        
        peripherals = adapter.scan_get_results()
        
        for p in peripherals:
            try:
                name = p.identifier()
                addr = p.address()
                if name and any(name.upper().startswith(t) for t in target_names):
                    logging.info(f"Scan Found: {name} [{addr}]")
                    found_devs.append(addr)
            except Exception:
                continue
                
    except Exception as e:
        if "InProgress" not in str(e):
            logging.error(f"SimplePyBLE Scan Error: {e}")

    return found_devs


# --- PROTOCOL HELPERS (Unchanged) ---

class Message(object):
    def __init__(self, msgType, payload):
        self.msgType = msgType
        self.payload = payload
        self.value = None
        self.button = None
        self.time = None
        if self.msgType == 5:
            self.value = self._decode_weight(payload)
        elif self.msgType == 11:
            if payload[2] == 5:
                self.value = self._decode_weight(payload[3:])
            elif payload[2] == 7:
                self.time = self._decode_time(payload[3:])
        elif self.msgType == 7:
            self.time = self._decode_time(payload)
        elif self.msgType == 8:
            if payload[0] == 0 and payload[1] == 5:
                self.button = 'tare'
                self.value = self._decode_weight(payload[2:])
            elif payload[0] == 8 and payload[1] == 5:
                self.button = 'start'
                self.value = self._decode_weight(payload[2:])
            elif payload[0] == 10 and payload[1] == 7:
                self.button = 'stop'
                self.time = self._decode_time(payload[2:])
                self.value = self._decode_weight(payload[6:])
            elif payload[0] == 9 and payload[1] == 7:
                self.button = 'reset'
                self.time = self._decode_time(payload[2:])
                self.value = self._decode_weight(payload[6:])
            else:
                self.button = 'unknownbutton'

    def _decode_weight(self, payload: bytes) -> float:
         if len(payload) < 6: return 0.0
         unit = payload[4] & 0xFF
         scales = {1:10.0, 2:100.0, 3:1000.0, 4:10000.0}
         divisor = scales.get(unit, 10.0)
         sign = -1 if (payload[5] & 0x02) else 1
         raw = struct.unpack('>I', payload[0:4])[0]
         w = sign * (raw / divisor)
         if 0 <= abs(w) <= 4000: return w
         raw = struct.unpack('<I', payload[0:4])[0]
         return sign * (raw / divisor)

    def _decode_time(self, time_payload):
        if len(time_payload) < 3: return 0.0
        value = (time_payload[0] & 0xff) * 60
        value = value + (time_payload[1])
        value = value + (time_payload[2] / 10.0)
        return value

class Settings(object):
    def __init__(self, payload):
        self.battery = payload[1] & 0x7F
        if payload[2] == 2: self.units = 'grams'
        elif payload[2] == 5: self.units = 'ounces'
        else: self.units = None
        self.auto_off = payload[4] * 5
        self.beep_on = payload[6] == 1

def encode(msgType, payload):
    bytes_arr = bytearray(5 + len(payload))
    bytes_arr[0] = HEADER1
    bytes_arr[1] = HEADER2
    bytes_arr[2] = msgType
    cksum1 = 0
    cksum2 = 0
    for i in range(len(payload)):
        val = payload[i] & 0xff
        bytes_arr[3 + i] = val
        if (i % 2 == 0): cksum1 += val
        else: cksum2 += val
    bytes_arr[len(payload) + 3] = (cksum1 & 0xFF)
    bytes_arr[len(payload) + 4] = (cksum2 & 0xFF)
    return bytes_arr

def decode(bytes_arr):
    messageStart = -1
    for i in range(len(bytes_arr) - 1):
        if bytes_arr[i] == HEADER1 and bytes_arr[i + 1] == HEADER2:
            messageStart = i
            break
    if messageStart < 0 or len(bytes_arr) - messageStart < 6:
        return (None, bytes_arr)
    try:
        payload_len = bytes_arr[messageStart + 3]
        messageEnd = messageStart + payload_len + 5
        if messageEnd > len(bytes_arr): return (None, bytes_arr)
        cmd = bytes_arr[messageStart + 2]
        if cmd == 12:
            msgType = bytes_arr[messageStart + 4]
            payloadIn = bytes_arr[messageStart + 5:messageEnd]
            return (Message(msgType, payloadIn), bytes_arr[messageEnd:])
        if cmd == 8:
            return (Settings(bytes_arr[messageStart + 3:]), bytes_arr[messageEnd:])
        return (None, bytes_arr[messageEnd:])
    except Exception:
        return (None, bytes_arr)

def encodeEventData(payload):
    bytes_arr = bytearray(len(payload) + 1)
    bytes_arr[0] = len(payload) + 1
    for i in range(len(payload)): bytes_arr[i + 1] = payload[i] & 0xff
    return encode(12, bytes_arr)

def encodeNotificationRequest(): return encodeEventData([0, 1, 1, 2, 2, 5, 3, 4])
def encodeId(isPyxisStyle=False):
    if isPyxisStyle: payload = bytearray([0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x30, 0x31, 0x32, 0x33, 0x34])
    else: payload = bytearray([0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d])
    return encode(11, payload)
def encodeHeartbeat(): return encode(0, [2, 0])
def encodeTare(): return encode(4, [0])

# --- ACAIA SCALE CLASS (SimplePyBLE Port) ---

class AcaiaScale(object):
    def __init__(self, mac=None):
        self.mac = mac
        self.connected = False
        self.weight = 0.0
        self.battery = 0 
        self.units = 'grams'
        
        self.adapter = None 
        self._peripheral = None
        self._service_uuid = None
        self._char_uuid = None
        self.isPyxisStyle = False
        
        # Threading for non-blocking operations
        self._connect_thread = None
        self._heartbeat_thread = None
        self._stop_event = threading.Event()
        
        self.packet = bytearray()

    def connect(self):
        """
        Starts the connection process in a background thread.
        Returns immediately (non-blocking).
        """
        if self.connected: return
        
        # Ensure only one connection thread runs
        if self._connect_thread and self._connect_thread.is_alive():
            return

        self._stop_event.clear()
        self._connect_thread = threading.Thread(target=self._connect_sync, daemon=True)
        self._connect_thread.start()
        logging.info("Starting Connection Thread (SimplePyBLE)...")

    def _connect_sync(self):
        """
        Synchronous connection logic (running in background thread).
        Includes retry logic for busy BlueZ adapters.
        """
        try:
            time.sleep(0.5)
            adapters = simplepyble.Adapter.get_adapters()
            if not adapters:
                logging.error("No Bluetooth adapters found")
                return
            
            self.adapter = adapters[0] 
            
            # Retry Loop for Scan
            target = None
            for attempt in range(3):
                try:
                    logging.info(f"Scanning to acquire peripheral {self.mac} (Attempt {attempt+1})...")
                    self.adapter.scan_for(2000) 
                    peripherals = self.adapter.scan_get_results()
                    
                    for p in peripherals:
                        if p.address() == self.mac:
                            target = p
                            break
                    
                    if target: break 
                        
                except Exception as e:
                    if "InProgress" in str(e):
                        logging.warning("BlueZ busy, waiting...")
                        time.sleep(1.0)
                    else:
                        logging.error(f"Scan Error: {e}")
                        break
            
            if not target:
                logging.warning(f"Device {self.mac} not found in scan.")
                return

            self._peripheral = target
            logging.info(f"Connecting to {self.mac}...")
            self._peripheral.connect()
            
            if self._peripheral.is_connected():
                logging.info(f"Connected to {self.mac}")
                self.connected = True
                
                # Small pause after connect to let MTU/Services settle
                time.sleep(1.0)
                
                # Setup Services (Find correct UUIDs)
                if self._setup_services():
                    # Subscribe
                    try:
                        self._peripheral.notify(self._service_uuid, self._char_uuid, self._notification_handler)
                        logging.info("Subscribed to notifications")
                    except Exception as e:
                        logging.error(f"Notify failed: {e}")

                    # Handshake
                    self._perform_handshake()
                    
                    # Start Heartbeat Loop
                    self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                    self._heartbeat_thread.start()
                else:
                    logging.error("Failed to find Acaia Service/Char UUIDs")
                    self.disconnect()
            else:
                logging.error("Failed to connect.")

        except Exception as e:
            logging.error(f"Connection Error: {e}")
            self.connected = False
            self._peripheral = None

    def _setup_services(self) -> bool:
        try:
            services = self._peripheral.services()
            for service in services:
                for char in service.characteristics():
                    u_norm = normalize_uuid(char.uuid())
                    if u_norm == normalize_uuid(PYXIS_CMD_UUID):
                        self._service_uuid = service.uuid()
                        self._char_uuid = char.uuid()
                        self.isPyxisStyle = True
                        logging.info("Detected Pyxis/Lunar 2021 Style")
                        return True
                    elif u_norm == normalize_uuid(OLD_CHAR_UUID):
                        self._service_uuid = service.uuid()
                        self._char_uuid = char.uuid()
                        self.isPyxisStyle = False
                        logging.info("Detected Old Style")
                        return True
        except Exception as e:
            logging.error(f"Service Discovery Error: {e}")
        return False

    def _perform_handshake(self):
        logging.info("Performing Handshake...")
        # Acaia handshake sequence
        self._write_sync(encodeId(self.isPyxisStyle))
        time.sleep(0.5)
        self._write_sync(encodeNotificationRequest())
        time.sleep(0.5)
        self._write_sync(encodeNotificationRequest())
        time.sleep(0.5)
        self._write_sync(encodeHeartbeat())
        logging.info("Handshake Sent.")

    def _heartbeat_loop(self):
        count = 0
        
        # Send one immediate heartbeat
        self._write_sync(encodeHeartbeat())
        
        while self.connected and not self._stop_event.is_set():
            try:
                time.sleep(2.0)
                
                if not self.connected: break
                
                if self._peripheral and not self._peripheral.is_connected():
                    logging.warning("SimplePyBLE reports disconnected during heartbeat.")
                    self.disconnect()
                    break

                self._write_sync(encodeHeartbeat())
                count += 1
                if count >= 10: 
                    self._write_sync(encodeId(self.isPyxisStyle))
                    self._write_sync(encodeNotificationRequest())
                    count = 0
            except Exception as e:
                logging.error(f"Heartbeat Error: {e}")
                self.disconnect()
                break

    def _notification_handler(self, payload):
        self.packet += payload
        while True:
            (msg, self.packet) = decode(self.packet)
            if not msg: break
            if isinstance(msg, Settings):
                self.battery = msg.battery
                self.units = msg.units
                self.auto_off = msg.auto_off
                self.beep_on = msg.beep_on
            elif isinstance(msg, Message):
                if msg.msgType == 5: self.weight = msg.value

    def _write_sync(self, data):
        if self.connected and self._peripheral:
            try:
                # --- FIX: Convert bytearray to bytes ---
                self._peripheral.write_command(self._service_uuid, self._char_uuid, bytes(data))
            except Exception as e:
                logging.error(f"Write CMD failed: {e}")

    def disconnect(self):
        logging.info("Disconnecting...")
        self.connected = False
        self._stop_event.set()
        
        if self._peripheral:
            try:
                self._peripheral.disconnect()
            except: pass
            self._peripheral = None

    def tare(self):
        self._write_sync(encodeTare())
        return True