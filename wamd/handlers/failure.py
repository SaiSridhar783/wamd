from twisted.python.failure import Failure

from .base import NodeHandler
from wamd.errors import AuthenticationFailedError

class FailureHandler(NodeHandler):

    def handleNode(self, conn, node):
        if not conn._authDone():
            conn._handleFailure(Failure(
                AuthenticationFailedError(
                    "Authentication Failed: %s" % (attrs['reason']))))
