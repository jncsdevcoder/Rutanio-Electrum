#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from functools import partial

import calendar
import datetime
import time
import signal
import copy

from http.client import CannotSendRequest

from xmlrpc.client import ServerProxy

from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QTextEdit, QGridLayout, QLineEdit

from electrum_rutanio import util, keystore, ecc, crypto
from electrum_rutanio import transaction
from electrum_rutanio.bip32 import BIP32Node
from electrum_rutanio.plugin import BasePlugin, hook, run_hook

from electrum_rutanio.i18n import _
from electrum_rutanio.wallet import Multisig_Wallet
from electrum_rutanio.util import bh2u, bfh

from electrum_rutanio.gui.qt.transaction_dialog import show_transaction_timeout, TxDialogTimeout
from electrum_rutanio.gui.qt.transaction_wait_dialog import show_timeout_wait_dialog, TimeoutWaitDialog
from electrum_rutanio.gui.qt.util import WaitingDialog, EnterButton, Buttons, WindowModalDialog, CloseButton, OkButton, read_QIcon

from . import server

import sys
import traceback


class Listener(util.DaemonThread):

    def __init__(self, parent):
        util.DaemonThread.__init__(self)
        self.daemon = True
        self.parent = parent
        self.received = set()
        self.keyhashes = []

    def set_keyhashes(self, keyhashes):
        self.keyhashes = keyhashes

    def clear(self, keyhash):
        server.delete(keyhash)
        self.received.remove(keyhash)

    def run(self):
        while self.running:
            if not self.keyhashes:
                time.sleep(2)
                continue
            for keyhash in self.keyhashes:
                try:
                    if server.get(keyhash+'_name') == None:
                        keyhash_concise = keyhash[0:10]+'...'+keyhash[-1:-5:-1]
                        server.put(keyhash+'_name', keyhash_concise)
                    pick = server.get(keyhash+'_pick')
                    signed = server.get(keyhash+'_signed')
                except CannotSendRequest:
                    self.logger.info("cannot contact cosigner pool")
                    continue
                except Exception as e:
                    self.logger.info(e)
                    continue

                if pick == 'False' or signed == 'True':
                    continue
                try:
                    message = server.get(keyhash)
                except Exception as e:
                    self.logger.info("cannot contact cosigner pool")
                    time.sleep(30)
                    continue
                if message:
                    self.received.add(keyhash)
                    self.logger.info(f"received message for {keyhash}")
                    self.parent.obj.cosigner_receive_signal.emit(
                        keyhash, message)
            # poll every 30 seconds
            time.sleep(30)


class QReceiveSignalObject(QObject):
    cosigner_receive_signal = pyqtSignal(object, object)


class Plugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.listener = None
        self.window = None
        self.obj = QReceiveSignalObject()
        self.obj.cosigner_receive_signal.connect(self.on_receive)
        self.keys = []
        self.cosigner_list = []
        self.suppress_notifications = False

    def requires_settings(self):
        return True

    def settings_widget(self, window):
        return EnterButton(_('Settings'),
                           partial(self.calibration_dialog, window))

    def calibration_dialog(self, window):
        d = WindowModalDialog(window, _("Cosigner Pool Settings"))

        d.setMinimumSize(150, 100)

        vbox = QVBoxLayout(d)

        purge = QPushButton(_("Purge Transactions"))
        purge.clicked.connect(self.purge_transaction)
        purge.setIcon(read_QIcon("warning.png"))
        vbox.addWidget(purge)

        vbox.addWidget(QLabel(_('Wallet Owner:')))
        grid = QGridLayout()
        vbox.addLayout(grid)

        grid.addWidget(QLabel(_('Name:')), 0, 0)
        name = QLineEdit()
        name.setText(self.config.get('wallet_owner', ''))
        grid.addWidget(name, 0, 1)

        sync = QPushButton(_("Sync Name"))
        sync.clicked.connect(partial(self.sync_name, name))
        vbox.addWidget(sync)

        status = QPushButton(_("Tx. Status"))
        status.clicked.connect(partial(self.tx_status_dialog, window))
        vbox.addWidget(status)

        vbox.addStretch()
        vbox.addSpacing(13)
        vbox.addLayout(Buttons(CloseButton(d), OkButton(d)))

        if not d.exec_():
            return

    def purge_transaction(self):
        mods = ['_pick', '_signed', '_lock', '_shutdown']
        if not self.window.question(_("Purging you transactions will erase the current transaction") + '\n' +
                        _("Are you sure you want to purge your transaction?")):
            return
        for mod in mods:
            for key, _hash, window in self.keys:
                server.delete(_hash)
                server.delete(_hash+mod)
            for window, xpub, K, _hash in self.cosigner_list:
                server.delete(_hash)
                server.delete(_hash+mod)
        self.window.show_message(_("Your transactions have been purged."))
    
    def sync_name(self, name):
        self.config.set_key('wallet_owner', name.text())
        if self.config.get('wallet_owner', ''):
            for key, _hash, window in self.keys:
                server.put(_hash+'_name', self.config.get('wallet_owner', ''))
        for key, _hash, window in self.keys:
            if self.config.get('wallet_owner', '') == server.get(_hash+'_name'):
                self.window.show_message(_("Your name has been synced"))
            else:
                self.window.show_message(_("Failed to sync name with cosigner pool"))


    def tx_status_dialog(self, window):
        d = QDialog(window)
        d.setWindowTitle(_("Transaction Status"))
        d.setMinimumSize(600, 300)

        vbox = QVBoxLayout(d)

        status_header = 'No transaction in progress'
        status = ''

        for window, xpub, K, _hash in self.cosigner_list:
            cosigner = server.get(_hash+'_name')
            signed = True if server.get(_hash+'_signed') else False
            signing = True if server.get(_hash+'_lock') else False
            if signed:
                status_header = 'Transaction in progress'
            if signed:
                message = 'Signed'
            elif signing: 
                message = 'Signing'
            else:
                message = 'Not signed'
            status += f'<br><br> {cosigner}: <b>{message}</b>'

        for key, _hash, window in self.keys:
            cosigner = server.get(_hash+'_name')
            signed = True if server.get(_hash+'_signed') else False
            signing = True if server.get(_hash+'_lock') else False
            if signed:
                status_header = 'Transaction in progress'
            if signed:
                message = 'Signed'
            elif signing: 
                message = 'Signing'
            else:
                message = 'Not signed'
            status += f'<br><br> You: <b>{message}</b>'

        self.tx_status = QLabel()
        vbox.addWidget(self.tx_status)
        self.tx_status.setTextFormat(Qt.RichText)
        self.tx_status.setText(_("<b>Transaction Status</b>") + ': ' + status_header + status)
        self.tx_status.show()

        vbox.addStretch()
        vbox.addSpacing(13)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setModal(True)
        d.show()
        
        if not d.exec_():
            return

    def correct_shutdown_state(self, _hash):
        shutdown_flag = server.get(_hash+'_shutdown')
        if shutdown_flag == 'down':
            return
        server.put(_hash+'_pick', 'True')
        server.put(_hash+'_shutdown', 'down')

    @hook
    def init_qt(self, gui):
        for window in gui.windows:
            self.on_new_window(window)

    @hook
    def on_new_window(self, window):
        self.update(window)
        
        wallet = self.window.wallet
        if type(wallet) != Multisig_Wallet:
            return
        for key, _hash, window in self.keys:
            self.correct_shutdown_state(_hash)

    @hook
    def on_close_window(self, window):
        self.update(window)

    def is_available(self):
        return True

    def update(self, window):
        wallet = window.wallet
        self.window = window
        if type(wallet) != Multisig_Wallet:
            return
        if self.listener is None:
            self.logger.info("starting listener")
            self.listener = Listener(self)
            self.listener.start()
        elif self.listener:
            self.logger.info("shutting down listener")
            self.listener.stop()
            self.listener = None
        self.keys = []
        self.cosigner_list = []
        for key, keystore in wallet.keystores.items():
            xpub = keystore.get_master_public_key()
            pubkey = BIP32Node.from_xkey(xpub).eckey.get_public_key_bytes(compressed=True)
            _hash = bh2u(crypto.sha256d(pubkey))
            if not keystore.is_watching_only():
                self.keys.append((key, _hash, window))
            else:
                self.cosigner_list.append((window, xpub, pubkey, _hash))
        if self.listener:
            self.listener.set_keyhashes([t[1] for t in self.keys])

    @hook
    def transaction_dialog(self, d):
        d.cosigner_send_button = b = QPushButton(_("Send to cosigner"))
        b.clicked.connect(lambda: self.do_send(d.tx, d))
        d.buttons.insert(0, b)
        self.transaction_dialog_update(d)

    @hook
    def transaction_dialog_update(self, d):
        if d.tx.is_complete() or d.wallet.can_sign(d.tx):
            d.cosigner_send_button.hide()
            return
        for window, xpub, K, _hash in self.cosigner_list:
            if window.wallet == d.wallet and self.cosigner_can_sign(d.tx, xpub):
                d.cosigner_send_button.show()
                break
        else:
            d.cosigner_send_button.hide()

    def cosigner_can_sign(self, tx, cosigner_xpub):
        from electrum_rutanio.keystore import is_xpubkey, parse_xpubkey
        xpub_set = set([])
        for txin in tx.inputs():
            for x_pubkey in txin['x_pubkeys']:
                if is_xpubkey(x_pubkey):
                    xpub, s = parse_xpubkey(x_pubkey)
                    xpub_set.add(xpub)
        return cosigner_xpub in xpub_set

    def do_send(self, tx, d):
        def on_success(result):
            [server.put(t[1]+'_signed', 'True') for t in self.keys]
            release_locks()
            self.window.show_message(_("Your transaction was sent to the cosigning pool.") + '\n' +
                                _("Open your cosigner wallet to retrieve it."))
            time.sleep(1)
            d.close()

        def on_failure(exc_info):
            e = exc_info[1]
            try: self.logger.error("on_failure", exc_info=exc_info)
            except OSError: pass
            self.window.show_error(_("Failed to send transaction to cosigning pool. Please resend") + ':\n' + str(e))

        def release_locks():
            wallet = self.window.wallet
            if type(wallet) == Multisig_Wallet:
                for key, _hash, window in self.keys:
                    # delete lock blocking other wallets from opening TX dialog
                    server.delete(_hash+'_lock')
                    # set pick flag to true
                    server.put(_hash+'_pick', 'True')
                    # set graceful shutdown flag to down to signify a graceful shutdown
                    server.put(_hash+'_shutdown', 'down')

        def send_to_cosigner():        
            for window, xpub, K, _hash in self.cosigner_list:
                if not self.cosigner_can_sign(tx, xpub):
                    continue
                # construct message
                raw_tx_bytes = bfh(str(tx))
                public_key = ecc.ECPubkey(K)
                message = public_key.encrypt_message(raw_tx_bytes).decode('ascii')
                # send message
                server.put(_hash, message)
                server.put(_hash+'_pick', 'True')

        task = lambda: send_to_cosigner()
        msg = _('Sending transaction to cosigning pool...')
        WaitingDialog(self.window, msg, task, on_success, on_failure)
        time.sleep(.5)

    def on_receive(self, keyhash, message):
        self.logger.info(f"signal arrived for {keyhash}")

        WAIT_TIME = 10 * 60

        if self.suppress_notifications:
            for window, xpub, K, _hash in self.cosigner_list:
                if server.get(_hash+'_lock'):
                    return
            self.suppress_notifications = False

        for key, _hash, window in self.keys:
            if _hash == keyhash:
                break
        else:
            self.logger.info("keyhash not found")
            return

        wallet = window.wallet
        if isinstance(wallet.keystore, keystore.Hardware_KeyStore):
            window.show_warning(_('An encrypted transaction was retrieved from cosigning pool.') + '\n' +
                                _('However, hardware wallets do not support message decryption, '
                                  'which makes them not compatible with the current design of cosigner pool.'))
            return
        elif wallet.has_keystore_encryption():
            # set pick to false when opening password dialog
            server.put(keyhash+'_pick', 'False')
            password = window.password_dialog(_('An encrypted transaction was retrieved from cosigning pool.') + '\n' +
                                              _('Please enter your password to decrypt it.'))
            if not password:
                # set pick back to true if password incorrect or omitted
                server.put(keyhash+'_pick', 'True')
                return
        else:
            password = None
            if not window.question(_("An encrypted transaction was retrieved from cosigning pool.") + '\n' +
                                   _("Do you want to open it now?")):
                return

        xprv = wallet.keystore.get_master_private_key(password)
        if not xprv:
            return
        try:
            privkey = BIP32Node.from_xkey(xprv).eckey
            message = bh2u(privkey.decrypt_message(message))
        except Exception as e:
            self.logger.exception('')
            window.show_error(_('Error decrypting message') + ':\n' + str(e))
            return

        #self.listener.clear(keyhash)
        tx = transaction.Transaction(message)

        def calculate_wait_time(expire):
            # calculate wait time
            wait_time = int((WAIT_TIME - (int(server.get_current_time()) - int(expire))))
            mins, secs = divmod(wait_time, 60)
            return '{:02d}:{:02d}'.format(mins, secs)

        # check if lock has been placed for any wallets
        for window, xpub, K, _hash in self.cosigner_list:
            expire = server.get(_hash+'_lock')
            if expire:
                # set pick back to true if user lock is present
                server.put(keyhash+'_pick', 'True')
                # suppress any further notifications
                self.suppress_notifications = True
                # calculate wait time based on lock expiry and server time
                timeformat = calculate_wait_time(expire)

                # display pop up
                window.show_warning(_("A cosigner is currently signing the transaction.") + '\n' +
                                    _("Please wait {} until the signing has concluded.".format(timeformat)))

                show_timeout_wait_dialog(tx, window, prompt_if_unsaved=True)

                return

        # test if wallet has previously placed a lock
        current_wallet_lock = server.get(keyhash+'_lock')
        if not current_wallet_lock:
            # no lock has been placed for current wallet => lock transaction dialog 
            server.put(keyhash+'_lock', str(server.get_current_time()))
            time_until_expired = '10 minutes'
        else:
            time_until_expired = calculate_wait_time(current_wallet_lock)
        
        # place flag to test for graceful shutdown
        server.put(keyhash+'_shutdown', 'up')
        window.show_warning(_("You have {} to conclude signing after which the dialog will".format(time_until_expired)) + '\n' +
                            _("automatically close."))
            

        show_transaction_timeout(tx, window, prompt_if_unsaved=True)
