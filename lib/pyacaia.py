#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 
# Released under GPLv3
#
# Copyright (c) 2019 Luca Pinello - original ideas based on Bluepy
# (https://github.com/lucapinello/pyacaia/blob/master/pyacaia/__init__.py)
#
# Copyright (c) 2025 George Agasandian - Updated for Bleak (for Debian 12/13 Support) 
#   Fully ported file to use Bleak backend for Bluetooth Low Energy communication. 
#   Includes "Turbo Scan" logic for instant detection.
#   Includes Heartbeat Loop to prevent disconnects.
#   Includes Battery initialization to prevent blank screen.
#   Includes "Double-Tap" Handshake.
#   Includes "Connection Verification" (Auto-Restart if Battery=0).

__version__ = "0.6.0"

import logging
import time
import threading
import asyncio
from typing import Optional, List

# Bleak Imports
from bleak import BleakScanner, BleakClient

root = logging.getLogger()
root.setLevel(logging.INFO)

HEADER1 = 0xef
HEADER2 = 0xdd

# Standard Acaia UUIDs
OLD_CHAR_UUID = "00002a80-0000-1000-8000-00805f9b34fb"
PYXIS_CMD_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"
PYXIS_WEIGHT_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"

# Helper for UUID checking
def normalize_uuid(uuid_str):
    return uuid_str.lower().replace('-', '')

def find_acaia_devices(timeout=5) -> List[str]:
    """
    Synchronous wrapper for Bleak discovery with "Return Early" logic.
    Returns immediately when an ACAIA device is found, or waits for timeout.
    """
    logging.debug('Looking for ACAIA devices (Turbo Scan)...')
    
    devices_start_names = ['ACAIA', 'PYXIS', 'UMBRA', 'LUNAR', 'PROCH']
    
    async def scan():
        found_devs = []
        stop_event = asyncio.Event()

        def detection_callback(device, advertisement_data):
            # Check if this is an Acaia device
            if device.name and any(device.name.upper().startswith(name) for name in devices_start_names):
                logging.info(f"Fast Scan Found: {device.name} [{device.address}]")
                found_devs.append(device.address)
                # Signal to stop scanning immediately
                stop_event.set()

        try:
            # Start the scanner with the callback
            async with BleakScanner(detection_callback=detection_callback):
                try:
                    # Wait for either the 'Found' event OR the timeout
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass # Timeout reached, no device found yet
        except Exception as e:
            logging.error(f"Bleak Scan Error: {e}")
        
        return found_devs

    try:
        return asyncio.run(scan())
    except Exception as e:
        logging.error(f"Scan runner failed: {e}")
        return []


# --- KEEPING EXISTING DECODING LOGIC (PURE PYTHON) ---

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
            logging.debug('heartbeat response (weight: ' + str(self.value) + ' time: ' + str(self.time))

        elif self.msgType == 7:
            self.time = self._decode_time(payload)
            logging.debug('timer: ' + str(self.time))

        elif self.msgType == 8:
            if payload[0] == 0 and payload[1] == 5:
                self.button = 'tare'
                self.value = self._decode_weight(payload[2:])
                logging.debug('tare (weight: ' + str(self.value) + ')')
            elif payload[0] == 8 and payload[1] == 5:
                self.button = 'start'
                self.value = self._decode_weight(payload[2:])
                logging.debug('start (weight: ' + str(self.value) + ')')
            elif payload[0] == 10 and payload[1] == 7:
                self.button = 'stop'
                self.time = self._decode_time(payload[2:])
                self.value = self._decode_weight(payload[6:])
                logging.debug('stop time: ' + str(self.time) + ' weight: ' + str(self.value))
            elif payload[0] == 9 and payload[1] == 7:
                self.button = 'reset'
                self.time = self._decode_time(payload[2:])
                self.value = self._decode_weight(payload[6:])
                logging.debug('reset time: ' + str(self.time) + ' weight: ' + str(self.value))
            else:
                self.button = 'unknownbutton'
                logging.debug('unknownbutton ' + str(payload))
        else:
            logging.debug('message ' + str(msgType) + ': %s' % payload)

    def _decode_weight(self, payload: bytes) -> float:
         import struct
         if len(payload) < 6: return 0.0
         unit = payload[4] & 0xFF
         scales = {1:10.0, 2:100.0, 3:1000.0, 4:10000.0}
         divisor = scales.get(unit, 10.0)
         sign = -1 if (payload[5] & 0x02) else 1
         
         # Try big-endian first
         raw = struct.unpack('>I', payload[0:4])[0]
         w = sign * (raw / divisor)
         if 0 <= abs(w) <= 4000:
             return w
         
         # Fallback to little-endian
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
        if payload[2] == 2:
            self.units = 'grams'
        elif payload[2] == 5:
            self.units = 'ounces'
        else:
            self.units = None
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
        if (i % 2 == 0):
            cksum1 += val
        else:
            cksum2 += val

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
        if messageEnd > len(bytes_arr):
            return (None, bytes_arr)

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

# Encoding Helpers
def encodeEventData(payload):
    bytes_arr = bytearray(len(payload) + 1)
    bytes_arr[0] = len(payload) + 1
    for i in range(len(payload)):
        bytes_arr[i + 1] = payload[i] & 0xff
    return encode(12, bytes_arr)

def encodeNotificationRequest():
    payload = [0, 1, 1, 2, 2, 5, 3, 4]
    return encodeEventData(payload)

def encodeId(isPyxisStyle=False):
    if isPyxisStyle:
        payload = bytearray([0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x30, 0x31, 0x32, 0x33, 0x34])
    else:
        payload = bytearray([0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d, 0x2d])
    return encode(11, payload)

def encodeHeartbeat():
    return encode(0, [2, 0])

def encodeTare():
    return encode(4, [0])

def encodeStartTimer():
    return encode(13, [0, 0])

def encodeStopTimer():
    return encode(13, [0, 2])

def encodeResetTimer():
    return encode(13, [0, 1])


# --- MAIN CLASS ADAPTED FOR BLEAK ---

class AcaiaScale(object):
    def __init__(self, mac=None):
        self.mac = mac
        self.connected = False
        self.weight = 0.0
        self.battery = 0 
        self.units = 'grams'
        self.auto_off = None
        self.beep_on = None
        self.timer_running = False
        self.timer_start_time = 0
        self.paused_time = 0
        self.transit_delay = 0.2
        
        self._loop = None
        self._thread = None
        self._client: Optional[BleakClient] = None
        self.packet = bytearray()
        self.char_uuid = None
        self.isPyxisStyle = False
        self._heartbeat_task = None 

    def start_async_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, args=(self._loop,), daemon=True)
            self._thread.start()
            logging.info("Bleak background thread started")

    def _run_loop(self, loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def connect(self):
        if self.connected:
            return

        self.start_async_loop()
        
        if not self.mac:
            logging.error("No MAC address provided for connection")
            return

        future = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
        try:
            # Increased timeout slightly to account for validation delay
            future.result(timeout=12) 
        except Exception as e:
            logging.error(f"Bleak Connect Timeout/Error: {e}")
            self.connected = False

    def _on_disconnect(self, client):
        logging.info("Bleak Client Disconnected (Callback)")
        self.connected = False
        self.timer_running = False
        self.packet = bytearray()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    async def _connect_async(self):
        logging.info(f"Connecting to {self.mac}...")
        
        self._client = BleakClient(self.mac, disconnected_callback=self._on_disconnect)
        
        try:
            await self._client.connect()
            if self._client.is_connected:
                logging.info(f"Connected to {self.mac}")
                self.connected = True
                
                await self._setup_services()
                await asyncio.sleep(1.0) 
                
                await self._send_handshake()
                
                self._heartbeat_task = self._loop.create_task(self._heartbeat_loop())
                
                # --- NEW: Connection Validation ---
                logging.info("Verifying Data Stream...")
                await asyncio.sleep(2.0) # Wait 2s for data to arrive
                
                if self.battery == 0:
                    logging.error("Handshake Failed (Battery is 0). Disconnecting to retry...")
                    # Force disconnect. This triggers _on_disconnect, setting connected=False
                    await self._client.disconnect() 
                    return
                
                logging.info(f"Connection Verified. Battery: {self.battery}%")
                # ----------------------------------
                
            else:
                logging.warning("Bleak client reports not connected")
                self.connected = False
        except Exception as e:
            logging.error(f"Connection Logic Error: {e}")
            self.connected = False
            if self._client:
                pass

    async def _heartbeat_loop(self):
        count = 0
        iterations = 20
        try:
            while self.connected:
                await asyncio.sleep(3)
                if self.connected:
                    await self._write_async(encodeHeartbeat())
                    
                    count += 1
                    if count >= iterations:
                        await self._write_async(encodeId(self.isPyxisStyle))
                        await self._write_async(encodeNotificationRequest())
                        count = 0
                        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Heartbeat Loop Error: {e}")

    async def _setup_services(self):
        self.char_uuid = OLD_CHAR_UUID 
        self.isPyxisStyle = False

        for service in self._client.services:
            for char in service.characteristics:
                uuid_norm = normalize_uuid(char.uuid)
                if uuid_norm == normalize_uuid(PYXIS_CMD_UUID):
                    self.char_uuid = char.uuid
                    self.isPyxisStyle = True
                    logging.info("Detected Pyxis/Lunar 2021 Style")
                elif uuid_norm == normalize_uuid(OLD_CHAR_UUID):
                    self.char_uuid = char.uuid
                    self.isPyxisStyle = False
                    logging.info("Detected Old Style")

        try:
            try:
                await self._client.stop_notify(self.char_uuid)
            except:
                pass 
                
            await self._client.start_notify(self.char_uuid, self._notification_handler)
            logging.info(f"Subscribed to {self.char_uuid}")
        except Exception as e:
            logging.error(f"Failed to subscribe to notifications: {e}")

    async def _send_handshake(self):
        logging.info("Performing Handshake (Double-Tap)...")
        
        # 1. Send ID (Identify)
        await self._write_async(encodeId(self.isPyxisStyle))
        await asyncio.sleep(0.4) 
        
        # 2. Notification Request (Double Tap)
        await self._write_async(encodeNotificationRequest())
        await asyncio.sleep(0.5) 
        await self._write_async(encodeNotificationRequest())
        await asyncio.sleep(0.3)
        
        # 3. Heartbeat
        await self._write_async(encodeHeartbeat())
        
        logging.info("Handshake Complete")

    def _notification_handler(self, sender, data):
        self.addBuffer(data)
        while True:
            (msg, self.packet) = decode(self.packet)
            if not msg:
                break
            
            if isinstance(msg, Settings):
                self.battery = msg.battery
                self.units = msg.units
                self.auto_off = msg.auto_off
                self.beep_on = msg.beep_on
            elif isinstance(msg, Message):
                if msg.msgType == 5:
                    self.weight = msg.value
                elif msg.msgType == 7:
                    self.timer_start_time = time.time() - msg.time
                    self.timer_running = True
                elif msg.msgType == 8 and msg.button == 'start':
                    self.timer_start_time = time.time() - self.paused_time + self.transit_delay
                    self.timer_running = True
                elif msg.msgType == 8 and msg.button == 'stop':
                    self.paused_time = msg.time
                    self.timer_running = False
                elif msg.msgType == 8 and msg.button == 'reset':
                    self.paused_time = 0
                    self.timer_running = False

    def addBuffer(self, buffer2):
        self.packet += buffer2

    # --- SYNCHRONOUS COMMANDS ---

    def disconnect(self):
        self.connected = False
        if self._loop and self._client:
            asyncio.run_coroutine_threadsafe(self._disconnect_async(), self._loop)

    async def _disconnect_async(self):
        if self._client:
            await self._client.disconnect()
            logging.info("Disconnected (User Request)")

    def _send_command(self, payload):
        if self.connected and self._loop:
            asyncio.run_coroutine_threadsafe(self._write_async(payload), self._loop)

    async def _write_async(self, data, with_response=False):
        if self._client and self._client.is_connected:
            try:
                # Always force response=False for Old Style scales
                await self._client.write_gatt_char(self.char_uuid, data, response=False)
            except Exception as e:
                logging.error(f"Write Failed: {e}")

    def tare(self):
        self._send_command(encodeTare())
        return True

    def startTimer(self):
        self._send_command(encodeStartTimer())
        self.timer_start_time = time.time()
        self.timer_running = True

    def stopTimer(self):
        self._send_command(encodeStopTimer())
        self.paused_time = time.time() - self.timer_start_time
        self.timer_running = False

    def resetTimer(self):
        self._send_command(encodeResetTimer())
        self.paused_time = 0
        self.timer_running = False

    def get_elapsed_time(self):
        if self.timer_running:
            return time.time() - self.timer_start_time + self.transit_delay
        else:
            return self.paused_time

    def auto_connect(self):
        if self.connected:
            return
        logging.info('Trying to find an ACAIA scale...')
        addresses = find_acaia_devices()

        if addresses:
            self.mac = addresses[0]
            logging.info('Connecting to:%s' % self.mac)
            self.connect()
        else:
            logging.info('No ACAIA scale found')

def main():
    logging.basicConfig(level=logging.INFO)
    addresses = find_acaia_devices()
    if addresses:
        scale = AcaiaScale(mac=addresses[0])
        scale.connect()
        print("Connected. Reading weight for 10 seconds...")
        for i in range(20):
            print(f"Weight: {scale.weight} g")
            time.sleep(0.5)
        scale.disconnect()
    else:
        print("No devices found.")

if __name__ == '__main__':
    main()