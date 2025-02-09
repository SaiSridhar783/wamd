import time
import base64
import os
import json

from twisted.internet.defer import (
    inlineCallbacks, Deferred, maybeDeferred, succeed, fail
)

from twisted.protocols.tls import TLSMemoryBIOFactory
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure
from twisted.python.reflect import qual
from twisted.logger import Logger

from autobahn.twisted.websocket import WebSocketClientProtocol, WebSocketClientFactory

from dissononce.processing.impl.handshakestate import HandshakeState
from dissononce.processing.impl.cipherstate import CipherState
from dissononce.cipher.aesgcm import AESGCMCipher
from dissononce.dh.x25519.x25519 import X25519DH
from dissononce.hash.sha256 import SHA256Hash
from dissononce.processing.handshakepatterns.interactive.XX import XXHandshakePattern
from dissononce.dh.keypair import KeyPair as X25519KeyPair
from dissononce.dh.x25519.private import PrivateKey as X25519PrivateKey
from dissononce.dh.x25519.public import PublicKey as X25519PublicKey

from consonance.dissononce_extras.processing.symmetricstate_wa import WASymmetricState

from axolotl.ecc import curve, djbec
from axolotl.util.keyhelper import KeyHelper
from axolotl.state.prekeybundle import PreKeyBundle
from axolotl.identitykey import IdentityKey

from .constants import Constants
from .common import AuthState
from .errors import (
    ConnectionClosedError,
    AuthenticationFailedError,
    StreamEndError,
    NodeStreamError
)
from .coder import (
    encodeInt, decodeInt, WABinaryReader, WABinaryWriter,
    splitJid, Node
)

from .utils import generateRandomNumber, toHex, inflate
from .handlers import createNodeHander
from ._tls import getTlsConnectionFactory
from .proto import WAMessage_pb2
from .messages import WhatsAppMessage, TextMessage, MediaMessage
from .signalhelper import processPreKeyBundle, encrypt as signalEncrypt


_VALID_EVENTS = ["open", "qr", "close", "inbox", "ack"]


class MultiDeviceWhatsAppClientProtocol(WebSocketClientProtocol):

    log = Logger() # set your own logger instance

    # noise protocol
    _noiseHandshakeState = None
    _recvCipher = None
    _sendCipher = None

    _serverHelloDeferred = None

    def __init__(self, authState=None, reactor=None):
        WebSocketClientProtocol.__init__(self)

        validEvents = [] if self._valid_events is None else self._valid_events.copy()
        validEvents.extend(_VALID_EVENTS)

        self.set_valid_events(validEvents)

        self.authState = authState

        if reactor is None:
            from twisted.internet import reactor

        self.reactor = reactor

        self._pendingRequest = {}
        self._nodeHandlers = {}


    def onOpen(self):
        self.log.info("Connected to whatsapp server")
        if self.authState is None:
            self.fire("open", self)
        else:
            self._doHandshake().addErrback(self._handleFailure)

    def onClose(self, wasClean, code, reason):
        self.log.info("Connection Closed: wasClean: {wasClean}, code: {code}, reason: {reason}",
            wasClean=wasClean, code=code, reason=reason)

        if self._keepAliveLoop is not None and self._keepAliveLoop.running:
            self._keepAliveLoop.stop()
            self._keepAliveLoop = None

        if self._failure is not None:
            failure, self._failure = self._failure, None
        else:
            failure = None

        # print()
        # print("failure: %s, self.factory.authDeferred: %r" % (failure, self.factory.authDeferred, ))
        if failure is not None and self.factory.authDeferred is not None:
            self.factory.authFailure(failure)
        else:
            # TODO
            # 1.do not fire "close" event when restarting connection
            #   after authentication success.
            # 2. Handle error when device is scanned using non md device

            if failure is None:
                excReason = ConnectionClosedError(reason="Connection Closed Cleanly")
            elif isinstance(failure.value, NodeStreamError):
                if failure.value.code == "401":
                    excReason = ConnectionClosedError(isLoggedOut=True, reason="Device Logged Out")
                else:
                    excReason = ConnectionClosedError(reason="Unhandled Stream Error")
            elif isinstance(failure.value, AuthenticationFailedError):
                excReason = ConnectionClosedError(isAuthDone=False, reason="Authentication Failed")
            else:
                excReason = ConnectionClosedError(reason="Unknown Failure: \n%s" % (str(failure)))

            self.fire("close", self, Failure(excReason))

    def _authDone(self):
        return self.factory.authDeferred is None

    def onMessage(self, message, isBinary):
        self.log.debug("OnMessage Received [{message}]", message=toHex(message))

        # TODO
        # Handle close message (b'\x88\x02\x03\xf3')

        if self._serverHelloDeferred is not None:
            handshakeMsg = WAMessage_pb2.HandshakeMessage()
            handshakeMsg.ParseFromString(message[3:])
            serverHello = handshakeMsg.serverHello
            d, self._serverHelloDeferred = self._serverHelloDeferred, None
            d.callback(serverHello)
        else:
            while message:
                messageLength = decodeInt(message[:3], 3)
                encrypted = message[3:messageLength + 3]
                message = message[messageLength + 3:]

                self.log.debug("OnMessage, Encrypted: [{encrypted}]", encrypted=toHex(encrypted))

                if self._recvCipher is not None:
                    try:
                        decrypted = self._recvCipher.decrypt_with_ad(b"", encrypted)
                    except:
                        self._handleFailure(Failure(), disconnect=True)
                    else:
                        self.log.debug("Decrypted Message: [{decrypted}]", decrypted=toHex(decrypted))

                        try:
                            if decrypted[0] & Constants.FLAG_COMPRESSED:
                                decrypted = inflate(decrypted[1:])
                            else:
                                decrypted = decrypted[1:]
                            node = WABinaryReader(decrypted).readNode()
                        except StreamEndError:
                            pass
                        else:
                            self.messageNodeReceived(node)


    def authenticate(self):
        d = self._doHandshake()
        d.addErrback(self._handleFailure)
        deferred = self.factory.authDeferred
        return deferred

    _failure = None

    def _handleFailure(self, failure, disconnect=False):
        self.log.error("Handle Failure: {failure}", failure=failure)

        self._failure = failure

        if not self._authDone():
            self._sendCipher = None
            self._recvCipher = None

        if disconnect:
            self.sendClose(code=1000)

    @inlineCallbacks
    def _doHandshake(self):
        serverHello = yield self._waitServerHello()

        self.log.debug("ServerHello: {serverHello}", serverHello=serverHello)

        messageBuffer = bytearray()

        self._noiseHandshakeState.read_message(
            serverHello.ephemeral + serverHello.static + serverHello.payload,
            messageBuffer)

        cert = WAMessage_pb2.NoiseCertificate()
        cert.ParseFromString(bytes(messageBuffer))
        certDetails = WAMessage_pb2.Details()
        certDetails.ParseFromString(cert.details)

        if certDetails.issuer != Constants.CERTIFICATE_ISSUER:
            raise AuthenticationFailedError("Noise certificate issued by unknown source: %s" % (certDetails.issuer))

        if not curve.Curve.verifySignature(
            djbec.DjbECPublicKey(Constants.WHATSAPP_LONG_TERM),
            cert.details,
            cert.signature
        ):
            raise AuthenticationFailedError("Invalid signature on noise ceritificate")

        if certDetails.key != self._noiseHandshakeState.rs.data:
            raise AuthenticationFailedError("Noise certificate key does not match proposed server static key")

        if certDetails.HasField("expires") and certDetails.expires < int(time.time()):
            raise AuthenticationFailedError("Noise certificate expired")

        self.log.debug("Certificate Verifcation OK")

        clientPayload = self._buildClientPayloadHandshake()

        self.log.debug("Client Payload: {clientPayload}", clientPayload=clientPayload)

        messageBuffer = bytearray()

        cipherPair = self._noiseHandshakeState.write_message(
            clientPayload.SerializeToString(),
            messageBuffer)

        self._sendCipher, self._recvCipher = cipherPair

        clientFinish = WAMessage_pb2.ClientFinish(
            static=bytes(messageBuffer[:48]),
            payload=bytes(messageBuffer[48:]))

        handshakeMsg = WAMessage_pb2.HandshakeMessage()
        handshakeMsg.clientFinish.MergeFrom(clientFinish)

        self.log.debug("Client Finish: {clientFinish}", clientFinish=handshakeMsg)

        clientFinishMessage = handshakeMsg.SerializeToString()
        clientFinishPayload = encodeInt(len(clientFinishMessage), 3) + clientFinishMessage

        self.sendMessage(clientFinishPayload, isBinary=True)

        self._startKeepAliveLoop()


    def _waitServerHello(self):
        self._sendClientHello()
        self._serverHelloDeferred = Deferred()
        return self._serverHelloDeferred

    def _sendClientHello(self):
        if self.authState is None:
            self.authState = AuthState()

        # Noise Initialization Noise_XX_25519_AESGCM_SHA256
        # ('e',),
        # ('e', 'ee', 's', 'es'),
        # ('s', 'se')

        self._noiseHandshakeState = HandshakeState(
            WASymmetricState(
                CipherState(
                    AESGCMCipher()
                ),
                SHA256Hash()
            ),
            X25519DH())

        self._noiseHandshakeState.initialize(
            XXHandshakePattern(),
            True,
            Constants.PROLOGUE,
            s=X25519KeyPair(
                X25519PublicKey(self.authState.noiseKey.getPublicKey().getPublicKey()),
                X25519PrivateKey(self.authState.noiseKey.getPrivateKey().getPrivateKey())
            ))

        ephemeralPublic = bytearray()
        self._noiseHandshakeState.write_message(b"", ephemeralPublic)

        clientHello = WAMessage_pb2.ClientHello()
        clientHello.ephemeral = bytes(ephemeralPublic)

        handshakeMsg = WAMessage_pb2.HandshakeMessage(
            clientHello=clientHello
        )

        self.log.debug("ClientHello: {handshakeMsg}", handshakeMsg=handshakeMsg)

        clientHelloMsg = handshakeMsg.SerializeToString()
        clientHelloPayload = bytes(Constants.PROLOGUE) + encodeInt(len(clientHelloMsg), 3) + clientHelloMsg

        self.sendMessage(clientHelloPayload, True)

    def _buildClientPayloadHandshake(self):
        browser = Constants.DEFAULT_BROWSER_KIND
        version = Constants.WHATSAPP_WEB_VERSION

        clientPayload = WAMessage_pb2.ClientPayload()

        clientPayload.connectReason = 1
        clientPayload.connectType = 1

        if not self.authState.has("me"):
            clientPayload.passive = False

            companionRegData = WAMessage_pb2.CompanionRegData()
            companionRegData.buildHash = base64.b64decode(Constants.BUILD_HASH)

            companionProps = WAMessage_pb2.CompanionProps()
            companionProps.os = browser[0]

            appVersion = WAMessage_pb2.AppVersion()
            appVersion.primary = 10
            companionProps.version.MergeFrom(appVersion)
            companionProps.platformType = 1
            companionProps.requireFullSync = False

            companionRegData.companionProps = companionProps.SerializeToString()
            companionRegData.eRegid = encodeInt(self.authState.registrationId, 4)
            companionRegData.eKeytype = encodeInt(5, 1)
            companionRegData.eIdent = self.authState.signedIdentityKey.getPublicKey().getPublicKey()
            companionRegData.eSkeyId = encodeInt(self.authState.signedPrekey.getId(), 3)
            companionRegData.eSkeyVal = self.authState.signedPrekey.getKeyPair().getPublicKey().getPublicKey()
            companionRegData.eSkeySig = self.authState.signedPrekey.getSignature()

            clientPayload.regData.MergeFrom(companionRegData)
        else:
            self.log.debug("Auth State: {authState}", authState=self.authState)
            clientPayload.passive = True
            user, agent, device, server = splitJid(self.authState.me['jid'])
            clientPayload.username = int(user)
            clientPayload.device = int(device)

        userAgentAppVersion = WAMessage_pb2.AppVersion()
        userAgentAppVersion.primary = version[0]
        userAgentAppVersion.secondary = version[1]
        userAgentAppVersion.tertiary = version[2]

        userAgent = WAMessage_pb2.UserAgent()
        userAgent.appVersion.MergeFrom(userAgentAppVersion)
        userAgent.platform = 14
        userAgent.releaseChannel = 0
        userAgent.mcc = "000"
        userAgent.mnc = "000"
        userAgent.osVersion = browser[2]
        userAgent.manufacturer = ""
        userAgent.device = browser[1]
        userAgent.osBuildNumber = "0.1"
        userAgent.localeLanguageIso6391 = "en"
        userAgent.localeCountryIso31661Alpha2 = "en"

        clientPayload.userAgent.MergeFrom(userAgent)

        webInfo = WAMessage_pb2.WebInfo()
        webInfo.webSubPlatform = 0

        clientPayload.webInfo.MergeFrom(webInfo)
        return clientPayload

    @inlineCallbacks
    def messageNodeReceived(self, node):
        self.log.debug("Node Received:\n\n{node}\n", node=node)

        nodeHandler = None

        try:
            nodeHandler = createNodeHander(node.tag, self.reactor)
        except:
            # TODO
            # handle failure when login
            self._handleFailure(Failure())

        if not nodeHandler:
            return

        self.log.debug("Using {handler} to handle [{tag}] node", handler=nodeHandler, tag=node.tag)

        try:
            yield maybeDeferred(nodeHandler.handleNode, self, node)
        except:
            # TODO
            # handle failure when login
            self._handleFailure(Failure())


    def sendMessageNode(self, node):
        self.log.debug("Message Node:\n\n{node}\n", node=node)
        encoded = b"\x00" + WABinaryWriter(node).getData()
        self.log.debug("Message Node Encoded: [{encoded}]", encoded=toHex(encoded))
        encrypted = self._sendCipher.encrypt_with_ad(b"", encoded)
        self.log.debug("Message Node Encrypted: [{encrypted}]", encrypted=toHex(encrypted))
        payload = encodeInt(len(encrypted), 3) + encrypted
        self.log.debug("Message Payload: [{payload}]", payload=toHex(payload))
        self.sendMessage(payload, isBinary=True)

    def request(self, node):
        deferred = Deferred()
        self._pendingRequest[node['id']] = deferred
        self.sendMessageNode(node)
        return deferred

    def getSelfJid(self):
        user, _, _, server = splitJid(self.authState.me['jid'])
        return "%s@%s" % (user, server)

    def sendReadReceipt(self, message):
        try:
            self.sendMessageNode(Node(
                "receipt", {
                    'to': message['from'],
                    'type': "read",
                    'id': message['id'],
                    't': str(int(time.time()))
                }))
        except:
            return fail(Failure())
        return succeed(None)

    def sendWhatsAppMessage(self, message):
        if not isinstance(message, WhatsAppMessage):
            return fail(
                TypeError("Must be an instance of %s" % qual(WhatsAppMessage))
            )

        if not isinstance(message, (TextMessage, MediaMessage)):
            return fail(
                NotImplementedError("%s is not implemented" % qual(message.__class__))
            )

        if isinstance(message, TextMessage):
            d = self._processTextMessageAndSend(message)
        elif isinstance(message, MediaMessage):
            d = self._processMediaMessageAndSend(message)

        return d.addCallback(lambda ignored: message)


    @inlineCallbacks
    def _processTextMessageAndSend(self, message):
        participantsNode = yield self._createParticipantsForMessage(message)

        messageNode = Node(
            "message", {
                'id': message['id'],
                'to': message['to'],
                'type': "text"
            },
            participantsNode)

        messageNode.addChild(self._buildDeviceIdentityNode())

        yield self.request(messageNode) # Just ignore the ack node response


    @inlineCallbacks
    def _createParticipantsForMessage(self, message):
        destUser, _, _, server = splitJid(message['to'])
        meUser, _, _, meServer  = splitJid(self.getSelfJid())

        sessionExists = yield maybeDeferred(self.authState.store.containSession, destUser, 1)
        sessionMeExists = yield maybeDeferred(self.authState.store.containSession, meUser, 1)

        keyRequestJids = []
        if not sessionExists:
            keyRequestJids.append(message['to'])

        if not sessionMeExists:
            keyRequestJids.append(self.getSelfJid())

        if keyRequestJids:
            preKeyBundles = yield self._requestPreKeyBundles(keyRequestJids)

            for jid, preKeyBundle in preKeyBundles.items():
                yield processPreKeyBundle(self.authState, preKeyBundle, jid.split("@")[0])

        messageProto = message.toProtobufMessage()

        deviceSentMessageProto = WAMessage_pb2.DeviceSentMessage()
        deviceSentMessageProto.destinationJid = message['to']
        deviceSentMessageProto.message.MergeFrom(messageProto)

        deviceMessageProto = WAMessage_pb2.Message()
        deviceMessageProto.deviceSentMessage.MergeFrom(deviceSentMessageProto)

        destType, destCipherText = yield signalEncrypt(
            self.authState,
            messageProto.SerializeToString(),
            destUser)

        destEncNode = Node("enc", {'v': "2", 'type': destType}, destCipherText)

        deviceType, deviceCipherText = yield signalEncrypt(
            self.authState,
            deviceMessageProto.SerializeToString(),
            meUser)

        deviceEncNode = Node("enc", {'v': "2", 'type': deviceType}, deviceCipherText)

        participantsNode = Node("participants", None, [
            Node("to", {'jid': message['to']}, destEncNode),
            Node("to", {'jid': self.getSelfJid()}, deviceEncNode)
        ])

        return participantsNode


    @inlineCallbacks
    def _requestPreKeyBundles(self, jids):
        if not isinstance(jids, list):
            jids = [jids]

        iqNode = Node("iq", {
            'id': self._generateMessageId(),
            'xmlns': "encrypt",
            'type': "get",
            'to': "@c.us"
        })

        keyNode = Node("key")

        for jid in jids:
            userNode = Node("user", {
                'jid': jid
            })
            keyNode.addChild(userNode)

        iqNode.addChild(keyNode)

        resultNode = yield self.request(iqNode)

        preKeyBundles = {}

        for userNode in resultNode.getChild("list").getChilds("user"):
            signedPreKeyNode = userNode.findChild("skey")
            preKeyNode = userNode.findChild("key")
            # TODO
            # handle if preKeyNode is None
            registrationId = decodeInt(userNode.findChild("registration").content, 4)
            identityKey = IdentityKey(djbec.DjbECPublicKey(userNode.findChild("identity").content))
            signedPreKeyId = decodeInt(signedPreKeyNode.findChild("id").content, 3)
            signedPreKeyPublic = djbec.DjbECPublicKey(signedPreKeyNode.getChild("value").content)
            signedPreKeySignature = signedPreKeyNode.getChild("signature").content

            preKeyId = decodeInt(preKeyNode.findChild("id").getContent(), 3)
            preKeyPublic = djbec.DjbECPublicKey(preKeyNode.getChild("value").content)

            preKeyBundles[userNode['jid']] = PreKeyBundle(
                registrationId, 1, preKeyId, preKeyPublic,
                signedPreKeyId, signedPreKeyPublic, signedPreKeySignature, identityKey)

        return preKeyBundles


    def _buildDeviceIdentityNode(self):
        deviceIdentityProto = WAMessage_pb2.ADVSignedDeviceIdentity()

        deviceIdentityProto.details = self.authState.signedDeviceIdentity['details']
        deviceIdentityProto.accountSignature = self.authState.signedDeviceIdentity['accountSignature']
        deviceIdentityProto.accountSignatureKey = self.authState.signedDeviceIdentity['accountSignatureKey']
        deviceIdentityProto.deviceSignature = self.authState.signedDeviceIdentity['deviceSignature']

        return Node(
            "device-identity",
            None,
            deviceIdentityProto.SerializeToString())

    @inlineCallbacks
    def _uploadPreKeys(self):
        nextPrekeyId = self.authState.nextPrekeyId

        self.log.debug("Uploading Prekeys")

        preKeys = KeyHelper.generatePreKeys(nextPrekeyId, 10)
        maxPreKeyId = [key.getId() for key in preKeys][-1]

        preKeysContent = []

        for preKey in preKeys:
            try:
                yield maybeDeferred(
                    self.authState.store.storePreKey, preKey.getId(), preKey)
                preKeysContent.append(
                    Node("key", None, [
                        Node("id", None, encodeInt(preKey.getId(), 3)),
                        Node("value", None, preKey.getKeyPair().getPublicKey().getPublicKey())
                    ]))
            except:
                self.log.error("Failed to store prekey: {failure}", failure=Failure())

        preKeys = []

        yield self.request(
            Node("iq", attributes={
                'id': self._generateMessageId(),
                'xmlns': "encrypt",
                'type': "set",
                'to': "@s.whatsapp.net"
            }, content=[
                Node("registration", None, encodeInt(self.authState.registrationId, 4)),
                Node("type", None, encodeInt(curve.Curve.DJB_TYPE, 1)),
                Node("identity", None, self.authState.signedIdentityKey.getPublicKey().getPublicKey()),
                Node("list", None, preKeysContent),
                Node("skey", None, [
                    Node("id", None, encodeInt(self.authState.signedPrekey.getId(), 3)),
                    Node("value", None, self.authState.signedPrekey.getKeyPair().getPublicKey().getPublicKey()),
                    Node("signature", None, self.authState.signedPrekey.getSignature())
                ])
            ]))

        self.authState.nextPrekeyId = nextPrekeyId + 1

    def _restart(self):
        self.log.info("Authentication Success, Restarting Connection")
        self.factory._authState = self.authState
        self.authState = None
        self._sendCipher = None
        self._recvCipher = None

    _keepAliveLoop = None

    def _startKeepAliveLoop(self):
        @LoopingCall
        def loop():
            return self.request(Node(
                "iq", attributes={
                    'id': self._generateMessageId(),
                    'to': "@s.whatsapp.net",
                    'type': "get",
                    'xmlns': "w:p"
                }, content=Node("ping")
            )).addErrback(lambda _: None)

        self._keepAliveLoop = loop
        self._keepAliveLoop.start(20, now=False)

    _messageTagCounter = 0
    _messageTagPrefix = None

    def _generateMessageId(self):
        if self._messageTagPrefix is None:
            _1 = generateRandomNumber(5)
            _2 = generateRandomNumber(5)
            self._messageTagPrefix = "%s.%s" % (_1, _2, )

        suffix = str((self._messageTagCounter + 1))
        self._messageTagCounter += 1

        messageTag = "%s-%s" %(self._messageTagPrefix, suffix)

        if self._messageTagCounter == 99:
            self._messageTagPrefix = None
            self._messageTagCounter = 0

        return messageTag



class MultiDeviceWhatsAppClientFactory(WebSocketClientFactory):

    _authState = None

    def __init__(self,
        url=None,
        origin=None,
        protocols=None,
        useragent=None,
        headers=None,
        proxy=None
    ):
        self.readyDeferred = Deferred()
        self.authDeferred = Deferred()
        WebSocketClientFactory.__init__(
            self, url=url, origin=origin, protocols=protocols,
            useragent=useragent, headers=headers, proxy=proxy)

    def buildProtocol(self, addr):
        protocol = self.protocol()
        if self._authState is not None:
            authState, self._authState = self._authState, None
            protocol.authState = authState
        protocol.factory = self
        protocol.on("open", self._onOpen)
        protocol.on("close", self._onClose)
        return protocol

    def _onOpen(self, connection):
        if self.readyDeferred is not None:
            d, self.readyDeferred = self.readyDeferred, None
            d.callback(connection)

    def _onClose(self, connection, reason=None):
        if self.readyDeferred is not None:
            d, self.readyDeferred = self.readyDeferred, None
            if reason is None:
                reason = ConnectionClosedError(reason="Websocket Handshake Failed")
            d.errback(reason)

    def authFailure(self, failure):
        if self.authDeferred is not None:
            d, self.authDeferred = self.authDeferred, None
            d.errback(failure)

    def authSuccess(self, connection):
        if self.authDeferred is not None:
            d, self.authDeferred = self.authDeferred, None
            d.callback(connection)

    def clientConnectionFailed(self, connector, reason):
        if self.readyDeferred is not None:
            d, self.readyDeferred = self.readyDeferred, None
            d.errback(reason)

    def clientConnectionLost(self, connector, reason):
        if self._authState is not None:
            connector.connect()



def connectToWhatsAppServer(
    protocolFactory=None,
    host=Constants.WHATSAPP_WEBSOCKET_HOST,
    port=Constants.WHATSAPP_WEBSOCKET_PORT,
    url=Constants.WHATSAPP_WEBSOCKET_URL,
    useragent=Constants.DEFAULT_USER_AGENT,
    origin=Constants.DEFAULT_ORIGIN,
    reactor=None
):
    if reactor is None:
        from twisted.internet import reactor

    factory = MultiDeviceWhatsAppClientFactory(
        url=url,
        useragent=useragent,
        origin=origin
    )
    if protocolFactory is None:
        protocolFactory = MultiDeviceWhatsAppClientProtocol
    factory.protocol = protocolFactory

    if host == Constants.WHATSAPP_WEBSOCKET_HOST:
        contextFactory = getTlsConnectionFactory()
        clientFactory = TLSMemoryBIOFactory(contextFactory, True, factory)
    else:
        clientFactory = factory

    reactor.connectTCP(host, port, clientFactory)
    return factory.readyDeferred
