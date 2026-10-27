"""Small container fixture for boundary tests; not a decodable movie."""
import struct


def box(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


VIDEO = box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", box(b"trak", b"x")) + box(b"mdat", b"x")
