#!/usr/bin/env python

"""The primary server for SpamBayes.

Currently serves the web interface, and any configured POP3 and SMTP
proxies.

The POP3 proxy works with classifier.py, and adds a simple
X-Spambayes-Classification header (ham/spam/unsure) to each incoming
email.  You point the proxy at your POP3 server, and configure your
email client to collect mail from the proxy then filter on the added
header.  Usage:

    sb_server.py [options] [<server> [<server port>]]
        <server> is the name of your real POP3 server
        <port>   is the port number of your real POP3 server, which
                 defaults to 110.

        options:
            -h      : Displays this help message.
            -d FILE : use the named DBM database file
            -p FILE : the the named Pickle database file
            -l port : proxy listens on this port number (default 110)
            -u port : User interface listens on this port number
                      (default 8880; Browse http://localhost:8880/)
            -b      : Launch a web browser showing the user interface.

            -o section:option:value :
                      set [section, option] in the options database
                      to value

        All command line arguments and switches take their default
        values from the [pop3proxy] and [html_ui] sections of
        bayescustomize.ini.

For safety, and to help debugging, the whole POP3 conversation is
written out to _pop3proxy.log for each run, if
options["globals", "verbose"] is True.

To make rebuilding the database easier, uploaded messages are appended
to _pop3proxyham.mbox and _pop3proxyspam.mbox.
"""

# This module is part of the spambayes project, which is Copyright 2002
# The Python Software Foundation and is covered by the Python Software
# Foundation license.

__author__ = "Richie Hindle <richie@entrian.com>"
__credits__ = "Tim Peters, Neale Pickett, Tim Stone, all the Spambayes folk."

try:
    True, False
except NameError:
    # Maintain compatibility with Python 2.2
    True, False = 1, 0


todo = """

Web training interface:

User interface improvements:

 o Once the pieces are on separate pages, make the paste box bigger.
 o Deployment: Windows executable?  atlaxwin and ctypes?  Or just
   webbrowser?
 o Save the stats (num classified, etc.) between sessions.
 o "Reload database" button.


New features:

 o Online manual.
 o Links to project homepage, mailing list, etc.
 o List of words with stats (it would have to be paged!) a la SpamSieve.


Code quality:

 o Cope with the email client timing out and closing the connection.


Info:

 o Slightly-wordy index page; intro paragraph for each page.
 o In both stats and training results, report nham and nspam - warn if
   they're very different (for some value of 'very').
 o "Links" section (on homepage?) to project homepage, mailing list,
   etc.


Gimmicks:

 o Classify a web page given a URL.
 o Graphs.  Of something.  Who cares what?
 o NNTP proxy.
 o Zoe...!
"""

import os, sys, re, errno, getopt, time, traceback, socket, cStringIO
from thread import start_new_thread
from email.Header import Header

import spambayes.message
from spambayes import Dibbler
from spambayes import storage
from spambayes.FileCorpus import FileCorpus, ExpiryFileCorpus
from spambayes.FileCorpus import FileMessageFactory, GzipFileMessageFactory
from spambayes.Options import options, get_pathname_option
from spambayes.UserInterface import UserInterfaceServer
from spambayes.ProxyUI import ProxyUserInterface
from spambayes.Version import get_version_string

# Increase the stack size on MacOS X.  Stolen from Lib/test/regrtest.py
if sys.platform == 'darwin':
    try:
        import resource
    except ImportError:
        pass
    else:
        soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
        newsoft = min(hard, max(soft, 1024*2048))
        resource.setrlimit(resource.RLIMIT_STACK, (newsoft, hard))

# exception may be raised if we are already running and check such things.
class AlreadyRunningException(Exception):
    pass

# number to add to STAT length for each msg to fudge for spambayes headers
HEADER_SIZE_FUDGE_FACTOR = 512

class ServerLineReader(Dibbler.BrighterAsyncChat):
    """An async socket that reads lines from a remote server and
    simply calls a callback with the data.  The BayesProxy object
    can't connect to the real POP3 server and talk to it
    synchronously, because that would block the process."""

    lineCallback = None

    def __init__(self, serverName, serverPort, lineCallback):
        Dibbler.BrighterAsyncChat.__init__(self)
        self.lineCallback = lineCallback
        self.request = ''
        self.set_terminator('\r\n')
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.connect((serverName, serverPort))
        except socket.error, e:
            error = "Can't connect to %s:%d: %s" % (serverName, serverPort, e)
            print >>sys.stderr, error
            self.lineCallback('-ERR %s\r\n' % error)
            self.lineCallback('')   # "The socket's been closed."
            self.close()

    def collect_incoming_data(self, data):
        self.request = self.request + data

    def found_terminator(self):
        self.lineCallback(self.request + '\r\n')
        self.request = ''

    def handle_close(self):
        self.lineCallback('')
        self.close()


class POP3ProxyBase(Dibbler.BrighterAsyncChat):
    """An async dispatcher that understands POP3 and proxies to a POP3
    server, calling `self.onTransaction(request, response)` for each
    transaction. Responses are not un-byte-stuffed before reaching
    self.onTransaction() (they probably should be for a totally generic
    POP3ProxyBase class, but BayesProxy doesn't need it and it would
    mean re-stuffing them afterwards).  self.onTransaction() should
    return the response to pass back to the email client - the response
    can be the verbatim response or a processed version of it.  The
    special command 'KILL' kills it (passing a 'QUIT' command to the
    server).
    """

    def __init__(self, clientSocket, serverName, serverPort):
        Dibbler.BrighterAsyncChat.__init__(self, clientSocket)
        self.request = ''
        self.response = ''
        self.set_terminator('\r\n')
        self.command = ''           # The POP3 command being processed...
        self.args = []              # ...and its arguments
        self.isClosing = False      # Has the server closed the socket?
        self.seenAllHeaders = False # For the current RETR or TOP
        self.startTime = 0          # (ditto)

        if not self.onIncomingConnection(clientSocket):
            # We must refuse this connection, so pass an error back
            # to the mail client.
            self.push("-ERR Connection not allowed\r\n")
            self.close_when_done()
            return

        self.serverSocket = ServerLineReader(serverName, serverPort,
                                             self.onServerLine)

    def onIncomingConnection(self, clientSocket):
        """Checks the security settings."""
        # Stolen from UserInterface.py

        remoteIP = clientSocket.getpeername()[0]
        trustedIPs = options["pop3proxy", "allow_remote_connections"]

        if trustedIPs == "*" or remoteIP == clientSocket.getsockname()[0]:
            return True

        trustedIPs = trustedIPs.replace('.', '\.').replace('*', '([01]?\d\d?|2[04]\d|25[0-5])')
        for trusted in trustedIPs.split(','):
            if re.search("^" + trusted + "$", remoteIP):
                return True

        return False

    def onTransaction(self, command, args, response):
        """Overide this.  Takes the raw request and the response, and
        returns the (possibly processed) response to pass back to the
        email client.
        """
        raise NotImplementedError

    def onServerLine(self, line):
        """A line of response has been received from the POP3 server."""
        isFirstLine = not self.response
        self.response = self.response + line

        # Is this the line that terminates a set of headers?
        self.seenAllHeaders = self.seenAllHeaders or line in ['\r\n', '\n']

        # Has the server closed its end of the socket?
        if not line:
            self.isClosing = True

        # If we're not processing a command, just echo the response.
        if not self.command:
            self.push(self.response)
            self.response = ''

        # Time out after 30 seconds for message-retrieval commands if
        # all the headers are down.  The rest of the message will proxy
        # straight through.
        if self.command in ['TOP', 'RETR'] and \
           self.seenAllHeaders and time.time() > self.startTime + 30:
            self.onResponse()
            self.response = ''
        # If that's a complete response, handle it.
        elif not self.isMultiline() or line == '.\r\n' or \
           (isFirstLine and line.startswith('-ERR')):
            self.onResponse()
            self.response = ''

    def isMultiline(self):
        """Returns True if the request should get a multiline
        response (assuming the response is positive).
        """
        if self.command in ['USER', 'PASS', 'APOP', 'QUIT',
                            'STAT', 'DELE', 'NOOP', 'RSET', 'KILL']:
            return False
        elif self.command in ['RETR', 'TOP', 'CAPA']:
            return True
        elif self.command in ['LIST', 'UIDL']:
            return len(self.args) == 0
        else:
            # Assume that an unknown command will get a single-line
            # response.  This should work for errors and for POP-AUTH,
            # and is harmless even for multiline responses - the first
            # line will be passed to onTransaction and ignored, then the
            # rest will be proxied straight through.
            return False

    def collect_incoming_data(self, data):
        """Asynchat override."""
        self.request = self.request + data

    def found_terminator(self):
        """Asynchat override."""
        verb = self.request.strip().upper()
        if verb == 'KILL':
            self.socket.shutdown(2)
            self.close()
            raise SystemExit
        elif verb == 'CRASH':
            # For testing
            x = 0
            y = 1/x

        self.serverSocket.push(self.request + '\r\n')
        if self.request.strip() == '':
            # Someone just hit the Enter key.
            self.command = ''
            self.args = []
        else:
            # A proper command.
            splitCommand = self.request.strip().split()
            self.command = splitCommand[0].upper()
            self.args = splitCommand[1:]
            self.startTime = time.time()

        self.request = ''

    def onResponse(self):
        # There are some features, tested by clients using CAPA,
        # that we don't support.  We strip them from the CAPA
        # response here, so that the client won't use them.
        for unsupported in ['PIPELINING', 'STLS', ]:
            unsupportedLine = r'(?im)^%s[^\n]*\n' % (unsupported,)
            self.response = re.sub(unsupportedLine, '', self.response)

        # Pass the request and the raw response to the subclass and
        # send back the cooked response.
        if self.response:
            cooked = self.onTransaction(self.command, self.args, self.response)
            self.push(cooked)

        # If onServerLine() decided that the server has closed its
        # socket, close this one when the response has been sent.
        if self.isClosing:
            self.close_when_done()

        # Reset.
        self.command = ''
        self.args = []
        self.isClosing = False
        self.seenAllHeaders = False


class BayesProxyListener(Dibbler.Listener):
    """Listens for incoming email client connections and spins off
    BayesProxy objects to serve them.
    """

    def __init__(self, serverName, serverPort, proxyPort):
        proxyArgs = (serverName, serverPort)
        Dibbler.Listener.__init__(self, proxyPort, BayesProxy, proxyArgs)
        print 'Listener on port %s is proxying %s:%d' % \
               (_addressPortStr(proxyPort), serverName, serverPort)


class BayesProxy(POP3ProxyBase):
    """Proxies between an email client and a POP3 server, inserting
    judgement headers.  It acts on the following POP3 commands:

     o STAT:
        o Adds the size of all the judgement headers to the maildrop
          size.

     o LIST:
        o With no message number: adds the size of an judgement header
          to the message size for each message in the scan listing.
        o With a message number: adds the size of an judgement header
          to the message size.

     o RETR:
        o Adds the judgement header based on the raw headers and body
          of the message.

     o TOP:
        o Adds the judgement header based on the raw headers and as
          much of the body as the TOP command retrieves.  This can
          mean that the header might have a different value for
          different calls to TOP, or for calls to TOP vs. calls to
          RETR.  I'm assuming that the email client will either not
          make multiple calls, or will cope with the headers being
          different.

     o USER:
        o Does no processing based on the USER command itself, but
          expires any old messages in the three caches.
    """

    def __init__(self, clientSocket, serverName, serverPort):
        POP3ProxyBase.__init__(self, clientSocket, serverName, serverPort)
        self.handlers = {'STAT': self.onStat, 'LIST': self.onList,
                         'RETR': self.onRetr, 'TOP': self.onTop,
                         'USER': self.onUser}
        state.totalSessions += 1
        state.activeSessions += 1
        self.isClosed = False

    def send(self, data):
        """Logs the data to the log file."""
        if options["globals", "verbose"]:
            state.logFile.write(data)
            state.logFile.flush()
        try:
            return POP3ProxyBase.send(self, data)
        except socket.error:
            # The email client has closed the connection - 40tude Dialog
            # does this immediately after issuing a QUIT command,
            # without waiting for the response.
            self.close()

    def recv(self, size):
        """Logs the data to the log file."""
        data = POP3ProxyBase.recv(self, size)
        if options["globals", "verbose"]:
            state.logFile.write(data)
            state.logFile.flush()
        return data

    def close(self):
        # This can be called multiple times by async.
        if not self.isClosed:
            self.isClosed = True
            state.activeSessions -= 1
            POP3ProxyBase.close(self)

    def onTransaction(self, command, args, response):
        """Takes the raw request and response, and returns the
        (possibly processed) response to pass back to the email client.
        """
        handler = self.handlers.get(command, self.onUnknown)
        return handler(command, args, response)

    def onStat(self, command, args, response):
        """Adds the size of all the judgement headers to the maildrop
        size."""
        match = re.search(r'^\+OK\s+(\d+)\s+(\d+)(.*)\r\n', response)
        if match:
            count = int(match.group(1))
            size = int(match.group(2)) + HEADER_SIZE_FUDGE_FACTOR * count
            return '+OK %d %d%s\r\n' % (count, size, match.group(3))
        else:
            return response

    def onList(self, command, args, response):
        """Adds the size of an judgement header to the message
        size(s)."""
        if response.count('\r\n') > 1:
            # Multiline: all lines but the first contain a message size.
            lines = response.split('\r\n')
            outputLines = [lines[0]]
            for line in lines[1:]:
                match = re.search(r'^(\d+)\s+(\d+)', line)
                if match:
                    number = int(match.group(1))
                    size = int(match.group(2)) + HEADER_SIZE_FUDGE_FACTOR
                    line = "%d %d" % (number, size)
                outputLines.append(line)
            return '\r\n'.join(outputLines)
        else:
            # Single line.
            match = re.search(r'^\+OK\s+(\d+)\s+(\d+)(.*)\r\n', response)
            if match:
                messageNumber = match.group(1)
                size = int(match.group(2)) + HEADER_SIZE_FUDGE_FACTOR
                trailer = match.group(3)
                return "+OK %s %s%s\r\n" % (messageNumber, size, trailer)
            else:
                return response

    def onRetr(self, command, args, response):
        """Adds the judgement header based on the raw headers and body
        of the message."""
        # Use '\n\r?\n' to detect the end of the headers in case of
        # broken emails that don't use the proper line separators.
        if re.search(r'\n\r?\n', response):
            # Remove the trailing .\r\n before passing to the email parser.
            # Thanks to Scott Schlesier for this fix.
            terminatingDotPresent = (response[-4:] == '\n.\r\n')
            if terminatingDotPresent:
                response = response[:-3]

            # Break off the first line, which will be '+OK'.
            ok, messageText = response.split('\n', 1)

            try:
                msg = spambayes.message.sbheadermessage_from_string(messageText)
                msg.setId(state.getNewMessageName())
                # Now find the spam disposition and add the header.
                (prob, clues) = state.bayes.spamprob(msg.asTokens(),\
                                 evidence=True)

                msg.addSBHeaders(prob, clues)

                # Check for "RETR" or "TOP N 99999999" - fetchmail without
                # the 'fetchall' option uses the latter to retrieve messages.
                if (command == 'RETR' or
                    (command == 'TOP' and
                     len(args) == 2 and args[1] == '99999999')):
                    cls = msg.GetClassification()
                    if cls == options["Headers", "header_ham_string"]:
                        state.numHams += 1
                    elif cls == options["Headers", "header_spam_string"]:
                        state.numSpams += 1
                    else:
                        state.numUnsure += 1

                    # Suppress caching of "Precedence: bulk" or
                    # "Precedence: list" ham if the options say so.
                    isSuppressedBulkHam = \
                        (cls == options["Headers", "header_ham_string"] and
                         options["Storage", "no_cache_bulk_ham"] and
                         msg.get('precedence') in ['bulk', 'list'])

                    # Suppress large messages if the options say so.
                    size_limit = options["Storage",
                                         "no_cache_large_messages"]
                    isTooBig = size_limit > 0 and \
                               len(messageText) > size_limit

                    # Cache the message.  Don't pollute the cache with test
                    # messages or suppressed bulk ham.
                    if (not state.isTest and
                        options["Storage", "cache_messages"] and
                        not isSuppressedBulkHam and not isTooBig):
                        # Write the message into the Unknown cache.
                        makeMessage = state.unknownCorpus.makeMessage
                        message = makeMessage(msg.getId(), msg.as_string())
                        state.unknownCorpus.addMessage(message)

                # We'll return the message with the headers added.  We take
                # all the headers from the SBHeaderMessage, but take the body
                # directly from the POP3 conversation, because the
                # SBHeaderMessage might have "fixed" a partial message by
                # appending a closing boundary separator.  Remember we can
                # be dealing with partial message here because of the timeout
                # code in onServerLine.
                headers = []
                for name, value in msg.items():
                    header = "%s: %s" % (name, value)
                    headers.append(re.sub(r'\r?\n', '\r\n', header))
                body = re.split(r'\n\r?\n', messageText, 1)[1]
                messageText = "\r\n".join(headers) + "\r\n\r\n" + body
            except:
                # Something nasty happened while parsing or classifying -
                # report the exception in a hand-appended header and recover.
                # This is one case where an unqualified 'except' is OK, 'cos
                # anything's better than destroying people's email...
                stream = cStringIO.StringIO()
                traceback.print_exc(None, stream)
                details = stream.getvalue()

                # Build the header.  This will strip leading whitespace from
                # the lines, so we add a leading dot to maintain indentation.
                detailLines = details.strip().split('\n')
                dottedDetails = '\n.'.join(detailLines)
                headerName = 'X-Spambayes-Exception'
                header = Header(dottedDetails, header_name=headerName)

                # Insert the header, converting email.Header's '\n' line
                # breaks to POP3's '\r\n'.
                headers, body = re.split(r'\n\r?\n', messageText, 1)
                header = re.sub(r'\r?\n', '\r\n', str(header))
                headers += "\n%s: %s\r\n\r\n" % (headerName, header)
                messageText = headers + body

                # Print the exception and a traceback.
                print >>sys.stderr, details

            # Restore the +OK and the POP3 .\r\n terminator if there was one.
            retval = ok + "\n" + messageText
            if terminatingDotPresent:
                retval += '.\r\n'
            return retval

        else:
            # Must be an error response.
            return response

    def onTop(self, command, args, response):
        """Adds the judgement header based on the raw headers and as
        much of the body as the TOP command retrieves."""
        # Easy (but see the caveat in BayesProxy.__doc__).
        return self.onRetr(command, args, response)

    def onUser(self, command, args, response):
        """Spins off three separate threads that expires any old messages
        in the three caches, but does not do any processing of the USER
        command itself."""
        start_new_thread(state.spamCorpus.removeExpiredMessages, ())
        start_new_thread(state.hamCorpus.removeExpiredMessages, ())
        start_new_thread(state.unknownCorpus.removeExpiredMessages, ())
        return response

    def onUnknown(self, command, args, response):
        """Default handler; returns the server's response verbatim."""
        return response

# Implementations of a mutex or other resource which can prevent
# multiple servers starting at once.  Platform specific as no reasonable
# cross-platform solution exists (however, an old trick is to use a
# directory for a mutex, as a "create/test" atomic API generally exists.
# Will return a handle to be later closed, or may throw AlreadyRunningException
def open_platform_mutex():
    if sys.platform.startswith("win"):
        try:
            import win32event, win32api, winerror, win32con
            import pywintypes, ntsecuritycon
            # ideally, the mutex name could include either the username,
            # or the munged path to the INI file - this would mean we
            # would allow multiple starts so long as they weren't for
            # the same user.  However, as of now, the service version
            # is likely to start as a different user, so a single mutex
            # is best for now.
            # XXX - even if we do get clever with another mutex name, we
            # should consider still creating a non-exclusive
            # "SpamBayesServer" mutex, if for no better reason than so
            # an installer can check if we are running
            mutex_name = "SpamBayesServer"
            try:
                hmutex = win32event.CreateMutex(None, True, mutex_name)
            except win32event.error, details:
                # If another user has the mutex open, we get an "access denied"
                # error - this is still telling us what we need to know.
                if details[0] != winerror.ERROR_ACCESS_DENIED:
                    raise
                raise AlreadyRunningException
            # mutex opened - now check if we actually created it.
            if win32api.GetLastError()==winerror.ERROR_ALREADY_EXISTS:
                win32api.CloseHandle(hmutex)
                raise AlreadyRunningException
            return hmutex
        except ImportError:
            # no win32all - no worries, just start
            pass
    return None

def close_platform_mutex(mutex):
    if sys.platform.startswith("win"):
        if mutex is not None:
            mutex.Close()

# This keeps the global state of the module - the command-line options,
# statistics like how many mails have been classified, the handle of the
# log file, the Classifier and FileCorpus objects, and so on.
class State:
    def __init__(self):
        """Initialises the State object that holds the state of the app.
        The default settings are read from Options.py and bayescustomize.ini
        and are then overridden by the command-line processing code in the
        __main__ code below."""
        self.logFile = None
        self.bayes = None
        self.platform_mutex = None
        self.prepared = False
        self.can_stop = True
        self.init()

        # Load up the other settings from Option.py / bayescustomize.ini
        self.uiPort = options["html_ui", "port"]
        self.launchUI = options["html_ui", "launch_browser"]
        self.gzipCache = options["Storage", "cache_use_gzip"]
        self.cacheExpiryDays = options["Storage", "cache_expiry_days"]
        self.runTestServer = False
        self.isTest = False

    def init(self):
        assert not self.prepared, "init after prepare, but before close"
        # Open the log file.
        if options["globals", "verbose"]:
            self.logFile = open('_pop3proxy.log', 'wb', 0)
        self.servers = []
        self.proxyPorts = []
        if options["pop3proxy", "remote_servers"]:
            for server in options["pop3proxy", "remote_servers"]:
                server = server.strip()
                if server.find(':') > -1:
                    server, port = server.split(':', 1)
                else:
                    port = '110'
                self.servers.append((server, int(port)))

        if options["pop3proxy", "listen_ports"]:
            splitPorts = options["pop3proxy", "listen_ports"]
            self.proxyPorts = map(_addressAndPort, splitPorts)

        if len(self.servers) != len(self.proxyPorts):
            print "pop3proxy_servers & pop3proxy_ports are different lengths!"
            sys.exit()

        # Set up the statistics.
        self.totalSessions = 0
        self.activeSessions = 0
        self.numSpams = 0
        self.numHams = 0
        self.numUnsure = 0

        # Unique names for cached messages - see `getNewMessageName()` below.
        self.lastBaseMessageName = ''
        self.uniquifier = 2

    def close(self):
        assert self.prepared, "closed without being prepared!"
        self.servers = None
        if self.bayes is not None:
            # Only store a non-empty db.
            if self.bayes.nham != 0 and self.bayes.nspam != 0:
                state.bayes.store()
            self.bayes.close()
            self.bayes = None

        self.spamCorpus = self.hamCorpus = self.unknownCorpus = None
        self.spamTrainer = self.hamTrainer = None

        self.prepared = False
        close_platform_mutex(self.platform_mutex)
        self.platform_mutex = None

    def prepare(self, can_stop=True):
        """Do whatever needs to be done to prepare for running.  If
        can_stop is False, then we may not let the user shut down the
        proxy - for example, running as a Windows service this should
        be the case."""
        # If we can, prevent multiple servers from running at the same time.
        assert self.platform_mutex is None, "Should not already have the mutex"
        self.platform_mutex = open_platform_mutex()

        self.can_stop = can_stop

        # Do whatever we've been asked to do...
        self.createWorkers()
        self.prepared = True

    def buildServerStrings(self):
        """After the server details have been set up, this creates string
        versions of the details, for display in the Status panel."""
        serverStrings = ["%s:%s" % (s, p) for s, p in self.servers]
        self.serversString = ', '.join(serverStrings)
        self.proxyPortsString = ', '.join(map(_addressPortStr, self.proxyPorts))

    def buildStatusStrings(self):
        """Build the status message(s) to display on the home page of the
        web interface."""
        nspam = self.bayes.nspam
        nham = self.bayes.nham
        if nspam > 10 and nham > 10:
            db_ratio = nham/float(nspam)
            big = small = None
            if db_ratio > 5.0:
                big = "ham"
                small = "spam"
            elif db_ratio < (1/5.0):
                big = "spam"
                small = "ham"
            if big is not None:
                self.warning = "Warning: you have much more %s than %s - " \
                               "SpamBayes works best with approximately even " \
                               "numbers of ham and spam." % (big, small)
            else:
                self.warning = ""
        elif nspam > 0 or nham > 0:
            self.warning = "Database only has %d good and %d spam - you should " \
                           "consider performing additional training." % (nham, nspam)
        else:
            self.warning = "Database has no training information.  SpamBayes " \
                           "will classify all messages as 'unsure', " \
                           "ready for you to train."
        # Add an additional warning message if the user's thresholds are
        # truly odd.
        spam_cut = options["Categorization", "spam_cutoff"]
        ham_cut = options["Categorization", "ham_cutoff"]
        if spam_cut < 0.5:
            self.warning += "<br/>Warning: we do not recommend setting " \
                            "the spam threshold less than 0.5."
        if ham_cut > 0.5:
            self.warning += "<br/>Warning: we do not recommend setting " \
                            "the ham threshold greater than 0.5."
        if ham_cut > spam_cut:
            self.warning += "<br/>Warning: your ham threshold is " \
                            "<b>higher</b> than your spam threshold. " \
                            "Results are unpredictable."

    def createWorkers(self):
        """Using the options that were initialised in __init__ and then
        possibly overridden by the driver code, create the Bayes object,
        the Corpuses, the Trainers and so on."""
        print "Loading database...",
        if self.isTest:
            self.useDB = "pickle"
            self.DBName = '_pop3proxy_test.pickle'   # This is never saved.
        if not hasattr(self, "DBName"):
            self.DBName, self.useDB = storage.database_type([])
        self.bayes = storage.open_storage(self.DBName, self.useDB)
        
        self.buildStatusStrings()

        # Don't set up the caches and training objects when running the self-test,
        # so as not to clutter the filesystem.
        if not self.isTest:
            def ensureDir(dirname):
                try:
                    os.mkdir(dirname)
                except OSError, e:
                    if e.errno != errno.EEXIST:
                        raise

            # Create/open the Corpuses.  Use small cache sizes to avoid hogging
            # lots of memory.
            sc = get_pathname_option("Storage", "spam_cache")
            hc = get_pathname_option("Storage", "ham_cache")
            uc = get_pathname_option("Storage", "unknown_cache")
            map(ensureDir, [sc, hc, uc])
            if self.gzipCache:
                factory = GzipFileMessageFactory()
            else:
                factory = FileMessageFactory()
            age = options["Storage", "cache_expiry_days"]*24*60*60
            self.spamCorpus = ExpiryFileCorpus(age, factory, sc,
                                               '[0123456789\-]*',
                                               cacheSize=20)
            self.hamCorpus = ExpiryFileCorpus(age, factory, hc,
                                              '[0123456789\-]*',
                                              cacheSize=20)
            self.unknownCorpus = ExpiryFileCorpus(age, factory, uc,
                                                  '[0123456789\-]*',
                                                  cacheSize=20)

            # Given that (hopefully) users will get to the stage
            # where they do not need to do any more regular training to
            # be satisfied with spambayes' performance, we expire old
            # messages from not only the trained corpora, but the unknown
            # as well.
            self.spamCorpus.removeExpiredMessages()
            self.hamCorpus.removeExpiredMessages()
            self.unknownCorpus.removeExpiredMessages()

            # Create the Trainers.
            self.spamTrainer = storage.SpamTrainer(self.bayes)
            self.hamTrainer = storage.HamTrainer(self.bayes)
            self.spamCorpus.addObserver(self.spamTrainer)
            self.hamCorpus.addObserver(self.hamTrainer)

    def getNewMessageName(self):
        # The message name is the time it arrived, with a uniquifier
        # appended if two arrive within one clock tick of each other.
        messageName = "%10.10d" % long(time.time())
        if messageName == self.lastBaseMessageName:
            messageName = "%s-%d" % (messageName, self.uniquifier)
            self.uniquifier += 1
        else:
            self.lastBaseMessageName = messageName
            self.uniquifier = 2
        return messageName


# Option-parsing helper functions
def _addressAndPort(s):
    """Decode a string representing a port to bind to, with optional address."""
    s = s.strip()
    if ':' in s:
        addr, port = s.split(':')
        return addr, int(port)
    else:
        return '', int(s)

def _addressPortStr((addr, port)):
    """Encode a string representing a port to bind to, with optional address."""
    if not addr:
        return str(port)
    else:
        return '%s:%d' % (addr, port)


state = State()
proxyListeners = []
def _createProxies(servers, proxyPorts):
    """Create BayesProxyListeners for all the given servers."""
    for (server, serverPort), proxyPort in zip(servers, proxyPorts):
        listener = BayesProxyListener(server, serverPort, proxyPort)
        proxyListeners.append(listener)

def _recreateState():
    global state

    # Close the existing listeners and create new ones.  This won't
    # affect any running proxies - once a listener has created a proxy,
    # that proxy is then independent of it.
    # (but won't closing the database screw them?)
    for proxy in proxyListeners:
        proxy.close()
    del proxyListeners[:]

    # Close the state (which saves if necessary)
    state.close()
    # And get a new one going.
    state = State()

    prepare()
    _createProxies(state.servers, state.proxyPorts)
    return state

def main(servers, proxyPorts, uiPort, launchUI):
    """Runs the proxy forever or until a 'KILL' command is received or
    someone hits Ctrl+Break."""
    _createProxies(servers, proxyPorts)
    httpServer = UserInterfaceServer(uiPort)
    proxyUI = ProxyUserInterface(state, _recreateState)
    httpServer.register(proxyUI)
    Dibbler.run(launchBrowser=launchUI)

def prepare(can_stop=True):
    state.init()
    state.prepare(can_stop)
    # Launch any SMTP proxies.  Note that if the user hasn't specified any
    # SMTP proxy information in their configuration, then nothing will
    # happen.
    from spambayes import smtpproxy
    servers, proxyPorts = smtpproxy.LoadServerInfo()
    proxyListeners.extend(smtpproxy.CreateProxies(servers, proxyPorts,
                                                  smtpproxy.SMTPTrainer(state.bayes, state)))

    # setup info for the web interface
    state.buildServerStrings()

def start():
    # kick everything off
    assert state.prepared, "starting before preparing state"
    try:
        main(state.servers, state.proxyPorts, state.uiPort, state.launchUI)
    finally:
        state.close()

def stop():
    # Shutdown as though through the web UI.  This will save the DB, allow
    # any open proxy connections to complete, etc.
    from urllib import urlopen, urlencode
    urlopen('http://localhost:%d/save' % state.uiPort,
            urlencode({'how': 'Save & shutdown'})).read()


# ===================================================================
# __main__ driver.
# ===================================================================

def run():
    global state
    # Read the arguments.
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hbd:p:l:u:o:')
    except getopt.error, msg:
        print >>sys.stderr, str(msg) + '\n\n' + __doc__
        sys.exit()

    runSelfTest = False
    for opt, arg in opts:
        if opt == '-h':
            print >>sys.stderr, __doc__
            sys.exit()
        elif opt == '-b':
            state.launchUI = True
        # '-p' and '-d' are handled by the storage.database_type call
        # below, in case you are wondering why they are missing.
        elif opt == '-l':
            state.proxyPorts = [_addressAndPort(arg)]
        elif opt == '-u':
            state.uiPort = int(arg)
        elif opt == '-o':
            options.set_from_cmdline(arg, sys.stderr)

    state.DBName, state.useDB = storage.database_type(opts)

    # Let the user know what they are using...
    print get_version_string("POP3 Proxy")
    print "and engine %s.\n" % (get_version_string(),)

    if 0 <= len(args) <= 2:
        # Normal usage, with optional server name and port number.
        if len(args) == 1:
            state.servers = [(args[0], 110)]
        elif len(args) == 2:
            state.servers = [(args[0], int(args[1]))]

        # Default to listening on port 110 for command-line-specified servers.
        if len(args) > 0 and state.proxyPorts == []:
            state.proxyPorts = [('', 110)]

        try:
            prepare()
        except AlreadyRunningException:
            print  >>sys.stderr, \
                   "ERROR: The proxy is already running on this machine."
            print  >>sys.stderr, "Please stop the existing proxy and try again"
            return
        start()

    else:
        print >>sys.stderr, __doc__

if __name__ == '__main__':
    run()
