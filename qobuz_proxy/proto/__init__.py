"""
Compiled Protocol Buffer modules for Qobuz Connect protocol.

Compile protos with:
    protoc --python_out=qobuz_proxy/proto -I protos protos/*.proto
"""

try:
    from . import qconnect_common_pb2 as common
    from . import qconnect_envelope_pb2 as envelope
    from . import qconnect_payload_pb2 as payload
    from . import qconnect_queue_pb2 as queue
except ImportError as e:
    raise ImportError(
        "Protocol buffer modules not compiled. Run: "
        "protoc --python_out=qobuz_proxy/proto -I protos protos/*.proto"
    ) from e

__all__ = ["common", "envelope", "payload", "queue"]
