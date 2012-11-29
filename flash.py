#import wx
import struct
import time
from intelhex import IntelHex
from threading import Thread, Event
from math import ceil

PAGESIZE = 256
SECTORSIZE = 64*1024
FLASHSIZE = 1024*1024

#Maximum number of addresses returned by STM in a single read callback
#Defined in m25_flash.c in the read callback
ADDRS_PER_READ_CALLBACK = 16

#Maximum number of addresses written in a single write callback to STM
ADDRS_PER_WRITE_CALLBACK = 128

#Maximum number of flash callbacks to have pending in STM
#in order to (1) keep commands queued up for speed while not
#overflowing STM's UART RX buffer, and (2) to keep track of
#where we are on the PC side
#  erase_sector : each sector erase is one callback
#  write : each write of 128 bytes or less is one callback
#  read : each read of 16 bytes or less is one callback
#         (can read arbitrary length of flash with just
#          a single read callback to STM, so only (2)
#          applies to read)
PENDING_COMMANDS_LIMIT = 20

def roundup_multiple(x, multiple):
  return x if x % multiple == 0 else x + multiple - x % multiple

def rounddown_multiple(x, multiple):
  return x if x % multiple == 0 else x - x % multiple

class Flash(Thread):
#  _flash_ready = Event()
  _command_queue = []
  _wants_to_exit = False
  _total_callbacks_expected = 0
  _done_callbacks_received = 0
  _read_callbacks_received = 0
  rd_cb_addrs = []
  rd_cb_lens = []
  rd_cb_data = []

  def __init__(self, link):
    super(Flash, self).__init__()
#    self._flash_ready.set()
    self.link = link
    self.link.add_callback(0xF0, self._flash_done_callback)
    self.link.add_callback(0xF1, self._flash_read_callback)

  def flash_callbacks_left(self):
    return len(self._command_queue) + self.flash_operations_pending()

  def flash_operations_pending(self):
    return self._total_callbacks_expected - self._done_callbacks_received - self._read_callbacks_received

  #Called by STM after a flash erase callback or a flash write callback
  def _flash_done_callback(self, data):
    self._done_callbacks_received += 1
#    if self.flash_operations_pending() < PENDING_COMMANDS_LIMIT:
#      self._flash_ready.set()

  #Called by STM after a flash write read
  #data = 3 bytes addr, 1 byte length, length bytes data
  def _flash_read_callback(self, data):
    #Append \x00 to the left side of the address as it is a 3-byte big endian
    #unsigned int and we unpack it as a 4-byte big endian unsigned int
    self.rd_cb_addrs.append(struct.unpack('>I','\x00' + data[0:3])[0])
    self.rd_cb_lens.append(struct.unpack('B',data[3])[0])
    self.rd_cb_data += list(struct.unpack(str(self.rd_cb_lens[-1]) + 'B',data[4:]))
    self._read_callbacks_received += 1
#    if self.flash_operations_pending() < PENDING_COMMANDS_LIMIT:
#      self._flash_ready.set()

  #Check that we received continuous addresses from the 
  #beginning of the flash read to the end, and that this
  #matches the length of the received data from those addrs
  def read_cb_sanity_check(self):
    expected_addrs = [self.rd_cb_addrs[0]]
    for length in self.rd_cb_lens[0:-1]:
      expected_addrs.append(expected_addrs[-1] + length)
    if self.rd_cb_addrs != expected_addrs:
      raise Exception('Addresses returned in read callback appear discontinuous')
    if sum(self.rd_cb_lens) != len(self.rd_cb_data):
      raise Exception('Length of read data does not match read callback lengths')

  def _schedule_command(self, cmd, addr):
    self._command_queue.append((cmd, addr))

  def stop(self):
    self._wants_to_exit = True

  def run(self):
    while not self._wants_to_exit:
      if len(self._command_queue)!=0 and (self.flash_operations_pending() < PENDING_COMMANDS_LIMIT):
        cmd, args = self._command_queue[0]
        self._command_queue = self._command_queue[1:]
        cmd_func = getattr(self, cmd)
        if cmd_func:
          cmd_func(*args)
      else:
        time.sleep(0.001)

  def _add_to_expected_callbacks(self, num):
    self._total_callbacks_expected += num
#    if self._total_callbacks_expected > PENDING_COMMANDS_LIMIT:
#      self._flash_ready.clear()

  def read(self, addr, length):
    self._schedule_command('_read', (addr, length))
  def _read(self, addr, length):
#    self._flash_ready.wait()
    self.rd_cb_addrs = []
    self.rd_cb_lens = []
    self.rd_cb_data = []
    msg_buf = struct.pack("<II", addr, length)
    self._add_to_expected_callbacks(int(ceil(float(length)/ADDRS_PER_READ_CALLBACK)))
    self.link.send_message(0xF1, msg_buf)

  def erase_sector(self, length):
    self._schedule_command('_erase_sector', (length,))
  def _erase_sector(self, addr):
#    self._flash_ready.wait()
    msg_buf = struct.pack("<I", addr)
    self._add_to_expected_callbacks(1)
    self.link.send_message(0xF2, msg_buf)

  def write(self, addr, data):
    self._schedule_command('_write', (addr,data))
  def _write(self, addr, data):
    while len(data) > ADDRS_PER_WRITE_CALLBACK:
#      self._flash_ready.wait()
      data_to_send = data[:ADDRS_PER_WRITE_CALLBACK]
      msg_header = struct.pack("<IB", addr, len(data_to_send))
      self._add_to_expected_callbacks(1)
      self.link.send_message(0xF0, msg_header+data_to_send)
      addr += ADDRS_PER_WRITE_CALLBACK
      data = data[ADDRS_PER_WRITE_CALLBACK:]
#    self._flash_ready.wait()
    msg_header = struct.pack("<IB", addr, len(data))
    self._add_to_expected_callbacks(1)
    self.link.send_message(0xF0, msg_header+data)

  def write_ihx(self, filename):
    ihx = IntelHex(filename)
    min_sector = rounddown_multiple(ihx.minaddr(), SECTORSIZE)
    max_sector = roundup_multiple(ihx.maxaddr(), SECTORSIZE)
    for addr in range(min_sector, max_sector, SECTORSIZE):
      self.erase_sector(addr)
    min_page = rounddown_multiple(ihx.minaddr(), ADDRS_PER_WRITE_CALLBACK)
    max_page = roundup_multiple(ihx.maxaddr(), ADDRS_PER_WRITE_CALLBACK)
    for addr in range(min_page, max_page, ADDRS_PER_WRITE_CALLBACK):
      self.write(addr, ihx.tobinstr(start=addr, size=ADDRS_PER_WRITE_CALLBACK))
#    min_page = rounddown_multiple(ihx.minaddr(), 128)
#    max_page = roundup_multiple(ihx.maxaddr(), 128)
#    for addr in range(min_page, max_page, 128):
#      self.write(addr, ihx.tobinstr(start=addr, size=128))
