#!/usr/bin/env python
import piksi_tools.serial_link as sl
from sbp.client.handler import Handler
from sbp.logging import SBP_MSG_PRINT
from sbp.tracking import MsgTrackingState
from sbp.piksi import MsgMaskSatellite, SBP_MSG_RESET
from sbp.system import SBP_MSG_HEARTBEAT
from sbp.table import dispatch

import time
import sys
import random
import threading

DEFAULT_POLL_INTERVAL = 60 # Seconds
DEFAULT_MIN_SATS = 5 # min satellites to try and retain


class LoopTimer(object):
  """ Interval timer (emulated from from stack overflow)
  This should get re-used on other actions during the log
  http://stackoverflow.com/questions/12435211/python-threading-timer-repeat-function-every-n-seconds
  """
  def __init__(self, interval, hfunction):
    self.interval = interval
    self.hfunction = hfunction
    self.thread = threading.Timer(self.interval, self.handle_function)

  def handle_function(self):
    self.hfunction()
    self.thread = threading.Timer(self.interval, self.handle_function)
    self.thread.start()

  def start(self):
    self.thread.start()

  def cancel(self):
    self.thread.cancel()



class TestState(object):
  """Super class for representing state-based actions during logging
  Parameters
  ----------
  handler: sbp.client.handler.Handler
      handler for SBP transfer to/from Piksi.
  filename : string
    File to log to.
  """
  def __init__(self, handler):
    self.init_time = time.time()
    self.handler = handler
  def process_message(self, msg):
    """Method to process messages from device
    """
    raise NotImplementedError("process_message not implemented!")
  def action(self):
    """Stub for communicating with device
    """
    pass


class DropSatsState(TestState):
  """
  Instance of testState that periodically drops a random number of satellite
  above some minimum value
  Parameters
  ----------
  handler: sbp.client.handler.Handler
      handler for SBP transfer to/from Piksi.
  interval : int
    number of seconds between sending mask tracking message
  min sats : int
    number of satellites to never go below
  debug : bool
    Print out extra info?
  """
  def __init__(self, handler, interval, min_sats, debug=False):
    super(DropSatsState, self).__init__(handler)
    self.min_sats = min_sats
    self.debug = debug

    # state encoding
    self.num_tracked_sats = 0
    self.prn_status_dict = {}
    self.channel_status_dict = {}

    # timer stuff
    self.timer = LoopTimer(interval, self.action)

  def __enter__(self):
    self.timer.start()
    return self

  def __exit__(self, *args):
    self.timer.cancel()

  def process_message(self, msg):
    """
    process an SBP message into State
    Parameters
    ----------
    msg: sbp object
      not yet dispatchedm message received by device
    """
    msg = dispatch(msg)
    if isinstance(msg, MsgTrackingState):
      if self.debug:
        print "currently tracking {0} sats".format(self.num_tracked_sats)
      self.num_tracked_sats = 0
      for channel, track_state in enumerate(msg.states):
        prn = track_state.prn + 1
        if track_state.state == 1:
          self.num_tracked_sats += 1
          self.prn_status_dict[prn] = channel
          self.channel_status_dict[channel] = prn
        else:
          if self.prn_status_dict.get(prn):
            del self.prn_status_dict[prn]
          if self.channel_status_dict.get(channel):
            del self.channel_status_dict[channel]

  def drop_prns(self, prns):
    """ drop prn array via sbp MsgMaskSatellite
    Parameters
    ----------
    prns : int[]
      list of prns to drop
    """
    if self.debug:
      print "Dropping the following prns {0}".format(prns)
    for prn in prns:
      msg = MsgMaskSatellite(mask=2, prn=int(prn)-1)
      self.handler.send_msg(msg)

  def get_num_sats_to_drop(self):
    """ return number of satellites to drop.
    Should drop a random number of satellites above self.min_sats
    If we haven't achieved min sats, it drops zero
    """
    max_to_drop = max(0, self.num_tracked_sats-self.min_sats)
    # end points included
    return random.randint(0, max_to_drop)

  def drop_random_number_of_sats(self):
    """ perform drop of satellites
    """
    num_drop = self.get_num_sats_to_drop()
    if num_drop > 0:
      prns_to_drop = random.sample(self.channel_status_dict.values(), num_drop)
      if self.debug:
        print ("satellite drop triggered: "
                "will drop {0} out of {1} sats").format(num_drop,
                                                          self.num_tracked_sats)
      self.drop_prns(prns_to_drop)

  def action(self):
    """ overload of
    """
    self.drop_random_number_of_sats()

def get_args():
  """
  Get and parse arguments.
  """
  import argparse
  parser = sl.base_options()
  parser.add_argument("-i", "--interval",
                      default=[DEFAULT_POLL_INTERVAL], nargs=1,
                      help="Number of seconds between satellite drop events.")
  parser.add_argument("-m", "--minsats",
                      default=[DEFAULT_MIN_SATS], nargs=1,
                      help="Minimum number of satellites to retain during drop events.")
  return parser.parse_args()

def main():
  """
  Get configuration, get driver, get logger, and build handler and start it.
  """
  args = get_args()
  port = args.port[0]
  baud = args.baud[0]
  timeout = args.timeout[0]
  log_filename = args.log_filename[0]
  append_log_filename = args.append_log_filename[0]
  watchdog = args.watchdog[0]
  tags = args.tags[0]
  interval = int(args.interval[0])
  minsats = int(args.minsats[0])

  #initialize state machines:

  # Driver with context
  with sl.get_driver(args.ftdi, port, baud) as driver:
    # Handler with context
    with Handler(driver.read, driver.write, args.verbose) as link:
      # Logger with context
      with sl.get_logger(args.log, log_filename) as logger:
        # Append logger iwth context
        with sl.get_append_logger(append_log_filename, tags) as append_logger:
          # print out SBP_MSG_PRINT messags
          link.add_callback(sl.printer, SBP_MSG_PRINT)
          # add logger callback
          link.add_callback(logger)
          # ad append logger callback
          link.add_callback(append_logger)
          # Reset device
          if args.reset:
            link.send(SBP_MSG_RESET, "")
          # Setup watchdog
          if watchdog:
            link.add_callback(sl.Watchdog(float(watchdog), sl.watchdog_alarm),
                                SBP_MSG_HEARTBEAT)
          # add list of states and test callbacks callbacks
          with DropSatsState(link, interval, minsats, debug=args.verbose) as drop:
            link.add_callback(drop.process_message)

            try:
              if timeout is not None:
                expire = time.time() + float(args.timeout[0])

              while True:
                if timeout is None or time.time() < expire:
                # Wait forever until the user presses Ctrl-C
                  time.sleep(1)
                else:
                  print "Timer expired!"
                  break
                if not link.is_alive():
                  sys.stderr.write("ERROR: Thread died!")
                  sys.exit(1)
            except KeyboardInterrupt:
              # Callbacks, such as the watchdog timer on SBP_HEARTBEAT call
              # thread.interrupt_main(), which throw a KeyboardInterrupt
              # exception. To get the proper error condition, return exit code
              # of 1. Note that the finally block does get caught since exit
              # itself throws a SystemExit exception.
              sys.exit(1)

if __name__ == "__main__":
  main()
