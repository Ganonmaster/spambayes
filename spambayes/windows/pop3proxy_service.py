# Run the pop3proxy as a WinNT service.  Should work on Windows 2000
# and Windows XP.
#
# * Install as a service using "pop3proxy_service.py install"
# * Start the service (Use Control Panel etc, or
#   "pop3proxy_service.py start".  Check the event
#   log should anything go wrong.
# * To debug the service: "pop3proxy_service.py debug"
#   Service then runs in the command prompt, showing all
#   print statements.
# * To remove the service: "pop3proxy_service.py remove"

# This module is part of the spambayes project, which is Copyright 2002
# The Python Software Foundation and is covered by the Python Software
# Foundation license.

# Originally written by Mark Hammond.

import sys, os
# We are in the 'spambayes\win32' directory.  We
# need the parent on sys.path, so 'spambayes.spambayes' is a package,
# and 'pop3proxy' is a module
sb_dir = os.path.dirname(os.path.dirname(__file__))

sys.path.insert(0, sb_dir)
# and change directory here, so pop3proxy uses the default
# config file etc
os.chdir(sb_dir)

# Rest of the standard Python modules we use.
import traceback
import threading

# The spambayes imports we need.
import pop3proxy

# The win32 specific modules.
import win32serviceutil, win32service
import pywintypes, win32con, winerror

from ntsecuritycon import *

class Service(win32serviceutil.ServiceFramework):
    _svc_name_ = "pop3proxy"
    _svc_display_name_ = "SpamBayes pop3proxy Service"
    _svc_deps_ =  ['tcpip'] # We depend on the tcpip service.
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.event_stop = threading.Event()
        self.thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.event_stop.set()

    def SvcDoRun(self):
        # Setup our state etc
        pop3proxy.state.createWorkers()
        assert not pop3proxy.state.launchUI, "Service can't launch a UI"

        # Start the thread running the server.
        thread = threading.Thread(target=self.ServerThread)
        thread.start()

        # Write an event log record - in debug mode we will also 
        # see this message printed.
        import servicemanager
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
            )

        # Wait for the stop event.
        try:
            self.event_stop.wait()
        except KeyboardInterrupt:
            pass
        # How do we cleanly shutdown the server?
        
        # Write another event log record.
        s = pop3proxy.state
        status = " after %d sessions (%d ham, %d spam, %d unsure)" % \
                (s.totalSessions, s.numHams, s.numSpams, s.numUnsure)

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, status)
            )

    def ServerThread(self):
        state = pop3proxy.state
        pop3proxy.main(state.servers, state.proxyPorts, state.uiPort, state.launchUI)

if __name__=='__main__':
    win32serviceutil.HandleCommandLine(Service)